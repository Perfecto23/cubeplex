# CubePlex × AgentCore

这是基于 [cubeplexai/cubeplex](https://github.com/cubeplexai/cubeplex) 的 fork，
上游基线为 `f5272e19`（产品版本 `0.7.2`）。本阶段从此 fork 的 PR #1 合入提交
`8fb3b5d6a171451848d7939be0209709e8ea05b3` 继续开发。本 fork 把 AgentCore 从一次性
执行 PoC 接到 CubePlex 原生的 Web、Slack 和 RunManager 路径上。

当前产品接入以 Runtime v3 的 `READY` readback 为最终部署基线。Web 计算、HITL、
普通续聊、文件回收后读取、长任务、Backend 重启、正常 native Slack，以及
duplicate replay 的验证已经通过，短路径 Web Stop 也有 teardown 证据。本轮是兼容性
PoC；准备阶段停止和停止后续聊现场验收已通过，命令执行中停止仍在验收，不能把已
通过的路径理解成全部停止与恢复边界已经闭环。

## 这个 fork 改了什么

目标运行边界是：

```text
员工使用 Web / Slack
  → Kubernetes 上的 CubePlex 控制面
      账号、workspace、conversation、RunManager、SSE、消息投递
  → AgentCore Runtime（ARM64 Worker）
      读取共享 Postgres / Redis / RustFS / OpenSandbox，运行原生 CubeLoop Agent
  → Redis 事件流、Web SSE 和 Slack durable delivery 回到员工
```

主要改动分为四层：

| 改动 | 责任边界 |
|---|---|
| PR #1 的 AgentCore HTTP entrypoint、真实 CubePlex factory 和模型 provider 适配 | 保留在 fork 中作为历史 PoC 基础；旧的 Slack polling 仅供回溯，不是新产品入口 |
| K8s 产品基线 | 单节点 k3s 控制面、Caddy HTTPS、持久化 PG/Redis/RustFS、OpenSandbox 和 ECR digest 镜像 |
| RunManager ↔ AgentCore | Postgres durable dispatch、一次 claim、followup/HITL、progress、stop unknown fence、Redis 事件和 delivery outcome |
| 原生 Slack | xapp/Socket Mode、Slack user identity → CubePlex user/workspace、限定测试 user/channel 的 durable ingress/outbound |

执行计算的 AgentCore 会话可以被回收；conversation、checkpoint、dispatch、Redis
协调状态和已保存 artifact 不依赖某个 AgentCore VM 会话。Kubernetes 这一阶段仍是
单节点 Testing 拓扑，节点故障时没有 HA 保证。

最终产品镜像来自提交 `11a4a524713fe06d290f5610196099c694f9b132`：Backend
`sha256:fa68ed7d039065064b6a3f24d57ca1312dfce07b4c3127257e4c472b039441bc`、
Worker `sha256:d632fe8f985fb88a2552a25068d090dc1a1747ef6f173355c1eb7d5938b62e40`，
Runtime v3 已用该 Worker 镜像 readback 为 `READY`；Node 24.21.0 Frontend 为
`sha256:5c2816ef1f898fb585246b585cc3d17efabb807a956e2af8233012ef81c7dbc1`。
Backend 和 Worker 各有 1 条供应商尚未提供修复的 High zlib CVE，不能宣称安全扫描清零。
私有 GitHub 授权、200 个仓库检索和替换成 AgentCore Browser 暂不属于这一阶段。

当前 Kubernetes OpenSandbox 仍是兼容性 PoC 的过渡工具环境，不是下一阶段的最终
执行形态。下一阶段先隔离平台凭据，再让 AgentCore MicroVM 同时承担 Agent 和工具执行，
随后再迁移文件与 Browser 能力；本 fork 当前文档不把该迁移写成已实现能力。

## 从哪里开始

| 阅读目的 | 入口 |
|---|---|
| 完整产品的 AWS/k3s/Helm/AgentCore/Slack 操作 | [AgentCore product operator guide](deploy/agentcore-product/README.md) |
| 产品部署架构、边界、状态和验收标准 | [AgentCore product deployment](docs/site/docs/deployment/agentcore-product.md) |
| 标准 Docker Compose / Kubernetes 部署 | [部署入口](deploy/README.md) |
| 旧的窄范围 AgentCore Slack PoC、证据和历史限制 | [PoC 运行指南](deploy/agentcore-poc/README.md) · [PoC 验证记录](deploy/agentcore-poc/VERIFICATION.md) |
| 产品接入设计和实现范围 | [设计](docs/dev/specs/2026-09-12-agentcore-product-design.md) · [计划](docs/dev/plans/2026-09-12-agentcore-product.md) |

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
