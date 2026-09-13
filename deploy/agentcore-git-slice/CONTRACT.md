# AgentCore MicroVM Git execution slice contract

See docs/dev/specs/2026-09-13-agentcore-git-slice-design.md for the authority/behavior contract.

Broker Lambda request envelope (every operation):
{"version":1,"task_id":"git-slice-20260913","stage":"work","capability":"SHORT_TASK_CAPABILITY","request_id":"UUID","op":"manifest","data":{}}

Response: {"ok":true,"result":{...}} or {"ok":false,"error":{"code":"..."}}.
model result: {"status":200,"headers":{"content-type":"text/event-stream"},"body":"complete SSE text"}.
manifest result: repo, remote_url, base_sha, branch, model, allowed_paths, artifact_paths, active_stage, deadline, canary_secret_arn, canary_sha256, max_model_calls, max_model_output_tokens, max_model_request_bytes, max_bundle_bytes, max_snapshot_bytes, worker_role_arn, forbidden_value_sha256. The capability hash is never returned.
checkpoint schema: {"schema_version":1,"stage":"work","boot_id":"...","head_sha":"40hex","base_sha":"40hex","branch":"agentcore/fix-inclusive-total","git_bundle_b64":"...","patch_b64":"...","untracked":{"continuation.md":"base64"},"messages":[...],"metrics":{...},"result":{...}}.
status result: {"commit":...,"pr":{number,url},"model_calls":...,"snapshot_sha256":...,"completed_stages":{...}}.

Recovery modes:
- A completed stage is terminal. A repeated `work` or `resume` request returns the saved result
  and does not call the Agent again.
- If `stage=work` is incomplete but `status.commit` is a valid broker-confirmed published SHA,
  the Worker enters `published_commit_only`. The Agent still calls `clone_repository`; the
  Worker checks out that broker-confirmed SHA, never accepts a SHA from tool input, and records
  `recovery_mode=published_commit_only` in the result, metrics and checkpoint. The continuation
  may verify HEAD/tests, confirm or complete the same PR, and write the README handoff patch and
  `continuation.md`; it must not modify `intervals.py`, create a commit or push.
- This path is not a reconstruction of a missing first session. Its CubeLoop native history starts
  with the continuation Agent. A checkpoint is written only after the unchanged published HEAD,
  PR, handoff files and terminal result pass their existing postconditions.
- Ordinary `stage=resume` still requires the saved snapshot and restores its typed native messages.
  A zero-model snapshot restore performed by an operator proves bundle/message/artifact integrity;
  it is not evidence that a Web/Slack entrypoint completed a follow-up. The 2026-09-13
  acceptance additionally ran a real `stage=resume`: a fresh workspace restored the saved
  27-message prefix, added 10 messages with 4 model calls, and preserved the same HEAD, PR and
  saved files. The original failed session's lost native history was not reconstructed; the
  saved continuation snapshot was the recovery source.

Worker environment: BROKER_FUNCTION_ARN, AWS_REGION. No platform config Secret.
Runtime input: {"version":1,"task_id":"git-slice-20260913","stage":"work"|"resume","capability":"...","mode":"probe"|"run"}.
Runtime output must never echo capability/credentials. boot_id derives from hostname and Linux kernel boot ID and is diagnostic only: two real sessions produced the same value. Use the AWS session ID, prior termination, workspace state and restored snapshot as recovery evidence; do not require boot_id uniqueness.

Git helper: executable git-remote-broker calls module cubeplex_git_slice.git_remote. After public clone, set remote.origin.pushurl to broker::<task_id>. Worker task client configuration file is provided via CUBEPLEX_GIT_TASK_CONFIG (contains only broker ARN, task/stage capability, region and fixed repo/base/branch). The helper exports its new commit in a bundle and invokes op=push; the broker relays this identical commit. It must speak the standard Git remote-helper push/status protocol.

The fresh first Agent run fixes/tests/commits/pushes/creates PR and leaves README.md modified plus
untracked continuation.md for handoff. The host checkpoints after a completed model run. If that
checkpoint is unavailable after the commit has already been published, the bounded
`published_commit_only` continuation verifies the same commit and creates the handoff without
repeating the fix or making a second commit. Otherwise, the second fresh VM restores the saved
state, verifies files/tests/HEAD, reconfirms the same push/PR and reports completion without
redoing the fix.

Broker env: TASK_BUCKET, TASK_ID, MODEL_SECRET_ARN, GITHUB_SECRET_ARN. Operator manifest key tasks/{TASK_ID}/manifest.json. Model secret JSON={api_key,base_url,model}; GitHub secret JSON={token}; canary is a separate harmless Secret.
Manifest fields: schema_version=1,task_id,repo,remote_url,base_sha,branch,model,allowed_paths=[intervals.py],artifact_paths=[README.md,continuation.md],active_stage,capability_sha256,deadline,max_model_calls=100,max_model_request_bytes=65536,max_model_output_tokens=2048,max_bundle_bytes=2097152,max_snapshot_bytes=4194304,max_changed_bytes=65536,canary_secret_arn,canary_sha256,worker_role_arn,forbidden_value_sha256. UTC deadline is an ISO timestamp. capability_sha256 rotates between stages; manifest is operator-only. Set max_model_calls=0 for the first denial probe, then raise the limit only through an explicit operator/user authorization; the recorded acceptance raised it from 20 to 100 while preserving the existing model_calls counter. forbidden_value_sha256 contains only operator-computed hashes of protected credentials, never the values.

The operator must initially create tasks/{TASK_ID}/state/state.json as {"model_calls":0,"completed_stages":{}} with an If-None-Match condition. Do not reinitialize existing task state or reset its budget counter. The broker has no ListBucket permission; a missing state object can therefore return AccessDenied rather than NoSuchKey.
Broker role can read manifest but cannot update it; can read/write task state, request records and immutable snapshots under separate keys. Worker role has no S3 permissions.

PR errors distinguish definite HTTP rejection from unknown outcomes. `github_http_<status>` may include a validated GitHub request ID in internal state, never the response body or credential-bearing headers. A definite rejected attempt permits a fresh request for the same repo/head/base/commit intent; the same request ID keeps its cached response. Transport errors, HTTP 408/5xx and legacy pending records remain fenced for readback. A 422 is treated as an existing PR only after exact repo/head/base/commit readback. An audited operator exception does not establish a lifetime one-POST or exactly-once guarantee.

Current source/image identities, manual recovery evidence, measurements and remaining acceptance gaps are in [README.md](README.md).
