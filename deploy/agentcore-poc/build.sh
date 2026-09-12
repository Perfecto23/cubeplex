#!/usr/bin/env bash
set -euo pipefail

poc_directory="$(cd "$(dirname "$0")" && pwd)"
poc_root="$(git -C "$poc_directory" rev-parse --show-toplevel)"
poc_state="$HOME/.local/state/cubeplex-agentcore-poc/20260912"
poc_commit="${1:?Usage: build.sh <expected-commit> [--push]}"
poc_publish="${2:-}"
[[ "$poc_publish" == "" || "$poc_publish" == "--push" ]] || exit 2
[[ "$(git -C "$poc_root" rev-parse HEAD)" == "$poc_commit" ]] || { echo 'Source HEAD differs from expected commit' >&2; exit 1; }
[[ -z "$(git -C "$poc_root" status --porcelain -- backend/cubeplex deploy/agentcore-poc)" ]] || { echo 'Commit the PoC source before building' >&2; exit 1; }
mkdir -p "$poc_state"
chmod 700 "$poc_state"
umask 077

poc_source_hash="$("$poc_directory/.venv/bin/python" - "$poc_root" "$poc_state" "$poc_commit" <<'PY'
from pathlib import Path
import hashlib,json,sys
root,state,commit=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
files=[]
for path in sorted((root/'backend/cubeplex').rglob('*')):
    if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
        continue
    if path.is_symlink():
        raise SystemExit('Refusing source symlink in image context')
    if path.name.startswith('.env') or path.suffix in {'.pem','.key'}:
        raise SystemExit('Refusing credential file in image context')
    files.append(path)
files += [root/'deploy/agentcore-poc'/name for name in ['Dockerfile','Dockerfile.dockerignore','pyproject.toml','uv.lock']]
manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
(state/'build-source.json').write_text(json.dumps({'source_commit':commit,'source_tree_sha256':digest,'files':manifest},indent=2)+'\n')
print(digest)
PY
)"
poc_tag="${poc_commit:0:12}-${poc_source_hash:0:12}"
poc_repository='986420599013.dkr.ecr.us-west-2.amazonaws.com/cubeplex-poc-20260912'
poc_image="$poc_repository:$poc_tag"
docker buildx build --platform linux/arm64 --provenance=false --load \
  -f "$poc_directory/Dockerfile" \
  --build-arg "SOURCE_COMMIT=$poc_commit" \
  --build-arg "SOURCE_TREE_SHA256=$poc_source_hash" \
  --metadata-file "$poc_state/build-metadata.json" \
  -t "$poc_image" "$poc_root" > "$poc_state/image-build.log" 2>&1
docker image inspect "$poc_image" > "$poc_state/local-image.json"
if [[ "$poc_publish" == '--push' ]]; then
  [[ "$(aws sts get-caller-identity --profile moego-testing --region us-west-2 --query Account --output text)" == '986420599013' ]] || exit 1
  aws ecr get-login-password --profile moego-testing --region us-west-2 \
    | docker login --username AWS --password-stdin '986420599013.dkr.ecr.us-west-2.amazonaws.com' > "$poc_state/ecr-login.log" 2>&1
  docker push "$poc_image" > "$poc_state/image-push.log" 2>&1
  aws ecr describe-images --profile moego-testing --region us-west-2 \
    --repository-name cubeplex-poc-20260912 --image-ids "imageTag=$poc_tag" > "$poc_state/ecr-image.json"
  "$poc_directory/.venv/bin/python" - "$poc_state" "$poc_repository" <<'PY'
from pathlib import Path
import json,sys
state,repository=Path(sys.argv[1]),sys.argv[2]
image=json.loads((state/'ecr-image.json').read_text())['imageDetails'][0]
uri=f"{repository}@{image['imageDigest']}"
(state/'immutable-image-uri.txt').write_text(uri+'\n')
print(uri)
PY
else
  printf '%s\n' "$poc_image"
fi
