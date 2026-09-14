# CubePlex AgentCore product deployment

This directory is the operator guide for the fork's full product path:

```text
Web / native Slack
        │
        ▼
Kubernetes CubePlex control plane
(accounts, workspaces, conversations, RunManager, SSE, delivery)
        ├── Postgres / Redis / RustFS / OpenSandbox in Kubernetes
        │ invoke / dispatch-scoped HTTPS callbacks
        ↕
Amazon Bedrock AgentCore Runtime
(ARM64 native CubeLoop worker)
```

The guide targets the economical Testing deployment in AWS account
`986420599013`, region `us-west-2`, stack `cubeplex-product-20260912`. The
Native Runtime v2 is the only AgentCore product runtime and is `READY`.
Native Web/Slack tasks and followups, saved-file recovery, same-run HITL after
Backend restart, running-command stop without a late write, and duplicate
admission/callback checks have passed. See the Native guide for the evidence
boundary. The former compatibility
Runtime v3 and the independent Git execution Runtime are retired.

The newer Native Runtime v2 is documented separately in the [Native entrypoint
guide](../agentcore-native-entry/README.md). It uses the same Kubernetes control
plane but runs CubeLoop and the bounded Git/Shell/file tools together in an
AgentCore MicroVM. Its current Testing baseline is Runtime
`cubeplex_native_entry_20260913-sWxaCf7pyC`, Worker source
`09f272ec4088f72fe709cc89b01d7abd68fe45d3` / digest
`sha256:b173931fb47dfa4ad1195938b1f4d40ed41d251801fd6467f6d3b2d8624b4edc`, and
Backend source `5e616137415f7b93d3e86b9514739ba129d9a7c4` / digest
`sha256:4f53c623cc2daa10fee54b3acfcab2b76e14ff5cb43c44cdccb293feb1c05812`.
The migration and image are a matched baseline. Native Web/Slack task and
follow-up, HITL, stop/reclaim, restart and duplicate-callback checks have passed.
OpenSandbox remains for Browser, Terminal and sandbox file-sidebar capabilities;
it is not an AgentCore rollback Runtime.

The fork starts from merged commit `8fb3b5d6a171451848d7939be0209709e8ea05b3`
(upstream product version `0.7.2`). The older bounded Slack polling PoC remains
under [`../agentcore-poc/`](../agentcore-poc/) for historical verification; it
is not the product ingress described here.

## What owns what

| Layer | Owns | Does not own |
|---|---|---|
| Kubernetes CubePlex | users, org/workspace membership, conversations, native RunManager, dispatch rows, Redis event streams, SSE, Slack queue/delivery, Postgres, Redis, RustFS, OpenSandbox | the AgentCore VM session lifetime |
| AgentCore worker | one claimed dispatch, native CubeLoop construction, model/tool execution, checkpoints, progress and terminal result | account admission, Slack event receipts, durable conversation storage |
| Postgres | conversations, memberships, dispatch identity/status, checkpoints, outbound delivery state/cursors and saved product records | transient live stream delivery |
| Redis | run coordination, event streams, locks and active-run metadata | the authoritative conversation history |
| RustFS/S3 | artifact and attachment bytes | user/workspace authorization |
| OpenSandbox | the existing CubePlex Browser, Terminal and sandbox file tools | AgentCore Runtime lifecycle |

One prompt or HITL answer creates one immutable dispatch and one derived
AgentCore session ID. The worker claims it atomically before entering
RunManager. A duplicate invoke observes the existing dispatch state and does
not call the model again. A stop whose remote teardown cannot be confirmed is
left fenced as `stop_unknown`; it is not released for a new execution.

AgentCore session reclamation does not delete the durable conversation or
artifact. A followup creates a new dispatch and reloads the native checkpoint.
The single k3s node is still a Testing single point of failure; its local-path
PVCs are persistent across pod restarts, but the topology is not HA.

## Testing baseline and cost

The current target is deliberately small:

