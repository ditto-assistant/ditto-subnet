#!/usr/bin/env bash
# Re-check every open PR's merge result against `main` as it is *now*.
#
# The per-PR migration-order check runs when the PR is pushed and never
# again. That is exactly how the 2026-07-28 outage happened: #505's last
# green run was 2026-07-27T18:59, #524 landed 85 minutes later, and nothing
# re-evaluated the pair. #505 then merged ~21 hours later on a check that had
# been true when it ran and false ever since, and `main` had two Alembic
# heads -- 1183 DB test errors and a dead deploy.
#
# GitHub re-runs a PR's checks when the PR moves, not when its base does, and
# "require branches to be up to date before merging" is off on this repo. So
# nothing else closes this window. This runs on every push to `main` and
# posts a commit status on each open PR that adds a migration, which is what
# makes a newly-stale PR say so on the PR itself.
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-ditto-assistant/ditto-subnet}"
BASE_REF="${1:-origin/main}"
CONTEXT="migration-order/merge-result"
if [[ -n "${GITHUB_RUN_ID:-}" ]]; then
  RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${GITHUB_RUN_ID}"
else
  # Manual/bootstrap sweeps run before this workflow reaches the default
  # branch. Keep their status link useful instead of pointing at run 0.
  RUN_URL="https://github.com/${REPO}/actions"
fi

# If `main` itself is divergent then every PR below inherits that failure and
# none of them caused it. Say so once, fail loudly, and post nothing: red
# statuses on innocent PRs are how a guard gets muted.
if ! head_revision=$(python3 apps/platform/scripts/check_migration_order.py --head 2>&1); then
  echo "::error title=main has multiple Alembic heads::${head_revision}"
  exit 1
fi
echo "${BASE_REF} resolves to a single head: ${head_revision}"

# Every open PR, with a count of the migrations it adds -- not just the ones
# that add migrations. A PR that adds none cannot fork the chain and is
# reported green without being fetched, which keeps the status present on
# every PR (so the context is safe to require) and stops a stale red from
# outliving the migration that caused it.
open_prs=$(gh api --paginate --slurp \
  "repos/${REPO}/pulls?state=open&per_page=100" \
  --jq 'add[] | "\(.number)\t\(.head.sha)"')

if [[ -z "${open_prs}" ]]; then
  echo "no open PRs"
  exit 0
fi

stale=()
while IFS=$'\t' read -r number sha; do
  [[ -n "${number}" ]] || continue
  # The pull-request files endpoint is capped at 100 entries per page. Slurp
  # every page before counting so a migration in a large generated/import PR
  # cannot silently escape the required status.
  migrations=$(gh api --paginate --slurp \
    "repos/${REPO}/pulls/${number}/files?per_page=100" \
    --jq '[add[].filename
      | select(startswith("apps/platform/alembic/versions/"))] | length')
  if ((migrations == 0)); then
    state=success
    description="Adds no migration; cannot fork the Alembic chain."
    echo "PR #${number}: no migrations"
  elif output=$(git fetch --quiet --force --no-tags origin \
    "pull/${number}/head:refs/pr/${number}" </dev/null 2>&1 &&
    python3 apps/platform/scripts/check_migration_order.py \
      "${BASE_REF}" "refs/pr/${number}" 2>&1); then
    state=success
    description="Merging into main leaves exactly one Alembic head."
    echo "PR #${number}: ok"
  else
    state=failure
    description="Merging into main would leave more than one Alembic head."
    stale+=("${number}")
    echo "::warning title=PR #${number} would now leave multiple Alembic heads::${output}"
  fi
  gh api --method POST "repos/${REPO}/statuses/${sha}" \
    -f state="${state}" \
    -f context="${CONTEXT}" \
    -f description="${description}" \
    -f target_url="${RUN_URL}" </dev/null >/dev/null
done <<<"${open_prs}"

# The finding belongs on the PRs, which now carry a red status. Failing this
# run as well would only paint `main` red for something main did not do.
if ((${#stale[@]})); then
  {
    echo "### Open PRs made undeployable by this push"
    echo
    echo "Each now resolves to more than one Alembic head when merged."
    echo "They carry a failing \`${CONTEXT}\` status until rebased."
    echo
    for number in "${stale[@]}"; do
      echo "- #${number}"
    done
  } >>"${GITHUB_STEP_SUMMARY:-/dev/stdout}"
fi
