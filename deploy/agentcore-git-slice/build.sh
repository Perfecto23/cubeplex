#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy/agentcore-git-slice/build.sh <committed-sha> [--push]

Build the independent AgentCore Git slice Worker and Lambda broker images for
linux/arm64 from a clean git archive. The worktree is never a Docker build
context. --push is restricted to the Testing account, region and ECR prefix.

Environment:
  CUBEPLEX_GIT_SLICE_STATE_DIR  private evidence root
  AWS_PROFILE                   AWS CLI profile (default: moego-testing)
EOF
}

source_sha_input=""
publish=false
while (($# > 0)); do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --push)
      publish=true
      ;;
    --)
      shift
      break
      ;;
    -* )
      printf 'ERROR: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "$source_sha_input" ]]; then
        printf 'ERROR: exactly one commit SHA is required\n' >&2
        usage >&2
        exit 2
      fi
      source_sha_input="$1"
      ;;
  esac
  shift
done
if (($# > 0)); then
  printf 'ERROR: unexpected argument: %s\n' "$1" >&2
  usage >&2
  exit 2
fi
if [[ -z "$source_sha_input" ]]; then
  printf 'ERROR: a committed Git SHA is required\n' >&2
  usage >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
resolved_sha="$(git -C "$repo_root" rev-parse --verify "${source_sha_input}^{commit}")"
short_sha="$(printf '%s' "$resolved_sha" | cut -c1-12)"

aws_profile="${AWS_PROFILE:-moego-testing}"
aws_region="us-west-2"
account_id="986420599013"
registry="${account_id}.dkr.ecr.${aws_region}.amazonaws.com"
repository_prefix="cubeplex-git-slice-20260913"
if [[ "$publish" == true ]]; then
  requested_region="${AWS_REGION:-$aws_region}"
  if [[ "$requested_region" != "$aws_region" ]]; then
    printf 'ERROR: --push is restricted to %s\n' "$aws_region" >&2
    exit 1
  fi
fi

user_home="${HOME:-/tmp}"
state_root="${CUBEPLEX_GIT_SLICE_STATE_DIR:-${user_home}/.local/state/cubeplex-agentcore-git-slice/20260913/build}"
state_dir="${state_root}/${resolved_sha}"
archive_dir="${state_dir}/archive"
mkdir -p "$state_dir"
chmod 700 "$state_dir"
if [[ -e "$archive_dir" ]]; then
  printf 'ERROR: evidence archive already exists: %s\n' "$archive_dir" >&2
  exit 1
fi
mkdir -m 700 "$archive_dir"
cleanup() {
  rm -rf "$archive_dir"
}
trap cleanup EXIT
umask 077

git -C "$repo_root" archive --format=tar "$resolved_sha" | tar -xf - -C "$archive_dir"
slice_dir="$archive_dir/deploy/agentcore-git-slice"
if [[ ! -f "$slice_dir/Dockerfile" || ! -f "$slice_dir/pyproject.toml" || ! -f "$slice_dir/uv.lock" ]]; then
  printf 'ERROR: committed SHA is missing the git-slice build inputs\n' >&2
  exit 1
fi

uv_bin="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$uv_bin" ]]; then
  printf 'ERROR: uv is required to export frozen dependencies\n' >&2
  exit 1
fi
(
  cd "$slice_dir"
  "$uv_bin" export \
    --frozen \
    --format requirements-txt \
    --no-hashes \
    --no-dev \
    --no-editable \
    --no-emit-project \
    > requirements-frozen.txt
)

