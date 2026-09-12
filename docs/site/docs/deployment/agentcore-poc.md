---
sidebar_position: 5
title: AgentCore execution PoC
---

# AgentCore execution PoC

This experimental module runs the existing CubePlex agent factory and LLM builder inside an Amazon Bedrock AgentCore Runtime. A bounded local Slack controller invokes it and posts source-grounded results to the original thread.

This is a hosting proof of concept. It does not migrate the full CubePlex Backend, web UI, database, native IM delivery or durable conversation history. The test repository is public, so this experiment does not establish private GitHub authorization.

## Components

- `backend/cubeplex/agentcore_poc/`: strict invocation contract, scope checks, commit-pinned repository reader, real CubePlex agent construction, AgentCore entrypoint and local Slack controller.
- `deploy/agentcore-poc/`: independently locked dependencies, ARM64 Docker build and narrow testing-account deployment utilities.
- `deploy/agentcore-poc/tests/`: focused contract and controller tests with external APIs replaced at their boundaries.

The runtime has only two repository tools: list files and read a bounded UTF-8 excerpt. It cannot run shell commands, select another repository or write to GitHub. Each run pins the current default-branch commit and checks returned Git blob hashes.

## Local checks

From the worktree root:

```bash
uv sync --project deploy/agentcore-poc --frozen
PYTHONPATH=backend deploy/agentcore-poc/.venv/bin/python -m pytest -q deploy/agentcore-poc/tests
deploy/agentcore-poc/.venv/bin/ruff check backend/cubeplex/agentcore_poc deploy/agentcore-poc
```

The isolated environment exists because this slice does not need the main application's database or sandbox services. The real CubePlex source is imported through `PYTHONPATH`; it is not replaced with a mock agent.

## Deployment boundary

The checked-in target describes a single authorized testing environment. The management script verifies the AWS account and caller before changing resources. It creates only a scoped provider Secret, ECR repository, execution role and AgentCore Runtime. Existing-name collisions stop creation and require readback rather than destructive replacement.

Provider values are read from a private operator file and transferred directly to Secrets Manager. Slack credentials stay in the controller's private local environment file. Neither credential set is placed in Git, image layers or invocation payloads. The runtime receives only the provider Secret ARN.

Before deployment, run focused checks, review the source diff and create a local source commit. The build script requires the expected commit and clean build-source paths:

```bash
deploy/agentcore-poc/build.sh <expected-full-commit> --push
deploy/agentcore-poc/.venv/bin/python deploy/agentcore-poc/manage.py runtime \
  --image-uri <poc-ecr-repository@sha256:digest>
deploy/agentcore-poc/.venv/bin/python deploy/agentcore-poc/manage.py readback
```

The first foundation creation is a separate operation. Do not repeat it after a partial or successful attempt. Inspect the protected operations journal and live resources first.

## Slack acceptance

The controller polls only the configured test channel, accepts only the configured user, ignores messages before its start timestamp, and requires the `cubeplex-poc:` prefix. It replies as the configured bot without modifying the Slack app's Events API URL or other relay deployment.

```bash
PYTHONPATH=backend deploy/agentcore-poc/.venv/bin/python \
  -m cubeplex.agentcore_poc.controller \
  --env-file <private-slack-env-file> \
  --ledger <private-directory-outside-worktree>/ledger.sqlite \
  --runtime-arn <deployed-runtime-arn> \
  --start-ts <seconds.six-digit-fraction> \
  --max-runs 1 --duration 600
```

Send an authorized test request as a channel root message. Discovery of new replies in existing threads is outside this slice. The controller's ledger prevents duplicate polling events from causing another invocation or reply. Unknown invocation outcomes are retained; unknown send outcomes are read back rather than resent.

Verify the actual request, runtime result and bot reply independently. A successful reply must contain a nonempty answer, source commit and file evidence. Wrong actor/channel/repository and mismatched runtime-session inputs must be rejected. Runtime `READY`, `/ping`, or a mocked model response alone does not establish business acceptance.

## Resource lifecycle

The testing Runtime uses a short idle timeout and maximum lifetime. Stop finite local controllers after testing and verify session shutdown through the platform. ECR images, the provider Secret and role remain until an explicitly scoped cleanup; do not delete unrelated AgentCore resources.
