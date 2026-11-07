#!/usr/bin/env bash
set -euo pipefail

image="${1:?supply the exact locally built Pylon image}"
context="${2:?supply the pinned TurboBT build context}"
repo_root="$(git rev-parse --show-toplevel)"
tmp="$(mktemp -d)"
tag="pylon-ca-downgrade-test:$(basename "$tmp")"
trap 'docker image rm "$tag" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT

# Make a disposable base that advertises a newer CA package. It is never run
# and contains no real credentials or data.
cat >"$tmp/Dockerfile" <<'EOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN mkdir -p /tmp/synthetic-ca/DEBIAN \
    && printf 'Package: ca-certificates\nVersion: 20250419+deb13u999\nArchitecture: all\nMaintainer: Synthetic Test <test@example.invalid>\nDescription: Disposable synthetic newer CA package\n' > /tmp/synthetic-ca/DEBIAN/control \
    && dpkg-deb --build /tmp/synthetic-ca /tmp/synthetic-ca.deb \
    && dpkg -i /tmp/synthetic-ca.deb \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" gt 20250419+deb13u1
EOF
docker buildx build --load --platform linux/amd64 -f "$tmp/Dockerfile" \
  --build-arg "BASE_IMAGE=$image" -t "$tag" "$tmp" >"$tmp/base.log" 2>&1

if docker buildx build --platform linux/amd64 --progress plain \
  --build-arg "PYLON_BASE_IMAGE=$tag" --build-context "turbobt=$context" \
  -f "$repo_root/Dockerfile.pylon" "$repo_root" >"$tmp/negative.log" 2>&1; then
  echo "Pylon build unexpectedly accepted a newer CA package" >&2
  exit 1
fi
if ! grep -q 'refusing to downgrade ca-certificates' "$tmp/negative.log"; then
  cat "$tmp/negative.log" >&2
  echo "Pylon build failed for a reason other than the downgrade guard" >&2
  exit 1
fi
echo "Pylon build rejects a newer synthetic CA package"
