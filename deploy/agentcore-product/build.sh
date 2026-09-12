#!/usr/bin/env bash
# Build the committed CubePlex backend and AgentCore worker images from a
# clean git archive. Frontend images are built by the separate baseline step.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: deploy/agentcore-product/build.sh <commit-sha> [--push]

Builds the backend target for linux/amd64 and the AgentCore worker target for
linux/arm64 from the exact committed SHA. The worktree is never used as a
Docker build context. --push is required before any ECR write.

Environment overrides:
  CUBEPLEX_PRODUCT_STATE_DIR  Evidence directory root
  CUBEPLEX_PRODUCT_REGISTRY   Registry host
  CUBEPLEX_PRODUCT_REPOSITORY ECR repository prefix
  AWS_PROFILE                  AWS profile (default: moego-testing)
  AWS_REGION                   AWS region (default: us-west-2)
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
    -*)
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
short_sha="${resolved_sha:0:12}"

aws_profile="${AWS_PROFILE:-moego-testing}"
aws_region="${AWS_REGION:-us-west-2}"
registry="${CUBEPLEX_PRODUCT_REGISTRY:-986420599013.dkr.ecr.${aws_region}.amazonaws.com}"
repository_prefix="${CUBEPLEX_PRODUCT_REPOSITORY:-cubeplex-product-20260912}"
if [[ "$publish" == true ]]; then
  if [[ "$aws_region" != "us-west-2" \
    || "$registry" != "986420599013.dkr.ecr.us-west-2.amazonaws.com" \
    || "$repository_prefix" != "cubeplex-product-20260912" ]]; then
    printf 'ERROR: --push is restricted to moego-testing us-west-2 product ECR\n' >&2
    exit 1
  fi
fi
state_root="${CUBEPLEX_PRODUCT_STATE_DIR:-${HOME}/.local/state/cubeplex-agentcore-product/build}"
state_dir="${state_root}/${resolved_sha}"
archive_dir="${state_dir}/archive"
mkdir -p "$state_dir"
chmod 700 "$state_dir"
if [[ -e "$archive_dir" ]]; then
  printf 'ERROR: evidence archive already exists: %s\n' "$archive_dir" >&2
  printf 'Choose a new state root or remove only this prior build archive after review.\n' >&2
  exit 1
fi
mkdir -m 700 "$archive_dir"
cleanup() {
  rm -rf "$archive_dir"
}
trap cleanup EXIT
umask 077

git -C "$repo_root" archive --format=tar "$resolved_sha" | tar -xf - -C "$archive_dir"
if [[ ! -f "$archive_dir/deploy/images/backend/Dockerfile" ]]; then
  printf 'ERROR: committed SHA has no backend Dockerfile\n' >&2
  exit 1
fi
if ! rg -q '^FROM runtime-base AS agentcore-worker$' "$archive_dir/deploy/images/backend/Dockerfile"; then
  printf 'ERROR: committed SHA does not contain the agentcore-worker target\n' >&2
  exit 1
fi
if [[ ! -f "$archive_dir/backend/cubeplex/agentcore/runtime.py" ]]; then
  printf 'ERROR: committed SHA has no backend/cubeplex/agentcore/runtime.py\n' >&2
  exit 1
fi

uv_bin="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$uv_bin" ]]; then
  printf 'ERROR: uv is required to export frozen backend dependencies\n' >&2
  exit 1
fi
(
  cd "$archive_dir/backend"
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
python3 - "$archive_dir" "$resolved_sha" "$source_manifest" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
commit = sys.argv[2]
destination = Path(sys.argv[3])
files: list[Path] = []
for prefix in ("backend", "deploy/images/backend"):
    for path in sorted((root / prefix).rglob("*")):
        if not path.is_file():
            continue
        if any(part in {"__pycache__", ".venv", ".pytest_cache"} for part in path.parts):
            continue
        files.append(path)
manifest = {
    str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in files
}
tree_sha256 = hashlib.sha256(
    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
destination.write_text(
    json.dumps(
        {
            "source_commit": commit,
            "source_tree_sha256": tree_sha256,
            "files": manifest,
        },
        indent=2,
    )
    + "\n"
)
print(tree_sha256)
PY

source_tree_sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_tree_sha256"])' "$source_manifest")"
image_tag="baseline-${short_sha}-${source_tree_sha256:0:12}"
backend_image="${registry}/${repository_prefix}/backend:${image_tag}"
worker_image="${registry}/${repository_prefix}/worker:${image_tag}"

printf '%s\n' "$resolved_sha" > "$state_dir/source-commit.txt"
printf '%s\n' "$image_tag" > "$state_dir/image-tag.txt"
printf '%s\n' "$backend_image" > "$state_dir/backend-image.txt"
printf '%s\n' "$worker_image" > "$state_dir/worker-image.txt"

docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --load \
  --target backend \
  --file "$archive_dir/deploy/images/backend/Dockerfile" \
  --tag "$backend_image" \
  "$archive_dir" \
  > "$state_dir/backend-build.log" 2>&1
docker image inspect "$backend_image" > "$state_dir/backend-local-image.json"

docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  --load \
  --target agentcore-worker \
  --file "$archive_dir/deploy/images/backend/Dockerfile" \
  --tag "$worker_image" \
  "$archive_dir" \
  > "$state_dir/worker-build.log" 2>&1
docker image inspect "$worker_image" > "$state_dir/worker-local-image.json"

if [[ "$publish" == true ]]; then
  account="$(aws --profile "$aws_profile" --region "$aws_region" sts get-caller-identity --query Account --output text)"
  if [[ "$account" != "986420599013" ]]; then
    printf 'ERROR: refusing unexpected AWS account %s\n' "$account" >&2
    exit 1
  fi
  aws --profile "$aws_profile" --region "$aws_region" ecr describe-repositories \
    --repository-names "${repository_prefix}/backend" "${repository_prefix}/worker" \
    > "$state_dir/ecr-repositories.json"
  aws --profile "$aws_profile" --region "$aws_region" ecr get-login-password \
    | docker login --username AWS --password-stdin "$registry" \
    > "$state_dir/ecr-login.log" 2>&1
  docker push "$backend_image" > "$state_dir/backend-push.log" 2>&1
  docker push "$worker_image" > "$state_dir/worker-push.log" 2>&1
  aws --profile "$aws_profile" --region "$aws_region" ecr describe-images \
    --repository-name "${repository_prefix}/backend" \
    --image-ids "imageTag=${image_tag}" \
    > "$state_dir/backend-ecr-image.json"
  aws --profile "$aws_profile" --region "$aws_region" ecr describe-images \
    --repository-name "${repository_prefix}/worker" \
    --image-ids "imageTag=${image_tag}" \
    > "$state_dir/worker-ecr-image.json"
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
for name in ("backend", "worker"):
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
