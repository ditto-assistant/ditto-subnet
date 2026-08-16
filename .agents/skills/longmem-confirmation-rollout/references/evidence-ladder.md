# LongMem confirmation evidence ladder

Use this checklist to separate implementation progress from live success.

| Rung | Required evidence | Insufficient substitute |
| --- | --- | --- |
| Source | Exact PR head/base and clean diff | Branch name or local commit |
| Review | Exact-head CI and independent approvals | A prior-head approval |
| Merge | Merge commit contains exact PR head | Mergeable PR |
| Release | Release source contains the exact merge in ancestry; tag resolves to the semantic release commit; workflow succeeded | Tag name or direct-parent assumption alone |
| Platform | Live health reports exact release commit | Successful deploy job without health |
| Artifacts | Immutable validator, scorer, and stack digests | Mutable version tag |
| Fleet | Exact descriptor/source/scorer across fresh healthy heartbeats | Candidate or draining state |
| Claim | One bundle/ticket leased to exact eligible validator | Available slot or UI card |
| Execution | Provider chronology and terminal dimension/report state | Aggregate request count |
| Evidence | Dimension rows, canonical provider accounting, root, reporter, signature | Completed provider calls |
| Verification | Platform accepted and verified the signed evidence | Local verifier pass |
| Result | Qualification/completion/public result reflects the attempt | Ticket merely expired or handed back |

## Failure localization fields

Record a timestamped tuple for every live failure:

```text
bundle, ticket, attempt, validator, slot, release SHA, profile/settings revision,
dimension stage, failure class/status, provider lane counts, last provider finish,
dimension row count, evidence root/signature/verification state
```

Keep raw submitted content, endpoint URLs, credentials, case IDs, and arbitrary
exception text out of shared logs and reports.

## Accepted-attempt handoff

The final handoff should contain:

- exact source, release, descriptor, and scorer identities;
- bundle/ticket/attempt and terminal timestamps;
- LongMem score and per-capability cases;
- reader/judge request, receipt, token, and cost totals;
- ablation outcomes;
- evidence digest/root, signature identity, and verification result;
- confirmation that no extra attempt or rollback appeared afterward.
