---
id: october-29-unmodified-fork-not-eligible
resolution: guidance
tags: [policy-v13, copy, released-base, fork, unmodified-fork, exact-duplicate, copy-court, family-triage, calibration, hfull-h3, dan0l]
holding: A fork of a publicly released agent is eligible only with its own code changes. A fork whose runtime files are byte-identical to the released base, with only attribution or declaration files changed, is an exact duplicate with no contribution, so REJECT. Building on released source with own changes stays permitted; the released-base clears stand.
---

Operator ruling, Peyton, Discord 2026-10-29, verbatim:

> So you have to make changes if you are going to fork yes

This adds one condition to the released-base copy-hold rule. Released source
can still be reused. Shared files that trace byte for byte to a source that
was public before the upload still explain the overlap (see the mnemo v1 and
nanodevs v2 clears against tensorworker v16). The fork must also change at
least one runtime file of that base. Runtime means `Dockerfile`,
`requirements.txt` and the code tree. Declaration, attribution, changelog and
upstream prose does not count, and neither does whitespace.

Applied 2026-10-29 in rulings batch `4fcfacf0-031f-4a5e-8a66-6f7a273d3159`.
Both rows were held against released dan0l v28
`b4b99c40-151a-4792-a814-9fad4f9d2f9c` (SHA `de97219a...b792`, first
exact-SHA public handoff 2026-10-28T21:05:43Z):

- hfull-h3 v3 `f56b3304-f91f-45a0-8896-359b8dbf0f56` (SHA `a4ca3de6...7dfb5`),
  REJECTED. All 34 runtime files are byte-identical to dan0l v28. The only
  changes are a new `ATTRIBUTION.md` and edits to `DECLARATION.md`, whose own
  text says no code changed. This is an exact duplicate of the released agent,
  so it gets no separate scoring or emission. No source violation of v28 is
  alleged.
- hfull-h3 v4 `7b096808-fd0e-48fa-8729-8814ddbb93f5` (SHA `05204907...13f5d9`),
  CLEARED. 33 files are byte-identical to v28, and `app/short.py` carries the
  fork's own change. That change is in neither of the unreleased dan0l
  versions, v29 and v30.

ember v7 `93337720-cc10-4f8d-a4fc-414ab4904389` is a released-base fork with
its own changes, and it was cleared in batch
`22c6e151-f7c8-457d-9840-268e62d38144`. It was built on dan0l v28 with own
changes in `app/short.py` and `app/action.py`, and none of the unreleased
tensorworker v16 or dan0l v29/v30 deltas are present.

## Calibration for the copy court

The court's family triage recommended CLEAR for hfull-h3 v3 on publication
timing alone (recommendation `79e58129`). The overlap was fully explained by
a proven-public base, but nothing checked whether the held agent changed
anything. The family triage (`apps/platform/ditto/api_server/copy_court_family.py`,
`family-diff-v3`) now also requires a non-empty runtime delta from the primary
base (`held_delta.runtime_changed_count > 0`) before a released-base CLEAR:

- If the runtime files are byte-identical to the base and only prose changed,
  the court recommends REJECT with basis `unmodified_fork_of_released_base`.
  The court never applies a reject; an operator must confirm it.
- If the fork only removes runtime files, or the delta cannot be measured
  (no primary base), the court ESCALATEs.

When you review a near-duplicate CLEAR by hand, read the held delta against
the released base before ruling. A fork whose declaration says no code
changed is not eligible, however clean its publication timing.
