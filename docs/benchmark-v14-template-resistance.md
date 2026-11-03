# Bench v14 template-resistance work

Status: implementation and qualification incomplete. Coordination:
[ditto-subnet#1832](https://github.com/ditto-assistant/ditto-subnet/issues/1832).
The October 26 owner direction is to make public-generator phrase matching
less useful through benchmark generalization. This is not a phrase blacklist,
a retrospective source-policy change or activation of screening policy v14.

## What exists

- Bench v14 compatibility merged in #2540. Later corrections #2769, #2827,
  #2839 and #3099 improve grading and case-file chronology. Apart from those
  dates, v14 carries forward the public v13 wording and case families.
- Open #1993, head `e3019c0554f821b60d293dac6c18a6d72b79edbc`, parallelizes
  research-router training. It changes neither generated wording nor scoring.
- Open draft #1994, head `2d6ea5c871dba1ae43f09960008ef535a951c710`, adds an
  opt-in literal-text producer mode. Its historical diagnostic reports shortcut
  quality 0.361 against honest quality 0.202944445. Provider errors contaminate
  the honest baseline; even the tool slice was 0.31 versus 0.30788889. The PR
  explicitly reports failed qualification, and those observations do not
  qualify its later profile revision. Rewrite percentage is not resistance.
- The generator-template source scanner from #2310 is report-only. Its
  starter-kit leads illustrate why overlap cannot become a scoring verdict.
- The private benchmark producer, pinning and delivery foundations remain
  retained research. They are separate from the retired optional screening
  private-audit program described in `screening-private-audit-retirement.md`.

At the October 26 Backroom readback, active and desired benchmark versions were
13. V14 had no rollout history. Mechanical `start_ready=true` is not evidence
of the missing anti-template qualification.

## First implementation boundary: measure the right public control

`parserprobe` can generate and diagnose v14, but the old public-control checker
only accepted v13. `PublicControlFailuresForVersion` now checks the explicitly
selected v13 or v14 contract, including every seed. Its legacy entry point stays
v13-only. Old-version reports and unreviewed future versions cannot stand in
for a v14 control. Coverage and the 0.90 per-slice floor are unchanged.

From `research/dittobench-datagen`:

```sh
go run ./cmd/parserprobe -bench-version 14 -seeds 40 -run-size full \
  -require-public-control -out /tmp/v14-public-control.json
```

This is a deterministic offline GIH diagnostic. Its reported quality is
`0.5 * tool_mean + 0.5 * memory_mean`; it is not an accepted live composite,
provider-cost measurement, honest-agent reference, private-artifact result or
proof about any particular miner. Passing only establishes that the public
adversary is strong enough for the specified control slices. It cannot qualify
private wording or authorize rollout.

## Remaining implementation sequence

1. **Exercise the actual strategy.** In addition to GIH and the independently
   trained router, evaluate an advisory-guidance carrier: phrase recognition
   chooses prompt notes, while the genuine model receives the request,
   evidence and full catalog and authors the calls and answer. A forced-call
   substitute would measure a different mechanism. Keep exact public carrier
   commitments without publishing private artifacts or miner source archives.
2. **Break cue-to-outcome shortcuts.** Add independently checked fresh
   phrasing and task combinations, plus paired familiar cues in quoted,
   negated and distracting contexts. Vary stored decisions so recognizing the
   request family is insufficient. Require reading the relevant state and
   preserving authorizations, chronology, units and tool contracts. Measure
   both axes and the weak families, not just aggregate accuracy.
3. **Version the retained machinery deliberately.** `GeneratePrivateBase`,
   `ApplyPrivateSurface`, `PrivateSurfaceRequests` and `DecodePrivateArtifact`
   remain v13-bound. The producer profile, recovery, preparation worker,
   Platform identity, scorer/validator delivery, confirmation reuse and
   disclosure closure need a coordinated v14 contract before private v14 work
   can execute. Do not change a guard to `>=13` and call the migration done.
   Preserve frozen v2-v13 bytes, grading and accepted ledgers.
4. **Qualify semantic fidelity and resistance together.** Preserve independent
   semantic checks and negative controls. Inspect unchanged question,
   catalog, fixture and identifier clues; expand the versioned contract if
   they still expose the recipe. Prepare and commit exact artifact bytes
   before leasing, share the same pin across compared participants, and close
   all authorized reuse before disclosure. A larger rewrite count or public
   salt alone does not demonstrate protection.
5. **Use clean measured controls.** Pin disjoint development and held-out
   data, carrier/model/image/profile identities and the full report. Budget
   actual honest locked-model and exact-artifact agent runs separately.
   Provider failures or missing deliveries invalidate that comparison rather
   than demonstrating resistance. Apply the documented honest gate-loss
   limit of 0.02 and adversary ceiling of honest starter-kit quality minus
   0.05, with required per-slice coverage and held-out replication. Do not
   confuse parser diagnostics with runtime score gates or measured costs.
6. **Review a release and rollout packet.** Pin the revised pre-activation
   v14 contract vector, image/source, historical-image replay, signed canary,
   shadow/rank-impact evidence and rollback boundary. Keep the unresolved
   anti-template work explicit in the rollout review. Merging compatibility
   code or passing a mechanical readiness check does not supply that evidence.

The existing [rollout plan](benchmark-v14-rollout.md) still governs fleet
readiness, historical replay and activation. This work authorizes no provider
spend, deployment, production setting, emission change or retrospective ban.
