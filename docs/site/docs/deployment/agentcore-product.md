---
sidebar_position: 5
title: Kubernetes control plane with AgentCore
---

# CubePlex control plane with AgentCore

This fork keeps CubePlex's Web and native Slack experience in Kubernetes and
moves the native CubeLoop agent loop to an Amazon Bedrock AgentCore Runtime:

```text
Web / Slack
    ↓
Kubernetes CubePlex control plane
    accounts · workspaces · conversations · RunManager · delivery
    ↓ durable Postgres dispatch + Redis events
AgentCore ARM64 worker
    native CubePlex factory · tools · checkpoints · progress
    ↓
Web SSE / Slack durable tailer
```

Kubernetes owns the durable product state. AgentCore owns one claimed dispatch
and its execution process. When the AgentCore session is reclaimed, the next
followup loads the native CubeLoop checkpoint from shared storage; it does not
depend on the old VM session still existing.

## Current status

The Testing control plane and Runtime v3 are deployed on a small single-node
k3s cluster, and the Runtime readback is `READY`. Web compute, HITL, ordinary
followup, file readback after AgentCore reclaim, long tasks, Backend restart,
normal native Slack, duplicate replay, prepared stop and stop-then-followup have
passed real checks. Stop during command execution remains under acceptance;
this is a compatibility PoC rather than a completed production migration.

The final image source is commit
`11a4a524713fe06d290f5610196099c694f9b132`. Runtime v3 uses Backend
`sha256:fa68ed7d039065064b6a3f24d57ca1312dfce07b4c3127257e4c472b039441bc` and
the ARM64 Worker
`sha256:d632fe8f985fb88a2552a25068d090dc1a1747ef6f173355c1eb7d5938b62e40`.

The fork starts from merged commit `8fb3b5d6` after upstream PR #1. The
historical bounded Slack/AgentCore PoC is documented separately in
[AgentCore PoC](./agentcore-poc.md). The full operator runbook is in the
[product deployment guide](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-product/README.md).

## Deployment boundary

| Component | Responsibility | Durable authority |
|---|---|---|
| CubePlex Backend on k3s | auth, workspace scope, RunManager, dispatch admission, SSE, native Slack ingress and delivery | Postgres + Redis |
| AgentCore Runtime | Runtime v3 claims one dispatch, runs native CubeLoop, invokes tools, persists checkpoints and events, and tracks async work as `HealthyBusy` | shared Postgres/Redis/RustFS/OpenSandbox |
| Postgres | conversations, memberships, dispatches, checkpoints and product records | source of truth for product history |
| Redis | run coordination, event streams, delivery cursors and locks | live coordination and replay window |
| RustFS/S3 | attachments and artifacts | durable object bytes |
| OpenSandbox | existing CubePlex shell/file/browser tool environment | sandbox workspace/PVC state |

The AgentCore invocation payload is only a protocol version and a dispatch ID.
The worker loads identity and scope from the server-created dispatch and
rejects caller-supplied scope. Prompt and HITL answers use separate durable
dispatches. Duplicate invokes return the existing dispatch state rather than
calling the model twice.

The node's invoke policy is deliberately narrow: `InvokeAgentRuntime` and
`StopRuntimeSession` are granted for the exact Runtime ARN and the exact
AgentCore default endpoint ARN. The Runtime itself uses the immutable ARM64
Worker image inside the private VPC subnet.

Stop and delivery have separate outcomes. A confirmed stop tears down the
native run; an unconfirmed stop remains fenced as `stop_unknown`. A Slack
transport retry resumes its delivery checkpoint and does not create a new
AgentCore execution.

## Testing topology

The economical target uses one amd64 `t3a.xlarge` k3s node (4 vCPU, 16 GiB),
60 GiB encrypted gp3 storage, local-path PVCs, host-network Caddy and SSM
access. The private AgentCore subnet reaches only the required node ports for
Postgres, Redis, RustFS and OpenSandbox plus outbound HTTPS. This is a
single-node non-HA test topology.

The fixed baseline is about **$118.24/month** at 730 hours, or **$3.89/day**,
before the two new Secrets Manager secrets (about **$0.80/month**), ECR,
AgentCore, model and traffic usage. Stop the node when the test is paused;
retain the disk and credential stores if recovery is still required.

## Acceptance boundary

| Path | Current state |
|---|---|
| Web prompt, progress and result | Compute/progress passed on v1; final-version ordinary followup passed on v3 |
| Web HITL and ordinary followup | HITL respond passed on v1 using the same run; ordinary followup passed on v3 using a new run and the existing conversation checkpoint |
| File readback after AgentCore reclaim | Passed |
| Web Stop during the short tool path | Passed with teardown verification; command-stop remains pending |
| Long-running async task | Passed; `HealthyBusy` observed through completion |
| Backend restart during remote work | Passed; existing task monitor reattached without replay |
| Normal native Slack task/reply | Passed after identity linking |
| Duplicate Slack/dispatch replay | Passed without duplicate execution or final reply |
| Stop before execution starts | Passed; native cancel and dispatch terminal state completed in about 0.22s with no `stop_unknown` |
| Stop during command execution | Pending final field acceptance; the live Backend Sandbox policy can retain the reservation for about 10.5 minutes (`create_timeout=300s`, `ready_timeout=300s`, `cleanup_interval=30s`, `pause_enabled=false`) |
| Stop followed by a new followup | Passed; a new AgentCore run returned the expected Web result without a tool call |

The operator guide records the source-freeze, CFN, SSM tunnel, private values,
Helm, ECR-refresh, Caddy, Runtime and Slack steps needed to reproduce these
checks. The current evidence does not justify claiming the full matrix is
complete.

## Security and deferred scope

Public registration is closed at the Caddy layer for this Testing deployment.
The operator bootstraps the test account through the private tunnel and grants
organization admin with `python -m cubeplex.cli admin grant-admin`; the existing
`default-org` is not treated as a first-registration owner flow.

Native Slack is Socket Mode with a test xapp, a linked CubePlex identity, and
an allowlist limited to the test user and channel. Credentials stay in Secrets
Manager or Kubernetes Secrets and never enter Git, image build arguments or
logs.

This compatibility PoC keeps OpenSandbox as a transitional Kubernetes tool
environment. The next phase first isolates platform credentials, then moves
Agent and tool execution together into an AgentCore MicroVM; file and Browser
capabilities can migrate after that boundary is secure. This fork does not
implement that migration yet.

The current phase defers per-employee private GitHub authorization, retrieval
across 200 private repositories, and replacing OpenSandbox Browser with
AgentCore Browser. The current Backend and Worker images still have one
unresolved vendor zlib High finding; the current Node 24.21.0 Frontend image
scan is clean.
