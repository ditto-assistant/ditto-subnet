"""Non-string values in reviewer tool arguments (ditto-subnet#2987).

Reviewer tool schemas are not strict, so a model can answer with a list or a
dict where the contract names a string. Every one of these parsers tested the
value for set membership before it tested the type, so such an answer raised
``TypeError: unhashable type`` instead of the ``ValueError`` the review paths
handle. The exception escaped to the gate's catch-all, which parked the review
as ``unexpected-infrastructure`` (``OPERATOR_RETRY``) and lost the notes and
the finding: an honest submission waited for a manual retry, and a flagged
artifact got an infrastructure code instead of a hold. Source under review can
also try to steer the reviewer into that shape, so the parse must stay a
contract miss.
"""

from __future__ import annotations

import ast
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from ditto_screener.adjudicator import _verdict_from
from ditto_screener.l2_review import _parse_l2_review
from ditto_screener.source_review import (
    TarSourceRepository,
    _note_from_arguments,
    _parse_review,
)

NOT_A_STRING: tuple[object, ...] = (["concern"], {"kind": "concern"}, 7, None, True)


def _l1_value() -> dict[str, object]:
    return {
        "risk_level": "low",
        "confidence": 1.0,
        "categories": ["none"],
        "evidence": [],
        "invariants": [],
        "summary": "Nothing to report.",
    }


def _archive(tmp_path: Path) -> tuple[TarSourceRepository, str, str]:
    """One two-line member, with its artifact and member digests."""
    archive = tmp_path / "agent.tar.gz"
    payload = b"fn main() {}\nfn other() {}\n"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("src/main.rs")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    repository = TarSourceRepository(str(archive))
    member = repository.member_sha256("src/main.rs")
    assert member is not None
    return repository, hashlib.sha256(archive.read_bytes()).hexdigest(), member


def _l2_value(digest: str) -> dict[str, object]:
    """An inconclusive L2 submission, which settles before the invariant pass.

    Inconclusive still walks the evidence, causal-path and generator-component
    loops, so it reaches every membership test those loops make.
    """
    return {
        "disposition": "inconclusive",
        "risk_level": "low",
        "confidence": 0.5,
        "resolution_basis": "insufficient_static_evidence",
        "categories": ["none"],
        "analyzed_files": [{"path": "src/main.rs", "sha256": digest}],
        "evidence": [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "category": "none",
                "role": "trigger",
            }
        ],
        "causal_path": [{"path": "src/main.rs", "line": 1, "role": "trigger"}],
        "generator_components": [],
        "invariants": [],
        "summary": "Could not settle this on static evidence.",
    }


def test_is_allowed_refuses_every_non_string() -> None:
    """The guard is the fix: a non-string can never be a member.

    Imported here, not at module scope, so that every other test in this
    file still collects against a tree without the guard and fails with
    the TypeError it was written for.
    """
    from ditto_screener.source_review import _is_allowed

    allowed = frozenset({"concern"})
    assert _is_allowed("concern", allowed) is True
    assert _is_allowed("other", allowed) is False
    for value in NOT_A_STRING:
        assert _is_allowed(value, allowed) is False, value


@pytest.mark.parametrize("kind", NOT_A_STRING)
def test_an_l1_note_kind_that_is_not_a_string_is_unusable(kind: object) -> None:
    """`unhashable type: 'list'` here cost the whole notes ledger."""
    assert _note_from_arguments({"kind": kind, "summary": "A concern."}) is None


@pytest.mark.parametrize("bad", NOT_A_STRING)
@pytest.mark.parametrize("field", ("risk_level", "categories", "evidence_category"))
def test_an_l1_submission_with_a_non_string_field_is_a_contract_miss(
    field: str, bad: object
) -> None:
    value = _l1_value()
    if field == "risk_level":
        value["risk_level"] = bad
    elif field == "categories":
        value["categories"] = [bad]
    else:
        value["risk_level"] = "medium"
        value["categories"] = ["prompt_injection"]
        value["evidence"] = [{"path": "src/main.rs", "line": 1, "category": bad}]

    with pytest.raises(ValueError):
        _parse_review(value, artifact_sha256="a" * 64, repository=None)


