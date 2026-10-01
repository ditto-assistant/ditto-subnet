#!/usr/bin/env bash
# Disposable source-bound userland installation. No cloud credentials or keys.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
REVISION="$(git rev-parse HEAD)"
QA="$(mktemp -d)"
IMAGE="collector-bootstrap-qa:${REVISION:0:12}"
cleanup() {
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  rm -rf -- "$QA"
}
trap cleanup EXIT
# These are public source files, readable by the unprivileged bootstrap UID.
chmod 0755 "$QA"
mkdir "$QA/source"
git archive HEAD | tar -x -C "$QA/source"
git init --bare --quiet "$QA/repo.git"
git -C "$QA/repo.git" fetch --depth=1 "file://$ROOT" "$REVISION:refs/heads/main"
chmod -R a+rX "$QA"
docker build --platform linux/amd64 --label "qa.source=$REVISION" \
  -f "$ROOT/infra/terraform/tests/collector-custody/Bootstrap.Dockerfile" \
  -t "$IMAGE" "$ROOT/infra/terraform/tests/collector-custody"
docker image inspect "$IMAGE" --format '{{.Id}} {{json .Config.Labels}}'
for role in registration transfer; do
  docker run --rm --platform linux/amd64 --cpus 2 --memory 4g \
    --mount "type=bind,src=$QA,dst=/fixture,readonly" \
    "$IMAGE" "$role" "$REVISION"
done
