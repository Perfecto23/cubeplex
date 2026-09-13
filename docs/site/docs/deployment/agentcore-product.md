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
    ↓ dispatch identity + task capability
AgentCore Native ARM64 MicroVM
    CubeLoop · Git/Shell · local files
    ↓ scoped model / checkpoint / event / file callbacks
CubePlex Backend → Web SSE / Slack durable tailer
```

Kubernetes owns the durable product state. AgentCore owns one claimed dispatch
and its execution process. When the AgentCore session is reclaimed, the next
followup loads the native CubeLoop checkpoint from shared storage; it does not
depend on the old VM session still existing.

## Native MicroVM adapter

The native adapter runs CubeLoop and the Git/Shell/file tools together in a PUBLIC AgentCore MicroVM. The Backend keeps provider credentials, scoped history, PostgreSQL callback receipts, event delivery and file storage. It does not give the tool VM direct database, Redis, object-store or provider credentials.

Testing Runtime v2 is `READY` and real Web/Slack task and follow-up checks have passed, including file cards, HITL pause/answer in a new Worker, Backend restart, stop/reclaim and duplicate callback fencing. The current baseline is Runtime `cubeplex_native_entry_20260913-sWxaCf7pyC`, Worker source `09f272ec4088f72fe709cc89b01d7abd68fe45d3` / digest `sha256:b173931fb47dfa4ad1195938b1f4d40ed41d251801fd6467f6d3b2d8624b4edc`, and Backend source `5e616137415f7b93d3e86b9514739ba129d9a7c4` / digest `sha256:4f53c623cc2daa10fee54b3acfcab2b76e14ff5cb43c44cdccb293feb1c05812`; the migration and image remain on the same source baseline.

The Native credential boundary is verified: the Worker role matched, the platform package was unavailable,
protected credential fingerprints had zero matches in env/proc, Secrets Manager and S3 reads were denied, the fake capability
was rejected, the CP login returned 200, and the exact Runtime session stopped with HTTP 200. Earlier failed probe attempts are retained as history only. The native
snapshot currently preserves ordinary workspace files, excluding `.git` and hidden or credential files.
The one-repository push/PR broker from the independent Git slice is not connected to native product
entrypoints. Browser and terminal file-sidebar behavior remains on OpenSandbox. See the [native operator
guide](https://github.com/Perfecto23/cubeplex/blob/feat/2026-09-13-agentcore-native-entry/deploy/agentcore-native-entry/README.md)
for actual tool and recovery limits.

## Retained compatibility baseline

The retained compatibility Runtime v3 shares the Testing control plane on a single-node
k3s cluster, and the Runtime readback is `READY`. Web compute, HITL, ordinary
followup, file readback after AgentCore reclaim, long tasks, Backend restart,
normal native Slack, duplicate replay, prepared stop and stop-then-followup have
passed real checks. The final running-command stop also passed: execution was
confirmed before Stop, and no late marker appeared after its original deadline.
This is a single-node compatibility PoC with the limits below.

The Native Runtime v2 is a separate execution baseline for the same control plane. Its Web/Slack task,
follow-up, HITL, stop/reclaim and duplicate callback checks are complete; the compatibility Runtime v3,
Frontend and OpenSandbox remain retained for rollback and legacy capabilities. A previous compatibility
rollback was also verified with the new Backend migration container; it did not downgrade the database.
The final Web and Slack acceptance completed after local forwarding and test Docker services were stopped,
using the deployed service path.

The compatibility baseline image source is commit
`11a4a524713fe06d290f5610196099c694f9b132`. Its previous Backend image was
`sha256:fa68ed7d039065064b6a3f24d57ca1312dfce07b4c3127257e4c472b039441bc`; Runtime v3 uses
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
| Compatibility AgentCore Runtime | Runtime v3 claims one dispatch, runs native CubeLoop, invokes tools, persists checkpoints and events, and tracks async work as `HealthyBusy` | shared Postgres/Redis/RustFS/OpenSandbox |
| Native AgentCore Runtime v2 | CubeLoop, bounded Git/Shell/file tools, HITL and control-plane callbacks inside one task MicroVM | PostgreSQL callbacks/checkpoints + Redis events + RustFS workspace files |
| Postgres | conversations, memberships, dispatches, checkpoints and product records | source of truth for product history |
| Redis | run coordination, event streams, delivery cursors and locks | live coordination and replay window |
| RustFS/S3 | attachments and artifacts | durable object bytes |
| OpenSandbox | existing CubePlex shell/file/browser tool environment | sandbox workspace/PVC state |

The compatibility invocation payload contains a version and dispatch ID. Native invocations additionally carry a short-lived task capability; privileged state is accessed through scoped Backend callbacks.
Both paths resolve identity and scope from the server-created dispatch and
rejects caller-supplied scope. Prompt and HITL answers use separate durable
dispatches. Duplicate invokes return the existing dispatch state rather than
calling the model twice.

The node's invoke policy is deliberately narrow: `InvokeAgentRuntime` and
`StopRuntimeSession` are granted for the exact Runtime ARN and the exact
AgentCore default endpoint ARN. The compatibility Runtime uses its immutable ARM64 image in the private VPC subnet. The Native Runtime uses its separate immutable ARM64 image with PUBLIC networking.

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
AgentCore, model and traffic usage. Keep the node running during the independent
Git slice: it hosts the product and storage and provides NAT for the existing
VPC Runtime. A later cost pause needs a confirmed outage window and recovery
checks; retained disk, public IPv4 and two product Secrets still cost about
$9.25/month before ECR and other usage.

## Acceptance boundary

| Path | Current state |
|---|---|
| Web prompt, progress and result | Compute/progress passed on v1; final-version ordinary followup passed on v3 |
| Web HITL and ordinary followup | HITL respond passed on v1 using the same run; ordinary followup passed on v3 using a new run and the existing conversation checkpoint |
| File readback after AgentCore reclaim | Passed |
| Web Stop during the short tool path | Passed with teardown verification and no late marker |
| Long-running async task | Passed on v2: tracked async execution and fresh heartbeats continued beyond the 60-second idle timeout |
| Backend restart during remote work | Passed on v2: Backend restarted at 85 seconds during a 150-second task, without replay or a second reply |
| Normal native Slack task/reply | Passed after identity linking |
| Duplicate Slack/dispatch replay | Passed without duplicate execution or final reply |
| Stop before execution starts | Passed; native cancel and dispatch terminal state completed in about 0.22s with no `stop_unknown` |
| Stop during command execution | Passed on v3: confirmed command start, stop at about 50 seconds, cancelled/finished in about 0.30 seconds, no late marker after the original deadline |
| Stop followed by a new followup | Passed; a new AgentCore run returned the expected Web result without a tool call |

The operator guide records the source-freeze, CFN, SSM tunnel, private values,
Helm, ECR-refresh, Caddy, Runtime and Slack steps needed to reproduce these
checks. The agreed compatibility PoC acceptance is complete. Preparation
cancellation releases the run immediately, but the existing Sandbox reservation
can take about 10.5 minutes to clear (`create_timeout=300s`, `ready_timeout=300s`,
`cleanup_interval=30s`, `pause_enabled=false`). Ordinary followup works during
that window; another tool request using the same Sandbox scope may wait.

## Security and deferred scope

Public registration is closed at the Caddy layer for this Testing deployment.
The operator bootstraps the test account through the private tunnel and grants
organization admin with `python -m cubeplex.cli admin grant-admin`; the existing
`default-org` is not treated as a first-registration owner flow.

Native Slack is Socket Mode with a test xapp, a linked CubePlex identity, and
an allowlist limited to the test user and channel. Credentials stay in Secrets
Manager or Kubernetes Secrets and never enter Git, image build arguments or
logs.

This compatibility route keeps OpenSandbox as a Kubernetes tool environment for
rollback and legacy Browser/terminal file-sidebar behavior. Native Runtime v2
now runs the Agent and bounded Git/Shell/file tools together in an AgentCore
MicroVM. The next phase covers private GitHub authorization, retrieval across
200 private repositories and replacing the OpenSandbox Browser. The fork also
contains an independent Git execution slice under `deploy/agentcore-git-slice`,
with separate source and live acceptance evidence; it does not migrate this
Web/Slack product path.

The current phase defers per-employee private GitHub authorization, retrieval
across 200 private repositories, and replacing OpenSandbox Browser with
AgentCore Browser. The current Backend and Worker images still have one
unresolved vendor zlib High finding; the current Node 24.21.0 Frontend image
scan is clean.
