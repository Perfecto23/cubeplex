#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <committed-sha> [--push]" >&2
  exit 2
fi

source_sha_input="$1"
publish=false
if [[ "${2:-}" == "--push" ]]; then
  publish=true
elif [[ $# -eq 2 ]]; then
  echo "unknown option: $2" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
resolved_sha="$(git -C "$repo_root" rev-parse --verify "${source_sha_input}^{commit}")"
short_sha="$(printf '%s' "$resolved_sha" | cut -c1-12)"
region=us-west-2
account=986420599013
registry="${account}.dkr.ecr.${region}.amazonaws.com"
native_repository="cubeplex-agentcore-native-entry-20260913/worker"
backend_repository="cubeplex-product-20260912/backend"
frontend_repository="cubeplex-product-20260912/frontend"
state_root="${CUBEPLEX_NATIVE_ENTRY_STATE_DIR:-${HOME}/.local/state/cubeplex-agentcore-native-entry/20260913/build}"
state_dir="$state_root/$resolved_sha"
archive_dir="$state_dir/archive"
if [[ -e "$state_dir" ]]; then
  echo "evidence already exists: $state_dir" >&2
  exit 1
fi
umask 077
mkdir -m 700 -p "$state_dir" "$archive_dir"
git -C "$repo_root" archive --format=tar "$resolved_sha" | tar -xf - -C "$archive_dir"
slice_dir="$archive_dir/deploy/agentcore-native-entry"
backend_dockerfile="$archive_dir/deploy/images/backend/Dockerfile"
frontend_dockerfile="$archive_dir/deploy/images/frontend/Dockerfile"
for build_input in "$slice_dir/Dockerfile" "$slice_dir/uv.lock" \
  "$backend_dockerfile" "$archive_dir/backend/uv.lock" \
  "$frontend_dockerfile" "$archive_dir/frontend/pnpm-lock.yaml"; do
  [[ -f "$build_input" ]] || { echo "missing committed build input: $build_input" >&2; exit 1; }
done
printf '%s\n' "$resolved_sha" > "$state_dir/source-commit.txt"

(cd "$slice_dir" && uv export --frozen --format requirements-txt --no-hashes \
  --no-dev --no-editable --no-emit-project > requirements-frozen.txt)
(cd "$archive_dir/backend" && uv export --frozen --format requirements-txt --no-hashes \
  --no-dev --no-editable --no-emit-project > requirements-frozen.txt)

source_manifest="$state_dir/source-manifest.json"
python3 - "$archive_dir" "$resolved_sha" "$source_manifest" <<'PY'
import hashlib,json,sys,tomllib
from pathlib import Path
root=Path(sys.argv[1]); commit=sys.argv[2]; out=Path(sys.argv[3])
files={}
for prefix in ("deploy/agentcore-native-entry", "backend", "deploy/images/backend",
               "frontend", "deploy/images/frontend"):
    for path in sorted((root / prefix).rglob('*')):
        if path.is_file() and path.name != 'requirements-frozen.txt':
            files[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
files['.dockerignore']=hashlib.sha256((root/'.dockerignore').read_bytes()).hexdigest()
tree=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
lock=tomllib.loads((root/'deploy/agentcore-native-entry/uv.lock').read_text())
cubeloop=next(package for package in lock['package'] if package['name']=='cubeloop')
out.write_text(json.dumps({'source_commit':commit,'source_tree_sha256':tree,
                          'native_cubeloop':cubeloop,'files':files},indent=2)+'\n')
print(tree)
PY

source_tree_sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_tree_sha256"])' "$source_manifest")"
image_tag="native-${short_sha}-${source_tree_sha256:0:12}"
native_image="$registry/$native_repository:$image_tag"
backend_image="$registry/$backend_repository:$image_tag"
frontend_image="$registry/$frontend_repository:$image_tag"
printf '%s\n' "$source_tree_sha256" > "$state_dir/source-tree-sha256.txt"
printf '%s\n' "$image_tag" > "$state_dir/image-tag.txt"
printf '%s\n' "$native_image" > "$state_dir/native-image.txt"
printf '%s\n' "$backend_image" > "$state_dir/backend-image.txt"
printf '%s\n' "$frontend_image" > "$state_dir/frontend-image.txt"

docker buildx build --platform linux/arm64 --provenance=false --load \
  --label "org.opencontainers.image.revision=$resolved_sha" \
  --file "$slice_dir/Dockerfile" --tag "$native_image" "$archive_dir" \
  > "$state_dir/native-build.log" 2>&1
docker image inspect "$native_image" > "$state_dir/native-local-image.json"
docker run --rm --platform linux/arm64 --network none --entrypoint python "$native_image" -c '
import importlib.metadata as metadata,json,os,platform,shutil,subprocess
distribution=metadata.distribution("cubeloop")
direct_url=distribution.read_text("direct_url.json")
print(json.dumps({"cubeloop_version":distribution.version,
                  "cubeloop_direct_url":json.loads(direct_url) if direct_url else None,
                  "python":platform.python_version(),"uid":os.getuid(),
                  "machine":platform.machine(),"bash":shutil.which("bash"),
                  "git":subprocess.check_output(["git","--version"],text=True).strip()}))
' > "$state_dir/native-runtime-dependencies.json"
python3 - "$source_manifest" "$state_dir/native-runtime-dependencies.json" <<'PY'
import json,sys
expected=json.load(open(sys.argv[1]))['native_cubeloop']
actual=json.load(open(sys.argv[2]))
assert actual['cubeloop_version']==expected['version'], 'Native CubeLoop version mismatch'
assert actual['uid']==10001 and actual['machine']=='aarch64', 'Native runtime identity mismatch'
assert actual['bash'] and actual['git'], 'Native shell/git tools missing'
direct_url=actual['cubeloop_direct_url']
if direct_url and 'vcs_info' in direct_url:
    assert direct_url['vcs_info']['commit_id']==expected['source']['git'].split('#')[-1]
PY

docker buildx build --platform linux/amd64 --provenance=false --load \
  --label "org.opencontainers.image.revision=$resolved_sha" \
  --target backend --file "$backend_dockerfile" --tag "$backend_image" "$archive_dir" \
  > "$state_dir/backend-build.log" 2>&1
docker image inspect "$backend_image" > "$state_dir/backend-local-image.json"

docker buildx build --platform linux/amd64 --provenance=false --load \
  --label "org.opencontainers.image.revision=$resolved_sha" \
  --file "$frontend_dockerfile" --tag "$frontend_image" "$archive_dir" \
  > "$state_dir/frontend-build.log" 2>&1
docker image inspect "$frontend_image" > "$state_dir/frontend-local-image.json"

if [[ "$publish" == true ]]; then
  caller_account="$(aws --profile moego-testing --region "$region" sts get-caller-identity --query Account --output text)"
  [[ "$caller_account" == "$account" ]] || { echo "unexpected AWS account" >&2; exit 1; }
  aws --profile moego-testing --region "$region" ecr describe-repositories \
    --repository-names "$native_repository" "$backend_repository" "$frontend_repository" \
    > "$state_dir/ecr-repositories.json"
  aws --profile moego-testing --region "$region" ecr get-login-password \
    | docker login --username AWS --password-stdin "$registry" > "$state_dir/ecr-login.log" 2>&1
  docker push "$native_image" > "$state_dir/native-push.log" 2>&1
  docker push "$backend_image" > "$state_dir/backend-push.log" 2>&1
  docker push "$frontend_image" > "$state_dir/frontend-push.log" 2>&1
  aws --profile moego-testing --region "$region" ecr describe-images \
    --repository-name "$native_repository" --image-ids imageTag="$image_tag" \
    > "$state_dir/native-ecr-image.json"
  aws --profile moego-testing --region "$region" ecr describe-images \
    --repository-name "$backend_repository" --image-ids imageTag="$image_tag" \
    > "$state_dir/backend-ecr-image.json"
  aws --profile moego-testing --region "$region" ecr describe-images \
    --repository-name "$frontend_repository" --image-ids imageTag="$image_tag" \
    > "$state_dir/frontend-ecr-image.json"
  python3 - "$state_dir" "$registry" "$image_tag" <<'PY'
import json,sys
from pathlib import Path
state=Path(sys.argv[1]); registry=sys.argv[2]; tag=sys.argv[3]
result={}
source=json.loads((state/'source-manifest.json').read_text())
for name,repo in (("native", "cubeplex-agentcore-native-entry-20260913/worker"),
                  ("backend", "cubeplex-product-20260912/backend"),
                  ("frontend", "cubeplex-product-20260912/frontend")):
    detail=json.loads((state/f"{name}-ecr-image.json").read_text())["imageDetails"][0]
    result[name]={"repository":repo,"tag":tag,"digest":detail["imageDigest"],
                  "immutable_uri":f"{registry}/{repo}@{detail['imageDigest']}",
                  "source_commit":source['source_commit'],
                  "source_tree_sha256":source['source_tree_sha256']}
(state/"image-digests.json").write_text(json.dumps(result,indent=2)+"\n")
PY
fi

printf 'build_succeeded source=%s native=%s backend=%s frontend=%s push=%s\n' \
  "$short_sha" "$native_image" "$backend_image" "$frontend_image" "$publish"
