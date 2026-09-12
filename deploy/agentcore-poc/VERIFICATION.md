# PoC verification — 2026-09-12

The tested path was a real Slack request, a bounded local controller, the actual
CubePlex factory and LLM builder on AgentCore, a real Responses-compatible model,
read-only GitHub retrieval and a bot reply in the original thread.

## Evidence by layer

| Layer | Observed result |
|---|---|
| Focused tests | 58 tests passed, including scope, source integrity, model completion, duplicate processing and unknown delivery recovery |
| Source checks | Configured pre-commit checks passed; no supplied credential values found in the new source files |
| PR backend checks | Full backend ruff, format and mypy passed; 2,770 tests passed, 7 skipped and 14 deselected |
| PR documentation checks | English/Chinese docs builds, TypeScript, 408 generated URL checks and worker checks passed |
| PR entrypoint smoke | Updated registration served `/ping` and returned `invalid_request` for an empty invocation, without model calls |
| Real local execution | A small Cargo question and the original multi-file question completed with actual file evidence |
| Container | Locally built Linux ARM64 image; UID 10001; 542 source files matched the build manifest with zero hash differences |
| Cloud control plane | One AgentCore Runtime reached READY with the expected immutable image, role, HTTP/IAM authentication, public outbound network and lifecycle |
| Cloud business call | Completed in 32.15 seconds; returned package information and source evidence independently checked against GitHub blobs |
| Cloud denial paths | Wrong user/repository denied; extra command field rejected; unsigned request returned HTTP 403 |
| Slack delivery | One real request produced one bot reply with project purpose, package name/version and fixed-commit source links |
| Duplicate processing | Same ledger preserved one event and one bot reply; completed execution was not repeated during delivery recovery |
| End of test | Local controller exited; both known test sessions were absent when stop was requested; the registered Runtime remained READY |

The full Slack test included an integration fix and is not a response-time
benchmark. The 32.15-second measurement applies only to the direct cloud call.

## Source and artifact identity

- Upstream baseline: `cubeplexai/cubeplex@f5272e1901d0c0c785bdc2381da94951726da601`.
- Deployed execution source: `577398d31d70c2c7a61b073c53cb12a27576a88d`.
- Accepted local controller source: `c6ada35cfb97ebeeb8f2df045300b24b15a468c4`.
- Image digest: `sha256:4fc05983e200094418cd237d1f5fc815d19b3b3cde69a298b7c633d6febf5f2a`.
- Image source-tree digest: `0ada0937aed2ec736a2dd89f258e9afe617b8e12c349bfa9b98c29ae2b3ffbab`.
- Source repository read during acceptance: `Perfecto23/corplink-rs@4468dc2027e934c94ca62a84199ae6d43cb86923`.

The second application commit changes only the local Slack read transport and
its tests. The cloud entrypoint does not import that transport, so the accepted
Runtime image remains based on the first commit. Later PR changes include documentation, CI and type-compatible model aliases /
entrypoint registration. They passed local checks but do not mean that the
accepted image was rebuilt or redeployed.

## Important constraints

- The test repository is public. This result does not establish private GitHub
  credentials or per-employee repository authorization.
- The controller polls new root messages in one bound test channel. Native
  Events API/Socket Mode ingress, full RunManager/Web integration and durable
  multi-turn conversations remain outside this slice.
- No Kubernetes Pod, EC2 instance, OpenSandbox, Browser or database was deployed
  for this PoC. Existing runtimes and the existing Slack relay were preserved.
- ECR basic scanning completed with **6 Critical, 11 High, 5 Medium and 4 Low**
  system-package findings, including Perl and glibc. Exploitability was not
  assessed, and this image is not marked production-ready.

Private operator evidence includes the source manifest, resource readbacks,
model/tool diagnostics, signed and unsigned invocation results, Slack ledger,
reply readback and image scan details. These files and credential values are
not published in this repository. The two temporary Slack test messages were removed as requested; their absence
was read back, and historical acceptance evidence is retained locally.