@pytest.mark.parametrize("bad", NOT_A_STRING)
@pytest.mark.parametrize(
    "field",
    (
        "disposition",
        "risk_level",
        "resolution_basis",
        "categories",
        "evidence_category",
        "evidence_role",
        "causal_role",
        "generator_kind",
    ),
)
def test_an_l2_submission_with_a_non_string_field_is_a_contract_miss(
    field: str, bad: object, tmp_path: Path
) -> None:
    repository, artifact_sha256, digest = _archive(tmp_path)
    value = _l2_value(digest)
    if field == "categories":
        value["categories"] = [bad]
    elif field == "evidence_category":
        value["evidence"][0]["category"] = bad  # type: ignore[index]
    elif field == "evidence_role":
        value["evidence"][0]["role"] = bad  # type: ignore[index]
    elif field == "causal_role":
        value["causal_path"][0]["role"] = bad  # type: ignore[index]
    elif field == "generator_kind":
        value["generator_components"] = [
            {
                "path": "src/main.rs",
                "line": 1,
                "file_sha256": digest,
                "kind": bad,
            }
        ]
    else:
        value[field] = bad

    with pytest.raises(ValueError):
        _parse_l2_review(value, artifact_sha256=artifact_sha256, repository=repository)


def test_the_l2_baseline_this_test_poisons_actually_parses(tmp_path: Path) -> None:
    """Without the baseline, every case above could pass for the wrong reason."""
    repository, artifact_sha256, digest = _archive(tmp_path)

    observation, analyzed, causal, basis = _parse_l2_review(
        _l2_value(digest), artifact_sha256=artifact_sha256, repository=repository
    )

    assert basis == "insufficient_static_evidence"
    assert observation.error_code == "l2-model-inconclusive"
    # Both loops ran and normalized their rows, which is what makes a
    # poisoned row above a proof rather than an early exit.
    assert [item["path"] for item in analyzed] == ["src/main.rs"]
    assert len(causal) == 1


@pytest.mark.parametrize("decision", NOT_A_STRING)
def test_an_l4_verdict_decision_that_is_not_a_string_is_a_contract_miss(
    decision: object,
) -> None:
    """`adjudicate` is documented to never raise and always settle terminally."""
    with pytest.raises(ValueError, match="adjudicator decision is invalid"):
        _verdict_from({"decision": decision, "reason": "Because of the evidence."})


def _handler_types(path: Path, *, inside: str) -> set[str]:
    """Exception names caught by the handlers of one function, by source text."""
    module = ast.parse(path.read_text())
    caught: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != inside:
            continue
        for handler in (
            item for item in ast.walk(node) if isinstance(item, ast.ExceptHandler)
        ):
            types = handler.type
            parts = types.elts if isinstance(types, ast.Tuple) else [types]
            for part in parts:
                if isinstance(part, ast.Name):
                    caught.add(part.id)
                elif isinstance(part, ast.Attribute):
                    caught.add(part.attr)
    return caught


@pytest.mark.parametrize(
    ("module", "function"),
    (
        ("source_review.py", "review"),
        ("l2_review.py", "_review_uncached"),
        ("adjudicator.py", "adjudicate"),
    ),
)
def test_the_review_entry_points_settle_a_wrong_json_type(
    module: str, function: str
) -> None:
    """Defence in depth behind the guards above.

    The guards turn today's known sites into contract misses. These handlers
    are what keeps the next unguarded membership test from reaching the gate
    and spending an operator retry on a model's bad answer.
    """
    path = Path(__file__).resolve().parents[1] / "ditto_screener" / module
    caught = _handler_types(path, inside=function)

    assert caught, f"{module}:{function} has no handlers to check"
    assert "TypeError" in caught, sorted(caught)
