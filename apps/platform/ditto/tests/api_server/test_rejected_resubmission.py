from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from ditto.api_server.fingerprint import reference_corpus_provenance
from ditto.api_server.scoring_gate import evaluate_rejected_resubmission
from ditto.db.queries.scores import RejectedArtifact

_NOW = datetime(2026, 8, 17, tzinfo=UTC)
_CORPUS = reference_corpus_provenance()["corpus_id"]

_CANDIDATE = UUID("00000000-0000-4000-8000-000000000001")
_REJECTED = UUID("00000000-0000-4000-8000-000000000002")
_OLDER_REJECTED = UUID("00000000-0000-4000-8000-000000000003")


def _fp(values: set[str]) -> dict:
    return {
        "v": 2,
        "k": 256,
        "card": len(values),
        "m": sorted(values),
        "corpus": _CORPUS,
    }


def _rejected(
    *,
    agent_id: UUID = _REJECTED,
    hotkey: str = "5Rejected",
    first_seen: datetime | None = None,
    sha256: str = "rejected-sha",
    normalized_source_hash: str | None = "rejected-normalized",
    content_fingerprint: dict | None = None,
) -> RejectedArtifact:
    return RejectedArtifact(
        agent_id=agent_id,
        miner_hotkey=hotkey,
        first_seen=first_seen if first_seen is not None else _NOW - timedelta(hours=2),
        sha256=sha256,
        normalized_source_hash=normalized_source_hash,
        content_fingerprint=content_fingerprint,
    )


def test_no_rejected_corpus_never_holds() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="fresh-sha",
        rejected=(),
    )
    assert decision.held is False


def test_byte_identical_resubmission_is_held() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="rejected-sha",
        rejected=[_rejected()],
    )
    assert decision.held is True
    assert decision.duplicate_of == _REJECTED
    assert decision.reason is not None
    assert "byte-identical" in decision.reason


def test_repack_matches_on_normalized_source() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="rejected-normalized",
        rejected=[_rejected()],
    )
    assert decision.held is True
    assert decision.duplicate_of == _REJECTED


def test_same_owner_resubmission_is_held() -> None:
    """The inversion that separates this gate from the copy rules.

    Every anti-copy rule skips the candidate's own owner. Here the same miner
    re-uploading its own rejected artifact is the primary case, so an identical
    hotkey must not exempt it.
    """
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="rejected-sha",
        rejected=[_rejected(hotkey="5SameMiner")],
    )
    assert decision.held is True


def test_lexical_near_duplicate_is_held() -> None:
    """A cosmetically-edited re-upload: one shingle differs out of ~200."""
    shared = {f"h{i}" for i in range(200)}
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="different-normalized",
        content_fingerprint=_fp(shared | {"edit-a"}),
        rejected=[_rejected(content_fingerprint=_fp(shared | {"edit-b"}))],
    )
    assert decision.held is True
    assert decision.reason is not None
    assert "near-duplicate" in decision.reason


def test_remediated_descendant_is_not_held() -> None:
    """The regression this rule's thresholds were re-cut for.

    A miner who deletes the cited code and resubmits is still overwhelmingly
    their own prior artifact — Crown-v11-v3 measured 0.945 Jaccard against the
    version it was cut from, on a 574-line deletion an operator then cleared as
    a textbook remediation. Overlap at that level is the population's baseline,
    not evidence, so the rule must sit above it.
    """
    kept = {f"h{i}" for i in range(190)}
    removed = {f"cited{i}" for i in range(20)}
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="different-normalized",
        content_fingerprint=_fp(kept),
        rejected=[_rejected(content_fingerprint=_fp(kept | removed))],
    )
    assert decision.held is False


def test_padded_reupload_is_held_on_containment() -> None:
    """Containment still catches the attack it exists for.

    Every shingle of the rejected artifact survives and junk is bolted on to
    dilute Jaccard (here to 0.870, well under the bar) — so the candidate is the
    *larger* residual and the padding direction fires.
    """
    rejected_shingles = {f"h{i}" for i in range(200)}
    padding = {f"junk{i}" for i in range(30)}
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="different-normalized",
        content_fingerprint=_fp(rejected_shingles | padding),
        rejected=[_rejected(content_fingerprint=_fp(rejected_shingles))],
    )
    assert decision.held is True
    assert decision.reason is not None
    assert "near-duplicate" in decision.reason


def test_containment_is_silent_when_direction_is_unknown() -> None:
    """No ``card`` on one side means the subset direction cannot be established.

    The same pair as the padded-re-upload test — containment is a full 1.0 — but
    with the rejected sketch's cardinality missing, so the rule cannot tell a
    padded copy from a deletion and must not spend a hold guessing. Jaccard
    (0.870) still had its say.
    """
    rejected_shingles = {f"h{i}" for i in range(200)}
    padding = {f"junk{i}" for i in range(30)}
    uncounted = _fp(rejected_shingles)
    del uncounted["card"]
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="different-normalized",
        content_fingerprint=_fp(rejected_shingles | padding),
        rejected=[_rejected(content_fingerprint=uncounted)],
    )
    assert decision.held is False


def test_exact_rules_still_hold_a_remediated_looking_repack() -> None:
    """Raising rule 3's bar did not soften rules 1 and 2.

    A resubmission whose lexical distance now clears the rule — the deletion
    shape above — is still held when the canonicalized source is byte-for-byte
    the rejected one, because a hash match is not an inference.
    """
    kept = {f"h{i}" for i in range(190)}
    removed = {f"cited{i}" for i in range(20)}
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="rejected-normalized",
        content_fingerprint=_fp(kept),
        rejected=[_rejected(content_fingerprint=_fp(kept | removed))],
    )
    assert decision.held is True


def test_unrelated_artifact_is_not_held() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="different-normalized",
        content_fingerprint=_fp({f"a{i}" for i in range(40)}),
        rejected=[_rejected(content_fingerprint=_fp({f"b{i}" for i in range(40)}))],
    )
    assert decision.held is False


def test_rejection_after_upload_does_not_hold_retroactively() -> None:
    """Prospective by construction: a miner cannot avoid a decision not yet made."""
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="rejected-sha",
        rejected=[_rejected(first_seen=_NOW + timedelta(hours=1))],
    )
    assert decision.held is False


def test_agent_never_matches_itself() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_REJECTED,
        submitted_at=_NOW,
        sha256="rejected-sha",
        rejected=[_rejected()],
    )
    assert decision.held is False


def test_earliest_rejection_is_named() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="rejected-sha",
        rejected=[
            _rejected(),
            _rejected(
                agent_id=_OLDER_REJECTED,
                first_seen=_NOW - timedelta(days=3),
            ),
        ],
    )
    assert decision.held is True
    assert decision.duplicate_of == _OLDER_REJECTED


def test_naive_timestamps_are_comparable() -> None:
    """Agent.created_at is naive in some rows; the gate must not raise on it."""
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=datetime(2026, 8, 17),
        sha256="rejected-sha",
        rejected=[_rejected(first_seen=datetime(2026, 8, 16))],
    )
    assert decision.held is True
