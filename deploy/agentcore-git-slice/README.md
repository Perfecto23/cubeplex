# AgentCore MicroVM Git execution slice

这是一个独立的执行层 PoC，用来验证“Agent 在新的 AgentCore MicroVM 中完成一次受控 Git 工作，再在另一台新 VM 中继续”的边界。Worker 运行 source `482fe5df40e694c66ae52fbf2540593ba4c9803c`；Broker 当前预算扩展来自提交 `125a202efb571d6bc24e1dc2d40ae94e6f766bad`。两者都已构建并推送到 Testing ECR。它没有把 CubePlex Web、Slack 或现有产品 RunManager 迁移到这里；旧产品和现有 Runtime 仍是回滚基线。

## 它解决什么问题

模型和 GitHub 写权限放在同一台长生命周期机器上，会让凭据、数据库和工作区互相暴露。本 slice 把边界拆成两部分：

- **MicroVM** 保留 CubeLoop Agent、Git 工作区和 Shell。代码检出、测试、修改和本地提交都在 VM 内完成。
- **Lambda broker** 是可信写入口。它读取 operator 管理的 manifest 和短期 repo-scoped GitHub writer，验证仓库、base、branch、commit、改动路径、大小、预算和 capability 后，才转发模型请求、push 或 PR 操作。

VM 可以看到自己的临时 task IAM/capability。业务调用权限限于当前任务 broker，另有自身镜像读取和日志写权限；平台数据库、Vault、Supabase、GitHub 主 token 和模型主凭据不下发到 VM，实际隔离还须由镜像检查和真实 IAM probe 验证。GitHub writer 只放在 broker，限定这个公开测试仓库并设置短期有效期。

本次 fixture 是公开的 `Perfecto23/cubeplex-microvm-git-poc-20260913`。固定 branch 是 `agentcore/fix-inclusive-total`，允许发布的代码路径只有 `intervals.py`；base SHA 和其他运行约束以 operator manifest/broker 返回为准。完整字段和错误合同见 [CONTRACT.md](./CONTRACT.md) 与 [设计说明](../../docs/dev/specs/2026-09-13-agentcore-git-slice-design.md)。

## 一次运行怎么走

```text
Runtime input
  → MicroVM probe：环境、/proc、凭据文件、Git helper、canary Secret
  → Agent 调用 clone_repository
  → Agent 调用 shell：读代码、改 intervals.py、运行测试
  → Agent 调用 git commit / git push
      → git-remote-broker
          → Lambda broker：验证 bundle、base、路径和 commit
          → GitHub writer 在 broker 侧 push 同一个 commit SHA
  → Agent 调用 create_pull_request
  → broker 状态 + snapshot：bundle、HEAD、README patch、continuation.md、native history
  → 新 MicroVM 恢复 snapshot，保留原 commit，验证并幂等确认同一个 push/PR
```

`git-remote-broker` 只实现 Git remote-helper 协议。它把 VM 创建的 bundle 交给 broker；broker 不 checkout、不运行仓库代码、不执行测试，只做验证和受控 GitHub 转发。push 和 PR 都按 repo/branch/commit 幂等，未知结果先 readback，不盲目重试。

当 `stage=work` 尚未完成、但 broker `status` 已经确认存在一个 published commit 时，Worker 使用
`published_commit_only` 继续路径。Agent 仍必须调用 `clone_repository`，但 checkout 从 broker 确认的
已发布 SHA 开始；它只能核对 HEAD、运行测试、确认或完成同一 PR，并生成未提交的 README handoff
patch 和 `continuation.md`。该路径禁止再次修改 `intervals.py`、创建新 commit 或 push；结果和 snapshot
metrics 会记录恢复模式。它不伪造丢失的首次 native history，新 snapshot 只保存本次继续会话。

## 如何构建

必须从已提交 SHA 构建，不能把当前 dirty worktree 当 Docker context：

```bash
deploy/agentcore-git-slice/build.sh <committed-sha>
```

这一步生成私有 archive、冻结依赖、source manifest，并构建 ARM64 Worker 与 broker 镜像。如果还要发布到 Testing ECR，直接选下面的构建加推送命令，两个命令不必连续执行：

```bash
deploy/agentcore-git-slice/build.sh <committed-sha> --push
```

`--push` 固定到 Testing account/region。构建前确认 source SHA，推送后读回镜像 digest 和 ECR scan；镜像构建、Runtime 部署和业务验收分别记录。

## 部署准备

