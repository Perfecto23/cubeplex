---
sidebar_position: 5
title: Kubernetes control plane with AgentCore
---

# CubePlex control plane with AgentCore

This fork keeps CubePlex Web and native Slack in Kubernetes and runs the
CubeLoop agent loop in one Amazon Bedrock AgentCore Native Runtime:

```text
Web / Slack
    ↓
Kubernetes CubePlex control plane
    accounts · workspaces · conversations · RunManager · delivery
    ↓ dispatch identity + task capability
AgentCore Native ARM64 MicroVM
    CubeLoop · bounded Git/Shell · local files · HITL
    ↓ scoped model / checkpoint / event / file callbacks
CubePlex Backend → Web SSE / Slack durable tailer
```

Kubernetes owns durable product state. AgentCore owns one claimed dispatch and
its execution process. When a Native session is reclaimed, the next followup
loads the CubeLoop checkpoint from shared storage; it does not depend on the old
VM session still existing.

## Current baseline

Testing Native Runtime v2 is `READY`. The current baseline is Runtime
`cubeplex_native_entry_20260913-sWxaCf7pyC`, Worker source
`09f272ec4088f72fe709cc89b01d7abd68fe45d3` / digest
`sha256:b173931fb47dfa4ad1195938b1f4d40ed41d251801fd6467f6d3b2d8624b4edc`,
Backend source `5e616137415f7b93d3e86b9514739ba129d9a7c4` / digest
`sha256:4f53c623cc2daa10fee54b3acfcab2b76e14ff5cb43c44cdccb293feb1c05812`,
and Frontend digest
`sha256:5c2816ef1f898fb585246b585cc3d17efabb807a956e2af8233012ef81c7dbc1`.
The Backend migration image and main image remain a matched baseline.

The Backend uses `execution.backend=agentcore`,
`agentcore.execution_mode=native`, and the same verified Native ARN in both
`runtime_arn` and `native_runtime_arn`. The old compatibility Runtime and the
independent Git slice Runtime, Broker/Lambda, S3 state and dedicated Secrets
are retired.

Browser, Terminal and sandbox file-sidebar behavior remains on OpenSandbox.
The OpenSandbox server, controller and PVCs are still product dependencies;
they are not an AgentCore rollback Runtime.

## Native MicroVM adapter

The Native adapter runs CubeLoop and bounded Git/Shell/file tools together in a
PUBLIC AgentCore MicroVM. The Backend keeps provider credentials, scoped
history, PostgreSQL callback receipts, event delivery and file storage. The VM
does not receive direct database, Redis, object-store or provider credentials.

Each prompt or HITL answer creates one immutable dispatch and one derived
Runtime session. The worker claims once, resolves identity and scope from the
server-created dispatch, and publishes progress and terminal state through the
existing Redis stream. Duplicate invokes return existing dispatch state rather
than calling the model twice.

The Native credential boundary was verified: protected credential fingerprints
had zero matches in env/proc, Secrets Manager and S3 reads were denied, the
fake capability was rejected, and the exact Runtime session stopped with HTTP
200. Native snapshots exclude `.git`, hidden files, credentials, installed
environments and background processes.

## Resource boundary

| Component | Current responsibility | Retention boundary |
|---|---|---|
| CubePlex Backend on k3s | auth, workspace scope, RunManager, dispatch admission, SSE, Slack ingress and delivery | ProductStack Node, Postgres, Redis, RustFS |
| Native AgentCore Runtime v2 | CubeLoop, bounded tools, HITL and scoped callbacks | Native stack and Native Worker image |
| OpenSandbox | Browser, Terminal and sandbox file tools | Server/controller Pods and PVCs |
| Backend/Frontend ECR | current product images | ProductStack repositories with `Retain` |
| Native ECR | Native Worker image | Native stack repository with immutable tags |
| Git slice Worker base | Native Dockerfile `FROM` base only | Keep `sha256:454e290...`; retire Git slice runtime resources |

## ProductStack template

[`deploy/agentcore-product/infra.yaml`](../../../../deploy/agentcore-product/infra.yaml)
is a Native-only retention template. It keeps the existing Node, NodeRole,
NodeProfile, NodeSecurityGroup, EIP, Backend/Frontend ECR repositories and
KubeconfigSecret. It no longer creates an AgentCore Runtime, WorkerRole,
WorkerConfigSecret, Worker ECR repository, private Runtime subnet, route table,
Runtime security group or old invoke policy.

Use the template only to review a change set against the existing Testing
stack. Do not use an old deploy recipe to create a new experiment stack or pass
`WorkerImageUri` to ProductStack. Native Runtime creation and its invoke policy
belong to [`deploy/agentcore-native-entry/infra.yaml`](../../../../deploy/agentcore-native-entry/infra.yaml),
which reuses the Product `NodeRole` and uses PUBLIC networking.

The Native Dockerfile uses the Git slice Worker digest
`sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d` as
its base. Keep that one ECR base image and repository for Native rebuilds; the
retired Git slice Runtime, Broker/Lambda, S3 state and dedicated Secrets do not
need to be recreated.

## Kubernetes and access

The single-node Testing control plane uses local-path PVCs for Postgres, Redis
and RustFS, one Backend and Frontend replica, host-network Caddy, and the
retained OpenSandbox server/controller. Use SSM for k3s access and keep the
kubeconfig in the retained KubeconfigSecret. Keep operator values and all
runtime credentials outside Git.

Verify Deployments, StatefulSets, PVCs, OpenSandbox CRDs and the RustFS bucket
before user acceptance. A healthy Helm release proves control-plane readiness;
Native acceptance also requires a real Web or Slack dispatch and readback.

## Acceptance boundary

| Path | Current Native state |
|---|---|
| Web/Slack product path | Prior real Web/Slack task and followup acceptance passed; current cleanup Web readback returned `CLEANUP-NATIVE-OK` with one model callback |
| HITL after Backend restart | Same-run question/answer passed after Backend restart; a fresh Worker restored the Native checkpoint and workspace |
| File/workspace continuity | Previous `Alpha` history remained readable after the cleanup session was absent |
| Stop B | Prepared running-command stop completed with no late file write or marker |
| Duplicate admission and callback replay | Passed without duplicate execution or final reply |
| Cleanup readback | 31 dispatches were `finished`; Native session was absent; 8 Pods were Ready and 6 PVCs were unchanged |

These Native records are separate from the retired compatibility Runtime and
Git slice evidence. OpenSandbox sandbox reservations still follow their
configured cleanup window.

## Cost and deferred scope

The fixed single-node figure of **$3.89/day** is a historical baseline estimate,
not a current repricing. Current continuing cost items include the Node, EBS,
public IPv4, ECR/Native base image, AgentCore and model usage. A cost pause
needs an explicit outage window, active-run readback, data backup and recovery
checks; do not delete the retained Node or disk.

Private employee GitHub authorization, indexing across 200 private repositories
and replacing the OpenSandbox Browser with an AgentCore Browser remain deferred.
The [Native operator guide](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-native-entry/README.md)
contains the current tool and recovery boundary. The
[retired Git slice guide](https://github.com/Perfecto23/cubeplex/blob/main/deploy/agentcore-git-slice/README.md) is
historical evidence only.
