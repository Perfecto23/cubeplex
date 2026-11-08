# CubePlex native entrypoints on AgentCore

This adapter keeps CubePlex's accounts, workspaces, conversations, run IDs, Slack transport and storage in its existing Backend. CubeLoop and the local Git/Shell/file tools run together in a task-specific AgentCore MicroVM. The Backend exposes only dispatch-scoped callbacks for the model, history, progress, saved files and human answers.

The local integration flow has passed: the real CubeLoop Worker calls the actual HTTP router, PostgreSQL, Redis and RustFS to execute a command, present a file, pause for human input and resume from a fresh workspace. The Native Runtime is the only AgentCore product runtime, is `READY` in Testing, and real Web/Slack task and follow-up checks have passed. See the [design](../../docs/dev/specs/2026-09-13-agentcore-native-entry-design.md) and [plan](../../docs/dev/plans/2026-09-13-agentcore-native-entry.md). The independent [Git slice](../agentcore-git-slice/README.md) is retired historical evidence, not a product runtime.

The November refresh integrates upstream main `7abf66d1472565bdcb5f13223bf2e358bfa79180`
and pins both Backend and Native Worker to CubeLoop 0.16.1. Deployment uses the
existing Runtime `cubeplex_native_entry_20260913-sWxaCf7pyC` and K3s control plane.
The fork release manifest records the committed source, all three image digests,
and the deployed Runtime version. September's Native v2 acceptance remains a
historical baseline; a new release requires its own deployment checks.

## What an employee uses

Employees use the existing CubePlex website or mention the configured Slack Bot. They do not install a local runner. The Backend creates a durable dispatch for the native run and invokes AgentCore. Progress and explicit file outputs return through the same CubePlex events consumed by Web and Slack. Channel-thread followups require an @mention under the current Slack contract.

Each new prompt or resumed human answer uses its own dispatch and Runtime session. The MicroVM loads the conversation's typed CubeLoop history and saved ordinary files from the Backend. The original run ID is retained across human approval and answer dispatches. Waiting for a human does not require keeping the MicroVM alive.

## Current capability boundary

- Available local tools: bounded Bash execution, Git via Bash, UTF-8 file read/write/edit, explicit file presentation and CubeLoop `ask_user`.
- Public repositories can be cloned, read and tested in the MicroVM. Ordinary, non-hidden workspace files are saved in the existing S3-compatible storage. The native tool environment does not receive model provider, database, object-store or GitHub master credentials. Native conversations and downloadable file cards use the existing CubePlex event and artifact paths.
- `.git`, hidden files, credentials, installed packages and background processes are not restored. A saved source directory is not a resumed Git checkout. The independent slice's one-repository push/PR broker is not connected to these native entrypoints. Do not claim native authenticated push/PR or complete Git-state persistence.
- Attachments, non-Responses providers, model fallback chains and configured organization command rules currently cause preparation to fail closed. Browser, Terminal and sandbox file-sidebar capabilities remain on the retained OpenSandbox service.

These boundaries keep this stage focused on the control-plane/execution connection. They do not grant access to company private repositories or change Slack subscriptions.

## Task callbacks and persistence

The Runtime receives `{version: 2, dispatch_id, capability}`. Its HTTPS control-plane origin is fixed by deployment configuration. The purpose-specific capability is accepted only by `/api/v1/agentcore/tasks/{dispatch_id}/...`; the server resolves identity and scope from the immutable dispatch row. A normal user login token cannot act as a task capability.

Checkpoint mutations and their request receipts commit in the same PostgreSQL transaction. Reusing a request ID with another body is rejected. Model-call claims are durable before calling the external provider, so an uncertain model response is not automatically invoked again. Progress events use a per-dispatch sequence and atomic Redis deduplication. The immutable dispatch also carries the upstream admission, execution generation and attempt binding. Callbacks verify current execution authority. Terminal outcome is persisted before Redis delivery and slot release; a prepared finish receipt lets the control plane retry only cleanup after interruption.

Successful completion requires both a durable CubeLoop completed-run checkpoint and an acknowledged workspace snapshot. A human-input pause requires the pending question and original run ID to be saved. Stop requests are persisted before stopping the exact AWS Runtime session; an unconfirmed stop remains fenced. New callbacks after a stop cannot mutate the conversation.

## Build and deploy

Use the normal repository hooks and commit reviewed source before building. [build.sh](build.sh) creates the Native ARM64 image plus AMD64 Backend and Frontend images from the same clean Git archive. It installs Native dependencies from its frozen lockfile and verifies CubeLoop version, architecture, user and shell tools inside the image.

