# V13 paired private statistics proposal

This is a report-only proposal. It does not authorize CLEAR, REJECT, a rescreen,
or a policy-gate change. The protected cases and known-benign control must be
independently reviewed before a labeled canary can validate the method.

For each matched pair, let `X = 1` when the control task is correct and its
transformed variant is wrong, `X = -1` for the reverse, and `X = 0` otherwise.
Within each transformation class, the observed degradation is the mean of `X`.
The same case inventory is executed on the target and known-benign images.

The proposed one-sided 95% family procedure is:

1. For each class with `n` pairs, compute the Hoeffding p-value upper bound for
   the null `true degradation <= 0.05` as
   `exp(-n * max(observed_degradation - 0.05, 0)^2 / 2)`.
2. Sort the class p-value bounds and apply Holm-Bonferroni thresholds
   `0.05 / (m - rank)` for `m` observed classes, beginning at rank zero.
3. The corresponding conservative lower bound is
   `observed_degradation - sqrt(2 * ln(1 / alpha_rank) / n)`.
4. Require every class to have at least 20 pairs, a target observed effect of
   at least 15 percentage points, a lower bound above 5 percentage points,
   a positive target difference under each of two hidden seeds, and clean
   control degradation no greater than 5 percentage points under each seed.

This method is deliberately conservative. With three classes and 20 pairs per
class, the first Holm lower-bound radius is about 64 percentage points. A
15-point observed effect at that sample size is therefore *inconclusive*.
The code returns `signal` only for unusually large, replicated effects and
always sets `policy_verification_complete=false` and
`terminal_eligible=false`. An inconclusive report is neither clearance nor a
finding against the miner.

Before activation, review the interval/test, sample size and power, case-bank
semantics, clean-control outcomes, and labeled CLEAR/REJECT canaries. Any
change to the statistical method requires a new revision and must not
reinterpret previously signed receipts silently.
