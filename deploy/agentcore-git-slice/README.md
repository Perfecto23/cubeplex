# AgentCore MicroVM Git execution slice

这是一个独立的执行层 PoC，用来验证“Agent 在新的 AgentCore MicroVM 中完成一次受控 Git 工作，再在另一台新 VM 中继续”的边界。它没有把 CubePlex Web、Slack 或现有产品 RunManager 迁移到这里；旧产品和现有 Runtime 仍是回滚基线。

## 它解决什么问题

模型和 GitHub 写权限放在同一台长生命周期机器上，会让凭据、数据库和工作区互相暴露。本 slice 把边界拆成两部分：

- **MicroVM** 保留 CubeLoop Agent、Git 工作区和 Shell。代码检出、测试、修改和本地提交都在 VM 内完成。
- **Lambda broker** 是可信写入口。它读取 operator 管理的 manifest 和短期 repo-scoped GitHub writer，验证仓库、base、branch、commit、改动路径、大小、预算和 capability 后，才转发模型请求、push 或 PR 操作。

VM 可以看到自己的临时 task IAM/capability，不能因此推断这些凭据不可读取；合同只保证平台数据库、Vault、Supabase、GitHub 主 token 和模型主凭据不会下发到 VM。GitHub token 不写进 VM，也不要求用户把个人 `main` token 交给 Worker。

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

这一步生成私有 archive、冻结依赖、source manifest，并构建 ARM64 Worker 与 broker 镜像。需要发布到 Testing ECR 时，再显式使用：

```bash
deploy/agentcore-git-slice/build.sh <committed-sha> --push
```

`--push` 固定到 operator 指定的 Testing account/region。构建前应读回 source SHA、镜像 digest 和 ECR scan；当前 IaC/Runtime 尚在首轮修正和验收，不能把镜像构建写成 Runtime 已部署或业务已验收。

## 如何运行和验收

Worker entrypoint 是：

```bash
python -m cubeplex_git_slice.runtime
```

Runtime 输入只有版本、task、stage、capability 和 `probe|run` 模式；输出不回显 capability、token 或请求 header。`probe` 先确认 canary Secret 必须返回 IAM denied，再报告环境和凭据面；任何平台主凭据可达、canary 可读或错误角色都会 fail closed。

`run/work` 只在 Agent 完成测试、commit、broker push、PR、README handoff 和 continuation snapshot 后成功。operator 随后确认旧 Runtime session 已停止、旋转 stage capability，再以 `run/resume` 启动新 VM。resume 用 snapshot 恢复相同 base/head/branch 和 CubeLoop native history，重复完成的 stage 只返回已有结果，不再次调用模型、push 或创建 PR。

本轮尚未有新的 Runtime、模型或 GitHub live acceptance 结果；完成标准是同一 commit SHA、同一个 PR、无第二次修改或发布，且能读回两次 VM 的 boot/session、tool 状态、clone cold/reuse/restore metrics 和 snapshot。旧 EC2、旧 K8s 产品、持久盘、EIP、Secrets、ECR 和现有 VPC Runtime 仍被产品/回滚依赖，不能因为这个独立 PoC 而停止或删除。

## 凭据和配置边界

broker 侧的 model/GitHub Secret 只由 Lambda role 读取；manifest 是 operator-only，worker role 无 S3 读写权限。Secret JSON、capability、真实 GitHub token 和 AWS 资源值不写入 README、镜像参数、日志或 snapshot。部署时使用 `infra.yaml` 的 immutable image URI 参数和私有 operator 流程填充配置；不要把任何真实 Secret 值复制到命令行或仓库。
