"""Versioned benchmark prerequisites shared by ticket and artifact APIs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkContract:
    version: int
    minimum_screening_policy_version: int
    requires_screened_image: bool


_CONTRACTS = {
    # v2 predates screened images and must remain source-build compatible for
    # validators which do not update during the rolling v3 activation.
    2: BenchmarkContract(2, 1, False),
    # A v3 dataset is only released after a policy-9 screener has produced an
    # archive whose complete bytes were verified by the platform.
    3: BenchmarkContract(3, 9, True),
    # v4 supersedes v3 without relaxing any prerequisite: same policy-9 screener
    # floor and the same verified-archive requirement.
    4: BenchmarkContract(4, 9, True),
    # v5 adds chat-quality and trusted token-efficiency scoring. Shipping the
    # contract makes it a rollout target but does not activate it; the scorer
    # also withholds v5 capability until every provider baseline is calibrated.
    5: BenchmarkContract(5, 9, True),
    # v6 adds memory-as-data (stored-instruction) plus the multi-query,
    # non-verbatim, and passive-consolidation complexity cases. Same policy-9
    # screener floor and verified-archive requirement as v5; shipping the
    # contract makes it an operator rollout target without activating it.
    6: BenchmarkContract(6, 9, True),
    # v7 changes the consensus inference model to OpenRouter-served GPT-OSS
    # 20B and rotates the generated surface. Capability advertisement remains
    # separately gated on reviewed provider-specific starter-kit baselines.
    7: BenchmarkContract(7, 9, True),
    # v8 is the urgent difficulty bump on the existing v7 infrastructure. It
    # keeps the policy-9 screener floor and verified archive requirement. The
    # deeper-history redesign is reserved for v9 rather than rushed into this
    # contract. Shipping this entry exposes an operator target; it does not
    # open or activate a rollout.
    8: BenchmarkContract(8, 9, True),
}


def benchmark_contract(version: int) -> BenchmarkContract:
    """Return the immutable contract for ``version``; unknown versions fail closed."""
    try:
        return _CONTRACTS[version]
    except KeyError as exc:
        raise ValueError(f"unsupported benchmark version: {version}") from exc


def benchmark_contracts() -> tuple[BenchmarkContract, ...]:
    """Return every shipped contract in stable version order.

    Shipping code makes a contract *available*; it does not activate or open a
    rollout. The authenticated operator control uses this registry for target
    discovery so future benchmark bumps do not require another API hardcode.
    """
    return tuple(_CONTRACTS[version] for version in sorted(_CONTRACTS))


def latest_benchmark_contract() -> BenchmarkContract:
    """Return the newest contract shipped by this platform release."""
    return _CONTRACTS[max(_CONTRACTS)]
