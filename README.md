# CubePlex × AgentCore

这是基于 [cubeplexai/cubeplex](https://github.com/cubeplexai/cubeplex) 的 fork，上游基线为 `f5272e19`（产品版本 `0.7.2`）。本 fork 将 CubePlex 的产品控制面与 Agent 执行环境拆开：员工继续使用原来的网页或 Slack，CubeLoop Agent 和 Git/Shell/文件工具在 AgentCore MicroVM 中按需运行。

```text
员工使用 Web / Slack
  → Kubernetes 上的 CubePlex
      账号、工作区、会话、任务状态、消息收发与持久化数据
  → AgentCore Native Runtime
      CubeLoop Agent + Git/Shell/文件工具
  → 任务回调返回 Backend
      保存检查点、进度、人工确认和文件，再展示给员工
```

当前 Testing 使用 `cubeplex_native_entry_20260913-sWxaCf7pyC` Runtime v2，状态 `READY`。Native 真实网页与 Slack 任务、跨会话追问、下载文件、人工确认、Backend 重启、停止与回收、重复消息和晚到回调均有对应验收记录；本次 cleanup Web readback 为 `CLEANUP-NATIVE-OK`。关闭本机运维连接后，网页与 Slack 的新任务仍由云端处理；没有进行物理电脑关机实验。

## 实现与部署

- **Kubernetes 控制面**：复用现有单节点 k3s、Backend、Frontend、Postgres、Redis、RustFS 和原生 Slack Socket Mode，入口不依赖本机轮询程序。
- **Native 执行层**：自制 ARM64 镜像运行 CubeLoop 与本地工具。模型、数据库和存储主凭据留在 Backend；MicroVM 只有对应任务的短期回调权限和最小 AWS 角色。
- **状态与恢复**：检查点与请求回执在同一 PostgreSQL 事务提交；事件、终态和停止有去重与隔离。等待人工回答时可以回收 VM，回答后用新会话继续同一个 run。
- **运行边界**：Native Runtime v2 是当前唯一产品执行路径。OpenSandbox 继续承载 Browser、Terminal 和文件侧栏；旧 compatibility Runtime、Git slice Runtime、Lambda、S3 状态和专属 Secrets 已退休。

Native Worker 来自 `09f272ec4088f72fe709cc89b01d7abd68fe45d3`，Backend 来自 `5e616137415f7b93d3e86b9514739ba129d9a7c4`，镜像 digest 与部署方法见 [Native 运行指南](deploy/agentcore-native-entry/README.md)。Backend 主容器和 migration 初始化容器须使用同一新版镜像。Frontend 沿用已部署版本。已修复有供应商补丁的镜像问题；仍保留已知未修复的 zlib High，Native 镜像另有 nghttp2 Medium，不能宣称扫描清零。

## 当前能力边界

原生路径支持公开仓库的 clone、读取和命令执行，以及普通文件的保存、展示和恢复。真实 Git 测试首轮因 Agent 未切换目录而发现 0 个测试，未计为通过；同会话纠正后实际运行了 4 个测试（1 通过、3 失败，测试仓库 main 故意保留缺陷），结果已如实保存。

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

CubePlex is a cloud-native platform for managed agents in team workspaces —
skills, shared memory, MCP tools, persistent sandboxes, governed access, and
self-hosted deploy on Docker Compose or Kubernetes.

<p align="center">
  <img src="docs/site/static/img/architecture/cubeplex-overview.svg" alt="CubePlex architecture: clients, the application and agent runtime, workspace sandboxes, external services, and persistent infrastructure" width="100%" />
</p>

The diagram reflects the current application architecture. CubePlex's agent
runtime is built on [CubeLoop](https://github.com/cubeplexai/cubeloop), an
async-native agent framework for multi-provider model access, tool execution,
streaming, middleware, and durable checkpoints. Workspace sandboxes
are isolated execution environments with persistent working state; external
model providers, MCP servers, and IM platforms remain outside CubePlex's trust
boundary.

## Demos

<div align="center">
  <video src="https://github.com/user-attachments/assets/716b9d39-e74a-4ae6-a053-d0c8d7a0af47" width="100%" controls></video>
</div>

> **Build Interactive Website** — a full product website generated from a single prompt.

<div align="center">
  <video src="https://github.com/user-attachments/assets/d93360b7-8141-42c9-bc4f-3d9488a309b1" width="100%" controls></video>
</div>

> **Skills Workflow** — find a skill, install it and use it to build agentic frontend, end to end.

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

> **Browser Control** — an agent drives the browser to complete a task autonomously.



## Features

| Area | What you get |
|---|---|
| **Multi-model chat** | Hosted and custom providers (Anthropic, OpenAI, and more). Attach files, stream replies, switch models mid-conversation. |
| **Skills** | Packaged agent capabilities — built-in, org-uploaded, or from remote registries (e.g. skills.sh). |
| **Memory** | Personal, workspace, and org-scoped memory the agent recalls across conversations. |
| **MCP tools** | Catalog of connectors with static credentials or OAuth; grant tools per workspace. |
| **Workspace sandboxes** | Per-workspace isolated runtimes with **persistent storage** — files, packages, and the working tree survive restarts so agents resume the same work site. |
| **Artifacts** | Versioned deliverables — files, previews, code, images — rendered in the thread. |
| **Automation** | Scheduled tasks (cron / interval / one-shot) and webhook event triggers. |
| **IM bridges** | Talk to agents from Slack, Discord, Teams, Feishu, DingTalk, and more. |
| **Team governance** | Organizations, workspaces, roles, model access policies, and cost tracking. |
| **Deploy anywhere** | Docker Compose for a single host; Helm for Kubernetes. |

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
