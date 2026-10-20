# V13 private comparison statistical report

This note describes the report-only analysis of private paired comparisons. It
is separate from the published [V13 policy](policy-v13.md), whose pinned bytes
and decision rules remain unchanged. A statistical report is evidence for
operator review; it does not itself issue CLEAR or REJECT.

The predeclared primary hypothesis is the pooled mean of paired ternary scores
(+1 control-only correct, −1 variant-only correct, 0 otherwise) across the
required classes and both seeds. Each class-and-seed cell contains exactly 10
completed pairs. Before computing a bound, the completed-pair count must match
that design and the profile digest must match the published profile.

The primary 95% lower bound is an approximate one-sided Student-t interval
(normal quantile plus its first finite-df correction). It assumes independently
sampled pairs; it is not an exact finite-sample coverage guarantee for every
ternary distribution. The report may
call the primary hypothesis supported only when the point estimate is at least
15 percentage points, the lower bound is above 5 points, both seeds move in the
same direction, and known-benign clean-control degradation stays at or below
5 points. A constant sample has undefined t uncertainty, not zero uncertainty:
its reported lower bound is the conservative outcome-space floor, -100 points,
and it cannot support the primary hypothesis.

Per-class comparisons are exploratory. Their bounds invert the **complete**
Holm step-down test against a common null effect: rank by the one-sided t
statistic at that null, apply alpha/(remaining hypotheses), and stop at the
first failed rank. Ranks are recomputed at each null, because standard errors
can differ. Independently computing each rank's interval would incorrectly
let later classes claim support after an earlier failure. These are
common-null test inversions, not a claim of an exact joint confidence region
for all class effects.

Two seeds with matching effect signs are a replication check, not evidence
that all 60 pairs are independent. The aggregates cannot distinguish repeated
cases or shared seed-level shocks from independent pairs. The caller must
verify the case-bank sampling design and explicitly pass
`pair_independence_verified=True` before inferential support is reported.
Without that verification, descriptive effects and seed replication remain
visible, but all lower bounds are -100 points and the report is inconclusive.
If seeds are the independent sampling units, this pooled-pair method is not
appropriate; a separate cluster-aware design is required. No production
caller or policy activation is added by this report-only module.

Hoeffding's bound is not the primary interval: at 20 pairs per class, it cannot
certify a 15-point effect within the 512-pair manifest cap.
