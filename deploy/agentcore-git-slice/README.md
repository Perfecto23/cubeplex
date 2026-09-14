# AgentCore MicroVM Git execution slice（已退休）

> 状态：历史实验，仅用于阅读证据。Git slice Runtime、Broker/Lambda、S3 状态和专属 Secrets 已退休；不要按本页命令重新创建收费资源。

这个独立 slice 曾用于验证“Agent 在新的 AgentCore MicroVM 中完成受控 Git 工作，再在另一台新 VM 中继续”的边界。它从未接入 CubePlex Web、Slack 或产品 RunManager，也不是 Native 产品入口。

## 与当前 Native 产品的关系

Native Worker 的 Dockerfile 仍以 Git slice Worker 镜像作为 `FROM` 基础：

```text
986420599013.dkr.ecr.us-west-2.amazonaws.com/cubeplex-git-slice-20260913/worker@sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d
```

因此只保留这个 ECR repository 中的上述基础镜像，供 Native 重建使用。Broker 镜像、Git slice Runtime、Lambda、S3 manifest/snapshot 状态和 GitHub/model/canary Secrets 不属于当前保留面。

## 历史设计

- MicroVM 曾运行 CubeLoop、Git 工作区和 Shell；仓库检出、测试、修改和本地提交都在 VM 内完成。
- Broker 曾作为受限 GitHub 写入口，校验仓库、base、branch、commit、改动路径、预算和 capability，再转发 push/PR。
- Worker 不应获得平台数据库、Vault、GitHub 主 token 或模型主凭据；这些约束只描述历史实验边界。
- Snapshot 曾保存 bundle、HEAD、README patch、continuation note 和 native history，用于新 VM 的有限恢复。

完整的历史合同仍在 [CONTRACT.md](./CONTRACT.md)，设计背景在 [设计说明](../../docs/dev/specs/2026-09-13-agentcore-git-slice-design.md)。这些文件是参考资料，不是当前运维步骤。

## 历史验收记录

| 项目 | 历史结果 |
| --- | --- |
| Worker source | `482fe5df40e694c66ae52fbf2540593ba4c9803c`；digest `sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d` |
| Broker source | `125a202efb571d6bc24e1dc2d40ae94e6f766bad`；历史 digest `sha256:e28d843a062c6b36e8dece6d764502c345a1388b75aa9aa52870e565b791d735` |
| Historical Runtime | `cubeplex_git_slice_20260913-7TBZpqBGCs` v2，曾 read back 为 `READY` |
| Historical work | fixture commit、测试 PR、published continuation 和新 VM restore 曾完成；不能替代 Native 产品验收 |
| Historical limits | 结果来自公开 fixture，不能外推到私有仓库规模或通用 exactly-once 恢复 |

## 当前边界

Native Runtime 只提供受限的公开仓库读取、Git/Shell/文件工具和产品回调。它不连接本 slice 的 push/PR broker，也不提供 Native authenticated push/PR。Browser、Terminal 和文件侧栏继续由 OpenSandbox 提供。

如需新的私有仓库授权、索引检索或 authenticated push/PR，应另行设计并取得独立授权；不要复用本页已退休的 Runtime、Lambda、S3 或 Secrets 操作流程。
