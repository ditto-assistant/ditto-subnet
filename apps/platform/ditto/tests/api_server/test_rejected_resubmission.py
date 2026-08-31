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
    name: str = "Zeus_v11",
    version: int | None = 6,
) -> RejectedArtifact:
    return RejectedArtifact(
        agent_id=agent_id,
        miner_hotkey=hotkey,
        first_seen=first_seen if first_seen is not None else _NOW - timedelta(hours=2),
        sha256=sha256,
        normalized_source_hash=normalized_source_hash,
        content_fingerprint=content_fingerprint,
        name=name,
        version=version,
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
        miner_hotkey="5SameMiner",
        rejected=[_rejected(hotkey="5SameMiner")],
    )
    assert decision.held is True
    assert decision.reason is not None
    assert "Same miner, previously rejected as Zeus_v11 v6" in decision.reason
    assert "cannot distinguish" not in decision.reason


def test_cross_owner_resubmission_names_the_other_hotkey() -> None:
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="rejected-sha",
        miner_hotkey="5OtherMiner",
        rejected=[_rejected(hotkey="5Rejected")],
    )
    assert decision.held is True
    assert decision.reason is not None
    assert "Same miner, previously rejected as" not in decision.reason
    assert "hotkey 5Rejected" in decision.reason
    assert "Zeus_v11 v6" in decision.reason


def test_lexical_near_duplicate_is_left_to_source_review() -> None:
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
    assert decision.held is False


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


def test_hogwarts_replacement_remediation_is_not_held() -> None:
    """Containment must not treat a same-owner replacement as padding.

    Hogwarts_v2 v4–v9 (2026-08-20/21) shared an owner with banned Hogwarts_v1
    v16. Operators later cleared them: the Gryffindor family dispatcher
    (``asks_outstanding`` / ``JOIN_IS_THE_WORK``) was deleted and replaced with
    a generic ``integer_arithmetic`` closer. Adding those replacement files
    made ``candidate_card >= rejected_card``, so the old ``>=`` direction guard
    treated an honest remediation as a padded re-upload.

    Production pair: Jaccard 0.945–0.973, containment 0.996, candidate only
    slightly larger than rejected. This fixture is 199 shared of 200 rejected
    plus 6 replacements (card 205 vs 200): Jaccard 0.966, containment 0.995.
    Jaccard stays under 0.98; containment would have fired under ``>=``. A 15%
    padding floor still holds the 200+30 junk attack while this replacement
    shape does not.
    """
    kept = {f"h{i}" for i in range(199)}
    cited_removed = {"asks_outstanding"}
    replacement = {f"integer_arithmetic{i}" for i in range(6)}
    decision = evaluate_rejected_resubmission(
        agent_id=_CANDIDATE,
        submitted_at=_NOW,
        sha256="different-sha",
        normalized_source_hash="different-normalized",
        miner_hotkey="5Hogwarts",
        content_fingerprint=_fp(kept | replacement),
        rejected=[
            _rejected(
                hotkey="5Hogwarts",
                name="Hogwarts_v1",
                version=16,
                content_fingerprint=_fp(kept | cited_removed),
            )
        ],
    )
    assert decision.held is False


def test_padded_lexical_resubmission_is_left_to_source_review() -> None:
    """Containment still catches the attack it exists for.

    Every shingle of the rejected artifact survives and junk is bolted on to
    dilute Jaccard (here to 0.870, well under the bar) — so the candidate is
    15% larger (200 rejected + 30 junk) and the padding-ratio guard fires.
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
    assert decision.held is False


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
