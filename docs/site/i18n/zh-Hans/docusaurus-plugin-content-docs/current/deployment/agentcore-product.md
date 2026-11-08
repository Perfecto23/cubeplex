---
sidebar_position: 5
title: Kubernetes 控制面与 AgentCore
---

# CubePlex 控制面与 AgentCore

本 fork 在 Kubernetes 中运行 CubePlex Web 和原生 Slack 入口，在一个 Amazon
Bedrock AgentCore Native Runtime 中运行 CubeLoop Agent 循环：

```text
Web / Slack
    ↓
Kubernetes CubePlex 控制面
    账号 · 工作区 · 会话 · RunManager · 消息投递
    ↓ dispatch 身份 + 任务 capability
AgentCore Native ARM64 MicroVM
    CubeLoop · 受限 Git/Shell · 本地文件 · 人工确认（HITL）
    ↓ 按范围校验的模型 / checkpoint / 事件 / 文件回调
CubePlex Backend → Web SSE / Slack 持久投递
```

Kubernetes 保存持久产品状态。AgentCore 负责一个已领取 dispatch 的执行进程。
Native 会话回收后，下次追问从共享存储加载 CubeLoop checkpoint，恢复过程无需旧 VM
会话继续存在。

## 当前基线

11 月 fork 更新整合了上游 main 和 CubeLoop 0.16.1。源码、Backend、Frontend、Native
镜像 digest 及已部署 Runtime 版本以 [fork 发布清单](https://github.com/Perfecto23/cubeplex/releases)
为准。Backend 的数据库迁移镜像与主容器使用同一版本。9 月 Native v2 的验收记录是历史基线；
新镜像需要对应部署验收。

9 月已有数据库须执行维护升级：备份数据、停止旧 Backend 写入进程，使用新镜像运行
`python -m cubeplex.scripts.lifecycle_upgrade --maintenance`，然后部署匹配的应用镜像。
保留现有 PVC 和鉴权 Secret。精确顺序及回滚边界见
[Native 运维指南](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-native-entry/README.md)。

Backend 使用 `execution.backend=agentcore`、`agentcore.execution_mode=native`，
`runtime_arn` 与 `native_runtime_arn` 均填写同一个已核验的 Native ARN。
旧兼容 Runtime、独立 Git slice Runtime、Broker/Lambda、S3 状态和专用 Secret 已退役。

Browser、Terminal 和沙箱文件侧栏继续使用 OpenSandbox。OpenSandbox server、controller
和 PVC 是产品依赖；AgentCore 回滚使用其自身 Runtime 和数据恢复流程。

## Native MicroVM 适配

Native 适配在 PUBLIC AgentCore MicroVM 中一起运行 CubeLoop 和受限 Git/Shell/文件工具。
Backend 保存模型提供商凭据、按范围隔离的历史、PostgreSQL 回调记录、事件投递和文件存储。
VM 只接收任务所需的 capability，数据库、Redis、对象存储和模型提供商凭据由 Backend 保管。

每次提示或 HITL 答复创建一个不可变 dispatch 和一个派生 Runtime 会话。Worker 领取一次，
从服务端生成的 dispatch 解析身份和范围，通过现有 Redis stream 发布进度及终态。
重复 invoke 返回已有 dispatch 状态，保持一次模型执行。

9 月验收验证了 Native 凭据边界：env/proc 中受保护凭据指纹匹配数为零；Secrets Manager
和 S3 读取被拒绝；伪造 capability 被拒绝；精确 Runtime 会话停止返回 HTTP 200。
Native snapshot 排除 `.git`、隐藏文件、凭据、已安装环境和后台进程。

## 资源边界

| 组件 | 当前职责 | 保留边界 |
|---|---|---|
| k3s 中的 CubePlex Backend | 鉴权、工作区范围、RunManager、dispatch 接纳、SSE、Slack 接收与投递 | ProductStack Node、Postgres、Redis、RustFS |
| Native AgentCore Runtime | CubeLoop、受限工具、HITL、按范围校验的回调 | Native stack 和 Native Worker 镜像 |
| OpenSandbox | Browser、Terminal 和沙箱文件工具 | Server/controller Pod 和 PVC |
| Backend/Frontend ECR | 当前产品镜像 | 设置 `Retain` 的 ProductStack 仓库 |
| Native ECR | Native Worker 镜像 | 使用不可变 tag 的 Native stack 仓库 |
| Git slice Worker 基础镜像 | 只作为 Native Dockerfile 的 `FROM` | 保留 `sha256:454e290...`；Git slice Runtime 资源已退役 |

## ProductStack 模板

[`deploy/agentcore-product/infra.yaml`](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-product/infra.yaml)
是 Native 产品保留模板。模板保留已有 Node、NodeRole、NodeProfile、NodeSecurityGroup、EIP、
Backend/Frontend ECR 仓库和 KubeconfigSecret。模板已移除旧 AgentCore Runtime、WorkerRole、
WorkerConfigSecret、Worker ECR 仓库、Runtime 私网子网、路由表、安全组和调用策略的创建定义。

该模板只用于审阅现有 Testing stack 的 change set。创建新实验环境须单独设计，ProductStack
的参数中已移除 `WorkerImageUri`。Native Runtime 和调用策略由
[`deploy/agentcore-native-entry/infra.yaml`](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-native-entry/infra.yaml)
管理；该模板复用 Product `NodeRole` 并使用 PUBLIC 网络。

Native Dockerfile 使用 Git slice Worker digest
`sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d`
作为基础镜像。Native 重建需要保留该 ECR 镜像及仓库。已退役的 Git slice Runtime、
Broker/Lambda、S3 状态和专用 Secret 继续保持退役。

## Kubernetes 与访问

单节点 Testing 控制面使用 Postgres、Redis 和 RustFS 的 local-path PVC，Backend 和 Frontend
各一个副本、host-network Caddy，以及保留的 OpenSandbox server/controller。
通过 SSM 访问 k3s；kubeconfig 保存在保留的 KubeconfigSecret 中。
运维 values 和所有运行凭据保存在 Git 之外。

升级曾用 `kubectl` 修改过的集群时，先比较线上 Backend 配置、镜像字段和 Helm 渲染结果。
出现 server-side apply 字段冲突后，只对齐已审阅字段再重试。使用 `Recreate` 的 Deployment
须清空旧 `rollingUpdate` 字段。维护迁移之后，数据库恢复和应用回滚分别处理。

用户验收前核对 Deployment、StatefulSet、PVC、OpenSandbox CRD 和 RustFS bucket。
Helm 健康说明控制面已就绪；Native 验收还须完成真实 Web 或 Slack dispatch 及结果读回。

## 9 月验收基线

| 路径 | Native 历史证据 |
|---|---|
| Web/Slack 产品路径 | 真实 Web/Slack 任务和追问通过；9 月清理后的 Web 返回 `CLEANUP-NATIVE-OK`，有一次模型回调 |
| Backend 重启后的 HITL | 重启后同一 run 的问题和答复通过；新 Worker 恢复 Native checkpoint 和工作区 |
| 文件/工作区连续性 | 清理会话消失后，旧 `Alpha` 历史仍可读取 |
| Stop B | 已开始执行的命令停止成功，未出现晚到文件或 marker |
| 重复接纳与回调重放 | 通过，未出现重复执行或重复最终回复 |
| 清理读回 | 31 个 dispatch 为 `finished`；Native 会话已消失；8 个 Pod Ready，6 个 PVC 保持原状 |

这些 Native 记录与已退役兼容 Runtime、Git slice 的证据分别保存。
OpenSandbox 的沙箱预留继续遵循其配置的清理窗口。

## 成本与后续范围

单节点 **$3.89/天** 是历史固定成本估算。当前持续费用包含 Node、EBS、公网 IPv4、
ECR/Native 基础镜像、AgentCore 和模型调用。暂停成本须先确定停机窗口，读回活动 run，
备份数据并核对恢复步骤；保留的 Node 和磁盘按恢复计划处理。

员工私有 GitHub 授权、多仓索引，以及用 AgentCore Browser 替换 OpenSandbox Browser 仍待推进。
当前工具和恢复边界见
[Native 运维指南](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-native-entry/README.md)。
[已退役 Git slice 指南](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-git-slice/README.md)
只用于历史回溯。
