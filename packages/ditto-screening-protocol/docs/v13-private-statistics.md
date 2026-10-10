# V13 private paired analysis (report-only)

`analyze_v13_private_pairs` implements the predeclared numerical test for a
completed target run and a separate known-benign clean-control run. It requires
two hidden seed commitments, at least 10 pairs per class per seed and 20 per
class, consistent paired counts, matching class/seed sample sizes, and distinct
target and clean-control images. Missing or inconsistent evidence is
inconclusive; it cannot be converted into a policy verdict.

For each class, the paired signed outcome is +1 when only the control is
correct, -1 when only the variant is correct, and 0 otherwise. The estimated
effect is the mean of these values. A one-sided Hoeffding lower bound for the
range [-1, 1] is `mean - sqrt(2 * ln(1/alpha) / n)`. Class hypotheses that the
effect is at most 5 percentage points are ordered by their one-sided bound
and tested with Holm's family-wise 5% correction. A candidate effect additionally
requires an observed degradation of at least 15 points, a corrected lower bound
above 5 points, positive effect in each seed, and no more than 5 points of
clean-control degradation in any seed or class aggregate. These conservative
bounds can return no finding even for a real moderate effect at the minimum
sample size; that is an inconclusive statistical result, never CLEAR.
At the minimum 20 pairs per class, the first three-class Holm-adjusted bound
has a 63.99-point margin. An observed 15-point effect would require at least
819 pairs per class to clear the 5-point lower-bound threshold with this method.
That is at least 2,457 pairs across the three required classes, while the
current sealed manifest allows at most 512 pairs total. The current profile
and this conservative bound therefore cannot certify a 15-point effect.
Before activation, the operator must predeclare and validate a higher-power
exact paired-difference interval or a feasible larger sample. The published
60 pairs are a minimum, not evidence that this conservative analysis has
adequate power at a 15-point effect.

The current function accepts aggregate inputs and does **not** authenticate
their origin or prove the two images received the same hidden tasks. The next
signed-evidence step must bind the target and known-benign results to the exact
artifact, attempt, image, profile, protected package, runner identity, and
pair inventory before this report can inform an operator. Even a bound result
does not establish source causality or satisfy the other V13 checks. It never
authorizes CLEAR or REJECT.
