"""Uncertainty handoff must survive malformed provisional policy assessments."""

import pytest

from ditto_screener.fanout_review import (
    _adjudication_tools,
    _normalize_obligation_resolutions,
    _review_obligations,
    _specialist_invariant_shapes_complete,
)
from ditto_screener.source_review import TarSourceRepository
from ditto_screening_protocol.models import source_review_invariants_for_policy

from .test_source_review import _archive_files


def test_inconclusive_invalid_pass_clause_and_locationless_note_survive(tmp_path):
    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/main.rs": b"one\ntwo\n"}))
    )
    rows = [
        {
            "name": "benchmark_engine",
            "raw_review": {
                "risk_level": "low",
                "evidence": [{"path": "src/main.rs", "line": 1}],
                "invariants": [
                    {
                        "invariant": "I4",
                        "disposition": "inconclusive",
                        "pass_clause": "invalid",
                        "evidence_indices": [0],
                        "summary": "Writer trace remains unresolved.",
                    }
                ],
            },
            "notes": [{"kind": "concern", "summary": "Investigate output provenance."}],
        }
    ]
    obligations = _review_obligations(rows, repo)
    assert len(obligations) == 2
    assert obligations[0]["locations"] == [{"path": "src/main.rs", "line": 1}]
    assert obligations[1]["locations"] == []
    schema = _adjudication_tools(13, [], final_turn=True, obligations=obligations)[0][
        "function"
    ]["parameters"]
    assert schema["properties"]["obligation_resolutions"]["required"] == [
        "obligation-001",
        "obligation-002",
    ]
    assert not _specialist_invariant_shapes_complete(rows, 13)
    resolution = {
        "disposition": "resolved",
        "summary": "Traced both locations.",
        "source_evidence": [
            {"path": "src/main.rs", "line": 1},
            {"path": "src/main.rs", "line": 2},
        ],
    }
    payload = {
        "obligation_resolutions": {o["obligation_id"]: resolution for o in obligations}
    }
    with pytest.raises(ValueError, match="not read"):
        _normalize_obligation_resolutions(payload, obligations, repo, set())
    assert (
        len(
            _normalize_obligation_resolutions(
                payload, obligations, repo, {("src/main.rs", 1), ("src/main.rs", 2)}
            )
        )
        == 2
    )
    del payload["obligation_resolutions"]["obligation-002"]
    with pytest.raises(ValueError, match="every exact"):
        _normalize_obligation_resolutions(payload, obligations, repo, set())


def test_malformed_and_duplicate_invariant_sets_cannot_clear():
    rows = [
        {
            "raw_review": {
                "invariants": [
                    {"invariant": i.value, "disposition": "pass"}
                    for i in source_review_invariants_for_policy(13)
                ]
            }
        }
    ]
    assert _specialist_invariant_shapes_complete(rows, 13)
    rows[0]["raw_review"]["invariants"][-1]["invariant"] = "I1"
    assert not _specialist_invariant_shapes_complete(rows, 13)
    assert not _specialist_invariant_shapes_complete([{"raw_review": {}}], 13)
    assert _specialist_invariant_shapes_complete([{"raw_review": {}}], 12)


def test_obligation_overflow_fails_closed(tmp_path):
    repo = TarSourceRepository(str(_archive_files(tmp_path, {"src/main.rs": b"one\n"})))
    with pytest.raises(Exception, match="obligation limit"):
        _review_obligations(
            [{"notes": [{"kind": "concern", "summary": "Investigate"}] * 65}], repo
        )


@pytest.mark.parametrize("failure", ["unresolved", "malformed", "unread", None])
def test_low_risk_cannot_bypass_obligation_or_shape_gate(tmp_path, failure):
    from ditto_screener.fanout_review import _normalize_final_adjudication

    from .test_fanout_candidate_contract import _keyed_review

    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/main.rs": b"one\ntwo\n"}))
    )
    obligation = {
        "obligation_id": "obligation-001",
        "locations": [{"path": "src/main.rs", "line": 1}],
    }
    payload = {
        "final_review": _keyed_review(),
        "candidate_assessments": {},
        "obligation_resolutions": {
            "obligation-001": {
                "disposition": "unresolved" if failure == "unresolved" else "resolved",
                "summary": "Traced source writer.",
                "source_evidence": [{"path": "src/main.rs", "line": 1}],
            }
        },
    }

    def normalize():
        return _normalize_final_adjudication(
            payload,
            artifact_sha256="a" * 64,
            policy_version=13,
            candidates=[],
            repository=repo,
            opened_lines=set() if failure == "unread" else {("src/main.rs", 1)},
            clearance_certified=True,
            obligations=[obligation],
            specialist_invariant_shapes_complete=failure != "malformed",
        )

    if failure:
        with pytest.raises(ValueError):
            normalize()
    else:
        result = normalize()
        assert result["outcome"] == "no_findings"
        assert result["obligation_evidence_verified"] is True
        assert isinstance(
            result["final_review"]["invariant_assessment"]["decisions"], list
        )