[infra.yaml](infra.yaml) creates the Native immutable ECR repository and minimal Runtime role. Providing the immutable image URI enables the PUBLIC Runtime and adds an exact invoke/stop permission to the existing Product `NodeRole`. The template creates no EC2, EKS, NAT Gateway, database or Secrets Manager secret. The Native Dockerfile uses the Git slice Worker digest `sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d` as its base; keep that one base image and repository for future rebuilds even though the Git slice Runtime, Lambda, S3 state and dedicated Secrets are retired. This target is account `986420599013`, region `us-west-2`, profile `moego-testing`; verify it with explicit-profile STS readback before any deployment change.

Configure the Backend with:

```yaml
execution:
  backend: agentcore
agentcore:
  execution_mode: native
  runtime_arn: <verified Native Runtime ARN>
  native_runtime_arn: <verified Runtime ARN>
  region: us-west-2
```

The existing auth signing secret stays in the Backend credential store. The migration chain keeps the already deployed `c3fd20f6c1d6` callback revision before the upstream lifecycle revisions. The Backend uses `execution.backend=agentcore`, `agentcore.execution_mode=native`, and the same verified Native ARN in both `runtime_arn` and `native_runtime_arn`. Update Backend, its migration image, Frontend and Native Runtime from one release manifest after callback and persistence checks pass.

### Upgrading the September deployment

1. Save a database backup, object-store data and the current deployment/configuration
   manifests. Confirm no run remains active, then scale the old Backend to zero;
   stop every other process that writes to the same database.
2. Run `python -m cubeplex.scripts.lifecycle_upgrade --maintenance` once using the
   new Backend image and the existing private configuration. This executes the
   upstream task-lifecycle backfill and moves the fork database from
   `c3fd20f6c1d6` to the current head. Existing data must not be stamped past DDL.
3. Apply the reviewed Helm values with the new Backend/migrator/Frontend images,
   `tempo.enabled=false` for the economical overlay, and OpenSandbox 1.1.1.
   Preserve the existing PVCs and server API key; the server TOML remains a Secret.
4. Update the existing Native Runtime with the release image, read back its
   version/endpoint and verify the new website, old history, a real task, human
   input/resume and cancellation. Later ordinary starts use the normal lifecycle
   upgrade command without `--maintenance`.

Keep the previous image/configuration identities and the pre-migration data
backup until acceptance. A failed schema upgrade is recovered by restoring the
matching backup and images; switching to retired compatibility resources is not
an available rollback. OpenSandbox continues serving Browser, Terminal and the
sandbox file sidebar. Native tool processes do not become persistent upstream
background tasks.

### Publishing a fork deployment

Fork tags use `agentcore-YYYY.MM.DD.N` to distinguish these deployments from
upstream's versioned public images. Build from the reviewed commit with
`deploy/agentcore-native-entry/build.sh <commit> --push`, merge through the normal
PR checks, and attach a secret-free `release-manifest.json` to the tag on the
merged commit. Record the build commit when its tree is identical to that merge.
The release contains the three immutable ECR images and the required Native base
image, plus the migration/validation result. The upstream `v*` image workflow is
reserved for the upstream release process.

## Acceptance contract and historical baseline

The September Native route passed real Web and Slack tasks and follow-ups, including command execution,
file presentation/download, HITL pause and answer in a new Worker session, stop with no late file write,
Backend restart with the same pending question/run, duplicate admission and late-callback fencing. Every new deployed release must repeat the affected checks against its actual images.
The final Web and Slack acceptance completed after local SSM, Kubernetes, database/Redis forwarding and
test Docker services were stopped; it used the deployed service path rather than a local relay.

Native mode is the current and only product execution mode. The Native credential boundary probe also passed: the Worker role matched, the platform package was unavailable, protected credential fingerprints had zero matches in env/proc, Secrets Manager and S3 reads were denied, the fake capability was rejected, and the exact Runtime session stopped with HTTP 200. Earlier failed probe attempts remain historical evidence only. The Frontend and OpenSandbox remain deployed for the product UI and legacy Browser/Terminal/file-sidebar capabilities.

Native ordinary workspace snapshots exclude `.git`, hidden files, credentials and installed environments.
The retired Git slice is not connected to the Native product entrypoint. Browser and terminal file-sidebar behavior remains on the OpenSandbox route. Per-employee private GitHub authorization, 200-repository retrieval and AgentCore Browser migration remain future work.

## Bounded simplification

Removed unused workspace/tool factory aliases and snapshot wrapper functions, diagnostic boot/hostname fields with no reliable session identity, an unused Backend context helper, and a build check for the compatibility Worker target that this adapter does not build. Existing tool and Runtime tests still pass. Keep the transactional callback receipts, terminal fences, process-group cleanup and event deduplication: their necessity is demonstrated by fault and replay tests.

The final Web and Slack confirmations also completed after local SSM/Kubernetes database forwards were closed and local test containers were stopped. This proves that those operator connections are not runtime dependencies; the test did not physically power off the operator computer.
