"""The Python generator score must reproduce the Go reference bit for bit.

``challenge/testdata/score_vectors.json`` is written by the Go contract's
``TestScoreVectorsGolden``; every vector here is replayed through
:func:`ditto.generator_scoring.score_cases` and every field compared exactly.
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
    score_cases,
)

_VECTORS = (
    Path(__file__).resolve().parents[4]
    / "research/dittobench-datagen/challenge/testdata/score_vectors.json"
)


def _load() -> list[dict[str, Any]]:
    return json.loads(_VECTORS.read_text())


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
    ],
)
def test_rejects_unscorable_input(
    panel: list[PanelMember], params: ScoreParams
) -> None:
    with pytest.raises(ScoreInputError):
        score_cases([CaseRef("c", "0" * 64)], panel, params)
