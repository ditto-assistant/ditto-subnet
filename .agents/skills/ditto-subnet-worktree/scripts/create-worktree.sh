#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <short-slug> [base-ref]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
slug="$1"
base_ref="${2:-origin/main}"

[[ "$slug" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || {
  echo "slug must use lowercase letters, digits, dot, underscore, or hyphen" >&2
  exit 2
}

repo_root="$(git rev-parse --show-toplevel)"
primary_root=""
worktree_list="$(git -C "$repo_root" worktree list --porcelain)"
while IFS= read -r line; do
  if [[ "$line" == worktree\ * ]]; then
    primary_root="${line#worktree }"
    break
  fi
done <<<"$worktree_list"
[[ -n "$primary_root" ]] || {
  echo "could not resolve the primary worktree" >&2
  exit 1
}

if [[ "$base_ref" == origin/* ]]; then
  remote_branch="${base_ref#origin/}"
  git -C "$repo_root" fetch origin "$remote_branch"
fi
git -C "$repo_root" rev-parse --verify "${base_ref}^{commit}" >/dev/null

branch="agent/$slug"
worktrees_root="$(dirname "$primary_root")/worktrees"
target="$worktrees_root/ditto-subnet-$slug"

git -C "$repo_root" show-ref --verify --quiet "refs/heads/$branch" && {
  echo "local branch already exists: $branch" >&2
  exit 1
}
[[ ! -e "$target" ]] || {
  echo "worktree target already exists: $target" >&2
  exit 1
}

mkdir -p "$worktrees_root"
git -C "$repo_root" worktree add -b "$branch" "$target" "$base_ref"

printf 'worktree=%s\nbranch=%s\nbase=%s\n' "$target" "$branch" "$(git -C "$target" rev-parse HEAD)"
echo "next: cd $target && read AGENTS.md && run the ditto-subnet-context lookup"
