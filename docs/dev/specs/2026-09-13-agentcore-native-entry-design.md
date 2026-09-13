# Native CubePlex entrypoints with a MicroVM Agent

Status: implementation in progress. The public Git slice has real publication and zero-model recovery evidence. The user authorized extending the artificial model-call cap on 2026-09-13; cumulative usage remains recorded. This document describes the native route, which is not yet deployed.

## Outcome and scope

A new Web or Slack request keeps the native account/workspace/conversation/run/dispatch identity, while the existing CubeLoop Harness and local Git/Shell/file tools execute together in an AgentCore MicroVM. The control plane owns authentication, history, progress/result delivery, approvals and saved files. Existing OpenSandbox and compatibility Runtime remain available for rollback. A successful old-path task is not evidence for this path.

Initial native acceptance uses only synthetic workspace files and the existing public Git fixture. Private repositories, 200-repository search, Browser, Supabase, a new database, EC2, EKS and NAT Gateway are excluded. Preserve the existing Slack channel/thread @mention contract; do not broaden ambient message ingestion.

## Selected boundary

Reuse the trusted CubePlex Backend as a task-scoped broker for its existing model configuration, native checkpoints, events, approvals and artifact services. This avoids extending the single-task Git experiment into a second product state platform. Keep the independent Lambda Git slice and its evidence intact.

The MicroVM receives a dispatch ID and a short task capability; its control-plane origin is fixed by deployment configuration. The capability is signed with a purpose-specific derived key, not accepted as a normal user login token, and bound to the server-side dispatch. Every privileged route resolves the authoritative RunContext from the dispatch; payload claims must not select org/workspace/user/conversation. No raw SQL, Redis, Vault, generic credential or arbitrary backend-tool proxy is exposed.

Only public model attributes and the authorized native conversation checkpoint cross into the VM. The long-lived model credential remains in the Backend; GitHub writer credentials remain in their trusted broker. The native VM role denies platform Secret retrieval and role assumption. Temporary task/IAM credentials are readable by arbitrary Shell and must therefore grant only the bounded task operations.

## Native contracts

- Admission: retain `start_run` and IM's preassigned run_id. A native dispatch is claimed once, persisted before remote work, and bound to its exact Runtime session. Replayed or uncertain admission cannot start a second Agent.
- Model: a task-authenticated Responses endpoint resolves the model from the frozen server configuration, preserves required CubeLoop wire fields, applies the approved budget and never returns provider credentials. A model error or incomplete response remains an error.
- History: load/save typed CubeLoop messages through the existing checkpointer. A new user message is appended once. Preserve tool-call/result IDs and checkpoint extra fields needed for continuation and approvals.
- Events: transfer versioned native CubeLoop events with a monotonic per-dispatch sequence. The Backend converts them with its existing StreamConverter and appends them to the native Redis stream. Sequence receipt and append need atomic deduplication; Web SSE and the Slack tailer continue consuming that stream.
- Files: local read/write/execute run in the VM. Explicitly presented outputs use the existing control-plane artifact/presented-file services. A bounded workspace checkpoint excludes task tokens, helper configuration and credential paths; extraction must reject traversal and links. Do not pretend a VM file exists in OpenSandbox.
- Approval: retain the same question_id/run_id and existing UI/Slack resume claim. Persist the pending question checkpoint before publishing it. Only the authorized control-plane answer may complete the pending tool result.
- Cancellation: persist cancellation before teardown, then stop the exact Runtime session and reconcile the existing terminal CAS/fence. A prepared/late-starting worker must check durable cancellation before model/tool execution. Confirm process/session termination; do not release `stop_unknown` on an unconfirmed stop.
- Completion: one terminal result, saved native checkpoint and explicitly published artifacts before declaring success. Terminal state cannot be reverted by a late callback. Do not automatically replay unknown side effects.

Event-level progress is sufficient for the first integration; it must show actual task/tool activity and the final result. Do not claim token streaming if transport buffers it.

## Implementation boundaries

1. Control-plane task API: task authorization, frozen manifest/model access, checkpoint and event/artifact methods. Reuse existing services and scoped repositories.
2. Native MicroVM host: HTTP callback client, CubeLoop Agent, local tools, checkpoint/approval lifecycle and bounded process cancellation. It imports no CubePlex platform bootstrap.
3. RunManager integration: native dispatch selection, invocation/recovery, native event delivery and terminal mapping. Existing Web/Slack consumers remain shared.
4. Deployment: build only affected Backend and MicroVM images after contracts pass; read the exact Testing account and change set. Keep the existing node, storage and OpenSandbox. Do not switch live execution until the current stage's acceptance gate is settled.

Code ownership is assigned per package before parallel writes. All browser operations stay with the main agent; small-node tool tests are serialized. No worker waits on a missing deployment or continuously polls another worker.

## Acceptance

Use new, visibly marked requests and record their actual route:

- Web: Git/Shell or file write/read, visible progress and a presented output, followed by a contextual question.
- Slack: new @mention task and a same-thread @mention followup, with run/dispatch/session and outbound-message links.
- Stop: stop a running command, verify no late marker/output, then ask a new question.
- Approval: one explicit question, a real UI or Slack answer, the same native run continuation.
- Reclaim/restart/replay: saved history/artifacts survive session teardown; a fresh session uses that state, completed delivery replay does not reexecute or repost.

The original Git task's cumulative ledger is preserved when its task limit is extended. The broker accepts a configurable finite limit; actual usage and any extension are recorded. Implementation preparation and zero-model checks do not prove completion of live native-entry acceptance.

## Protocol and rollout

The callback route family is `/api/v1/agentcore/tasks/{dispatch_id}/`: claim, control, checkpoint, events, workspace, present, model/responses and finish. A purpose-specific Bearer capability authenticates only this route family. Each handler resolves org, workspace, conversation and run from the durable dispatch. Payloads cannot choose another scope.

The native selector is `agentcore.execution_mode: native` with `agentcore.native_runtime_arn`; compatibility remains the default. The selected mode, model reference and system prompt are frozen in the dispatch request. Runtime requests contain only the version, dispatch identity and short capability. The callback origin is fixed in the image deployment configuration.

Checkpoint calls implement a closed subset of the existing CubeLoop Checkpointer protocol, with the thread and run supplied by the control plane. Event callbacks carry a monotonically increasing sequence and typed CubeLoop event. The control plane uses the existing stream conversion and native event schema. Completion requires saved history and workspace state; duplicate callbacks do not append duplicate events or start another Agent.

Alternative designs considered were extending the one-task Lambda broker into a new product state service, or keeping the Agent loop in Kubernetes and using only remote Shell. The selected Backend callback reuses native product state while keeping the Agent loop and tools together in the MicroVM.
