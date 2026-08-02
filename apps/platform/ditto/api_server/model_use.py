"""The model-use rule: did this run actually put the cases to the model?

SN118 pays for a *retrieval agent* competition, and the platform supplies the
inference infrastructure precisely so that agents call the reader model. A
harness that answers from retrieval plus a hardcoded phrase-to-tool router is
out of bounds -- it would fail in front of a real user, and it is not the thing
being paid for.

Detecting that is not the same as counting calls.

Why the obvious rule is wrong
-----------------------------

"At least one call per case" is defeated by 280 empty calls, and that is not a
hypothetical: production shows a family of submissions whose entire model spend
for a 280-case run is **one call carrying 74 prompt tokens** -- a bare
capability probe. Padding that to 280 identical probes costs almost nothing and
would clear any call-count floor.

What cannot be faked cheaply
----------------------------

A call that actually carries a case must carry the case: system prompt +
retrieved memory + the question + tool schemas. That payload is not optional
and not compressible away -- you cannot ask the model about a case without
sending it the case. So the primary gate is on **prompt tokens per case**,
which prices the evasion at exactly what honest work costs. To clear it by
padding, a miner must send real-sized prompts, pay for them, and wait for them;
at that point the model is genuinely in the loop, which is the outcome the rule
wants.

A second gate covers the mirror-image evasion: one enormous call that clears a
per-case *average* while never engaging with cases individually. Requiring the
calls to be spread across the run closes it.

A third gate closes the hole the first two leave, and it was found by test
rather than by inspection: 2800 calls of 74 tokens across 280 cases gives 740
prompt tokens per case and 10 calls per case, clearing both of the above at
roughly a third of honest cost. Padding scales the per-case total but can never
raise the per-*call* average, so gating on prompt tokens per call cannot be
padded at all -- only paid for. It also inverts the economics: with a floor of
300 tokens on every call, a run making N calls must carry >= 300 x N prompt
tokens, so each added dummy call raises the bar it has to clear. At the
2800-call shape that demands 840k prompt tokens, more than the honest champion
spends (687k).

All three must pass. Breadth without depth is padding; depth without breadth is
a single stuffed request; volume without substance is probing.

Calibration
-----------

Measured against all 436 scored ``bench_version=7`` runs in production, joining
each score to the grant bound to its lease:

===========================  ==========  ===========  ==========  ==========
metric                       honest min  honest p01   honest p50   threshold
===========================  ==========  ===========  ==========  ==========
prompt tokens / case               2086        2548        5984         200
model calls / case                1.379       1.404          ~2        0.25
prompt tokens / call               1409        1693        3150         300
===========================  ==========  ===========  ==========  ==========

The honest minimum on every axis is the reigning champion, kabaw-v49. The
retrieval-only family sits at **0.26** prompt tokens per case, **0.0036** calls
per case, and **74** prompt tokens per call.

The thresholds below are set an order of magnitude *below* the lowest honest
run and two to three orders of magnitude *above* the retrieval-only family.
That asymmetry is deliberate: zeroing an honest miner is far worse than missing
an abuser, so the gate sits nowhere near the honest floor.

What this catches, and what it does not
---------------------------------------

Catches:

* answering every case from retrieval with a token-probe call (the observed
  case);
* padding with many cheap or empty calls -- they raise calls-per-case and, in
  bulk, prompt-tokens-per-case, but never prompt-tokens-per-call;
* one stuffed mega-call -- it raises tokens but not calls-per-case;
* skipping the model entirely.

Does **not** catch:

* an agent that sends genuinely case-sized prompts and then ignores the
  responses. It pays full inference cost for no benefit, so this is
  economically irrational rather than a cheap exploit -- but it is not
  detected here, and closing it needs response-conditioned evidence (see
  ditto-platform#518, trace publication).
* *partial* substitution -- using the model on some fraction of cases and
  retrieval on the rest. If the model-answered fraction is large enough, the
  averages still clear all three gates. This rule measures whether the model
  was used, not whether it was used on every case.

Neither gap is closed by tightening the numbers; both need a different kind of
evidence, and tightening toward the honest floor trades a real risk of zeroing
an honest miner for a marginal gain against a hypothetical one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from ditto.api_models.model_use import ModelUseVerdict
from ditto.db.queries.inference import LeaseModelUsage

MIN_PROMPT_TOKENS_PER_CASE = 200.0
"""Prompt tokens per graded case below which a run has not engaged the model.