- one `t3a.xlarge` EC2 node: 4 vCPU, 16 GiB memory;
- Ubuntu 24.04, k3s `v1.36.4+k3s1`, containerd `2.3.4`;
- 60 GiB encrypted gp3 root disk retained on stack deletion;
- one public EIP used for Caddy HTTPS and SSM-managed access;
- no EKS control-plane charge, managed NAT Gateway or ALB;
- the node is amd64, so the PGroonga/pgvector Postgres image is scheduled on
  this architecture.

The historical baseline estimate is approximately **$118.24/month** at 730
hours, or **$3.89/day**, before ECR storage and scans, AgentCore execution,
model calls and traffic. It is not a current repricing. Standard CPU credits can
slow a busy node. Stop the node when testing is paused to reduce compute cost,
but remember that retained EBS, EIP and ECR resources still have their own
costs. Do not delete the retained disk until the recovery decision is made.

## 1. Freeze source and build immutable images
The current Testing image pair is already built and deployed from committed
sources. Backend source `5e616137415f7b93d3e86b9514739ba129d9a7c4` uses digest
`sha256:4f53c623cc2daa10fee54b3acfcab2b76e14ff5cb43c44cdccb293feb1c05812`.
Native Worker source `09f272ec4088f72fe709cc89b01d7abd68fe45d3` uses digest
`sha256:b173931fb47dfa4ad1195938b1f4d40ed41d251801fd6467f6d3b2d8624b4edc`.
The deployed Frontend uses digest
`sha256:5c2816ef1f898fb585246b585cc3d17efabb807a956e2af8233012ef81c7dbc1`.

If a future Native rebuild is required, use
[`../agentcore-native-entry/build.sh`](../agentcore-native-entry/build.sh),
which creates a clean archive and records the source manifest. Its Dockerfile
uses the Git slice Worker digest
`sha256:454e29089075d2e3c49bb91f9d73624218e7a8eb953e5d9cd2ed9c323953974d` as
its base. Retain that one Git slice Worker image and repository for rebuilding;
the Git slice Runtime, broker, Lambda, S3 state and dedicated Secrets are
retired.

The Frontend image is Node 24.21.0 and its ECR scan is clean. Backend and
Native Worker each have one unresolved vendor High in zlib (`CVE-2026-85091`),
and the Native image has the recorded nghttp2 Medium; do not describe these
images as vulnerability-free until the vendor publishes fixed packages.

## 2. Review ProductStack retention

Use only the Testing profile and region:

```bash
export AWS_PROFILE=moego-testing
export AWS_REGION=us-west-2
export STACK_NAME=cubeplex-product-20260912
```

The ProductStack template is now Native-only. It retains the existing Node,
NodeRole, NodeProfile, NodeSecurityGroup, EIP, Backend/Frontend ECR
repositories and KubeconfigSecret. It no longer creates an AgentCore Runtime,
WorkerRole, WorkerConfigSecret, Worker ECR repository, private Runtime subnet,
route table, security group or old Runtime invoke policy.

Use [`infra.yaml`](infra.yaml) only to review a change set against the existing
Testing stack. Do not use the old `aws cloudformation deploy` recipe to create
a new experiment stack, and do not pass `WorkerImageUri` to this ProductStack
template. Native Runtime creation and its invoke policy belong to
[`../agentcore-native-entry/infra.yaml`](../agentcore-native-entry/infra.yaml).
The Native template reuses the Product `NodeRole`; its Runtime is PUBLIC and
has no ProductStack worker Secret or private subnet dependency.

The 2026-09-14 cleanup is complete. After AWS released the old AgentCore ENI,
dependency checks found no consumers of the orphaned security group or subnet;
both were deleted and direct EC2 reads confirmed they no longer exist.
CloudFormation had reported `UPDATE_COMPLETE` despite `DELETE_FAILED` resource
events, so stack status alone is not evidence that retired resources are gone.

