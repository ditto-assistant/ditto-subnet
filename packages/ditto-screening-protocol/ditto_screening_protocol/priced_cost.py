"""Bench v16+ priced inference cost factor (``details.priced_cost``).

Python mirror of ``services/dittobench-api/internal/scoregates/priced_cost.go``
(vectors: ``internal/scoregates/testdata/priced_cost_vectors.json``). From
:data:`PRICED_COST_MIN_BENCH_VERSION` the scorer prices the WHOLE run at the
contract's locked public per-token rates and multiplies the composite by a
steep factor of the priced cost per case::

    cost       = 70 x prompt + 150 x completion + 4 x embedding
                 + 42 x decision_input + 0 x decision_output    (nano-USD)
    normalized = floor(cost x 350 / cases)       (a 350-case run equivalent)
    ratio_bps  = min(floor(normalized x 10000 / reference), 100000)
    factor_bps = piecewise-linear(curve, ratio_bps)

v16 uses priced-cost-v1: reference $1.00, floor 0.6, curve 10000@1x/8000@1.5x/6000@2x.
v17+ uses priced-cost-v2: reference $0.50, floor 0.3, steeper curve
10000@1x/6500@1.5x/3000@2x. Every step is unsigned integer arithmetic with floor
division and uint64 saturation, so this module reproduces the Go block bit
for bit from the token counts the report already carries.

The applied factor rides inside ``details.dueling.datagen_gate`` on a dueling
run, so :func:`ditto_screening_protocol.dueling.dueling_composite` needs no
change; this module lets Platform and the validator recompute and display it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PRICED_COST_MIN_BENCH_VERSION = 16
"""First bench version whose composite carries the priced cost factor."""

PRICED_COST_V2_MIN_BENCH_VERSION = 17
"""First bench version that uses the priced-cost-v2 contract ($0.50 reference, 0.3 floor)."""

PRICED_COST_CONTRACT_ID = "priced-cost-v1"
PRICED_COST_V2_CONTRACT_ID = "priced-cost-v2"

PRICED_COST_CHAT_MODEL = "openai/gpt-oss-20b"
PRICED_COST_NANO_USD_PER_PROMPT_TOKEN = 70
PRICED_COST_NANO_USD_PER_COMPLETION_TOKEN = 150
PRICED_COST_EMBEDDING_MODEL = "perplexity/pplx-embed-v1-0.6b"
PRICED_COST_NANO_USD_PER_EMBEDDING_TOKEN = 4
PRICED_COST_DECISION_MODEL = "typesafe/jev-1.14"
PRICED_COST_NANO_USD_PER_DECISION_INPUT_TOKEN = 42
PRICED_COST_NANO_USD_PER_DECISION_OUTPUT_TOKEN = 0

PRICED_COST_NORMALIZATION_CASES = 350
PRICED_COST_REFERENCE_NANO_USD = 1_000_000_000
PRICED_COST_FLOOR_BPS = 6000
# v2 contract constants (bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION)
PRICED_COST_V2_REFERENCE_NANO_USD = 500_000_000
PRICED_COST_V2_FLOOR_BPS = 3000
PRICED_COST_MAX_RATIO_BPS = 100_000
BASIS_POINT_SCALE = 10_000

PRICED_COST_CURVE: tuple[tuple[int, int], ...] = (
    (10_000, 10_000),
    (15_000, 8_000),
    (20_000, PRICED_COST_FLOOR_BPS),
)
"""``(ratio_bps, factor_bps)`` breakpoints for priced-cost-v1, ratio ascending."""

PRICED_COST_V2_CURVE: tuple[tuple[int, int], ...] = (
    (10_000, 10_000),
    (15_000, 6_500),
    (20_000, PRICED_COST_V2_FLOOR_BPS),
)
"""``(ratio_bps, factor_bps)`` breakpoints for priced-cost-v2, ratio ascending."""

_UINT64_MAX = (1 << 64) - 1
_MAX_USAGE_COUNT = 9_007_199_254_740_991

PricedCostPosture = Literal["enforce", "shadow"]
PricedCostResult = Literal["priced", "insufficient_evidence"]


def is_priced_cost_bench_version(bench_version: int) -> bool:
    """Whether ``bench_version`` prices its run (a floor, never a set)."""
    return bench_version >= PRICED_COST_MIN_BENCH_VERSION


def _clamp(tokens: int) -> int:
    return min(max(tokens, 0), _MAX_USAGE_COUNT)


def _sat(value: int) -> int:
    return min(value, _UINT64_MAX)


def priced_cost_factor_bps_for_curve(
    ratio_bps: int, curve: tuple[tuple[int, int], ...]
) -> int:
    """Map a ratio (normalized cost over the reference, bps) through any curve."""
    first_ratio, first_factor = curve[0]
    if ratio_bps <= first_ratio:
        return first_factor
    for (lo_r, lo_f), (hi_r, hi_f) in zip(curve, curve[1:], strict=False):
        if ratio_bps > hi_r:
            continue
        drop = (lo_f - hi_f) * (ratio_bps - lo_r) // (hi_r - lo_r)
        return lo_f - drop
    return curve[-1][1]


def priced_cost_factor_bps(ratio_bps: int) -> int:
    """Map a ratio (normalized cost over the reference, bps) through the v1 curve."""
    return priced_cost_factor_bps_for_curve(ratio_bps, PRICED_COST_CURVE)


def _priced_cost_contract_id_for_version(bench_version: int) -> str:
    if bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION:
        return PRICED_COST_V2_CONTRACT_ID
    return PRICED_COST_CONTRACT_ID


def _priced_cost_reference_for_version(bench_version: int) -> int:
    if bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION:
        return PRICED_COST_V2_REFERENCE_NANO_USD
    return PRICED_COST_REFERENCE_NANO_USD


def _priced_cost_floor_for_version(bench_version: int) -> int:
    if bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION:
        return PRICED_COST_V2_FLOOR_BPS
    return PRICED_COST_FLOOR_BPS


def _priced_cost_curve_for_version(
    bench_version: int,
) -> tuple[tuple[int, int], ...]:
    if bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION:
        return PRICED_COST_V2_CURVE
    return PRICED_COST_CURVE


def build_priced_cost(
    *,
    bench_version: int,
    posture: str,
    cases: int,
    chat_complete: bool,
    source: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    embedding_tokens: int = 0,
    decision_calls: int = 0,
    decision_input_tokens: int = 0,
    decision_output_tokens: int = 0,
) -> dict[str, Any] | None:
    """The ``details.priced_cost`` block ``BuildPricedCost`` reports, or ``None``
    below :data:`PRICED_COST_MIN_BENCH_VERSION`.

    bench_version >= 17 uses priced-cost-v2 ($0.50 reference, 0.3 floor, steeper
    curve); bench_version 16 uses priced-cost-v1 ($1.00 reference, 0.6 floor).
    """
    if not is_priced_cost_bench_version(bench_version):
        return None
    applied_posture: PricedCostPosture = (
        "shadow" if posture.strip().lower() == "shadow" else "enforce"
    )
    contract_id = _priced_cost_contract_id_for_version(bench_version)
    reference = _priced_cost_reference_for_version(bench_version)
    floor_bps = _priced_cost_floor_for_version(bench_version)
    curve = _priced_cost_curve_for_version(bench_version)
    prompt = _clamp(prompt_tokens)
    completion = _clamp(completion_tokens)
    embedding = _clamp(embedding_tokens)
    decision_in = _clamp(decision_input_tokens)
    decision_out = _clamp(decision_output_tokens)
    prompt_cost = _sat(prompt * PRICED_COST_NANO_USD_PER_PROMPT_TOKEN)
    completion_cost = _sat(completion * PRICED_COST_NANO_USD_PER_COMPLETION_TOKEN)
    embedding_cost = _sat(embedding * PRICED_COST_NANO_USD_PER_EMBEDDING_TOKEN)
    decision_cost = _sat(
        _sat(decision_in * PRICED_COST_NANO_USD_PER_DECISION_INPUT_TOKEN)
        + _sat(decision_out * PRICED_COST_NANO_USD_PER_DECISION_OUTPUT_TOKEN)
    )
    cost = _sat(
        _sat(prompt_cost + completion_cost) + _sat(embedding_cost + decision_cost)
    )
    block: dict[str, Any] = {
        "contract_id": contract_id,
        "posture": applied_posture,
        "applied": False,
        "result": "insufficient_evidence",
        "window": "whole_run",
        "source": source,
        "cases": cases,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "embedding_tokens": embedding,
        "decision_calls": decision_calls,
        "decision_input_tokens": decision_in,
        "decision_output_tokens": decision_out,
        "prices": priced_cost_prices(),
        "prompt_cost_nano_usd": prompt_cost,
        "completion_cost_nano_usd": completion_cost,
        "embedding_cost_nano_usd": embedding_cost,
        "decision_cost_nano_usd": decision_cost,
        "cost_nano_usd": cost,
        "normalization_cases": PRICED_COST_NORMALIZATION_CASES,
        "normalized_cost_nano_usd": 0,
        "reference_nano_usd": reference,
        "ratio_bps": 0,
        "curve": [{"ratio_bps": r, "factor_bps": f} for r, f in curve],
        "floor_bps": floor_bps,
        "factor_bps": BASIS_POINT_SCALE,
        "applied_factor_bps": BASIS_POINT_SCALE,
    }
    if not chat_complete or cases <= 0:
        return block
    normalized = _sat(cost * PRICED_COST_NORMALIZATION_CASES) // cases
    ratio = min(
        _sat(normalized * BASIS_POINT_SCALE) // reference,
        PRICED_COST_MAX_RATIO_BPS,
    )
    factor = priced_cost_factor_bps_for_curve(ratio, curve)
    block["result"] = "priced"
    block["normalized_cost_nano_usd"] = normalized
    block["ratio_bps"] = ratio
    block["factor_bps"] = factor
    if applied_posture == "enforce":
        block["applied"] = True
        block["applied_factor_bps"] = factor
    return block


def priced_cost_prices() -> dict[str, Any]:
    """The locked rates as published in the report (nano-USD per token)."""
    return {
        "chat_model": PRICED_COST_CHAT_MODEL,
        "prompt_token": PRICED_COST_NANO_USD_PER_PROMPT_TOKEN,
        "completion_token": PRICED_COST_NANO_USD_PER_COMPLETION_TOKEN,
        "embedding_model": PRICED_COST_EMBEDDING_MODEL,
        "embedding_token": PRICED_COST_NANO_USD_PER_EMBEDDING_TOKEN,
        "decision_model": PRICED_COST_DECISION_MODEL,
        "decision_input_token": PRICED_COST_NANO_USD_PER_DECISION_INPUT_TOKEN,
        "decision_output_token": PRICED_COST_NANO_USD_PER_DECISION_OUTPUT_TOKEN,
        "completion_includes_reasoning": True,
    }


_Count = Annotated[int, Field(ge=0)]
_BPS = Annotated[int, Field(ge=0, le=BASIS_POINT_SCALE)]


class PricedCostReport(BaseModel):
    """The ``details.priced_cost`` wire shape (``protocol.PricedCostSummary``).

    Lenient on unknown keys so a scorer that grows a field does not drop the
    block; :meth:`recomputed` re-derives the factor from the block's own token
    counts under the published constants.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    contract_id: str
    posture: PricedCostPosture
    applied: bool
    result: PricedCostResult
    window: str
    source: str = ""
    cases: _Count
    prompt_tokens: _Count = 0
    completion_tokens: _Count = 0
    embedding_tokens: _Count = 0
    decision_calls: _Count = 0
    decision_input_tokens: _Count = 0
    decision_output_tokens: _Count = 0
    prices: dict[str, Any] | None = None
    prompt_cost_nano_usd: _Count = 0
    completion_cost_nano_usd: _Count = 0
    embedding_cost_nano_usd: _Count = 0
    decision_cost_nano_usd: _Count = 0
    cost_nano_usd: _Count = 0
    normalization_cases: _Count = PRICED_COST_NORMALIZATION_CASES
    normalized_cost_nano_usd: _Count = 0
    reference_nano_usd: _Count = PRICED_COST_REFERENCE_NANO_USD
    curve: list[dict[str, int]] | None = None
    ratio_bps: Annotated[int, Field(ge=0, le=PRICED_COST_MAX_RATIO_BPS)] = 0
    floor_bps: _BPS = PRICED_COST_FLOOR_BPS
    factor_bps: _BPS
    applied_factor_bps: _BPS

    def recomputed(self, bench_version: int) -> dict[str, Any] | None:
        """The block the published rule produces for these inputs."""
        return build_priced_cost(
            bench_version=bench_version,
            posture=self.posture,
            cases=self.cases,
            chat_complete=self.result == "priced",
            source=self.source,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            embedding_tokens=self.embedding_tokens,
            decision_calls=self.decision_calls,
            decision_input_tokens=self.decision_input_tokens,
            decision_output_tokens=self.decision_output_tokens,
        )

    def matches_published_rule(self, bench_version: int) -> bool:
        """Whether the reported factor is the one the published rule gives."""
        want = self.recomputed(bench_version)
        if want is None:
            return False
        return (
            self.contract_id == want["contract_id"]
            and self.window == want["window"]
            and self.result == want["result"]
            and self.prompt_tokens == want["prompt_tokens"]
            and self.completion_tokens == want["completion_tokens"]
            and self.embedding_tokens == want["embedding_tokens"]
            and self.decision_input_tokens == want["decision_input_tokens"]
            and self.decision_output_tokens == want["decision_output_tokens"]
            and self.prices == want["prices"]
            and self.curve == want["curve"]
            and self.normalization_cases == want["normalization_cases"]
            and self.reference_nano_usd == want["reference_nano_usd"]
            and self.floor_bps == want["floor_bps"]
            and self.prompt_cost_nano_usd == want["prompt_cost_nano_usd"]
            and self.completion_cost_nano_usd == want["completion_cost_nano_usd"]
            and self.embedding_cost_nano_usd == want["embedding_cost_nano_usd"]
            and self.decision_cost_nano_usd == want["decision_cost_nano_usd"]
            and self.factor_bps == want["factor_bps"]
            and self.applied_factor_bps == want["applied_factor_bps"]
            and self.applied == want["applied"]
            and self.cost_nano_usd == want["cost_nano_usd"]
            and self.normalized_cost_nano_usd == want["normalized_cost_nano_usd"]
            and self.ratio_bps == want["ratio_bps"]
        )
