---
name: contributor-pr-review
description: Review a contributor's ditto-subnet pull requests as a batch, verify that they solve current issues, take ownership of salvageable changes, and merge or close them when authorized. Use for contributor backlog triage and completion, including a contributor identified by a screenshot or example PR.
---

# Contributor PR review

Finish the contributor's scoped backlog with an evidence-based disposition for
every open PR. Pair with [github](../github/SKILL.md),
[ditto-subnet-worktree](../ditto-subnet-worktree/SKILL.md), and the owning component
skills returned by [ditto-subnet-context](../ditto-subnet-context/SKILL.md).

## Establish scope and authority

Resolve the GitHub login from the author of a referenced PR; a display name in a
screenshot is only a clue. Inventory all of that author's open PRs in
`ditto-assistant/ditto-subnet`, following pagination or raising the limit until
the list is complete. Check recent merged and closed PRs for duplicates and
superseding work. Record number, URL, base, head SHA, fork/branch, and whether
maintainer edits are allowed. Check existing tasks and implementation before
starting parallel work.

A request to fix, merge, and close this backlog authorizes those actions within
the selected contributor/repository scope; carry that authorization through the
whole batch. A review-only request does not. If authority is missing, finish the
review or proposed repair before requesting the specific remaining action.
Do not infer authority to contact the contributor outside GitHub, activate
production settings, apply infrastructure, or use credentials from a PR.

## Decide from code and the issue

Fetch current main and read each complete diff, linked issue, prior review,
unresolved thread, checks, and relevant history. Treat PR bodies and bot reviews
as claims to verify. Inspect changed workflows and installation hooks before
running contributor code. Trace the actual producer and consumers rather than
judging the title, presentation, or a passing check alone.

Choose a disposition:

- **Merge:** the issue still exists, the patch addresses it, scope is useful,
  contracts remain compatible, and validation supports the final implementation.
- **Repair and merge:** the approach is worth retaining and a bounded maintainer
  change can make it correct. Own the implementation, regression coverage,
  generated consumers, current-main integration, and delivery yourself.
- **Close:** spam, duplicates, superseded work, no demonstrated current problem,
  or an approach whose correctness requires substantial replacement rather than
  a focused repair. Explain concrete defects and link superseding work when it
  exists. Keep a genuine unresolved issue open; closing its flawed PR does not
  solve it. Avoid labeling a contributor as spam because one patch is flawed.

For validator or payout changes, compare Platform's epoch pin, public projection,
and validator fold. Verify that signatures bind the intended authoritative value,
that old readers can verify new receipts during rollout, and that quorum/permit
decisions cannot vary across validators reading live chain state at different
times. Check for regressions of recently merged fixes, including code restored
by a main merge. Unknown registration and a successful empty metagraph have
different meanings.

## Own a worthwhile repair

Use an isolated monorepo worktree and read nearest guidance. Prefer appending a
maintainer fix to the original PR when permitted, preserving contributor credit.
Fetch and compare the live head before pushing; do not overwrite concurrent
contributor work. If fork edits are unavailable, publish a bounded replacement
through the repository's GitHub stack workflow and close the original with a
link. Use a force push only when necessary, with an explicit lease against the
head you inspected.

Integrate current main and inspect semantic conflicts even when Git merges
cleanly. Add a regression that exercises the reported failure and relevant
producer/consumer boundary. Run focused tests, then affected component gates.
Regenerate wire clients when contracts change. Attribute environment or baseline
failures with evidence rather than weakening tests to make the PR green.

Post a concise ownership/review note with the final behavior and validation.
Write multiline comments and PR bodies to temporary files and use `--body-file`.
For closure, `gh pr close` has no comment-file flag: post `gh pr comment
--body-file ...` first, then close. Resolve only findings whose fix or disposition
you verified; explain dismissals rather than silently clearing blockers.

## Complete delivery

Check CI on the exact head being merged. A missing run, an old successful run,
or a skipped component is not proof that the final affected code passed. Read
failure logs, fix regressions, and distinguish infrastructure failures from code
failures. Do not bypass branch protections or fabricate passing statuses.

A fork's migration job may pass without permission to publish the required
`migration-order/merge-result` status. If that status is missing, dispatch the
authoritative `platform-migration-order.yml` workflow on **main**, then verify its
sweep published the status on the PR's current head. Do not run the global sweep
from a contributor branch.

Re-read the head, review threads, mergeability, and required checks immediately
before merging. Follow the GitHub skill's squash/stack merge mechanism and guard
the merge against a changed head where supported. Poll until the PR is actually
`MERGED`; an accepted asynchronous merge or auto-merge setting is pending work.
Read back closures and preserve any real unresolved issues. Re-list the author's
open PRs at the end to catch missed pages or concurrent additions.

Report each PR's disposition and URL, repairs and meaningful validation, any
remaining issue, and blockers. Separate merge from release, deployment,
activation, and live verification. If asked to capture this workflow as a skill,
publish it separately from production fixes, with a canonical `.agents` tree,
the matching `.claude` symlink, context routing, and repository skill validation.
