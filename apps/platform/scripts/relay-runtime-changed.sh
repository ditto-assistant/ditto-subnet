#!/usr/bin/env bash

# Exit successfully when a newline-delimited path list contains a change that
# can alter the relay runtime. The relay runtime is the Go model-relay service
# (services/model-relay) plus the scripts that build, define, and roll its
# releases, plus the deploy workflow itself. The Python Platform runtime no
# longer affects the relay: the binary is built solely from the Go module.
#
# CONTRACT: every path is REPO-ROOT-relative — the output of a plain
# `git diff --name-only` run from the monorepo root (release.yml's plan job
# feeds exactly that). Never feed platform-relative paths (`git diff
# --relative` inside apps/platform): they would silently never match the
# services/model-relay/ arm, and Go-source changes would never roll the relay.
#
# Go tests and fixtures live inside the module tree, so a broad
# services/model-relay/** match would unnecessarily roll critical relay
# infrastructure for test-only releases.
set -euo pipefail

while IFS= read -r path || [[ -n "$path" ]]; do
  case "$path" in
    services/model-relay/*_test.go | services/model-relay/testdata/* | services/model-relay/*/testdata/*)
      continue
      ;;
    services/model-relay/* | apps/platform/scripts/ecosystem.config.js | apps/platform/scripts/build-relay-release.sh | apps/platform/scripts/deploy-relay-release.sh | .github/workflows/platform-deploy.yml)
      exit 0
      ;;
  esac
done

exit 1
