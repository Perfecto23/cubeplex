# Historical CubePlex AgentCore PoC (retired)

> This experiment has been retired. Its Runtime, ECR repository, execution role
> and Provider Secret are no longer active. The commands below document the
> original experiment and must not be used for the current deployment: they can
> recreate billable resources. Use the [Native product guide](../agentcore-native-entry/README.md)
> for the current Web/Slack service. The original source and verification records
> remain available for reference.

This directory belongs to the `Perfecto23/cubeplex` fork of
[`cubeplexai/cubeplex`](https://github.com/cubeplexai/cubeplex), based on upstream
commit `f5272e1901d0c0c785bdc2381da94951726da601`. It packages the real CubePlex
agent factory and LLM builder with CubeLoop 0.14.1 for AgentCore hosting.

The application has two processes: a locally running Slack controller and an
AgentCore-hosted execution handler. The controller reads one allowed channel,
invokes the handler using AWS IAM authentication, then posts the answer as a bot
to the original thread. The handler calls the configured Responses-compatible
model and reads GitHub source at one fixed commit. No Kubernetes Pod is created.

## Historical prerequisites

- Work from the root of the fork checkout containing this PR. Use an isolated
  worktree for changes.
- Install `uv`, Git, Python 3.12+ and AWS CLI. Docker with ARM64 build support is
  required only for building an image.
- Read [target.json](target.json). The deployment helpers deliberately bind one
  testing account/profile, region, resource prefix and user. They are not a
  generic installer for arbitrary AWS accounts.
- The Slack app needs a Bot token with `chat:write` and channel-history access,
  plus an authorized User token for thread readback. Both identities must belong
  to the configured Slack workspace and channel.
- The source repository is public. No GitHub token is used in this slice.

The identity values also appear in the controller's constants and AWS target
checks. To adapt the PoC to another environment, review those checks together
with `target.json`, the build registry and the corresponding tests. Changing a
JSON file alone does not change every authorization boundary.

## 1. Install and verify locally

```bash
uv sync --project deploy/agentcore-poc --frozen
PYTHONPATH=backend uv run --project deploy/agentcore-poc \
  python -m pytest -q deploy/agentcore-poc/tests
uv run --project deploy/agentcore-poc \
  ruff check backend/cubeplex/agentcore_poc deploy/agentcore-poc
```

The PoC has an independent dependency lock because it does not need the full
Backend's databases or OpenSandbox services. OpenAI 2.40.0, Anthropic 0.105.2 and
AnyIO 4.13.0 match the upstream provider SDK versions used for acceptance.

## 2. Prepare private configuration

The deployment helper reads `~/.config/my-provider/.env` with these keys:

```dotenv
BASE_URL=https://your-responses-compatible-provider.example/v1
KEY=your-private-provider-key
MODEL=your-enabled-model
EFFORT=low
```

`BASE_URL` accepts either the host root or its `/v1` endpoint. The PoC uses the
OpenAI Responses-compatible protocol. Keep this file outside Git with mode
`0600`. The helper transfers the values directly into the scoped Secrets Manager
Secret; only its ARN is placed in Runtime environment configuration.

Prepare a separate private Slack env file:

```dotenv
SLACK_BOT_TOKEN=your-private-bot-token
SLACK_USER_TOKEN=your-private-user-token
```

The controller verifies each token's identity. Slack tokens stay on the operator
machine and are not transmitted to AgentCore. Never add either env file to the
checkout, image, request payload or logs.

## 3. Historical deployment readback

The retired PoC's historical operator state is under
`~/.local/state/cubeplex-agentcore-poc/20260912/`. It contains resource identifiers
and deployment/readback evidence, not the plaintext Provider key. Its old cloud
targets have been removed; the readback command below is not a current health
check and must not be followed by recreating the retired resources.

```bash
uv run --project deploy/agentcore-poc \
  python deploy/agentcore-poc/manage.py readback
```

The command requires `runtime.json` and `runtime-create-request.json` from the
original deployment. A new machine must recover that authorized state first;
do not run `foundation` to recreate existing names. Readback compares the exact
artifact, role, network, lifecycle and environment configuration.

## 4. Historical first-deployment procedure

These operations create AWS resources. Verify that the checked-in target is the
intended account and that the names are unused. The helper validates the actual
STS caller, not just the profile name.

```bash
aws sts get-caller-identity --profile moego-testing --region us-west-2
```

The foundation command consumes an IAM Policy Autopilot result. Generate it
locally before creating resources, using the account and region from the target:

```bash
umask 077
POC_STATE="$HOME/.local/state/cubeplex-agentcore-poc/20260912"
mkdir -p "$POC_STATE"
chmod 700 "$POC_STATE"
POC_ACCOUNT=$(python3 -c 'import json; print(json.load(open("deploy/agentcore-poc/target.json"))["account"])')
POC_REGION=$(python3 -c 'import json; print(json.load(open("deploy/agentcore-poc/target.json"))["region"])')
uvx iam-policy-autopilot@0.3.0 generate-policies \
  "$PWD/backend/cubeplex/agentcore_poc/runtime.py" \
  --account "$POC_ACCOUNT" --region "$POC_REGION" --pretty \
  > "$POC_STATE/runtime-policy-autopilot.json"
uv run --project deploy/agentcore-poc \
  python deploy/agentcore-poc/manage.py foundation
```

This creates one Provider Secret, ECR repository and execution role. The role
can pull this image, write this Runtime's logs and read this exact Secret. The
command checks collisions before writing and records each operation. If a call
fails or its result is unknown, inspect the journal and AWS resources before
continuing; the command is not a blind retry mechanism.

After review and local checks, commit the build source and build locally:

```bash
POC_COMMIT=$(git rev-parse HEAD)
deploy/agentcore-poc/build.sh "$POC_COMMIT" --push
```

The script checks HEAD and clean build-source paths, builds `linux/arm64`, hashes
the source inputs, labels the image and pushes it to the target ECR repository.
It writes the immutable URI to `"$POC_STATE/immutable-image-uri.txt"`.

Create the single Runtime using that digest:

```bash
POC_IMAGE=$(cat "$POC_STATE/immutable-image-uri.txt")
uv run --project deploy/agentcore-poc \
  python deploy/agentcore-poc/manage.py runtime --image-uri "$POC_IMAGE"
uv run --project deploy/agentcore-poc \
  python deploy/agentcore-poc/manage.py readback
```

Creation is asynchronous. Continue bounded readback until `READY` or a named
failure; do not issue another create while it is in progress. This helper does
not update an existing Runtime. A later image deployment needs an explicit
update plan and version readback.

## 5. Start a bounded Slack controller

Choose an absolute private env path and a private ledger directory outside the
checkout. Use the Runtime ARN from deployment readback. Record the start time
before sending the test request:

```bash
POC_START_TS=$(python3 -c 'import time; print(f"{time.time():.6f}")')
PYTHONPATH=backend uv run --project deploy/agentcore-poc \
  python -m cubeplex.agentcore_poc.controller \
  --env-file /absolute/private/slack.env \
  --ledger /absolute/private/poc-state/ledger.sqlite \
  --runtime-arn '<deployed-runtime-arn>' \
  --start-ts "$POC_START_TS" \
  --max-runs 1 --duration 600
```

Wait for the `polling` status, then send a new root message from the allowed
user in the allowed channel, for example:

```text
cubeplex-poc: Read Cargo.toml lines 1–20 in Perfecto23/corplink-rs and report the package name and version with source line references.
```

The controller polls every four seconds, at concurrency one. It accepts only
the configured sender, channel, time window and prefix; new replies inside
existing threads are not discovered in this version. The bot replies to the
request's thread. Existing Events API URLs and relay deployments are untouched.

The SQLite ledger records execution and delivery separately. Reusing the same
ledger with `--once` can finish delivery of an already completed result without
invoking the model again. Unknown invocation outcomes are not replayed; unknown
send outcomes are read back rather than resent. Do not delete the ledger to
force a retry. Exit code `2` indicates an unresolved or unsuccessful outcome.

## 6. Stop and verify

The controller exits after its run/duration limit. Stop it explicitly with
Ctrl+C if the test is cancelled. The Runtime is configured for a 60-second idle
timeout and 900-second maximum compute lifetime; the registered Runtime can
remain `READY` after its individual compute sessions have ended.

For a known test session, request an explicit stop with the recorded session ID:

```bash
aws bedrock-agentcore stop-runtime-session \
  --profile moego-testing --region us-west-2 \
  --agent-runtime-arn '<deployed-runtime-arn>' \
  --runtime-session-id '<recorded-session-id>'
```

Keep source, ledger and resource metadata for review. ECR images, the Secret,
role and registered Runtime remain until a separately scoped cleanup. Do not
delete unrelated resources or remove test messages from Slack unless that
cleanup is requested.

## Verification and limitations

See [VERIFICATION.md](VERIFICATION.md) for the tested commits, real cloud/Slack
results and unresolved image findings. This slice deliberately validates
hosting and delivery before migration of the full CubePlex product. It does not
establish private GitHub authorization, persistent conversations, Web UI parity
or production readiness.
