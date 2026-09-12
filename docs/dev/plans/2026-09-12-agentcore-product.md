# Full AgentCore product integration

Baseline PR #1 merged as `8fb3b5d6`; development is isolated on
`feat/2026-09-12-agentcore-product`.

1. Verify Testing infrastructure, official prices, compatible images and fix the
   existing runtime base image findings. Record an owned-resource manifest.
2. Provision an economical Kubernetes node and persistent volumes; deploy the
   upstream web/backend/storage/OpenSandbox stack from the merged baseline.
3. Implement durable remote dispatch and worker bootstrap, integrating both
   RunManager prompt and HITL paths. Preserve native progress/checkpoint contracts.
4. Configure native Slack with a verified test app identity and event transport.
   Preserve unrelated Multica relay configuration.
5. Validate positive, stop, followup, HITL, duplicate, restart, reclaimed-session
   and delivery-uncertainty paths using real product interfaces.
6. Publish deployment/operator docs, final cost/resource inventory and acceptance
   evidence; commit/push normal feature workflow without bypassing hooks.

Root owns architecture, integration and acceptance. Independent executor packages
own image baseline and Slack configuration discovery. Code ownership will be
assigned before parallel implementation; shared infrastructure changes stay serial.