本实验固定使用 `moego-testing / us-west-2`。先通过 STS 确认账号为 `986420599013`，用 `infra.yaml` 创建 CloudFormation change set 并检查改动，再执行。镜像参数留空时只创建存储、ECR、Secrets 和两个受限角色；传入 `WorkerImageUri`、`BrokerImageUri` 的 ECR digest 后才创建 Lambda 和 PUBLIC Runtime。不会创建 K8s、数据库或 VPC NAT。

从 stack Outputs 获取 Bucket、Role、Secret 和 Runtime ARN。配置文件放在仓库外的私有目录（目录 `0700`、文件 `0600`），使用标准 API 按这个顺序初始化：

1. 向 model Secret 写入 `{api_key, base_url, model}`；向 GitHub Secret 写入 `{token}`。GitHub 凭据须只允许这个测试仓库的 Contents/PR 写权限，并设置短期过期时间。canary Secret 只存随机的无害测试值。Secret 使用 `put-secret-value --secret-string file://<private-file>`，不把值放在命令参数中。
2. 按 [CONTRACT.md](./CONTRACT.md) 创建完整 manifest，填入固定 base SHA、Worker Role ARN、canary 哈希和凭据指纹。生成新的随机 capability，只把 SHA-256 写入 manifest。首轮 `max_model_calls=0`，设置近期 UTC deadline。
3. 用带 `If-None-Match: *` 的 S3 PutObject 首次写入 `tasks/git-slice-20260913/manifest.json` 和 `tasks/git-slice-20260913/state/state.json`；后者初始内容为 `{"model_calls":0,"completed_stages":{}}`。已有状态不能重置。
4. 创建 Runtime 输入私有 JSON，字段为 `version=1`、`task_id`、`stage=work`、`capability`、`mode=probe`。使用新的、至少 33 字符的 session ID 调用 `InvokeAgentRuntime`，完整保存结果。
5. 确认实际 Worker role、canary IAM 拒绝、凭据面检查，以及错误 repo/ref/op/capability 和零预算拒绝均符合预期。随后才把 manifest 的调用上限提升至已授权的上限，并以 `mode=run` 执行。本次用户明确授权后把上限从 `20` 提高到 `100`，保留已用的 `20` 次，不重置任务状态。

恢复时先确认原 session 停止，再把 operator manifest 切到 `active_stage=resume` 并旋转 capability；保留预算计数和所有已完成状态。新 session 使用 `stage=resume, mode=run`。AWS CLI 可通过 `--payload fileb://<private-input.json>` 传入内容，响应写入私有结果文件；不要在终端打印 capability。运行结束不自动合并测试 PR。首次失败 session 丢失的 native history 不由 resume 伪造；resume 只恢复已经成功保存的 snapshot。

## 如何运行和验收

Worker entrypoint 是：

```bash
python -m cubeplex_git_slice.runtime
```

Runtime 输入只有版本、task、stage、capability 和 `probe|run` 模式；输出不回显 capability、token 或请求 header。`probe` 先确认 canary Secret 必须返回 IAM denied，再报告环境和凭据面；任何平台主凭据可达、canary 可读或错误角色都会 fail closed。

`run/work` 的 fresh 路径只在 Agent 完成测试、commit、broker push、PR、README handoff 和 continuation
snapshot 后成功；已发布但没有成功 checkpoint 的 work 则走上面的 `published_commit_only` 路径。operator
随后确认旧 Runtime session 已停止、旋转 stage capability，再以 `run/resume` 启动新 VM。resume 用
snapshot 恢复相同 base/head/branch 和 CubeLoop native history；本次新 VM 从空 workspace 恢复了 27 条
消息的完整前缀，新增 10 条消息、4 次模型调用，累计 37 条消息和 24 次调用，并保持同一 HEAD、PR
和已保存文件。重复完成的 stage 返回 `already_completed`，不再次修改、push 或创建 PR。原始失败
session 的 history 不在恢复范围内；成功 continuation 保存的 snapshot 才是 resume 的输入。

## 当前验收结果（2026-09-13）

