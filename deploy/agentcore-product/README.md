# CubePlex AgentCore product deployment

This directory is the operator guide for the fork's full product path:

```text
Web / native Slack
        │
        ▼
Kubernetes CubePlex control plane
(accounts, workspaces, conversations, RunManager, SSE, delivery)
        │ durable dispatch + Redis event stream
        ▼
Amazon Bedrock AgentCore Runtime
(ARM64 native CubeLoop worker)
        │
        └── Postgres / Redis / RustFS / OpenSandbox on the private path
```

The guide targets the economical Testing deployment in AWS account
`986420599013`, region `us-west-2`, stack `cubeplex-product-20260912`. The
control plane and Runtime v3 are deployed; the Runtime readback is `READY`.
Web compute, HITL, ordinary followup, file readback after AgentCore reclaim,
long tasks, Backend restart, normal native Slack, duplicate replay, prepared
stop and stop-then-followup have passed real checks. This is a compatibility
PoC: the final running-command stop and followup checks have also passed.

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
| OpenSandbox | the existing CubePlex sandbox tools and workspace files | AgentCore Runtime lifecycle |

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
- one private Runtime subnet (`172.31.128.0/24`) with outbound HTTPS through
  the node's NAT rules;
- no EKS control-plane charge, managed NAT Gateway or ALB in this PoC;
- the node is amd64, so the PGroonga/pgvector Postgres image is scheduled on
  this architecture.

The measured baseline fixed cost is approximately **$118.24/month** at 730
hours, or **$3.89/day**, before the two new Secrets Manager secrets (about
**$0.80/month**), ECR storage and scans, AgentCore execution, model calls and
traffic. Standard CPU credits can
slow a busy node. Stop the node when testing is paused to reduce compute cost,
but remember that retained EBS, EIP and credential-store resources still have
their own costs. Do not delete the retained disk until the recovery decision is
made.

## 1. Freeze source and build immutable images

Build only from a committed SHA. `build.sh` creates a private `git archive`,
exports `requirements-frozen.txt` with `uv --frozen`, builds the standard
Backend for `linux/amd64` and the AgentCore worker target for `linux/arm64`,
and records the source manifest and ECR digests. It refuses to use the current
dirty worktree and `--push` is restricted to the Testing account, region and
repository prefix.

```bash
SOURCE_SHA=<committed-product-sha>
deploy/agentcore-product/build.sh "$SOURCE_SHA" --push
```

The worker build is intentionally separate from the frontend build. The
Backend Dockerfile has two named final targets: `backend` remains the default
target, while `agentcore-worker` runs `python -m cubeplex.agentcore.runtime` on
port 8080. The worker image must be pushed to:

```text
986420599013.dkr.ecr.us-west-2.amazonaws.com/cubeplex-product-20260912/worker@<digest>
```

The final image readbacks for Runtime v3 are:

```text
source=11a4a524713fe06d290f5610196099c694f9b132
backend@sha256:fa68ed7d039065064b6a3f24d57ca1312dfce07b4c3127257e4c472b039441bc
worker@sha256:d632fe8f985fb88a2552a25068d090dc1a1747ef6f173355c1eb7d5938b62e40
frontend@sha256:5c2816ef1f898fb585246b585cc3d17efabb807a956e2af8233012ef81c7dbc1
```

The Frontend image is Node 24.21.0 and its ECR scan is clean. Backend and
Worker each have one unresolved vendor High in zlib (`CVE-2026-85091`); do not
describe the current images as vulnerability-free until the vendor publishes a
fixed Debian package.

## 2. Create the small AWS foundation

Use only the Testing profile and region:

```bash
export AWS_PROFILE=moego-testing
export AWS_REGION=us-west-2
export STACK_NAME=cubeplex-product-20260912
```

The first CloudFormation deployment should leave `WorkerImageUri` empty. That
creates the node, private Runtime subnet, ECR repositories, worker config
Secret, worker IAM role, node IAM role and SSM access without creating a
Runtime from an unverified image. The template is
[`infra.yaml`](infra.yaml).

Supply the VPC, public subnet, Availability Zone and Ubuntu AMI chosen from
current AWS readback. The current Testing selection is the default VPC in
`us-west-2a`; do not copy an old subnet or AMI into another account.

```bash
aws cloudformation deploy \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file deploy/agentcore-product/infra.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    VpcId=<testing-vpc-id> \
    PublicSubnetId=<testing-public-subnet-id> \
    AvailabilityZone=us-west-2a \
    ImageId=<ubuntu-24.04-ami-id> \
    WorkerImageUri=
```

Read back the stack outputs and node state before continuing. The node role
pulls only the product ECR repositories and has the SSM managed policy. The
AgentCore role reads only the worker config Secret, pulls only the worker
repository, and writes its own runtime log group. When `WorkerImageUri` is
non-empty, the template additionally creates the AgentCore Runtime and grants
the node only `InvokeAgentRuntime` and `StopRuntimeSession` for the exact
Runtime ARN **and** the exact AgentCore default endpoint ARN. Both resources
are required by IAM even though the invoke call targets the Runtime ARN.

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

## 7. Connect AgentCore after the worker image is frozen

For a fresh deployment, create the Runtime only after the final ARM64 worker
image has been pushed and its immutable digest has been read back from ECR. The
current Testing Runtime v3 is `READY` with the Worker image above, private VPC
networking, 60-second idle timeout and 900-second maximum lifetime. Its tracked
async task state reports `HealthyBusy` while work is active, and API restart
attaches a monitor to the existing remote task instead of treating it as a new
dispatch. The long-task, Backend-restart, duplicate replay, prepared-stop and
stop-then-followup and running-command stop checks have passed.

