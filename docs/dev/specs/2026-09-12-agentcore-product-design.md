# CubePlex control plane with AgentCore execution

Status: implementation specification. Baseline: merged PR #1, `8fb3b5d6`.

## Product outcome

Employees use the existing CubePlex web UI and native Slack connector. Kubernetes
owns accounts, workspaces, conversation history, task admission, message delivery,
Postgres, Redis, object storage and OpenSandbox. AgentCore runs the existing
CubeLoop agent through CubePlex's RunManager execution methods. No local computer
is part of the deployed request path.

This phase preserves CubePlex's existing tools and storage. Private GitHub grants,
cross-repository retrieval and AgentCore Browser are separate future work.

## Execution boundary

`execution.backend=agentcore` selects remote execution in the API process; the
default remains the existing local backend. Both prompt and HITL respond dispatch
through this boundary. Worker initialization explicitly selects local execution
and creates only the dependencies needed by RunManager. It must not start the
Slack connector, scheduler, sandbox cleanup or startup run recovery.

The control plane persists an immutable dispatch before invoking AgentCore. The
invoke payload contains only a protocol version and dispatch ID. It does not carry
credentials or accept caller-supplied organization/workspace/user scope. The worker
loads the dispatch from shared storage, verifies the native run and conversation
scope, and atomically claims that dispatch once before entering RunManager.

One dispatch has one AgentCore session. A new prompt creates a new dispatch; an
HITL answer creates a new dispatch for the same native run with its existing CAS
resume claim. Followups load the native Postgres checkpoint, independent of any
AgentCore session lifetime. Duplicate invokes must return the existing dispatch
state without executing model calls or tools again.

Native RunManager remains responsible for agent construction, tools, checkpoint
writes, progress events, terminal status, usage and saved artifacts. The remote
worker publishes to the same Redis streams that the existing UI SSE and IM tailer
consume. The control plane never manufactures a completed result from an HTTP 200.

## Stop, restart and uncertainty

- Progress, completion and delivery are separate outcomes. Slack transport retry
  must not restart the agent.
- Stop first fences output and asks the owning remote RunManager to cancel through
  the existing control channel. Bounded fallback uses StopRuntimeSession. Only
  confirmed teardown releases the conversation; an unknown stop leaves it blocked.
- SDK invocation retries are disabled. If delivery of an invoke is uncertain,
  reconcile the durable dispatch and native run, not a new execution attempt.
- API restart must not mark a healthy remote run stale. Recovery reconciles known
  remote dispatches, preserves their stream and attaches monitoring/delivery only.
  It never resubmits a previously claimed dispatch.
- Worker failure is an explicit failure/stop after teardown verification. A new
  user request may use saved conversation history; the old task is not replayed.
- Redis persistence is enabled for coordination/dispatch records; Postgres and
  object/PVC storage remain the source of chat and saved artifact durability.

## Deployment and cost boundary

Only account `986420599013`, region `us-west-2` is authorized. Initial discovery
found no EKS cluster or EC2 instance there. Choose a small single-node Kubernetes
deployment after pricing and capacity readback. Keep one API and frontend replica;
avoid managed NAT/ALB/EKS fixed charges if an economical k3s node meets the test.
This is a single-node PoC, not a high-availability production topology.

AgentCore needs private access to the existing Postgres, Redis, RustFS and
OpenSandbox endpoints. Prefer VPC networking with narrowly scoped security groups;
do not expose databases publicly. Any NAT instance role is confined to the new
PoC node and dedicated runtime subnet. Shared default VPC resources are retained.

Pin built images by digest and record source/build/config provenance. Address
upgradable OS image findings and report unresolved vendor findings explicitly.
No provider, Slack or database credentials belong in Git, build args or logs.

## Acceptance evidence

1. Deployed HTTPS web UI: login, workspace, real AgentCore task and progress.
2. Native Slack event: one admitted run and one reply, with local poller absent.
3. Stop during tool/model work: teardown confirmed, no late result or side effect.
4. HITL request/answer and followup after AgentCore session reclamation.
5. Saved artifact and conversation readable after worker and API restart.
6. Duplicate Slack event, duplicate invoke, interrupted delivery and process restart
   do not execute the same dispatch or post the final answer twice.
7. Unauthorized invoke/scope tampering is rejected before model/tool execution.

Each acceptance record binds source commit, image digest, Runtime version, native
conversation/run/dispatch IDs, channel message IDs where applicable, and observed
business outcome. Infrastructure readiness alone does not satisfy this contract.
