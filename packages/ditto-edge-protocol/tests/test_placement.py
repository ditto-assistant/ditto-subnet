from __future__ import annotations

import random

from ditto_edge_protocol.placement import Candidate, plan_assignments


def _c(
    name: str,
    *,
    size: int = 10,
    replicas: int = 0,
    operator: bool = False,
    created: float = 0.0,
) -> Candidate:
    return Candidate(
        digest=name * 64 if len(name) == 1 else name,
        size_bytes=size,
        replicas=replicas,
        operator_holds=operator,
        created_at=created,
    )


def _plan(candidates: list[Candidate], **overrides: object) -> list[str]:
    args: dict[str, object] = {
        "held": set(),
        "free_bytes": 1_000,
        "replicas_target": 3,
        "max_new": 10,
        "max_object_bytes": 100,
    }
    args.update(overrides)
    return plan_assignments(candidates=candidates, **args)  # type: ignore[arg-type]


def test_least_replicated_then_oldest_then_digest() -> None:
    candidates = [
        _c("a", replicas=2),
        _c("b", replicas=0, created=5.0),
        _c("c", replicas=0, created=1.0),
        _c("d", replicas=1),
    ]
    assert _plan(candidates) == ["c" * 64, "b" * 64, "d" * 64, "a" * 64]


def test_order_does_not_depend_on_input_order() -> None:
    candidates = [
        _c(ch, replicas=i % 3, created=float(i % 2)) for i, ch in enumerate("abcdef")
    ]
    expected = _plan(candidates)
    for seed in range(5):
        shuffled = candidates[:]
        random.Random(seed).shuffle(shuffled)
        assert _plan(shuffled) == expected


def test_skips_held_full_operator_and_oversized_objects() -> None:
    candidates = [
        _c("a"),
        _c("b", replicas=3),
        _c("c", operator=True),
        _c("d", size=101),
        _c("e"),
    ]
    assert _plan(candidates, held={"a" * 64}) == ["e" * 64]


def test_fills_quota_without_overflowing_it() -> None:
    candidates = [_c("a", size=60), _c("b", size=50), _c("c", size=40)]
    # a (60) fits; b (50) would overflow 100; c (40) still fits.
    assert _plan(candidates, free_bytes=100) == ["a" * 64, "c" * 64]


def test_respects_max_new_and_empty_budgets() -> None:
    candidates = [_c(ch) for ch in "abc"]
    assert _plan(candidates, max_new=2) == ["a" * 64, "b" * 64]
    assert _plan(candidates, max_new=0) == []
    assert _plan(candidates, free_bytes=0) == []
    assert _plan(candidates, replicas_target=0) == []