source_manifest="$state_dir/source-manifest.json"
python3 - "$slice_dir" "$resolved_sha" "$source_manifest" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
commit = sys.argv[2]
destination = Path(sys.argv[3])
files: dict[str, str] = {}
for path in sorted(root.rglob("*")):
    if not path.is_file():
        continue
    if any(part in {"__pycache__", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"} for part in path.parts):
        continue
    if path.name == "requirements-frozen.txt":
        continue
    files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
tree_sha256 = hashlib.sha256(
    json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
destination.write_text(
    json.dumps(
        {
            "source_commit": commit,
            "source_tree_sha256": tree_sha256,
            "files": files,
        },
        indent=2,
    )
    + "\n"
)
print(tree_sha256)
PY

source_tree_sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_tree_sha256"])' "$source_manifest")"
requirements_sha256="$(shasum -a 256 "$slice_dir/requirements-frozen.txt" | awk '{print $1}')"
image_tag="baseline-${short_sha}-${source_tree_sha256:0:12}"
worker_image="${registry}/${repository_prefix}/worker:${image_tag}"
broker_image="${registry}/${repository_prefix}/broker:${image_tag}"

printf '%s\n' "$resolved_sha" > "$state_dir/source-commit.txt"
printf '%s\n' "$source_tree_sha256" > "$state_dir/source-tree-sha256.txt"
printf '%s\n' "$requirements_sha256" > "$state_dir/requirements-sha256.txt"
printf '%s\n' "$image_tag" > "$state_dir/image-tag.txt"
printf '%s\n' "$worker_image" > "$state_dir/worker-image.txt"
printf '%s\n' "$broker_image" > "$state_dir/broker-image.txt"

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --load \
  --target worker \
  --file "$slice_dir/Dockerfile" \
  --tag "$worker_image" \
  "$archive_dir" \
  > "$state_dir/worker-build.log" 2>&1
docker image inspect "$worker_image" > "$state_dir/worker-local-image.json"

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --load \
  --target broker \
  --file "$slice_dir/Dockerfile" \
  --tag "$broker_image" \
  "$archive_dir" \
  > "$state_dir/broker-build.log" 2>&1
docker image inspect "$broker_image" > "$state_dir/broker-local-image.json"
if [[ "$publish" == true ]]; then
  caller_account="$(aws --profile "$aws_profile" --region "$aws_region" sts get-caller-identity --query Account --output text)"
  if [[ "$caller_account" != "$account_id" ]]; then
    printf 'ERROR: refusing unexpected AWS account %s\n' "$caller_account" >&2
    exit 1
  fi
  aws --profile "$aws_profile" --region "$aws_region" ecr describe-repositories \
    --repository-names "${repository_prefix}/worker" "${repository_prefix}/broker" \
    > "$state_dir/ecr-repositories.json"
  aws --profile "$aws_profile" --region "$aws_region" ecr get-login-password \
    | docker login --username AWS --password-stdin "$registry" \
    > "$state_dir/ecr-login.log" 2>&1
  docker push "$worker_image" > "$state_dir/worker-push.log" 2>&1
  docker push "$broker_image" > "$state_dir/broker-push.log" 2>&1
  aws --profile "$aws_profile" --region "$aws_region" ecr describe-images \
    --repository-name "${repository_prefix}/worker" \
    --image-ids "imageTag=${image_tag}" \
    > "$state_dir/worker-ecr-image.json"
  aws --profile "$aws_profile" --region "$aws_region" ecr describe-images \
    --repository-name "${repository_prefix}/broker" \
    --image-ids "imageTag=${image_tag}" \
    > "$state_dir/broker-ecr-image.json"
  python3 - "$state_dir" "$registry" "$repository_prefix" "$image_tag" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

state = Path(sys.argv[1])
registry = sys.argv[2]
prefix = sys.argv[3]
tag = sys.argv[4]
result = {}
for name in ("worker", "broker"):
    data = json.loads((state / f"{name}-ecr-image.json").read_text())
    detail = data["imageDetails"][0]
    digest = detail["imageDigest"]
    result[name] = {
        "repository": f"{prefix}/{name}",
        "tag": tag,
        "digest": digest,
        "immutable_uri": f"{registry}/{prefix}/{name}@{digest}",
    }
(state / "image-digests.json").write_text(json.dumps(result, indent=2) + "\n")
PY
fi

printf 'build_succeeded source=%s tag=%s push=%s\n' "$short_sha" "$image_tag" "$publish"
