"""The Python generator score must reproduce the Go reference bit for bit.

``challenge/testdata/score_vectors.json`` and ``run_vectors.json`` are written
by the Go contract's ``TestScoreVectorsGolden`` and ``TestRunVectorsGolden``;
every vector here is replayed through :func:`ditto.generator_scoring.score_cases`
or :func:`~ditto.generator_scoring.score_run` and every field compared exactly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ditto.generator_scoring import (
    CaseRef,
    CaseResult,
    PanelMember,
    ScoreInputError,
    ScoreParams,
    SeedPanel,
    score_cases,
    score_run,
)

_VECTORS = (
    Path(__file__).resolve().parents[4]
    / "research/dittobench-datagen/challenge/testdata/score_vectors.json"
)


_RUN_VECTORS = _VECTORS.with_name("run_vectors.json")


def _load() -> list[dict[str, Any]]:
    return json.loads(_VECTORS.read_text())


def _load_runs() -> list[dict[str, Any]]:
    return json.loads(_RUN_VECTORS.read_text())


def _panel(raw: list[dict[str, Any]] | None) -> list[PanelMember]:
    return [
        PanelMember(
            agent_id=m["agent_id"],
            owner_key=m["owner_key"],
            champion=bool(m.get("champion", False)),
            results=tuple(
                CaseResult(r["case_id"], float(r["score"]), bool(r.get("fault", False)))
                for r in m.get("results") or ()
            ),
        )
        for m in raw or ()
    ]


@pytest.mark.parametrize("vector", _load(), ids=lambda v: v["name"])
def test_python_port_matches_go_golden_vector(vector: dict[str, Any]) -> None:
    got = score_cases(
        [CaseRef(c["case_id"], c["fingerprint"]) for c in vector["cases"]],
        _panel(vector["input"]["panel"]),
        ScoreParams.from_mapping(vector["params"]),
        generator_owner_key=vector["input"].get("generator_owner_key", ""),
        claimed_fingerprints=vector["input"].get("claimed_fingerprints") or (),
    )
    want = vector["expected"]
    for key in (
        "status",
        "score",
        "raw_credit",
        "discriminating",
        "trivial",
        "unsolved",
        "duplicate",
        "unsolved_rate",
    ):
        assert getattr(got, key) == want[key], key
    assert len(got.cases) == len(want["cases"])
    for case, expected in zip(got.cases, want["cases"], strict=True):
        assert case.__dict__ == expected


def test_vectors_cover_every_outcome() -> None:
    seen = {c["status"] for v in _load() for c in v["expected"]["cases"]}
    assert seen == {
        "discriminating",
        "trivial",
        "unsolved",
        "duplicate",
        "insufficient_panel",
    }


@pytest.mark.parametrize(
    ("panel", "params"),
    [
        ([], ScoreParams(min_panel=1)),
        (
            [PanelMember("a", "o"), PanelMember("a", "p")],
            ScoreParams(),
        ),
        (
            [PanelMember("a", "o", results=(CaseResult("c", 1.5),))],
            ScoreParams(),
        ),
        (
            [PanelMember("a", "o", results=(CaseResult("c", float("nan")),))],
            ScoreParams(),
        ),
        ([PanelMember("a", "")], ScoreParams(min_panel=1)),
        (
            [
                PanelMember(
                    "a", "o", results=(CaseResult("c", 1.0), CaseResult("c", 0.0))
                )
            ],
            ScoreParams(min_panel=1),
        ),
    ],
)
def test_rejects_unscorable_input(
    panel: list[PanelMember], params: ScoreParams
) -> None:
    with pytest.raises(ScoreInputError):
        score_cases([CaseRef("c", "0" * 64)], panel, params)


@pytest.mark.parametrize("vector", _load_runs(), ids=lambda v: v["name"])
def test_python_run_score_matches_go_golden_vector(vector: dict[str, Any]) -> None:
    raw = vector["input"]
    got = score_run(
        [
            SeedPanel(
                seed=s["seed"],
                cases=[CaseRef(c["case_id"], c["fingerprint"]) for c in s["cases"]],
                panel=_panel(s["panel"]),
            )
            for s in raw["seeds"]
        ],
        ScoreParams.from_mapping(vector["params"]),
        generator_owner_key=raw.get("generator_owner_key", ""),
        claimed_fingerprints=raw.get("claimed_fingerprints") or (),
    )
    want = vector["expected"]
    assert got.status == want["status"]
    assert got.score == want["score"]
    assert len(got.seeds) == len(want["seeds"])
    for pack, expected in zip(got.seeds, want["seeds"], strict=True):
        assert pack.score == expected["score"]
        assert pack.duplicate == expected["duplicate"]
        assert [c.status for c in pack.cases] == [
            c["status"] for c in expected["cases"]
        ]
        assert [c.credit for c in pack.cases] == [
            c["credit"] for c in expected["cases"]
        ]


def test_run_score_rejects_empty_runs() -> None:
    with pytest.raises(ScoreInputError):
        score_run(
            [],
            ScoreParams.from_mapping(
                {
                    "min_panel": 3,
                    "min_solver_owners": 2,
                    "solve_threshold": 0.5,
                    "champion_bonus": 0.5,
                    "target_cases": 16,
                }
            ),
        )


def test_a_repeat_within_one_pack_earns_credit_once() -> None:
    fingerprint = "f" * 64
    panel = [
        PanelMember(
            agent,
            agent,
            results=(CaseResult("c1", score), CaseResult("c2", score)),
        )
        for agent, score in (("a", 1.0), ("b", 1.0), ("c", 0.0))
    ]
    got = score_cases(
        [CaseRef("c1", fingerprint), CaseRef("c2", fingerprint)],
        panel,
        ScoreParams(min_panel=3, min_solver_owners=2),
    )
    assert [c.status for c in got.cases] == ["discriminating", "duplicate"]
    assert got.discriminating == 1 and got.duplicate == 1


def test_one_case_id_cannot_name_two_fingerprints() -> None:
    with pytest.raises(ScoreInputError, match="two different fingerprints"):
        score_cases([CaseRef("c", "f1"), CaseRef("c", "f2")], [], ScoreParams())
