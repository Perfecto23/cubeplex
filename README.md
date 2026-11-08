# CubePlex × AgentCore

这是基于 [cubeplexai/cubeplex](https://github.com/cubeplexai/cubeplex) 的 fork，当前源码已同步上游 main `7abf66d1472565bdcb5f13223bf2e358bfa79180`（2026-11-08，版本线 `0.9.4`）。本 fork 将 CubePlex 的产品控制面与 Agent 执行环境拆开：员工继续使用原来的网页或 Slack，CubeLoop Agent 和 Git/Shell/文件工具在 AgentCore MicroVM 中按需运行。

```text
员工使用 Web / Slack
  → Kubernetes 上的 CubePlex
      账号、工作区、会话、任务状态、消息收发与持久化数据
  → AgentCore Native Runtime
      CubeLoop Agent + Git/Shell/文件工具
  → 任务回调返回 Backend
      保存检查点、进度、人工确认和文件，再展示给员工
```

本 fork 的部署入口仍为 Native AgentCore。11 月升级对齐 CubeLoop 0.16.1、上游 execution admission/attempt 生命周期，以及新版网页和 OpenSandbox 1.1.1。可部署版本使用独立的 `agentcore-YYYY.MM.DD.N` 发布标签；镜像、源码和 Runtime 版本从 [fork Releases](https://github.com/Perfecto23/cubeplex/releases) 的 `release-manifest.json` 核对。

## 实现与部署

- **Kubernetes 控制面**：复用现有单节点 k3s、Backend、Frontend、Postgres、Redis、RustFS 和原生 Slack Socket Mode，入口不依赖本机轮询程序。
- **Native 执行层**：自制 ARM64 镜像运行 CubeLoop 与本地工具。模型、数据库和存储主凭据留在 Backend；MicroVM 只有对应任务的短期回调权限和最小 AWS 角色。
- **状态与恢复**：检查点与请求回执在同一 PostgreSQL 事务提交；事件、终态和停止有去重与隔离。等待人工回答时可以回收 VM，回答后用新会话继续同一个 run。
- **运行边界**：Native Runtime 是当前唯一产品执行路径。OpenSandbox 继续承载 Browser、Terminal 和文件侧栏；旧 compatibility Runtime、Git slice Runtime、Lambda、S3 状态和专属 Secrets 已退休。

[Native 运行指南](deploy/agentcore-native-entry/README.md) 说明同一提交的三镜像构建、数据库维护迁移和升级验证。Backend、migration 初始化容器和 Frontend 使用同一发布；Native Worker 的 CubeLoop 版本与 Backend 对齐。升级保留账号、会话和成果，迁移前须备份并暂停旧 Backend 写入。安全扫描结果随发布资产记录。

## 当前能力边界

原生路径支持公开仓库的 clone、读取和有界前台命令执行，以及普通文件的保存、展示和恢复。上游新增的持久后台命令使用 OpenSandbox 任务体系；Native 本地工具仍随所属 AgentCore 会话结束。

普通文件快照不保存 `.git`、隐藏文件、凭据、安装环境或后台进程。Browser、Terminal 和文件侧栏仍使用 OpenSandbox 环境；Native 的实际用户入口是对话和下载卡片。独立 Git slice 只保留历史证据，未接入产品入口；其 Worker ECR 基础镜像仍因 Native Dockerfile 的 `FROM` 依赖保留。员工私库授权、200 仓库检索、AgentCore Browser 和其余工具迁移继续分批实施。

这是单节点 Testing PoC，节点故障没有 HA 保证。约 $3.89/天是历史基线估计，不是本次重新计价；当前持续计费项包括节点、EBS、EIP、ECR/Native 基础镜像、模型和 AgentCore 按量费用。本阶段没有新增 EC2、EKS、NAT Gateway 或数据库，当前只保留一个 Product KubeconfigSecret。

## 文档入口

| 阅读目的 | 入口 |
|---|---|
| 当前 Native 路径、镜像、配置、验收和边界 | [Native 运行指南](deploy/agentcore-native-entry/README.md) |
| 现有 AWS/k3s/Helm/Slack 基础设施与 Native 运维 | [产品部署指南](deploy/agentcore-product/README.md) |
| 当前 Native 产品部署架构与边界 | [产品部署说明](docs/site/docs/deployment/agentcore-product.md) |
| 已退休的独立 Git/Shell 实验与历史证据 | [Git execution slice](deploy/agentcore-git-slice/README.md) |
| 标准上游 Docker Compose / Kubernetes 部署 | [部署入口](deploy/README.md) |
| 历史窄范围 Slack PoC | [PoC 指南](deploy/agentcore-poc/README.md) · [验证记录](deploy/agentcore-poc/VERIFICATION.md) |

独立 Git slice 的真实修复、测试、commit、受限 push、[测试 PR](https://github.com/Perfecto23/cubeplex-microvm-git-poc-20260913/pull/1) 和新 VM 续聊仅作为历史证据保留。它的 Runtime、Lambda、S3 状态和专属 Secrets 已退休；Worker ECR 中的 `sha256:454e290...` 仍是 Native Dockerfile 的构建基础，不应随实验资源一并删除。该实验不能替代 Native 产品验收，也不能被当作 Native 已接入 authenticated push/PR 的证据。

## 上游 CubePlex

以下保留上游的产品介绍、演示和文档入口。这些描述属于完整 CubePlex 产品；本
fork 的 AgentCore 产品接入边界、Testing 操作和当前状态以上方产品部署指南为准，
旧 PoC 仅用于历史证据。

<p align="center">
  <picture>
    <source
      media="(prefers-color-scheme: dark)"
      srcset="frontend/packages/web/public/brand/cubeplex-lockup-on-dark.svg"
    />
    <img
      src="frontend/packages/web/public/brand/cubeplex-lockup-on-light.svg"
      alt="CubePlex"
      width="320"
    />
  </picture>
</p>

<p align="center">
  <strong>Cloud-native platform for managed agents in team workspaces</strong>
</p>

<p align="center">
  <a href="https://github.com/cubeplexai/cubeplex/actions/workflows/ci.yml">
    <img src="https://github.com/cubeplexai/cubeplex/actions/workflows/ci.yml/badge.svg" alt="CI" />
  </a>
  <a href="https://docs.cubeplex.ai">
    <img src="https://img.shields.io/badge/docs-docs.cubeplex.ai-1268E8" alt="Docs" />
  </a>
  <a href="https://cubeplex.ai">
    <img src="https://img.shields.io/badge/website-cubeplex.ai-14213D" alt="Website" />
  </a>
  <img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/node-20%2B-339933?logo=node.js&logoColor=white" alt="Node 20+" />
  <a href="https://cubeplex.ai/docs/deployment/overview">
    <img src="https://img.shields.io/badge/deploy-Docker%20%7C%20Kubernetes-2496ED?logo=docker&logoColor=white" alt="Docker | Kubernetes" />
  </a>
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a>
</p>

CubePlex is a cloud-native platform for **long-lived, team-owned Agents**. A Workspace gives a team one Agent with a stable role, shared knowledge, approved tools, and a persistent work environment—available from the web and the channels where the team already works.

<p align="center">
  <img src="docs/site/static/img/architecture/cubeplex-overview.svg" alt="CubePlex architecture: clients, the application and agent runtime, workspace sandboxes, external services, and persistent infrastructure" width="100%" />
</p>

## Why CubePlex

- **One long-lived Workspace Agent** — The team returns to the same Agent across tasks, with its role, Skills, memory, MCP connections, and working state intact.
- **Team-owned state, not personal state** — Agent configuration, shared knowledge, approved tools, deliverables, and working state belong to the Workspace and remain with the team.
- **One Agent across every entry point** — Use the same Workspace Agent from the web, Slack, Discord, Teams, Feishu, DingTalk, WeCom, and more, while each conversation keeps its own participants and execution context.
- **Durable, governed execution** — Isolated persistent sandboxes and organization controls make ongoing Agent work inspectable, repeatable, and manageable.

CubePlex's Agent runtime is built on [CubeLoop](https://github.com/cubeplexai/cubeloop), an async-native agent framework for multi-provider model access, tool execution, streaming, middleware, and durable checkpoints. Workspace sandboxes are isolated execution environments with persistent working state; external model providers, MCP servers, and IM platforms remain outside CubePlex's trust boundary.

## Demos

<div align="center">
  <video src="https://github.com/user-attachments/assets/716b9d39-e74a-4ae6-a053-d0c8d7a0af47" width="100%" controls></video>
</div>

> **Build Interactive Website** — a full product website generated from a single prompt.

<details>
<summary>More demos</summary>

<div align="center">
  <video src="https://github.com/user-attachments/assets/d93360b7-8141-42c9-bc4f-3d9488a309b1" width="100%" controls></video>
</div>

> **Skills Workflow** — find a skill, install it, and use it to build an agentic frontend end to end.

<div align="center">
  <video src="https://github.com/user-attachments/assets/85975ccb-b512-45ff-96d2-0b7df7c8de57" width="100%" controls></video>
</div>

> **Data Analysis** — transform raw tabular data into a formatted spreadsheet.

<div align="center">
  <video src="https://github.com/user-attachments/assets/c8ad3c71-4102-4bcb-931a-5fc9378be140" width="100%" controls></video>
</div>

> **One-Page PDF** — turn a one-page PDF into a polished, navigable page.

<div align="center">
  <video src="https://github.com/user-attachments/assets/1d979ec6-7ddc-489b-bb43-9f4c78c89b38" width="100%" controls></video>
</div>

> **Browser Control** — an Agent drives the browser to complete a task autonomously.

</details>

## Platform capabilities

| Area | What you get |
|---|---|
| **Long-lived Workspace Agents** | Give each team a stable Agent role, shared configuration, approved capabilities, and a work environment that continues across tasks. |
| **Team conversations** | Use private chats, group chats, and Topics to keep participants, conversation context, and execution separate while the Workspace Agent keeps its shared role and knowledge. |
| **Layered memory** | Recall personal preferences, shared workspace facts and procedures, and organization policies at the appropriate scope. |
| **Skills** | Package reusable Agent workflows from built-in capabilities, organization-provided skills, or remote registries such as skills.sh, then make them available in the right workspace. |
| **Governed MCP tools** | Org admins curate a connector catalog; workspaces enable the tools they need. Credentials can be organization-, workspace-, or user-scoped, using OAuth or static credentials. |
| **Workspace sandboxes** | Per-workspace isolated runtimes with **persistent storage** — files, packages, and the working tree survive restarts so Agents resume the same work site. |
| **Secrets and credential controls** | Credentials are encrypted at rest. Kubernetes deployments can resolve approved sandbox secrets at request time rather than exposing them as plain environment variables. |
| **Multi-model chat** | Use hosted and custom providers including Anthropic, OpenAI, and more. Attach files, stream replies, and switch models mid-conversation. |
| **Automation** | Run scheduled tasks (cron, interval, or one-shot) and webhook event triggers. |
| **Artifacts** | Deliver versioned files, previews, code, images, and other outputs directly in the conversation. |
| **IM bridges** | Bring the same Workspace Agent to Slack, Discord, Teams, Feishu, DingTalk, WeCom, and more; configure shared or per-member conversations in group chats. |
| **Organization and workspace governance** | Manage organizations, workspaces, roles, model access policies, connector catalogs, credentials, and cost tracking. |
| **Deploy anywhere** | Use Docker Compose for a single host or Helm for Kubernetes. |

## How CubePlex compares

- [**CubePlex vs DeerFlow**](https://cubeplex.ai/blog/cubeplex-vs-deerflow-workspace-and-harness) — team-owned Workspace Agents versus personal Agent environments.
- [**CubePlex vs Dify**](https://cubeplex.ai/blog/cubeplex-vs-dify-team-agent-workspace) — long-lived Agents versus scenario-specific Apps.
- [**CubePlex vs QM**](https://cubeplex.ai/blog/qm-cubeplex-team-agent-collaboration) — a long-lived team Agent with entry-point-specific execution contexts versus Agent-computer scopes.


## Get started

- **Docker Compose** (single host): [installation guide](https://cubeplex.ai/docs/deployment/docker-compose)
- **Kubernetes with Helm**: [installation guide](https://cubeplex.ai/docs/deployment/kubernetes)

Both modes use the same backend and frontend images. Guides cover image builds,
configuration, secrets, and verification.

## Develop locally

Prerequisites: Python 3.12+, Node.js 20+, pnpm 10+, and Docker (recommended for
local services).

```bash
git clone https://github.com/cubeplexai/cubeplex.git
cd cubeplex
make install

# Terminal 1 — API
cd backend && python main.py

# Terminal 2 — web UI
cd frontend && pnpm dev
```

Backend: `http://localhost:8000` · Frontend: `http://localhost:3000`.

Local setup also needs backend env/config files described in the
[contribution guide](CONTRIBUTING.md).

## Repository layout

```text
backend/    FastAPI API and Cubeloop-based agent runtime
frontend/   Next.js web app and shared TypeScript packages
deploy/     Docker Compose and Kubernetes/Helm assets
docs/       Product docs site and engineering reference
scripts/    Worktree provisioning and dev helpers
```

## Documentation and contributing

- [Documentation site](https://docs.cubeplex.ai)
- [Core concepts](docs/site/docs/getting-started/core-concepts.md)
- [Deployment overview](deploy/README.md)
- [Contributing](CONTRIBUTING.md)
- [Agent guidance (AGENTS.md)](AGENTS.md)
