# CubePlex × AgentCore

这是基于 [cubeplexai/cubeplex](https://github.com/cubeplexai/cubeplex) 的实验性 fork，起点为上游 `f5272e1901d0c0c785bdc2381da94951726da601`（版本标识 `0.7.2`）。上游的产品与版权归属保持不变；本 fork 增加了一个 **Amazon Bedrock AgentCore 执行 PoC**，验证能否保留 CubePlex 的 Agent 构造逻辑，把计算放到按需分配的云端运行环境。

## 这个 fork 改了什么

已经跑通的路径是：

```text
Slack 测试频道中的请求
  → 本地 Python 控制器读取消息并检查权限
  → AgentCore 启动自制 ARM64 Runtime 镜像
  → 真实 CubePlex factory / LLM builder / CubeLoop 调用模型
  → 只读工具从 GitHub 获取固定 commit 的代码片段
  → 本地控制器以 Bot 身份回复原 thread
```

本轮没有部署 Kubernetes Pod。镜像在本地 Docker 构建，推送到 ECR，由 AgentCore Runtime 承载。Slack 控制器是有运行时限的本地进程；聊天历史去重记录保存在本地 SQLite，Provider key 保存在 AWS Secrets Manager。

| 本 fork 增加的能力 | 当前边界 |
|---|---|
| AgentCore HTTP entrypoint 与真实 CubePlex Agent 构造 | 独立 PoC 模块；完整 Web / Backend RunManager 仍沿用上游实现 |
| GitHub 只读工具、commit/blob 校验与行号证据 | 本轮固定 `Perfecto23/corplink-rs`；该仓库公开，不证明私库授权 |
| Slack polling、Bot 回帖与持久去重 | 限定测试用户、频道、前缀和时间窗；只发现新 root 消息 |
| 本地构建、ECR digest、最小执行角色和部署 readback | 单个 Testing Runtime；不创建 EC2、Kubernetes、Browser 或数据库 |
| 严格请求合同、工具预算、模型完成状态检查 | 拒绝其他仓库、任意 URL / shell、错误身份和未完成的模型结果 |

58 项 focused tests 和真实云端、Slack 业务链路已验证。实际部署镜像仍有未解决的系统包扫描发现；这是可复现的 PoC，不是生产迁移完成的声明。源码、镜像及验收边界见[验证记录](deploy/agentcore-poc/VERIFICATION.md)。

## 如何运行

在包含本 PR 的 checkout / worktree 根目录执行：

```bash
uv sync --project deploy/agentcore-poc --frozen
PYTHONPATH=backend uv run --project deploy/agentcore-poc \
  python -m pytest -q deploy/agentcore-poc/tests
```

随后按照[AgentCore PoC 运行指南](deploy/agentcore-poc/README.md)配置 Provider、核对已绑定的测试环境、部署镜像并启动有限时长的 Slack 控制器。指南分别说明“使用已有 Runtime”和“首次创建环境”，后者会创建收费资源，不应在已有环境上重复执行。

| 阅读目的 | 入口 |
|---|---|
| 配置、构建、部署、启动和停止 | [运行指南](deploy/agentcore-poc/README.md) |
| 真实验证结果与仍未覆盖的能力 | [验证记录](deploy/agentcore-poc/VERIFICATION.md) |
| 本次设计与实现范围 | [设计](docs/dev/specs/2026-09-12-agentcore-poc-design.md) · [实现计划](docs/dev/plans/2026-09-12-agentcore-poc.md) |
| 完整 CubePlex 产品的 Docker / Kubernetes 部署 | [上游部署入口](deploy/README.md) |

## 上游 CubePlex

以下保留上游的产品介绍、演示和文档入口。这些描述属于完整 CubePlex 产品；本 fork 的 AgentCore 改造范围以上文和 PoC 运行指南为准。

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
