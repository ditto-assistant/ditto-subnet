from __future__ import annotations

import pytest
from cpm_scorer import metrics as m


def test_rank_ties_go_to_lexically_smaller_id():
    assert m.rank({"b": 1.0, "a": 1.0, "c": 2.0, "aa": 1.0}) == ["c", "a", "aa", "b"]


def test_pack_skips_and_continues():
    ranked = ["big", "small1", "huge", "small2"]
    counts = {"big": 1500, "small1": 400, "huge": 900, "small2": 148}
    # big (1500) + small1 (400) = 1900; huge does not fit and is skipped;
    # small2 (148) brings the total to exactly 2048.
    assert m.pack(ranked, counts, 2048) == ["big", "small1", "small2"]
    assert m.pack(["x"], {"x": 2049}, 2048) == []


def test_recall_and_distractor_share():
    assert m.recall(["a", "b"], ["a", "c"]) == 0.5
    assert m.recall([], ["a"]) == 0.0
    with pytest.raises(ValueError):
        m.recall(["a"], [])
    # d1 is a distractor; r1 carries a distractor span but is relevant.
    assert m.distractor_share(["r1", "d1", "x"], ["r1"], {"r1", "d1"}) == 1 / 3
    assert m.distractor_share([], ["r1"], {"d1"}) == 0.0


def test_distractor_ids_from_spans():
    spans = {
        "a": [{"type": "answer_value"}],
        "b": [{"type": "distractor"}],
        "c": [{"type": "answer_value"}, {"type": "forbidden"}],
    }
    assert m.distractor_ids(spans) == {"b", "c"}


def test_memory_case_packs_by_score_under_budget():
    scores = {"r": 5.0, "d": 4.0, "x": 3.0, "r2": 1.0}
    counts = {"r": 1000, "d": 900, "x": 200, "r2": 100}
    res = m.memory_case(
        "c", scores, counts, ["r", "r2"], {"d": [{"type": "distractor"}]}, 2048
    )
    # r, d (1900) packed; x (200) skipped; r2 (100) packed -> 2000 tokens
    assert res.recall == 1.0
    assert res.distractor_rate == pytest.approx(1 / 3)
    assert res.packed == 3 and res.packed_tokens == 2000


def test_tool_case_top_k():
    scores = {f"t{i}": float(i) for i in range(20)}
    res = m.tool_case("c", scores, ["t19", "t0"], k=8)
    assert res.recall == 0.5


def test_seed_score_and_aggregate():
    comps = {"memory_recall_at_b": 0.5, "distractor_rate": 0.2, "tool_recall_at_k": 1.0}
    assert m.seed_score(comps) == pytest.approx(0.6 * 0.5 * 0.9 + 0.4)
    best = {"memory_recall_at_b": 1.0, "distractor_rate": 0.0, "tool_recall_at_k": 1.0}
    assert m.seed_score(best) == pytest.approx(1.0)
    worst = {"memory_recall_at_b": 0.0, "distractor_rate": 1.0, "tool_recall_at_k": 0.0}
    assert m.seed_score(worst) == 0.0
    per_seed = [{**comps, "score": 0.67}, {**best, "score": 1.0}]
    agg = m.aggregate(per_seed)
    assert agg["score"] == pytest.approx(0.835)
    assert agg["distractor_rate"] == pytest.approx(0.1)


def test_seed_components_empty_kinds_are_zero():
    assert m.seed_components([], []) == {
        "memory_recall_at_b": 0.0,
        "distractor_rate": 0.0,
        "tool_recall_at_k": 0.0,
    }
