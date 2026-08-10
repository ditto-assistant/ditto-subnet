#!/usr/bin/env bash
# Publish an attestation-free runtime index whose platform manifests are exactly
# the children of a canonical release index. Frozen host updaters authenticate
# the signed stack descriptor and inspect these children with Docker; the
# canonical indexes retain their provenance and SBOM attachment manifests.
set -euo pipefail

usage() {
  echo "usage: $0 REPOSITORY CANONICAL_DIGEST TAG PLATFORM [PLATFORM ...]" >&2
  exit 2
}

[[ $# -ge 4 ]] || usage
repository="$1"
canonical_digest="$2"
tag="$3"
shift 3
platforms=("$@")

[[ "$repository" =~ ^ghcr\.io/ditto-assistant/[a-z0-9._/-]+$ ]] || {
  echo "invalid first-party repository: $repository" >&2
  exit 2
}
[[ "$canonical_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "invalid canonical digest" >&2
  exit 2
}
[[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || {
  echo "invalid runtime tag" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

canonical_ref="$repository@$canonical_digest"
canonical_raw="$(docker buildx imagetools inspect --raw "$canonical_ref")"
children=()
expected_digests=()
seen_platforms='|'
for platform in "${platforms[@]}"; do
  [[ "$platform" =~ ^linux/(amd64|arm64)$ ]] || {
    echo "unsupported runtime platform: $platform" >&2
    exit 2
  }
  [[ "$seen_platforms" != *"|$platform|"* ]] || {
    echo "duplicate runtime platform: $platform" >&2
    exit 2
  }
  seen_platforms="${seen_platforms}${platform}|"
  architecture="${platform#linux/}"
  child_digest="$(
    jq -er --arg architecture "$architecture" '
      [.manifests[] |
        select(.platform.os == "linux" and .platform.architecture == $architecture)] |
      if length == 1 then .[0].digest
      else error("expected exactly one canonical child for linux/" + $architecture)
      end
    ' <<<"$canonical_raw"
  )"
  [[ "$child_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
  expected_digests+=("$child_digest")
  children+=("$repository@$child_digest")
done

runtime_ref="$repository:$tag"
docker buildx imagetools create --prefer-index=true \
  --tag "$runtime_ref" "${children[@]}" >&2

runtime_raw="$(docker buildx imagetools inspect --raw "$runtime_ref")"
[[ "$(jq -er '.mediaType' <<<"$runtime_raw")" = "application/vnd.oci.image.index.v1+json" ]]
[[ "$(jq -er '.manifests | length' <<<"$runtime_raw")" -eq "${#platforms[@]}" ]] || {
  echo "runtime index contains attachment or unexpected platform manifests" >&2
  exit 1
}
for index in "${!platforms[@]}"; do
  platform="${platforms[$index]}"
  architecture="${platform#linux/}"
  actual="$(
    jq -er --arg architecture "$architecture" '
      [.manifests[] |
        select(.platform.os == "linux" and .platform.architecture == $architecture)] |
      if length == 1 then .[0].digest
      else error("expected exactly one runtime child for linux/" + $architecture)
      end
    ' <<<"$runtime_raw"
  )"
  [[ "$actual" = "${expected_digests[$index]}" ]] || {
    echo "runtime index changed the canonical $platform child" >&2
    exit 1
  }
done

runtime_digest="$(
  docker buildx imagetools inspect "$runtime_ref" \
    --format '{{json .Manifest}}' | jq -er '.digest'
)"
[[ "$runtime_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "runtime tag did not resolve to an immutable digest" >&2
  exit 1
}
printf '%s\n' "$runtime_digest"
