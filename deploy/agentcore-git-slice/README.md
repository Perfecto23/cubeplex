# AgentCore MicroVM Git execution slice

这是一个独立的执行层 PoC，用来验证“Agent 在新的 AgentCore MicroVM 中完成一次受控 Git 工作，再在另一台新 VM 中继续”的边界。它没有把 CubePlex Web、Slack 或现有产品 RunManager 迁移到这里；旧产品和现有 Runtime 仍是回滚基线。

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
5. 确认实际 Worker role、canary IAM 拒绝、凭据面检查，以及错误 repo/ref/op/capability 和零预算拒绝均符合预期。随后才把 manifest 的调用上限提升至最多 20，并以 `mode=run` 执行。

恢复时先确认原 session 停止，再把 operator manifest 切到 `active_stage=resume` 并旋转 capability；保留预算计数和所有已完成状态。新 session 使用 `stage=resume, mode=run`。AWS CLI 可通过 `--payload fileb://<private-input.json>` 传入内容，响应写入私有结果文件；不要在终端打印 capability。运行结束不自动合并测试 PR。

## 如何运行和验收

Worker entrypoint 是：

```bash
python -m cubeplex_git_slice.runtime
```

Runtime 输入只有版本、task、stage、capability 和 `probe|run` 模式；输出不回显 capability、token 或请求 header。`probe` 先确认 canary Secret 必须返回 IAM denied，再报告环境和凭据面；任何平台主凭据可达、canary 可读或错误角色都会 fail closed。

`run/work` 只在 Agent 完成测试、commit、broker push、PR、README handoff 和 continuation snapshot 后成功。operator 随后确认旧 Runtime session 已停止、旋转 stage capability，再以 `run/resume` 启动新 VM。resume 用 snapshot 恢复相同 base/head/branch 和 CubeLoop native history，重复完成的 stage 只返回已有结果，不再次调用模型、push 或创建 PR。

新 Runtime 和真实凭据隔离 probe 已通过；模型与 GitHub 业务验收尚未开始。后续完成标准是同一 commit SHA、同一个 PR、无第二次修改或发布，且能读回 session、tool 状态、clone cold/reuse/restore metrics 和 snapshot。旧 EC2、旧 K8s 产品、持久盘、EIP、Secrets、ECR 和现有 VPC Runtime 仍被产品/回滚依赖，不能因为这个独立 PoC 而停止或删除。

## 凭据和配置边界

broker 侧的 model/GitHub Secret 只由 Lambda role 读取；manifest 是 operator-only，worker role 无 S3 读写权限。Secret JSON、capability、真实 GitHub token 和 AWS 资源值不写入 README、镜像参数、日志或 snapshot。部署时使用 `infra.yaml` 的 immutable image URI 参数和私有 operator 流程填充配置；不要把任何真实 Secret 值复制到命令行或仓库。

## 当前验收结果（2026-09-13）

| 层次 | 已核对的结果 |
| --- | --- |
| 应用代码 | CubeLoop 原生 provider/tool loop、Git helper、快照与权限拒绝等 74 项测试通过；ruff/mypy 通过 |
| Worker 镜像 | Source `9cc4ba7573511b311656f577558f0ab4c4b55dac`；digest `sha256:59e36cff66364050412fc78a2e86c68d8b6d440f62b3a77c3674b53dc520d906` |
| Broker 镜像 | Source `44764e11cd21883da6170e272d1dc9ce7964f1cf`；digest `sha256:f4a6756020ceb7f96663524e3a1de2730303cfe449accc2e06379805a3afde1c` |
| 部署 | Runtime `cubeplex_git_slice_20260913-7TBZpqBGCs`，v1 READY、PUBLIC、idle 60 秒、最长 900 秒；Lambda Active、非 VPC、ARM64、1024 MiB、并发上限 1 |
| 真实身份和权限 | Worker STS 身份匹配指定 task role；canary GetSecretValue 返回 IAM 拒绝 |
| 凭据面 | 2 个可读进程的环境、已检查凭据路径/helper 和 11 项保护值指纹未发现平台主凭据；临时 task IAM 凭据仍可由 VM 代码读取 |
| broker 拒绝 | 错误 repo/ref/op/capability 和零模型预算分别被拒绝 |
| 日志和任务状态 | 检查 34 条当前任务日志，保护值/capability 明文匹配均为 0；model_calls=0，无发布 commit/PR，无完成 stage |
| 计算回收 | 成功 probe 的 session 已由 StopRuntimeSession 返回 200 结束 |
| 实际 Git 业务 | **待验收**：尚未创建单仓库短期 GitHub writer；云端 Agent 未修复、提交、push 或创建 PR，尚无云端 clone/reuse/restore 数据 |

首次云端 probe 曾因 broker 源文件仅允许 root 读取而失败；已保留该失败记录，并通过非 root 容器验证和后续真实 probe 证明修复。没有把失败尝试写成成功，也没有让本机代替云端 Agent 修复 fixture。公开仓库 main 仍为 `d3a437256ede620f70b31e9eb99621ae420dee42`。

两个不同 session 观察到了相同 hostname/kernel boot ID，因此 `boot_id` 只作为诊断字段，不能充当唯一 VM 标识。恢复验收应结合 AWS 返回的 session ID、旧 session 停止结果、新 workspace 状态和已保存成果。AWS 的[会话隔离合同](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)规定每个 session 使用独立 MicroVM；本轮还未完成 Git 工作后的跨 session 恢复。

## 镜像安全与费用

Worker 已采用官方 Debian backports 的 curl `8.21.0-2~bpo13+1`，修复 [8927](https://curl.se/docs/CVE-2026-8927.html)、[8924](https://curl.se/docs/CVE-2026-8924.html)、[8286](https://curl.se/docs/CVE-2026-8286.html) 三项 CVE。Broker 的 OpenSSL 按 [ALAS2023-2026-2088](https://alas.aws.amazon.com/AL2023/ALAS2023-2026-2088.html) 升级至发行版修复包。最新 ECR scan：Worker 0 Critical、1 High、1 Medium；Broker 无 findings。

保留项为 zlib `CVE-2026-85091`（[Debian 当前未修复](https://security-tracker.debian.org/tracker/CVE-2026-85091)）及 nghttp2 `CVE-2026-58055`（Medium）。它们没有被伪装为已修复。本镜像只用于这次受控 Testing 实验。

新增三个 Secrets 的存储基线约 $1.20/月，另有 ECR/S3 存储及 Lambda/AgentCore 按量费用；[Secrets Manager 价格](https://aws.amazon.com/secrets-manager/pricing/)按存储时间和调用量计费。未新增 EC2、EKS、NAT Gateway 或数据库。当前预算保持 0，等待 GitHub 凭据就绪再开启最多 20 次模型调用。

旧产品 EC2 当前仍须运行，固定基线约 $3.89/天。后续停机候选窗口可设为 2026-09-14 20:00–20:30（Asia/Singapore），但这只是建议，尚未安排或执行；前提是独立 Git/出网验收通过、无活跃工作、已有数据备份，并明确接受网页、Slack、存储及旧 VPC Runtime 出网中断。恢复时启动同一 EC2，经 SSM 核对 k3s、PVC、NAT 路由和 ECR 拉取。保留的 60 GiB gp3、EIP 和两个旧产品 Secrets 仍约 $9.25/月，另加镜像及本 slice 的保留资源费用。
