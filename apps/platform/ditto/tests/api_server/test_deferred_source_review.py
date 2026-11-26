"""Deterministic trigger tests for deferred source review."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.deferred_source_review import (
    DEFERRED_REVIEW_REASON,
    INTEGRITY_DOUBLE_CHECK_REASON,
    LEGACY_INTEGRITY_DOUBLE_CHECK_REASON,
    deep_review_attempt_id,
    is_no_finding_reason_code,
    public_deferred_review_triggers,
    public_review_conclusion,
    public_review_reason,
    verified_review_notes,
)
from ditto.db.queries.scores import LedgerRow
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION
from ditto_screening_protocol.models import (
    ScreenReviewAudit,
    SourceReviewNote,
    source_review_notes_digest,
)

# Seeded versions mean "the version the platform REQUIRES" — the floor in the
# default no-scheduled-activation state.
SCREENING_POLICY_VERSION = SCREENING_FLOOR_POLICY_VERSION


def _row(
    index: int,
    composite: float,
    *,
    tool: float | None = None,
    memory: float | None = None,
    eligible: bool = True,
) -> LedgerRow:
    return LedgerRow(
        miner_hotkey=f"miner-{index}",
        agent_id=UUID(int=index + 1),
        composite=composite,
        tool_mean=tool if tool is not None else composite,
        memory_mean=memory if memory is not None else composite,
        first_seen=datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=index),
        sha256=f"{index:064x}",
        size_bytes=100,
        run_id=f"run-{index}",
        seed=index,
        validator_hotkey="validator",
        signature=None,
        status=AgentStatus.SCORED,
        bench_version=8,
        n=351,
        eligible=eligible,
    )


def test_double_check_reason_is_neutral_for_new_and_stored_rows() -> None:
    """Every top-five entrant gets the double-check, so its public reason must
    not read as an integrity finding (#562), including on rows stored with the
    legacy wording."""
    assert "integrity" not in INTEGRITY_DOUBLE_CHECK_REASON.lower()
    # Operator reason searches for the double-check still match new rows.
    assert "double-check" in INTEGRITY_DOUBLE_CHECK_REASON
    assert INTEGRITY_DOUBLE_CHECK_REASON != DEFERRED_REVIEW_REASON
    assert (
        public_review_reason(
            "Top-five rank qualified this submission for an integrity double-check"
        )
        == INTEGRITY_DOUBLE_CHECK_REASON
    )
    assert (
        public_review_reason(LEGACY_INTEGRITY_DOUBLE_CHECK_REASON)
        == INTEGRITY_DOUBLE_CHECK_REASON
    )
    assert (
        public_review_reason(INTEGRITY_DOUBLE_CHECK_REASON)
        == INTEGRITY_DOUBLE_CHECK_REASON
    )
    # Anything else, operator prose included, is exact-match only and passes
    # through unchanged.
    for reason in (
        DEFERRED_REVIEW_REASON,
        "Manual benchmark-integrity review of the scored artifact.",
        f"{LEGACY_INTEGRITY_DOUBLE_CHECK_REASON}.",
        "",
    ):
        assert public_review_reason(reason) == reason
    assert public_review_reason(None) is None


# ── Public projection (#562) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["memory_anomaly", "top_five", "composite_anomaly"], ["top_five", "anomaly"]),
        (["unknown-private-trigger"], []),
        ("top_five", []),
    ],
)
def test_public_deferred_review_triggers_are_coarse(
    raw: object, expected: list[str]
) -> None:
    assert (
        public_deferred_review_triggers({"deferred_review": {"triggers": raw}})
        == expected
    )
    assert public_deferred_review_triggers(None) == []
    assert public_deferred_review_triggers({"deferred_review": "bad"}) == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("source-review-inconclusive", True),
        ("l2-runtime-evidence-unavailable", True),
        # Exact set, not a suffix: an unknown budget code fails closed.
        ("source-review-finding-budget-exhausted", False),
        ("l3-critic-model-budget-exhausted", False),
        ("source-safety-malicious-risk", False),
        (None, False),
    ],
)
def test_no_finding_codes_are_an_exact_allowlist(
    code: str | None, expected: bool
) -> None:
    assert is_no_finding_reason_code(code) is expected


def _digest(notes: object) -> str | None:
    """The digest a genuine writer records with ``notes`` (None if unparsable)."""
    if not isinstance(notes, list):
        return None
    try:
        return source_review_notes_digest(
            [SourceReviewNote.model_validate(note) for note in notes]
        )
    except ValueError:
        return None


def _conclude(
    *,
    active: bool,
    evidence: object,
    quarantined: bool = False,
    code: str | None = None,
    quarantine_digest: str | None = None,
    quarantine_finding: object = None,
    quarantine_audit: object = None,
    quarantine_notes: object = None,
    quarantine_notes_digest: object = "matching",
    hold_count: int = 3,
) -> object:
    return public_review_conclusion(
        deferred_review_active=active,
        deferred_evidence=evidence,
        deferred_concern_hold_count=hold_count,
        quarantined=quarantined,
        screening_reason_code=code,
        quarantine_finding_digest=quarantine_digest,
        quarantine_finding=quarantine_finding,
        quarantine_review_audit=quarantine_audit,
        quarantine_review_notes=quarantine_notes,
        # "matching": the digest recorded with a genuine ledger.
        quarantine_review_notes_digest=(
            _digest(quarantine_notes)
            if quarantine_notes_digest == "matching"
            else quarantine_notes_digest
        ),
        quarantine_concern_hold_count=hold_count,
    )


def _deep(**result: object) -> dict[str, object]:
    return {"deep_review_result": result}


# Audit shapes mirror the real producers in workers/screener: L1
# ``SourceReviewBudgetExhausted.audit()``, the L2 trajectory-budget audit, the L2
# ``l2-model-inconclusive`` audit, and the V13 ``_runtime_evidence_hold``
# preflight audit (lease unavailable / review disabled, zero steps).
L1_READ_BUDGET_AUDIT = ScreenReviewAudit(
    stage="l1",
    reason_code="source-review-read-budget-exhausted",
    prompt_revision="l1-v13",
    max_steps=240,
    steps_used=37,
    max_read_bytes=320_000,
    read_bytes_used=338_278,
).model_dump(mode="json")
L2_STEP_BUDGET_AUDIT = ScreenReviewAudit(
    stage="l2",
    reason_code="l2-model-step-budget",
    prompt_revision="l2-v13",
    harness_revision="h1",
    max_steps=64,
    steps_used=64,
    model_steps_observed=64,
    tool_calls_observed=80,
    budget_stop_reason="step",
).model_dump(mode="json")
L2_INCONCLUSIVE_AUDIT = ScreenReviewAudit(
    stage="l2",
    reason_code="l2-model-inconclusive",
    prompt_revision="l2-v13",
    harness_revision="h1",
    max_steps=64,
    steps_used=12,
    model_disposition="inconclusive",
    resolution_basis="insufficient_static_evidence",
    model_steps_observed=12,
    tool_calls_observed=20,
    budget_stop_reason="none",
).model_dump(mode="json")


def _preflight_audit(cause: str) -> dict[str, object]:
    return ScreenReviewAudit.model_validate(
        {
            "stage": "l2",
            "reason_code": "l2-runtime-evidence-unavailable",
            "prompt_revision": "l2-v13",
            "harness_revision": "h1",
            "max_steps": 64,
            "steps_used": 0,
            "max_input_tokens": 400_000,
            "input_tokens_used": 0,
            "max_output_tokens": 64_000,
            "output_tokens_used": 0,
            "max_cost_usd": 5.0,
            "cost_usd_used": 0,
            "model_steps_observed": 0,
            "tool_calls_observed": 0,
            "requested_model": "provider/model",
            "final_stage": "preflight",
            "cause_detail": cause,
            "max_elapsed_ms": 600_000,
            "elapsed_ms": 0,
        }
    ).model_dump(mode="json")


# (audit, expected) for a no-verdict hold with no finding, shared by the
# deferred and quarantine routes.
_AUDIT_CASES = [
    # No recorded audit: nothing proves any review completed (legacy rows, or
    # the V13 L2 path that fails after L1 already read the source).
    (None, "not_completed"),
    # V13 signed-runtime preflight: the L2 model stage never started.
    (_preflight_audit("lease_unavailable"), "not_completed"),
    (_preflight_audit("review_disabled"), "not_completed"),
    # Malformed or empty audits prove nothing.
    ({"stage": "l3", "reason_code": "x"}, "not_completed"),
    ({}, "not_completed"),
    # A well-formed audit with no model steps is not a review either.
    (
        {**L2_INCONCLUSIVE_AUDIT, "steps_used": 0, "model_steps_observed": 0},
        "not_completed",
    ),
    # A recorded budget audit: the review ran and ran out of budget.
    (L1_READ_BUDGET_AUDIT, "budget_exhausted"),
    (L2_STEP_BUDGET_AUDIT, "budget_exhausted"),
    # The review ran, ended inconclusive, and exhausted nothing.
    (L2_INCONCLUSIVE_AUDIT, "no_finding"),
]


# The code branches only on the interrupted outcome and checks the code
# against the allowlist, so one outcome/code pair carries the audit sweep.
# Cases 2 and 4 repeat the preflight and malformed branches.
@pytest.mark.parametrize(
    ("audit", "expected"),
    [case for i, case in enumerate(_AUDIT_CASES) if i not in (2, 4)],
)
@pytest.mark.parametrize(
    ("outcome", "code"), [("inconclusive", "source-review-inconclusive")]
)
def test_deferred_no_verdict_requires_a_recorded_review_audit(
    outcome: str, code: str, audit: object, expected: str
) -> None:
    # A recorded, concern-free ledger: the audit alone decides the state.
    evidence = _deep(
        outcome=outcome,
        reason_code=code,
        review_audit=audit,
        review_notes=[],
        review_notes_digest=_digest([]),
    )
    assert _conclude(active=True, evidence=evidence) == expected


# Quarantine routes into the same conclusion helper swept in full above;
# three audits prove the routing.
@pytest.mark.parametrize(("audit", "expected"), [_AUDIT_CASES[i] for i in (0, 6, 8)])
@pytest.mark.parametrize("code", ["source-review-inconclusive"])
def test_quarantine_no_verdict_requires_a_recorded_review_audit(
    code: str, audit: object, expected: str
) -> None:
    assert (
        _conclude(
            active=False,
            evidence=None,
            quarantined=True,
            code=code,
            quarantine_audit=audit,
            quarantine_notes=[],
        )
        == expected
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # A recorded finding is never softened, whatever the code or audit says.
        (
            {
                "outcome": "inconclusive",
                "reason_code": "source-review-inconclusive",
                "finding_digest": "ab" * 32,
                "review_audit": L1_READ_BUDGET_AUDIT,
            },
            "adverse_signal",
        ),
        # A budget audit cannot soften an adverse or unknown code.
        (
            {
                "outcome": "quarantine",
                "reason_code": "agentic-source-review-tripwire",
                "review_audit": L1_READ_BUDGET_AUDIT,
            },
            "adverse_signal",
        ),
        # Interrupted attempts have no conclusion yet.
        (
            {
                "outcome": "retryable_infra",
                "reason_code": "docker-build-infrastructure",
            },
            "pending",
        ),
        (
            {"outcome": "deterministic_reject", "reason_code": "health-contract"},
            "pending",
        ),
        # ...unless the interrupted attempt still carries a finding.
        (
            {
                "outcome": "retryable_infra",
                "reason_code": "docker-build-infrastructure",
                "finding_digest": "ab" * 32,
            },
            "adverse_signal",
        ),
        # Any other deterministic reject, unknown outcomes and unknown codes
        # fail closed.
        (
            {"outcome": "deterministic_reject", "reason_code": "archive-invalid"},
            "adverse_signal",
        ),
    ],
)
def test_deep_review_conclusion(result: dict[str, object], expected: str) -> None:
    assert _conclude(active=True, evidence=_deep(**result)) == expected


def test_public_review_conclusion_without_a_deep_result() -> None:
    # The admission-time code on the deferred snapshot is not a deep result.
    assert (
        _conclude(
            active=True,
            evidence={
                "deferred_review": {
                    "screening_reason_code": "source-review-inconclusive"
                }
            },
        )
        == "pending"
    )
    assert (
        _conclude(
            active=True,
            evidence={},
            quarantined=True,
            code="source-review-read-budget-exhausted",
            quarantine_audit=L1_READ_BUDGET_AUDIT,
            quarantine_notes=[],
        )
        == "budget_exhausted"
    )
    assert (
        _conclude(active=False, evidence=None, quarantined=True, code="tripwire")
        == "adverse_signal"
    )
    assert _conclude(active=False, evidence=None, quarantined=True) is None
    assert (
        _conclude(active=False, evidence=None, code="source-review-inconclusive")
        is None
    )


@pytest.mark.parametrize(
    ("digest", "finding"),
    [
        ("cd" * 32, None),
        (None, {"risk": "high"}),
    ],
)
def test_quarantine_finding_is_never_softened(digest: object, finding: object) -> None:
    for code in ("source-review-inconclusive", "l2-model-budget-exhausted", None):
        assert (
            _conclude(
                active=False,
                evidence=None,
                quarantined=True,
                code=code,
                quarantine_digest=digest if isinstance(digest, str) else None,
                quarantine_finding=finding,
                quarantine_audit=L1_READ_BUDGET_AUDIT,
            )
            == "adverse_signal"
        )


# ── Concern-driven budget holds (#562 review) ────────────────────────────────


def _concern(path: str, line: int, category: str = "none") -> dict[str, object]:
    return {
        "kind": "concern",
        "category": category,
        "path": path,
        "line": line,
        "summary": "lead",
        "stage": "l1",
    }


# Three distinct cited sites reach the default hold count of 3; a single-site
# multi-location claim and cleared notes do not count.
CONCERN_NOTES = [_concern("a.rs", 1), _concern("a.rs", 9), _concern("b.rs", 4)]
THIN_NOTES = [
    _concern("a.rs", 1),
    _concern("c.rs", 2, "benchmark_emulation"),
    {"kind": "cleared", "category": "none", "summary": "ok", "stage": "l1"},
]


@pytest.mark.parametrize(
    ("notes", "hold_count", "expected"),
    [
        (CONCERN_NOTES, 3, "adverse_signal"),
        (CONCERN_NOTES, 4, "budget_exhausted"),
        (THIN_NOTES, 3, "budget_exhausted"),
        # No recorded ledger (legacy row, or a path that kept no notes):
        # nothing shows thin coverage, so fail closed.
        (None, 3, "adverse_signal"),
    ],
)
def test_concern_threshold_decides_a_budget_hold_on_both_paths(
    notes: object, hold_count: int, expected: str
) -> None:
    deferred = _deep(
        outcome="inconclusive",
        reason_code="source-review-inconclusive",
        review_audit=L1_READ_BUDGET_AUDIT,
        review_notes=notes,
        review_notes_digest=_digest(notes),
    )
    assert _conclude(active=True, evidence=deferred, hold_count=hold_count) == (
        expected
    )
    assert (
        _conclude(
            active=False,
            evidence=None,
            quarantined=True,
            code="source-review-inconclusive",
            quarantine_audit=L1_READ_BUDGET_AUDIT,
            quarantine_notes=notes,
            hold_count=hold_count,
        )
        == expected
    )


def test_concern_threshold_only_reclassifies_budget_holds() -> None:
    # Concern notes on a non-budget or not-completed hold keep their state: the
    # worker's ledger rule applies only to budget-terminated reviews.
    for audit, expected in (
        (L2_INCONCLUSIVE_AUDIT, "no_finding"),
        (None, "not_completed"),
        (_preflight_audit("lease_unavailable"), "not_completed"),
    ):
        evidence = _deep(
            outcome="inconclusive",
            reason_code="source-review-inconclusive",
            review_audit=audit,
            review_notes=CONCERN_NOTES,
        )
        assert _conclude(active=True, evidence=evidence) == expected


def test_deep_review_attempt_id() -> None:
    attempt = uuid4()
    assert (
        deep_review_attempt_id(_deep(attempt_id=str(attempt), outcome="x")) == attempt
    )
    assert deep_review_attempt_id(_deep(attempt_id="not-a-uuid")) is None
    assert deep_review_attempt_id({"deep_review_result": None}) is None
    assert deep_review_attempt_id(None) is None


# ── Only a complete, digest-verified ledger may lower a budget hold ──────────

_STALE_CONCERN_DIGEST = _digest(CONCERN_NOTES)
_MALFORMED_NOTE = {"kind": "concern", "category": "none", "summary": ""}


@pytest.mark.parametrize(
    ("notes", "digest"),
    [
        # A concern-bearing ledger replaced by an empty list, stale digest.
        ([], _STALE_CONCERN_DIGEST),
        # Truncated to a shorter (thin) list, stale digest.
        # A digest that matches nothing recorded.
        # A malformed entry is never dropped to make the rest verify.
        ([*THIN_NOTES, _MALFORMED_NOTE], _digest(THIN_NOTES)),
        # No recorded digest at all, even for an otherwise valid list.
        (THIN_NOTES, None),
        # Over the protocol bound.
        ([THIN_NOTES[2]] * 49, _digest([THIN_NOTES[2]] * 49)),
    ],
)
def test_unverified_ledger_keeps_a_budget_hold_adverse_on_both_paths(
    notes: object, digest: object
) -> None:
    deferred = _deep(
        outcome="inconclusive",
        reason_code="source-review-inconclusive",
        review_audit=L1_READ_BUDGET_AUDIT,
        review_notes=notes,
        review_notes_digest=digest,
    )
    assert _conclude(active=True, evidence=deferred) == "adverse_signal"
    assert (
        _conclude(
            active=False,
            evidence=None,
            quarantined=True,
            code="source-review-inconclusive",
            quarantine_audit=L1_READ_BUDGET_AUDIT,
            quarantine_notes=notes,
            quarantine_notes_digest=digest,
        )
        == "adverse_signal"
    )


def test_verified_review_notes_contract() -> None:
    # The empty ledger's digest is the canonical digest of ``[]``.
    empty_digest = source_review_notes_digest([])
    assert verified_review_notes([], empty_digest) == []
    assert verified_review_notes([], None) is None
    parsed = verified_review_notes(THIN_NOTES, _digest(THIN_NOTES))
    assert parsed is not None and len(parsed) == len(THIN_NOTES)
    assert verified_review_notes(THIN_NOTES, _STALE_CONCERN_DIGEST) is None
    assert verified_review_notes({"kind": "concern"}, empty_digest) is None
