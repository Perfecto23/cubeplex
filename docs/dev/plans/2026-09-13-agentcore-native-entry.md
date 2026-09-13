# Native entry integration plan

1. Keep the Git slice in its own PR and retain its evidence and cumulative usage ledger. Prepare this separate worktree from its verified head.
2. Freeze the task API, typed event/history/approval and workspace artifact contracts against existing Backend services.
3. Assign disjoint control-plane, MicroVM host and RunManager/test ownership. Run meaningful local tests at the changed boundaries, without invoking a real model during preparation.
4. Integrate and review task-scope authorization, stable event deduplication, terminal CAS, stop and approval behavior. Never transfer platform master credentials into the tool VM.
5. Once dependencies are satisfied, build affected committed-source images together; read/execute the narrow Testing change set and preserve the old product rollback path.
6. Complete new Web and Slack user-visible tests on the native MicroVM route, plus stop/approval/reclaim/replay checks, then update current docs and report links, exact runtime identity and remaining limits.