The fork's documentation workflow builds and checks these pages on PRs and
main. Cloudflare publication is opt-in: enable repository variable
`DOCS_CLOUDFLARE_DEPLOY_ENABLED=true` only after configuring `CF_API_TOKEN` and
`CF_ACCOUNT_ID` for the intended `cubeplex-docs` Pages project. The upstream
repository retains its existing publication behavior.

Read back the stack, Node, EIP, ECR repositories and KubeconfigSecret before
any change. Keep the retained Node and its storage protected. The separate
Native ECR base image `sha256:454e290...` remains required because the Native
Dockerfile uses it as `FROM`; retirement of the Git slice runtime does not
retire that build dependency.

## 3. Obtain cluster access through SSM

Do not open SSH or the k3s API to the public Internet. Use the Session Manager
plugin to forward the node's private port 6443 to a local port, then publish or
retrieve the kubeconfig through the stack's designated Secrets Manager record.
The helper [`ssm.py`](ssm.py) runs a reviewed script on the node and writes the
result to a private operator evidence file; it does not put credentials in Git
or command arguments.

The operational sequence is:

1. Start an SSM port-forward from the node to local `127.0.0.1:16443`.
2. Run [`publish_kubeconfig.py`](publish_kubeconfig.py) on the node through
   SSM so the k3s kubeconfig is stored in the stack's kubeconfig Secret.
3. Fetch it into a mode-0600 local file and rewrite its server address to the
   local forward.
4. Run all `kubectl` and Helm commands with that explicit kubeconfig.

The SSM tunnel is an operator access path, not a runtime dependency. Close it
after the deployment or keep it only while an operator is actively testing.

## 4. Deploy the persistent Kubernetes control plane

The product overlay is [`values.yaml`](values.yaml). It selects:

- local-path PVCs for Postgres, Redis and RustFS;
- one Backend and one Frontend replica;
- one OpenSandbox Server replica with server-side sandbox create timeout 180s;
- the official OpenSandbox controller digest that supports
  `/run/k3s/containerd/containerd.sock`;
- Redis AOF with `appendfsync always`;
- Caddy-owned host-network HTTPS, so the CubePlex chart Ingress is disabled.

The OpenSandbox Server config is stored as a Secret and its checksum is part of
the Deployment template. Changing the API key or the 180-second create timeout
therefore triggers a controlled rollout instead of leaving an old Pod with
stale configuration.

Create the two OpenSandbox namespaces before Helm if they do not already
exist. Build the local chart dependencies from the fork's checked-in vendor
sources; do not run a vendor refresh that replaces the fork's controller,
Secret, timeout or config-merge changes.

```bash
kubectl --kubeconfig "$KUBECONFIG" create namespace cubeplex --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG" apply -f -
kubectl --kubeconfig "$KUBECONFIG" create namespace opensandbox-system --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG" apply -f -
kubectl --kubeconfig "$KUBECONFIG" create namespace opensandbox --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG" apply -f -

helm dependency build deploy/kubernetes/charts/cubeplex/vendor/opensandbox
helm dependency build deploy/kubernetes/charts/cubeplex
```

Create a private values overlay containing the generated auth secrets,
Postgres/Redis/RustFS credentials, model provider configuration, sandbox API
key, and final Caddy hostname. Do not put those values in this repository. The
overlay must also set the immutable Backend and Frontend tags from the ECR
readback. `values.yaml` deliberately leaves those tags empty so a forgotten
image injection cannot silently select a mutable release tag.

```bash
helm upgrade --install cubeplex deploy/kubernetes/charts/cubeplex \
  --kubeconfig "$KUBECONFIG" \
  --namespace cubeplex \
  --create-namespace \
  -f deploy/kubernetes/charts/cubeplex/values.yaml \
  -f deploy/agentcore-product/values.yaml \
  -f <private-values-file>
```