1. Populate the Secrets Manager worker config record with the flat
   `CUBEPLEX_*` environment map. It includes database, Redis, RustFS,
   sandbox, auth vault and `ENV_FOR_DYNACONF=production` values. The API
   control plane additionally needs `CUBEPLEX_EXECUTION__BACKEND=agentcore` and
   `CUBEPLEX_AGENTCORE__RUNTIME_ARN=<runtime-arn>` in its private config.
2. Update the CloudFormation stack with the ARM64 worker immutable URI as
   `WorkerImageUri`. The Runtime uses VPC networking to reach the private
   Postgres, Redis, RustFS and OpenSandbox NodePorts.
3. Read back the Runtime ARN, version, role, image digest, lifecycle settings
   and readiness. The current template uses a 60-second idle timeout and
   900-second maximum lifetime for Testing. Read back the node policy and
   confirm both the exact Runtime ARN and the exact AgentCore default endpoint
   ARN are present in the `InvokeAgentRuntime` and `StopRuntimeSession` resource
   list.
4. Update the Backend control plane with the Runtime ARN, restart it, and
   verify that a new RunManager prompt creates a durable dispatch before the
   AgentCore invoke.

The worker loads its config Secret before importing CubePlex configuration.
The invocation wire contains only protocol version and dispatch ID; it does
not contain provider keys or caller-supplied scope. The worker loads the
server-owned dispatch from Postgres, verifies org/workspace/conversation/user
scope, claims once, and publishes to the existing Redis stream.

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

Record the source SHA, image digests, Runtime version, conversation/run/dispatch
IDs and user-visible result for every check:

| Path | Current evidence | Remaining boundary |
|---|---|---|
| Web prompt, progress and final result | Web compute/progress passed on v1; final-version ordinary followup passed on v3 | None for the tested path |
| Web HITL answer and ordinary followup | HITL respond passed on v1 with a new dispatch for the same run; ordinary followup passed on v3 with a new run and the existing conversation checkpoint | None for the tested path |
| File/artifact read after AgentCore reclaim | Passed; content remained readable after session absence | None for the tested path |
| Web Stop during the short tool path | Passed with teardown and no late marker | None for the tested path |
| Long-running async task | Passed on v2: tracked async execution and fresh heartbeats continued beyond the 60-second idle timeout | None for the tested path |
| Backend restart during remote work | Passed on v2: Backend was restarted at 85 seconds during a 150-second task; no replay or second reply | None for the tested path |
| Normal native Slack task and reply | Passed after real user identity link | None for the tested path |
| Duplicate Slack/dispatch replay | Passed; no duplicate execution or final reply | None for the tested path |
| Stop before execution starts | Passed; native cancel and dispatch terminal state completed in about 0.22s with no `stop_unknown` | Keep the readback in the acceptance evidence |
| Stop during command execution | Passed on v3: the start marker was independently observed; stop at about 50 seconds reached cancelled/finished in about 0.30 seconds; no late marker after the original deadline | None for the tested path |
| Stop followed by a new followup | Passed; a new AgentCore run returned the expected Web result without a tool call | None for the tested path |

The agreed compatibility PoC acceptance is complete. Preparation cancellation
releases the run immediately, but the existing Sandbox reservation can remain
until its cleanup window: `create_timeout=300s`, `ready_timeout=300s` and
`cleanup_interval=30s` with `pause_enabled=false`. The observed cleanup took
about 10.5 minutes. Ordinary followup works during that window; another tool
request in the same Sandbox scope may wait. This PR preserves that behavior.

## 10. Stop, retain and recover

Keep the EC2 node running during the independent MicroVM Git slice. It still
hosts the Web/Slack control plane and storage, and provides outbound NAT for
this compatibility Runtime. Stopping it interrupts all of those paths.

A later, separately confirmed cost pause must first record active-run state,
back up persistent data and accept that outage. Retain EBS, EIP, Secrets
Manager records, ECR repositories and stack state. The known retained baseline
is about $9.25/month for 60 GiB gp3, one public IPv4 address and two product
Secrets, plus ECR and other usage; stopping EC2 does not remove those costs.
Resume by starting the same node, reopening SSM, checking k3s/PVC health and
the Runtime NAT route, then running Helm readback and the ECR refresh Job.
Do not stop or delete shared unrelated resources.

If a deployment fails, read the CloudFormation stack, SSM invocation, Pod
events, Runtime status and Redis/Postgres dispatch state before retrying. The
durable dispatch is the authority for whether a worker may execute; an unknown
invoke or stop must be reconciled, never blindly replayed.

This is a single-node Testing topology. A lost node can lose local-path PVC
availability even though the Kubernetes objects and AgentCore session are
still present. Production would need replicated storage, multiple control-plane
instances, a managed or HA Kubernetes topology, stronger inbound auth and a
separate secret rotation policy.

## Next phase and deferred capabilities

This compatibility PoC keeps OpenSandbox as a transitional Kubernetes tool
environment. The next phase first isolates platform credentials, then moves
Agent and tool execution together into an AgentCore MicroVM; file and Browser
capabilities can migrate after that boundary is secure. The independent
[Git execution slice](../agentcore-git-slice/README.md) supplies a separate
test entrypoint. Its acceptance does not migrate the existing Web/Slack path.

Per-employee private GitHub authorization, indexing and retrieval across 200
private repositories, and replacing the existing OpenSandbox browser with an
AgentCore Browser capability remain deferred until the next phase.
