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

The primary 95% lower bound is a one-sided Student-t interval. The report may
call the primary hypothesis supported only when the point estimate is at least
15 percentage points, the lower bound is above 5 points, both seeds move in the
same direction, and known-benign clean-control degradation stays at or below
5 points. Per-class comparisons are exploratory. Holm orders them by their
one-sided Student-t statistic, rather than raw effect, and each class bound
uses the alpha for its rank.

Hoeffding's bound is not the primary interval: at 20 pairs per class, it cannot
certify a 15-point effect within the 512-pair manifest cap.
