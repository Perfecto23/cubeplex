# CubePlex AgentCore execution PoC

## Goal

Prove that a real Slack request can execute through CubePlex's existing agent factory on Amazon Bedrock AgentCore and return a source-grounded answer to the same Slack thread.

## Context and chosen scope

CubePlex currently starts CubeLoop runs in its Backend process. This PoC adds an independently runnable execution module to the CubePlex fork. It reuses `cubeplex.agents.graph.create_cubeplex_agent` and the existing LLM builder, while avoiding the full application's database, onboarding and OpenSandbox dependencies for the first hosting experiment.

The ingress is a bounded local controller that polls one authorized Slack test channel. This preserves the existing Slack app's production Events API configuration. A real bot posts the answer; the existing authorized user can send the test request. This validates the cloud execution path, not the native CubePlex IM adapter or the full web application migration.

The supplied `Perfecto23/corplink-rs` repository is public as verified on 2026-09-12. Reading it validates real source retrieval but does not validate private-repository credentials.

## Approaches considered

1. Move the full Backend and its database, global workers and IM connections to AgentCore. This adds unrelated lifecycle dependencies and does not isolate the hosting question.
2. Add a small PoC controller and AgentCore entrypoint that reuse the real CubePlex factory and model builder. Chosen: it supports a locally built artifact, real model and source calls, and a bounded Slack acceptance test.
3. Build an unrelated standalone agent. Rejected because it would not establish compatibility with CubePlex's actual factory and dependencies.

## Request and response boundary

The controller produces a strict versioned request with `schema_version`, `run_id`, `team_id`, `channel_id`, `thread_ts`, `user_id`, `prompt`, and `repository`. Unknown fields are rejected. The runtime independently checks configured team, channel, user and repository allowlists. Runtime session identity is derived from the trusted team/channel/thread/user scope and checked against the AgentCore SDK context.

The first scope is one workspace, one channel, one user and `Perfecto23/corplink-rs`. The source tool resolves the repository's current default-branch commit at run start, then reads files at that commit. Tools do not accept arbitrary repositories, URLs, refs or shell commands. Requests are limited to 240 seconds, eight tool calls and a maximum output of 4096 tokens.

Responses carry an explicit status, run/session identity, answer, repository commit and file evidence. Only a completed model turn with usable evidence can be reported as success. Transport errors, incomplete provider responses, rejection, cancellation and timeouts are separate outcomes; raw credential-bearing exceptions are never returned.

## Credentials and infrastructure

- AWS target: profile `moego-testing`, account `986420599013`, region `us-west-2`.
- Small owned resource set: one ECR repository, execution IAM role, provider Secret and AgentCore Runtime, plus its log group. No EC2 instance, Kubernetes workload, Browser, Code Interpreter, Gateway or database is required for this PoC.
- Runtime uses IAM inbound authentication, public outbound networking, idle timeout 60 seconds and maximum compute lifetime 900 seconds.
- Provider configuration is read from the user-provided local file by the deployment process, stored through Secrets Manager, and read by the runtime using an exact Secret ARN. Only the ARN is in runtime environment configuration.
- Slack tokens remain on the local controller. They are not sent to AgentCore, stored in the image, or included in the request.
- No GitHub credential is needed for this public repository test. Private access is a separate future acceptance case.

## Slack handling

The controller requires the configured channel, sender, creation time window and `cubeplex-poc:` prefix. It ignores the output bot's messages, runs at concurrency one, and persists processing state in a local SQLite ledger outside the repository. Duplicate polling results cannot produce duplicate execution or replies.

A reply is sent to the original thread and visibly identifies the PoC. A send with an unknown outcome is not automatically retried; subsequent readback must determine what happened. The controller has a duration/run limit and is not installed as a permanent background service.

## Acceptance

- Focused tests reject invalid scope, unknown fields, arbitrary source targets and incomplete provider turns.
- Duplicate Slack events and unknown send results cannot cause duplicate side effects.
- The locally built image imports the real CubePlex factory and runs its entrypoint.
- The deployed ECR digest, runtime version, execution role, lifecycle and Secret reference are independently read back.
- A direct invocation answers a real repository question with the current commit and verifiable file lines.
- A real authorized Slack message produces a bot answer in the same thread, with request and reply timestamps recorded.
- Wrong user/channel/repository inputs fail before model or source calls.
- The PoC leaves existing AWS runtimes, Slack Webhook/Relay configuration and unrelated local changes intact.

## Delivery limits

This slice does not migrate CubePlex's full RunManager, web UI, OAuth catalog, database, streaming event persistence, browser takeover or shared workspace storage. It does not claim private GitHub authorization or full production readiness. These remain follow-up work informed by the measured hosting result.
