# AgentCore PoC implementation plan

Goal: Deliver the scoped Slack to CubePlex factory to AgentCore to GitHub to Slack execution path defined in the accompanying spec.

Architecture: A local bounded controller validates and records a Slack request, invokes a private-by-IAM AgentCore Runtime, and posts the verified result to the original thread. The runtime uses the real CubePlex agent factory with only commit-pinned GitHub read tools. Product credentials are separated between the local Slack controller and a scoped runtime provider Secret.

Tech stack: Python, CubeLoop 0.14.1, CubePlex agent factory/LLM builder, Bedrock AgentCore Python SDK, boto3, HTTP GitHub API, Slack Web API, SQLite controller ledger, uv and Docker.

The scoped implementation and real cloud/Slack acceptance are complete. Current commands and evidence are maintained in the [operator guide](../../../deploy/agentcore-poc/README.md) and [verification record](../../../deploy/agentcore-poc/VERIFICATION.md).

## 1. Execution contract and agent

Responsibility: execution request validation, model/tool loop and source evidence.

Files: `backend/cubeplex/agentcore_poc/{__init__,contracts,github,agent,runtime}.py` and focused runtime tests under `deploy/agentcore-poc/tests/`.

Interfaces: strict `InvocationRequest`, explicit `InvocationResponse`, `RuntimeScope.from_env()` and shared `derive_runtime_session_id(request)`. Provider Secret fields are `base_url`, `api_key`, `model`, `effort`; model transport is OpenAI Responses-compatible.

Logic: validate scope and SDK session; resolve source commit; expose bounded read-only tools; build the real CubePlex agent; enforce deadline/tool budget/provider terminal integrity; return source evidence without exposing credentials.

Verification: invalid scope/URL/path rejection, commit pinning, non-completed model output, missing evidence, timeout and output redaction.

## 2. Slack controller

Responsibility: Slack ingress, local execution ledger and result delivery.

Files: `backend/cubeplex/agentcore_poc/{controller,slack}.py` and focused controller tests under `deploy/agentcore-poc/tests/`.

Interfaces: import the shared request/response/session contract; local CLI takes the exact Runtime ARN, authorized Slack env path, ledger path, time window and finite run/duration limits. The AWS profile, region and account are fixed by the PoC target checks.

Logic: poll only the allowlisted channel, accept the authorized sender/prefix/time window, exclude bot output, claim the event once, invoke with SigV4, and send the result to the original thread. Persist unknown outcomes and require readback rather than blind retries.

Verification: duplicate events, wrong actor/channel/time/prefix, exact thread reply, interrupted invoke and unknown post response.

## 3. Build and deployment

Responsibility: artifact construction, scoped AWS resources and independent deployment readback.

Files: `deploy/agentcore-poc/` with an independent uv project/lock, Dockerfile and narrow deploy/readback utilities. Machine state and credentials stay outside Git.

Logic: build a minimal container from the relevant CubePlex source using locked dependencies; transfer provider configuration via Secrets Manager; create only the owned small resource set; publish an immutable image; deploy one microVM Runtime with IAM authentication and short lifecycle settings.

Verification: clean dependency import, local container entrypoint, image digest/provenance, resource ownership and permissions, runtime READY and configuration readback.

## 4. Integrated acceptance

Responsibility: independent source, cloud and Slack business verification.

Read the test repository independently to establish expected source facts. Run direct positive and negative invocations, then send a clearly marked test request as the authorized user to the allowlisted Slack channel. Run the finite controller and verify the bot reply, source SHA and file evidence by independent API reads. Report hosting success separately from native IM/full-product migration and private-repository access.

Stop controller processes after acceptance; use the configured runtime idle policy and verify compute session behavior. Retain source, resource metadata and evidence for follow-up. Do not delete shared resources or use any existing production-write authorization.