10.4x below the lowest honest run observed (2086), 770x above the
retrieval-only family (0.26).
"""

MIN_CALLS_PER_CASE = 0.25
"""Model calls per graded case below which the run was not case-by-case.

5.5x below the lowest honest run observed (1.379), 69x above the
retrieval-only family (0.0036). Set well under 1.0 on purpose: an agent that
legitimately batches a handful of cases per request is doing real work and must
not be punished for efficiency.
"""

MIN_PROMPT_TOKENS_PER_CALL = 300.0
"""Average prompt size below which the calls are probes, not questions.

This is the gate that makes padding *unprofitable* rather than merely
detectable, and it is here because the first draft of this rule was defeated in
test: 2800 calls of 74 tokens across 280 cases yields 740 prompt tokens per
case, clearing a per-case-only gate at roughly a third of honest cost.

Scaling the number of cheap calls raises the per-case total but never the
per-call average, so this axis cannot be padded at all -- it can only be met by
sending bigger prompts. Combined with the calls-per-case floor it means any
run with N calls must carry at least ``300 x N`` prompt tokens, so the more a
miner pads, the more it pays. At the 2800-call shape that is 840k prompt
tokens, which exceeds what the honest champion spends (687k). Padding is
therefore strictly worse than doing the work.

4.7x below the lowest honest run observed (1409), 4x above the retrieval-only
family's 74-token probe.
"""


class ModelUseMode(StrEnum):
    """How far the rule is allowed to act.

    Follows the shadow-before-enforce precedent that ditto-platform#506
    invariant 5 requires of every new gate.
    """

    OFF = "off"
    """Do not evaluate. Usage is still measured and published."""

    SHADOW = "shadow"
    """Evaluate and publish the verdict; never change a score. **Default.**"""

    ENFORCE = "enforce"
    """Evaluate, publish, and zero a ``NOT_USED`` run."""


@dataclass(frozen=True)
class ModelUsePolicy:
    """Thresholds and enforcement level for the model-use rule."""

    mode: ModelUseMode = ModelUseMode.SHADOW
    min_prompt_tokens_per_case: float = MIN_PROMPT_TOKENS_PER_CASE
    min_calls_per_case: float = MIN_CALLS_PER_CASE
    min_prompt_tokens_per_call: float = MIN_PROMPT_TOKENS_PER_CALL


def parse_model_use_policy_from_env() -> ModelUsePolicy:
    """Build the policy from the environment.

    Defaults to ``SHADOW``. Enforcement is an explicit operator action, because
    the threshold must be published before it bites -- see ``mode``.
    """
    raw = os.getenv("DITTO_MODEL_USE_MODE", ModelUseMode.SHADOW.value).strip().lower()
    try:
        mode = ModelUseMode(raw)
    except ValueError:
        mode = ModelUseMode.SHADOW
    return ModelUsePolicy(mode=mode)


@lru_cache(maxsize=1)
def model_use_policy() -> ModelUsePolicy:
    """The process-wide policy, parsed once."""
    return parse_model_use_policy_from_env()


@dataclass(frozen=True)
class ModelUseAssessment:
    """The verdict for one run, with the numbers that produced it.

    Carries its own inputs so the public surface can show a miner *why* without
    re-deriving anything, and so the reason survives in the audit trail.
    """

    verdict: ModelUseVerdict
    calls: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cases: int | None = None
    prompt_tokens_per_case: float | None = None
    calls_per_case: float | None = None
    prompt_tokens_per_call: float | None = None
    reason: str | None = None

    @property
    def zeroes_score(self) -> bool:
        return self.verdict is ModelUseVerdict.NOT_USED

    def as_public_dict(self) -> dict[str, object]:
        """The miner-facing projection.

        Deliberately only ratios, counts and the verdict. No prompts, no
        completions, no case content, no source -- nothing here can be
        inverted toward another miner's implementation.
        """
        return {
            "verdict": self.verdict.value,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cases": self.cases,
            "prompt_tokens_per_case": (
                None
                if self.prompt_tokens_per_case is None
                else round(self.prompt_tokens_per_case, 2)
            ),
            "calls_per_case": (
                None if self.calls_per_case is None else round(self.calls_per_case, 4)
            ),
            "prompt_tokens_per_call": (
                None
                if self.prompt_tokens_per_call is None
                else round(self.prompt_tokens_per_call, 2)
            ),
            "reason": self.reason,
            "min_prompt_tokens_per_case": MIN_PROMPT_TOKENS_PER_CASE,
            "min_calls_per_case": MIN_CALLS_PER_CASE,
            "min_prompt_tokens_per_call": MIN_PROMPT_TOKENS_PER_CALL,
        }


def model_use_factor(verdict: ModelUseVerdict | None, *, mode: ModelUseMode) -> float:
    """Ranking multiplier for ``verdict``: ``0.0`` zeroes the run.

    Shaped as a multiplier so it composes with the token-efficiency bonus
    (``ditto/api_server/efficiency.py``), which is the established way this
    codebase adjusts a score at ranking time without ever touching the
    composite the validator signed. Chain of custody survives intact: the
    stored composite and its signature stay verbatim, and the platform's
    decision is a separate, separately-auditable fact.

    Only ``ENFORCE`` can return ``0.0``. In ``SHADOW`` the verdict is computed
    and published but the multiplier is always ``1.0``, so the blast radius is
    measurable before anything is withheld.
    """
    if mode is not ModelUseMode.ENFORCE:
        return 1.0
    return 0.0 if verdict is ModelUseVerdict.NOT_USED else 1.0


def evaluate_model_use(
    usage: LeaseModelUsage | None,
    *,
    cases: int,
    policy: ModelUsePolicy,
) -> ModelUseAssessment:
    """Decide whether ``usage`` shows real model use across ``cases`` cases.

    Returns ``UNMEASURED`` -- never a penalty -- when the rule is off, when no
    grant was bound to the lease, or when the run graded no cases. Absence of
    evidence is not evidence of abuse, and every one of those three has an
    innocent cause.
    """
    if policy.mode is ModelUseMode.OFF:
        return ModelUseAssessment(
            verdict=ModelUseVerdict.UNMEASURED, reason="model-use rule disabled"
        )
    if usage is None:
        return ModelUseAssessment(
            verdict=ModelUseVerdict.UNMEASURED,
            reason="no metered inference grant for this lease",
        )
    if cases <= 0:
        return ModelUseAssessment(
            verdict=ModelUseVerdict.UNMEASURED,
            calls=usage.chat_calls,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cases=cases,
            reason="run graded no cases",
        )

    prompt_per_case = usage.prompt_tokens / cases
    calls_per_case = usage.chat_calls / cases
    prompt_per_call = (
        usage.prompt_tokens / usage.chat_calls if usage.chat_calls else 0.0
    )
    failures: list[str] = []
    if prompt_per_case < policy.min_prompt_tokens_per_case:
        failures.append(
            f"{prompt_per_case:.1f} prompt tokens/case "
            f"(minimum {policy.min_prompt_tokens_per_case:.0f})"
        )
    if calls_per_case < policy.min_calls_per_case:
        failures.append(
            f"{calls_per_case:.3f} model calls/case "
            f"(minimum {policy.min_calls_per_case:.2f})"
        )
    if prompt_per_call < policy.min_prompt_tokens_per_call:
        failures.append(
            f"{prompt_per_call:.1f} prompt tokens/call "
            f"(minimum {policy.min_prompt_tokens_per_call:.0f})"
        )

    return ModelUseAssessment(
        verdict=ModelUseVerdict.NOT_USED if failures else ModelUseVerdict.USED,
        calls=usage.chat_calls,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cases=cases,
        prompt_tokens_per_case=prompt_per_case,
        calls_per_case=calls_per_case,
        prompt_tokens_per_call=prompt_per_call,
        reason="; ".join(failures) if failures else None,
    )
