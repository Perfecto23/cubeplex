---
sidebar_position: 5
title: AgentCore execution PoC
---

# AgentCore execution PoC

The `Perfecto23/cubeplex` fork adds an experimental AgentCore execution path on
top of upstream `cubeplexai/cubeplex@f5272e19`. It reuses the real CubePlex agent
factory and LLM builder with CubeLoop 0.14.1.

A bounded local Python controller reads one authorized Slack test channel,
invokes a custom ARM64 image hosted by AgentCore Runtime, and posts the answer
as a bot in the original thread. The agent reads GitHub files through a small
read-only API tool surface, fixing the source commit for each request.

The image is built locally with Docker and pushed to ECR. This PoC does not
deploy Kubernetes Pods or the full CubePlex web application. The local
controller and the AgentCore execution handler have separate responsibilities
and credentials.

## Run the fork

The maintained operator guide lives in the fork:

- [Configuration, local checks, deployment and Slack controller](https://github.com/Perfecto23/cubeplex/blob/feat/2026-09-12-agentcore-poc/deploy/agentcore-poc/README.md)
- [Verification evidence and remaining limits](https://github.com/Perfecto23/cubeplex/blob/feat/2026-09-12-agentcore-poc/deploy/agentcore-poc/VERIFICATION.md)

From a checkout containing this change, local checks do not need AWS or Slack
credentials:

```bash
uv sync --project deploy/agentcore-poc --frozen
PYTHONPATH=backend uv run --project deploy/agentcore-poc \
  python -m pytest -q deploy/agentcore-poc/tests
```

The deployment helpers bind one testing target and reject other accounts or
callers. Read the guide and target checks before creating resources. Existing
deployments use readback and their saved operator state; they must not repeat
the first-time foundation command.

## Verified scope

The real cloud and Slack path returned project and package information from
`Perfecto23/corplink-rs`, with independently checked commit/blob evidence.
Wrong-user and wrong-repository requests were denied, an extra command field
was rejected, and unsigned Runtime calls returned HTTP 403. Duplicate polling
did not create another execution record or bot reply.

The Runtime uses IAM inbound authentication, a scoped Provider Secret, a
60-second idle timeout and a 900-second maximum compute lifetime. Test
controllers exited and their known compute sessions were absent at the end of
acceptance. The registered Runtime and supporting resources remain available
for explicitly scoped follow-up work.

The repository used in acceptance is public. Full Web/RunManager integration,
native Slack event delivery, private-repository authorization, durable
conversations and browser migration remain outside this slice. The tested
image has unresolved system-package scan findings and is not production-ready.