The Backend uses `Recreate` while the node runs host-networked control-plane
services. If an older Deployment still retains a `rollingUpdate` field from a
previous strategy, clear it once before the upgrade; Kubernetes rejects the
mixed strategy even when the Helm values are correct:

```bash
kubectl --kubeconfig "$KUBECONFIG" -n cubeplex patch deployment cubeplex-backend \
  --type=merge \
  -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'
```

Verify the Deployments, StatefulSets, PVCs, OpenSandbox CRDs and the RustFS
bucket Job. A successful Helm release is not an AgentCore acceptance result;
it only proves the control plane and its local dependencies are ready.

## 5. Publish Caddy HTTPS and bootstrap the operator

Apply [`access.yaml`](access.yaml) after creating a `cubeplex-endpoint`
ConfigMap with the chosen host. The current Testing host follows the
`sslip.io` pattern and terminates TLS in the host-network Caddy Pod. Caddy
routes `/api/*`, `/health/*` and `/sandbox-panel/*` to Backend and the rest to
Frontend, preserving SSE and sandbox panel paths.

Public registration is closed in this Testing deployment. The operator first
creates or verifies the test user through the private operator path, then uses
the Backend CLI to grant organization admin:

```bash
kubectl --kubeconfig "$KUBECONFIG" -n cubeplex exec deploy/cubeplex-backend -- \
  python -m cubeplex.cli admin grant-admin <operator-email> --org-slug default
```

The exact command must run against the deployed Backend image and the existing
`default` organization. The first Web registration is not treated as an
automatic owner bootstrap here: the current database already has the default
organization, and operator role assignment is explicit. Confirm Web login,
workspace selection and a persisted conversation before enabling external
Slack traffic.

## 6. Keep the ECR pull Secret current

The operator must create `cubeplex/ecr-pull` once with a real ECR docker config;
[`ecr-refresh.yaml`](ecr-refresh.yaml) intentionally does not create an empty
Secret. Apply the manifest after replacing `__BACKEND_IMAGE__` with the pinned
Backend digest already available in the node cache.

The CronJob runs every six hours with `IfNotPresent`, host networking and
`ClusterFirstWithHostNet`. It uses the EC2 node role to call ECR, then patches
only the named Secret through the Kubernetes API using the mounted service
account token and cluster CA. Its Role has only `get`, `patch` and `update` on
that one Secret; it cannot list or create Secrets. This lets a restarted Pod
use a cached Backend image even when the previous ECR token has expired.

Read the Job outcome as `ecr_refresh_succeeded`; the job never prints the ECR
token, request body or Secret value.

## 7. Use the Native Runtime

The Native Runtime is already connected to the current Backend. Its immutable
ARM64 image is `sha256:b173931fb47dfa4ad1195938b1f4d40ed41d251801fd6467f6d3b2d8624b4edc`.
The Native stack owns the Runtime, WorkerRole and Native ECR repository. The
ProductStack owns the Node and its NodeRole; the Native stack attaches the
exact invoke/stop policy to that existing role.

The Backend private configuration uses:

```yaml
execution:
  backend: agentcore
agentcore:
  execution_mode: native
  runtime_arn: <verified-native-runtime-arn>
  native_runtime_arn: <same-verified-native-runtime-arn>
  region: us-west-2
```

The Runtime is PUBLIC and has no ProductStack worker Secret, private subnet or
ProductStack Worker ECR dependency. The Worker loads the server-owned dispatch
through the Native callback API. The Backend owns dispatch claims and publishes
callback progress and terminal state to the existing Redis stream.

Read back the Runtime ARN, version, Worker image digest, readiness and the
exact `InvokeAgentRuntime`/`StopRuntimeSession` resources after any Native
stack change. Do not recreate the retired compatibility Runtime or the retired
Git slice Runtime, Lambda, S3 state or dedicated Secrets.

## 8. Configure native Slack safely

Use the dedicated Testing xapp/Socket Mode Slack app. Do not reuse or reroute
the old Multica relay. Store the bot token and app token in the Backend's
credential store or private Kubernetes Secret. The native gateway reads the
account's `bot_token`, `app_token` and `bot_open_id`, and filters events before
ingest with:

```yaml
im:
  slack:
    allowed_channel_ids: [<test-channel-id>]
    allowed_user_ids: [<test-user-id>]
```

For the current Testing binding, the admitted private test channel and user
are the ones recorded in the private operator evidence. Keep those IDs out of
public docs and do not broaden the allowlist during acceptance.

The user sends `/link <company-email>` or `link <company-email>` in Slack.
CubePlex returns a short-lived Web link; after the authenticated user confirms
it, the Slack identity maps to the existing CubePlex user/workspace. Verify
the binding before testing a task. Socket Mode commits the inbound receipt and
queue admission before acknowledging the event. The outbound tailer resumes
from its durable checkpoint and uses stable client message IDs, so a Slack
retry does not create a second AgentCore dispatch or a second final reply.

## 9. Acceptance sequence

Record the Native source SHA, image digests, Runtime version,
conversation/run/dispatch IDs and user-visible result for every check. The
historical compatibility Runtime evidence is not reassigned to Native.

| Path | Current Native evidence |
|---|---|
| Web and Slack product path | Real Web/Slack task and followup acceptance passed; the current cleanup Web readback returned `CLEANUP-NATIVE-OK` with one model callback |
| HITL after Backend restart | Same-run question/answer passed after Backend restart; a fresh Worker restored the Native checkpoint and workspace |
| File/workspace continuity | Previous `Alpha` history remained readable after the cleanup session was absent |
| Stop B | The prepared running-command stop completed with no late file write or marker |
| Duplicate admission and callback replay | Passed without duplicate execution or final reply |
| Cleanup readback | 31 dispatches were `finished`; the Native session was absent; 8 Pods were Ready and 6 PVCs were unchanged |

See the [Native entrypoint guide](../agentcore-native-entry/README.md) for the
Native evidence boundary. OpenSandbox sandbox reservations still follow the
configured cleanup window; ordinary followup works during that window and
another tool request in the same Sandbox scope may wait.

## 10. Stop, retain and recover

Keep the EC2 node running while the current Web/Slack control plane, persistent
storage and OpenSandbox services are in use. The retired compatibility egress
has been removed; Git slice Lambda and S3 state were never hosted on this node.
Stopping the node interrupts the current product path.

A separately confirmed cost pause must first record active-run state, back up
persistent data and accept the outage. Retain the Node, EBS, EIP, Backend and
Frontend ECR repositories, KubeconfigSecret, Native ECR repository and the
Git slice Worker base image required by the Native Dockerfile. Stopping EC2
does not remove retained-resource costs.

Resume by starting the same node, reopening SSM, checking k3s/PVC health and
running the ECR refresh and Helm readbacks. Do not stop or delete shared
unrelated resources.

If a deployment fails, read the CloudFormation stack, SSM invocation, Pod
events, Native Runtime status and Redis/Postgres dispatch state before retrying.
The durable dispatch is the authority for whether a worker may execute; an
unknown invoke or stop must be reconciled, never blindly replayed.

This is a single-node Testing topology. A lost node can lose local-path PVC
availability even though the Kubernetes objects and Native session are still
present. Production would need replicated storage, multiple control-plane
instances, a managed or HA Kubernetes topology, stronger inbound auth and a
separate secret rotation policy.

## Next phase and deferred capabilities

OpenSandbox remains the Kubernetes environment for Browser, Terminal and
sandbox file-sidebar capabilities. Native Runtime v2 runs the Agent and
bounded Git/Shell/file tools together in an AgentCore MicroVM.

The independent [Git execution slice](../agentcore-git-slice/README.md) is
retired historical evidence. Per-employee private GitHub authorization,
indexing and retrieval across 200 private repositories, and replacing the
existing OpenSandbox browser with an AgentCore Browser capability remain
deferred until a separately approved next phase.
