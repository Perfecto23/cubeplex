# CubePlex native entrypoints on AgentCore

This adapter keeps CubePlex's accounts, workspaces, conversations, run IDs, Slack transport and storage in its existing Backend. CubeLoop and the local Git/Shell/file tools run together in a task-specific AgentCore MicroVM. The Backend exposes only dispatch-scoped callbacks for the model, history, progress, saved files and human answers.

The local integration flow has passed: the real CubeLoop Worker calls the actual HTTP router, PostgreSQL, Redis and RustFS to execute a command, present a file, pause for human input and resume from a fresh workspace. Only the external model HTTPS response was scripted in that test. The native route has not yet been deployed or accepted through the live Web and Slack interfaces. See the [design](../../docs/dev/specs/2026-09-13-agentcore-native-entry-design.md) and [plan](../../docs/dev/plans/2026-09-13-agentcore-native-entry.md). The independent [Git slice](../agentcore-git-slice/README.md) has its own cloud evidence and is not the native product acceptance result.

## What an employee uses

Employees use the existing CubePlex website or mention the configured Slack Bot. They do not install a local runner. The Backend creates a durable dispatch for the native run and invokes AgentCore. Progress and explicit file outputs return through the same CubePlex events consumed by Web and Slack. Channel-thread followups require an @mention under the current Slack contract.

Each new prompt or resumed human answer uses its own dispatch and Runtime session. The MicroVM loads the conversation's typed CubeLoop history and saved ordinary files from the Backend. The original run ID is retained across human approval and answer dispatches. Waiting for a human does not require keeping the MicroVM alive.

## Current capability boundary

- Available local tools: bounded Bash execution, Git via Bash, UTF-8 file read/write/edit, explicit file presentation and CubeLoop `ask_user`.
- Public repositories can be cloned, read and tested in the MicroVM. Ordinary, non-hidden workspace files are saved in the existing S3-compatible storage. The native tool environment does not receive model provider, database, object-store or GitHub master credentials.
- `.git`, hidden files, credentials, installed packages and background processes are not restored. A saved source directory is not a resumed Git checkout. The independent slice's one-repository push/PR broker is not connected to these native entrypoints. Do not claim native authenticated push/PR or complete Git-state persistence.
- Attachments, non-Responses providers, model fallback chains and configured organization command rules currently cause preparation to fail closed. Other CubePlex tools, plugins and OpenSandbox-specific capabilities remain available on the retained compatibility route.

These boundaries keep this stage focused on the control-plane/execution connection. They do not grant access to company private repositories or change Slack subscriptions.

## Task callbacks and persistence

The Runtime receives `{version: 2, dispatch_id, capability}`. Its HTTPS control-plane origin is fixed by deployment configuration. The purpose-specific capability is accepted only by `/api/v1/agentcore/tasks/{dispatch_id}/...`; the server resolves identity and scope from the immutable dispatch row. A normal user login token cannot act as a task capability.

Checkpoint mutations and their request receipts commit in the same PostgreSQL transaction. Reusing a request ID with another body is rejected. Model-call claims are durable before calling the external provider, so an uncertain model response is not automatically invoked again. Progress events use a per-dispatch sequence and atomic Redis deduplication. Terminal events and run status are applied together, then the dispatch records completion.

Successful completion requires both a durable CubeLoop completed-run checkpoint and an acknowledged workspace snapshot. A human-input pause requires the pending question and original run ID to be saved. Stop requests are persisted before stopping the exact AWS Runtime session; an unconfirmed stop remains fenced. New callbacks after a stop cannot mutate the conversation.

## Build and deploy

Use the normal repository hooks and commit reviewed source before building. [build.sh](build.sh) creates images from a clean Git archive and records the commit and image identity. The MicroVM image is ARM64; the existing Testing node uses an AMD64 Backend image. The Frontend and compatibility Worker do not need rebuilding for this adapter.

[infra.yaml](infra.yaml) creates an immutable, scanned ECR repository and a minimal Runtime role. Providing the immutable image URI enables the PUBLIC Runtime and adds an exact invoke/stop permission to the existing controller role. The template creates no EC2, EKS, NAT Gateway, database or Secrets Manager secret. This PoC target is account `986420599013`, region `us-west-2`, profile `moego-testing`; verify it with explicit-profile STS readback before each deployment operation.

Configure the Backend with:

```yaml
execution:
  backend: agentcore
agentcore:
  execution_mode: native
  native_runtime_arn: <verified Runtime ARN>
  region: us-west-2
```

The existing auth signing secret stays in the Backend credential store. Run the generated `agentcore_callbacks` migration before enabling the native route. Update only the Backend image and native execution configuration after callback and persistence checks pass.

Rollback selects `agentcore.execution_mode: compatibility` and the retained compatibility image/configuration. A dispatch stores its selected Runtime ARN, so stop and recovery must target the Runtime that actually owns it. Keep the existing node and OpenSandbox running; the node still hosts the website, persistent services and the old Runtime's VPC egress route.

## Acceptance evidence required before declaring completion

Use newly marked Web and Slack tasks on the native route and record their run, dispatch and Runtime session IDs. Verify visible tool progress, an explicit downloadable file, contextual followups, real human input, stop with no late file write, fresh-session recovery, Backend restart and duplicate admission/callback delivery. Old compatibility tasks and the independent Git slice do not substitute for these results.

## Bounded simplification

Removed unused workspace/tool factory aliases and snapshot wrapper functions, diagnostic boot/hostname fields with no reliable session identity, an unused Backend context helper, and a build check for the compatibility Worker target that this adapter does not build. Existing tool and Runtime tests still pass. Keep the transactional callback receipts, terminal fences, process-group cleanup and event deduplication: their necessity is demonstrated by fault and replay tests.
