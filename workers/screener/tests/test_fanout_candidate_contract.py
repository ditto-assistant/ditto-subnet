"""Candidate IDs are server-owned bindings, not semantic model suggestions."""

import pytest

from ditto_screener.fanout_review import (
    _adjudication_tools,
    _normalize_candidate_adjudications,
)
from ditto_screener.source_review import TarSourceRepository

from .test_source_review import _archive_files


def _candidates():
    return [
        {
            "candidate_id": f"candidate-{i:03d}",
            "source_pass": "generalist",
            "finding": {
                "evidence": [
                    {
                        "path": "src/main.rs",
                        "line": i,
                        "category": "benchmark_emulation",
                    }
                ]
            },
        }
        for i in (1, 2)
    ]


def _assessment(disposition="unresolved"):
    return {
        "disposition": disposition,
        "supporting_evidence": [],
        "counterevidence": [],
        "summary": "Evidence remains unresolved.",
    }


def test_tool_contract_uses_exact_required_candidate_keys():
    schema = _adjudication_tools(
        13, ["candidate-001", "candidate-002"], final_turn=True
    )[0]["function"]["parameters"]["properties"]["candidate_assessments"]
    assert schema["type"] == "object"
    assert schema["required"] == ["candidate-001", "candidate-002"]
    assert set(schema["properties"]) == set(schema["required"])
    assert schema["additionalProperties"] is False
    assert "candidate_id" not in schema["properties"]["candidate-001"]["properties"]
    empty = _adjudication_tools(13, [], final_turn=True)[0]["function"]["parameters"][
        "properties"
    ]["candidate_assessments"]
    assert empty["properties"] == {} and empty["required"] == []


def test_keyed_assessments_bind_in_server_order_without_guessing(tmp_path):
    repository = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/main.rs": b"one\ntwo\n"}))
    )
    result = _normalize_candidate_adjudications(
        {
            "candidate_assessments": {
                "candidate-002": _assessment(),
                "candidate-001": _assessment("supported"),
            }
        },
        candidates=_candidates(),
        repository=repository,
        opened_lines=set(),
    )
    assert [row["candidate_id"] for row in result["candidate_assessments"]] == [
        "candidate-001",
        "candidate-002",
    ]
    # Binding alone never certifies support or bypasses original-source reads.
    assert [row["disposition"] for row in result["candidate_assessments"]] == [
        "unresolved",
        "unresolved",
    ]


@pytest.mark.parametrize(
    "submitted,diagnostic",
    [
        ({"candidate-001": _assessment()}, "missing_count=1"),
        (
            {
                "candidate-001": _assessment(),
                "candidate-002": _assessment(),
                "invented": _assessment(),
            },
            "unknown_count=1",
        ),
        (
            {
                "candidate-001": {**_assessment(), "candidate_id": "candidate-002"},
                "candidate-002": _assessment(),
            },
            "must not repeat",
        ),
        (
            [{**_assessment(), "candidate_id": "candidate-001"}] * 2,
            "duplicate candidate_id",
        ),
        (
            [{**_assessment(), "candidate_id": "arbitrary private text"}],
            "unknown candidate_id",
        ),
        ([_assessment()], "missing or not a string"),
    ],
)
def test_binding_errors_are_specific_and_do_not_echo_untrusted_ids(
    tmp_path, submitted, diagnostic
):
    repository = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/main.rs": b"one\ntwo\n"}))
    )
    with pytest.raises(ValueError, match=diagnostic) as error:
        _normalize_candidate_adjudications(
            {"candidate_assessments": submitted},
            candidates=_candidates(),
            repository=repository,
            opened_lines=set(),
        )
    assert "arbitrary private text" not in str(error.value)


def test_keyed_candidate_summary_bounds_preserve_evidence_and_full_text():
    import json
    from copy import deepcopy

    from ditto_screener.fanout_review import ExperimentalReviewer

    # The method only needs these presentation-audit fields; no inference occurs.
    reviewer = object.__new__(ExperimentalReviewer)
    reviewer._review_policy_version = 13
    reviewer.full_summaries = []
    assessment = {
        **_assessment("supported"),
        "summary": "x" * 300,
        "supporting_evidence": [
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"}
        ],
    }
    payload = {"candidate_assessments": {"candidate-001": deepcopy(assessment)}}
    message = {
        "tool_calls": [
            {
                "id": "bound",
                "type": "function",
                "function": {
                    "name": "submit_fanout_adjudication",
                    "arguments": json.dumps(payload),
                },
            }
        ]
    }
    bounded = reviewer._bound_summary_fields(message)
    actual = json.loads(bounded["tool_calls"][0]["function"]["arguments"])[
        "candidate_assessments"
    ]["candidate-001"]
    assert len(actual["summary"]) == 240
    assert {key: value for key, value in actual.items() if key != "summary"} == {
        key: value for key, value in assessment.items() if key != "summary"
    }
    assert (
        reviewer.full_summaries[0]["field"]
        == "candidate_assessments[candidate-001].summary"
    )
    assert reviewer.full_summaries[0]["original_chars"] == 300


@pytest.mark.parametrize("budget_failure", [True, False])
async def test_reports_preserve_only_bounded_internal_budget_reasons(
    tmp_path, budget_failure
):
    import hashlib

    from ditto_screener.fanout_review import FanoutBudgetExhausted, review_archive

    error = (
        FanoutBudgetExhausted("fanout token budget exhausted " + "x" * 200)
        if budget_failure
        else ValueError("private arbitrary error text")
    )

    class Reviewer:
        def __init__(self, **_kwargs):
            self.usage = {}
            self.response_models = set()
            self.opened_paths = set()

        async def review_provisional(self, *_args, **_kwargs):
            raise error

        async def adjudicate_review(self, *_args, **_kwargs):
            raise error

    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() {}"})
    report = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file="unused",
        partition="specialists",
        reviewer_factory=Reviewer,
    )
    for row in [*report["passes"], report["critic"]]:
        if budget_failure:
            assert row["budget_exhaustion_reason"] == str(error)[:160]
        else:
            assert row["budget_exhaustion_reason"] is None
        assert "private arbitrary error text" not in str(row)
