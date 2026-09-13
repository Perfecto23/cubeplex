# AgentCore Git slice implementation

1. Freeze b559c1ac in an isolated worktree; seed one public fixture repo and record its base SHA. Obtain a repo-scoped temporary GitHub writer for the trusted broker.
2. Implement strict broker authorization/state, buffered Responses forwarding and Git bundle relay/PR idempotency. Validate denial and terminal-response contracts locally.
3. Implement the independent CubeLoop host, Lambda transport, shell process lifecycle, Git remote helper and checkpoint/clone metrics. Preserve typed native messages across a new host instance.
4. Build committed-source Worker/Broker ARM64 images once the contracts pass. Create only task-owned PUBLIC Runtime, Lambda, S3, ECR, scoped roles and broker/canary secrets. Verify no dependence on the existing EC2 route.
5. Run the credential boundary probe first, then one Git fix/publication/recovery cycle. Independently read GitHub and AWS outcomes; report any new out-of-scope blocker rather than expanding the platform.
6. Submit the implementation/docs together through normal hooks. Keep live business acceptance distinct from CI, and the independent execution slice distinct from CubePlex Web/Slack integration.
