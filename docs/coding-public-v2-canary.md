# Coding public v2 certification canary

Status: proposed shadow-only qualification profile. This document does not
issue leases, alter public screening, change Coding scores, or enable weights.

The public v2 canary is an artifact compatibility and integrity gate. It is not
a local practice score, task-quality score, or competitive benchmark result.

## Authority

One immutable canary manifest binds:

```text
public v2 release manifest SHA-256
one public task ID and condition
visible workspace and public grader identities
runtime policy and resource profile
coding contract and tool-contract revisions
screened artifact SHA-256
canary execution policy and deadline
weight_eligible = false
```

The canary uses only fully public material. It grants no private catalog,
Hippius, decryption, curator, private grader, or scoring capability. Public
grader material is injected only after freeze.

## Required sequence

```text
health
  -> seed
  -> identical seed replay
  -> exchange ticket-scoped inference grant
  -> run
  -> revoke workspace capability
  -> freeze workspace
  -> pristine public grade
  -> append terminal certification evidence
```

The sequence must demonstrate a valid coding interface, source-bound inference,
validator-owned tool activity, bounded workspace mutation, and reproducible
public grading. A candidate failure affects only coding qualification; it
cannot alter normal DittoBench results.

## v2 release transition

The existing public certification manifest remains valid for practice v1. A
public v2 manifest may be admitted only after all of the following are present:

1. A validated ten-task public v2 pack.
2. An immutable public release descriptor and verified archive.
3. A selected public v2 task whose source snapshot and public grader identities
   match the release.
4. A multi-file public runtime policy that names only reviewed command IDs.
5. Updated public runner, grader, and certification vectors.

Changing the public release, task, runtime policy, grader, resource profile, or
tool contract creates a new canary identity. The prior canary is never silently
rewritten.

## Failure handling

```text
unsupported interface          -> non-scoring qualification result
candidate protocol/integrity   -> terminal qualification failure
pre-authoritative transport    -> validator infrastructure
post-authoritative transport   -> freeze and grade the existing workspace
public grader infrastructure   -> validator infrastructure
```

No local result or miner-provided report may substitute for this validator-run
canary. A canary pass is necessary for coding-capability certification, but it
is not private-shadow admission: the exact screened artifact must already have
a durable Tool + Memory core-qualification decision and a claimed certification
lease. A canary pass is never sufficient for leaderboard, reward, or emission
eligibility.
