# AgentCore MicroVM Git execution slice

Goal: a CubeLoop Agent in a new PUBLIC AgentCore Runtime clones one public fixture repository, fixes a failing test, commits and initiates a brokered push/PR, then resumes the same work in a fresh MicroVM without replaying the modification or duplicating publication.

Baseline: PR #2 merge b559c1ac7ac7ab00e24cc6e03e06841416a7e790. The existing K8s product and VPC Runtime v3 (business source 11a4a524) remain unchanged as rollback. This is execution-layer acceptance, not a Web/Slack migration.

## Selected boundary

A standalone CubeLoop 0.14.1 host contains no CubePlex database/config/Vault bootstrap. Shell, Git and workspace files are local to the MicroVM. A new Lambda broker outside VPC holds a repo-scoped, one-day GitHub writer credential and the model credential. The MicroVM has only permission to invoke that broker, plus its own image/log permissions; explicit denies cover secret retrieval, parameter retrieval, KMS decrypt and role assumption. The tool environment can see its temporary task IAM/capability credentials; these must never be described as non-exportable or as platform credentials.

The broker binds one repository, base SHA, branch, model, permitted operations, request/output sizes, model-call budget and expiry on the server. Runtime invocation supplies a task/stage capability; its hash and active stage are operator-owned in an S3 manifest. Workers cannot read/write S3 or change the manifest directly. The capability rotates before the recovery stage. Broker mutable state uses conditional S3 writes; no database migration, Supabase project, shared code cache or index is needed.

Alternatives: a GitHub token inside the tool VM adds a credential-exfiltration surface and needs separate branch controls; reusing the old Worker Secret exposes database/Vault/model master credentials and is rejected. A broker provides the required constraints without replacing the Harness or building another platform.

## Git and state

The fixture repo is Perfecto23/cubeplex-microvm-git-poc-20260913. Only intervals.py may change in the published commit; the fixed branch is agentcore/fix-inclusive-total. The Agent calls clone_repository, reads/runs shell/tests, modifies code and uses normal git commit/git push. A git remote helper relays an Agent-created bundle to the broker. The broker validates base, commit ancestry, changed paths and size, then forwards the same commit SHA to GitHub. It never runs repository code, hooks or tests with trusted credentials. Tests execute in the MicroVM.

Publication is idempotent by repository/branch/commit. Existing identical remote heads and PRs are read back, not rewritten or recreated. Main, another repository, a second commit, unexpected paths and a force update are denied. Unknown publication outcomes require remote readback, never blind retries.

After the first successful run, the MicroVM saves a Git bundle, native CubeLoop messages, HEAD/branch/base, the uncommitted README patch and an untracked continuation note. The operator confirms Runtime session teardown, rotates the stage capability and invokes a new session. The new VM restores that state, runs verification, and reconfirms push/PR idempotently; it must retain the original commit and PR. A repeated completed invocation returns its saved result without another Agent run.

Clone metrics separately report fresh clone, same-environment reuse and restoration. Git HTTP trace receive sizes are preferred; interface byte deltas are labelled estimates if used. This small fixture does not establish a need for a shared cache.

## Broker protocol v1

All requests: version=1, task_id, stage (work/resume), capability, request_id, op, data. Reject unknown envelope fields. The broker validates IAM admission, capability hash, stage and expiry before downstream work. Errors return a safe code, not raw credential/provider error text.

Operations:
- manifest: data={}; returns only public task constraints/state summary.
- model: data={body}; fixed OpenAI Responses model, bounded request size/calls/max_output_tokens, stream=true/store=false. Buffer bounded SSE and require response.completed with status=completed; incomplete/truncated/failed streams are errors. Credentials/headers are not returned.
- push: data={repo,branch,base_sha,commit,bundle_b64}; exact server repo/branch/base, one descendant commit, allowed file/size constraints. Returns commit and pushed/already_pushed.
- pr: data={repo,branch,commit,title,body}; validates fixed repo/ref/current pushed commit; returns number/url and created/already_exists.
- checkpoint_put: data={snapshot}; bounded JSON with git bundle, head/base/branch, patch, untracked files, messages, metrics, boot_id and stage result. Reject path traversal and .git contents in untracked files. Does not change authorization or budgets.
- checkpoint_get: data={}; returns the saved stage checkpoint.
- status: data={}; returns publication/state results without secrets.

Manifest and mutable state are separate objects. Operator alone writes manifest. Budget claims are persisted before provider calls. Request IDs bind payload hashes; same ID with different data is rejected. Side-effect-bearing errors never trigger implicit retries.

## Acceptance

1. Public clone and shell run inside the new Runtime; no laptop executes the fix or publication on the Agent's behalf.
2. Fake-secret/availability probes verify no DB/Vault/Supabase/GitHub/model master credentials in environment, /proc, files, helpers or logs. Real IAM denies a harmless canary Secret retrieval. Broker rejects wrong repo/ref/action, stale capability and exceeded budget.
3. Initial tests fail, Agent changes intervals.py, tests pass, a normal commit is created in the VM and the Agent initiates push and PR.
4. A new boot/session restores commit, native history and uncommitted artifacts. No old modification, duplicate push or duplicate PR.
5. Record source/image/runtime, Git SHA/PR, tool events and clone measurements. Preserve the old product/Runtime/OpenSandbox.

The new Runtime and broker use managed public egress, subject to live validation. The old EC2 cannot be stopped while its product, storage and VPC Runtime remain needed; a stop proposal must include that outage, retained EBS/EIP/Secret/ECR costs and restart/SSM recovery. No stop/delete is authorized now.