| 层次 | 已核对的结果 |
| --- | --- |
| 应用代码 | 上一应用基线 86 项 slice 测试通过；本次预算调整 36 项相关测试通过，ruff/mypy 通过 |
| Worker 镜像 | Source `482fe5df40e694c66ae52fbf2540593ba4c9803c`；digest `sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d`；ECR scan 完成，保留 1 High/1 Medium |
| Broker 镜像 | Source `125a202efb571d6bc24e1dc2d40ae94e6f766bad`；digest `sha256:e28d843a062c6b36e8dece6d764502c345a1388b75aa9aa52870e565b791d735`；ECR scan 完成且无 findings |
| Runtime | `cubeplex_git_slice_20260913-7TBZpqBGCs` v2，真实 readback 为 `READY`；使用上述 Worker digest |
| 首次云端 work | `fc1637492a841c84dba9206595bf2289d6706b3e` 已 push；首次 PR 结果 unknown，native history/checkpoint 未成功保存，旧 session 已回收；人工 readback 已保存异常收敛记录 |
| published continuation | 同一 SHA、PR #1 OPEN 未合入；初始预算阶段新增 9 次模型调用，累计 `20` 次；snapshot 含 27 条新 native history、README 487B patch 和 continuation note |
| 新 VM restore | 真实 S3 snapshot 经过官方 Shell stdin 恢复；27 条消息、HEAD、README patch、continuation note 逐字段一致，model calls=0，shell exit=0，StopRuntimeSession=200 |
| 预算扩展 | 用户明确授权将 `max_model_calls` 从 `20` 提高到 `100`；已用计数保留为 `20`，任务状态未重置 |
| 实际 resume | 新 VM 恢复 27 条消息的 exact prefix；新增 10 条消息、4 次模型调用，累计 37 条消息、24 次调用；same HEAD/PR/saved files，最新 snapshot `5d7f0ab86ac34ff19be80cc5bc60c6904dd83e0a450699d5ef43796d513bc7d3` |
| 重复完成调用 | 新 session 返回 `already_completed`，保持同一 SHA、PR、snapshot 和 `24` 次计数，StopRuntimeSession=200 |
| session 回收 | resume session 的终止读回为 `ResourceNotFoundException`，表示 session 不存在或已回收 |
| 后续边界 | Native Web/Slack bridge 不属于本 slice；Native entry PR4 为 Draft，正在实现，尚未部署或验收 |

继续会话的冷 clone 为约 511ms/1423 HTTP 接收字节，同环境 reuse 为约 329ms/951 HTTP 接收字节（含响应头），本地 Git objects 为 3303 字节。新 VM 中纯 restore 函数约 31ms；通过官方 Shell stdin 传入的序列化快照为 30236 字节，后者不是总网络流量。这个小 fixture 的结果不能外推到 200 个仓库。旧 EC2、旧 K8s 产品、持久盘、EIP、Secrets、ECR 和现有 VPC Runtime 仍被产品/回滚依赖，
不能因为这个独立 PoC 而停止或删除。

## 凭据和配置边界

broker 侧的 model/GitHub Secret 只由 Lambda role 读取；manifest 是 operator-only，worker role 无 S3 读写权限。Secret 值、capability 和真实 GitHub token 不写入 README、镜像参数、日志或 snapshot。部署时使用 `infra.yaml` 的 immutable image URI 参数和私有 operator 流程填充配置；不要把任何真实 Secret 值复制到命令行或仓库。

## 镜像安全与费用

Worker 已采用官方 Debian backports 的 curl `8.21.0-2~bpo13+1`，修复 [8927](https://curl.se/docs/CVE-2026-8927.html)、[8924](https://curl.se/docs/CVE-2026-8924.html)、[8286](https://curl.se/docs/CVE-2026-8286.html) 三项 CVE。Broker 的 OpenSSL 按 [ALAS2023-2026-2088](https://alas.aws.amazon.com/AL2023/ALAS2023-2026-2088.html) 升级至发行版修复包。最新 ECR scan：Worker 0 Critical、1 High、1 Medium；Broker 无 findings。

保留项为 zlib `CVE-2026-85091`（[Debian 当前未修复](https://security-tracker.debian.org/tracker/CVE-2026-85091)）及 nghttp2 `CVE-2026-58055`（Medium）。它们没有被伪装为已修复。本镜像只用于这次受控 Testing 实验。

新增三个 Secrets 的存储基线约 $1.20/月，另有 ECR/S3 存储及 Lambda/AgentCore 按量费用；[Secrets Manager 价格](https://aws.amazon.com/secrets-manager/pricing/)按存储时间和调用量计费。未新增 EC2、EKS、NAT Gateway 或数据库。本次预算扩展只来自用户明确授权，当前任务累计调用 `24/100`；不能把预算扩展解释为自动重置或通用上限。

旧产品 EC2 当前仍须运行，固定基线约 $3.89/天。后续停机候选窗口可设为 2026-09-14 20:00–20:30（Asia/Singapore），但这只是建议，尚未安排或执行；前提是独立 Git/出网验收通过、无活跃工作、已有数据备份，并明确接受网页、Slack、存储及旧 VPC Runtime 出网中断。恢复时启动同一 EC2，经 SSM 核对 k3s、PVC、NAT 路由和 ECR 拉取。保留的 60 GiB gp3、EIP 和两个旧产品 Secrets 仍约 $9.25/月，另加镜像及本 slice 的保留资源费用。
