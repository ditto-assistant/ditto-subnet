"""Unit tests for the pure anti-copy gate :mod:`ditto.api_server.scoring_gate`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.fingerprint import _FP_VERSION, _MINHASH_K, _PROMPT_VERSION
from ditto.api_server.scoring_gate import PublicSourceRelease
from ditto.api_server.scoring_gate import (
    evaluate_duplicate_signals as _evaluate_duplicate_signals,
)
from ditto.db.queries.scores import LedgerRow

_FIRST_SEEN = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
_CHALLENGER_SEEN = _FIRST_SEEN + timedelta(hours=1)


def evaluate_duplicate_signals(**kwargs):
    """Keep ordinary fixtures chronological; tests may override for edge cases."""
    kwargs.setdefault("submitted_at", _CHALLENGER_SEEN)
    return _evaluate_duplicate_signals(**kwargs)


def _sk(shingles: set[str]) -> dict:
    """Build a fingerprint sketch from a set of shingle hashes.

    The sets here are far smaller than the bottom-k budget, so the sketch is the
    whole set and :func:`content_similarity` computes Jaccard/containment exactly —
    letting these gate tests assert on precise thresholds.
    """
    return {
        "v": _FP_VERSION,
        "k": _MINHASH_K,
        "card": len(shingles),
        "m": sorted(shingles)[:_MINHASH_K],
    }


def _psk(shingles: set[str]) -> dict:
    """Build an prompt sketch (version ``"p1"``) from a set of shingle hashes."""
    return {
        "v": _PROMPT_VERSION,
        "k": _MINHASH_K,
        "card": len(shingles),
        "m": sorted(shingles)[:_MINHASH_K],
    }


def _entry(
    *,
    composite: float,
    miner: str = "5Incumbent",
    sha256: str = "aa" * 32,
    size_bytes: int | None = 524288,
    content_fingerprint: dict | None = None,
    structural_fingerprint: dict | None = None,
    normalized_source_hash: str | None = None,
    prompt_fingerprint: dict | None = None,
    first_seen: datetime = _FIRST_SEEN,
    agent_id: UUID | None = None,
    coldkey: str | None = None,
) -> LedgerRow:
    return LedgerRow(
        miner_hotkey=miner,
        agent_id=agent_id or uuid4(),
        composite=composite,
        tool_mean=composite,
        memory_mean=composite,
        first_seen=first_seen,
        sha256=sha256,
        size_bytes=size_bytes,
        run_id="run_1",
        seed=42,
        validator_hotkey="5Validator",
        signature="ab" * 64,
        status=AgentStatus.SCORED,
        miner_coldkey=coldkey,
        content_fingerprint=content_fingerprint,
        structural_fingerprint=structural_fingerprint,
        normalized_source_hash=normalized_source_hash,
        prompt_fingerprint=prompt_fingerprint,
    )


class TestEvaluateAntidup:
    def test_clean_submission_not_held(self) -> None:
        incumbent = _entry(composite=0.70, sha256="aa" * 32, size_bytes=500000)
        # A genuine improvement from another miner: far higher, different size+hash.
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.85,
            size_bytes=700000,
            eligible=[incumbent],
        )
        assert decision.held is False
        assert decision.duplicate_of is None

    def test_exact_sha256_copy_is_held(self) -> None:
        incumbent = _entry(composite=0.70, sha256="cc" * 32, size_bytes=500000)
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="cc" * 32,  # byte-identical resubmission
            composite=0.70,
            size_bytes=500000,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert "sha256" in (decision.reason or "")

    def test_same_coldkey_across_hotkeys_is_lineage_not_copy(self) -> None:
        incumbent = _entry(
            composite=0.70,
            miner="5OldHotkey",
            coldkey="5SharedColdkey",
            sha256="cc" * 32,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5NewHotkey",
            miner_coldkey="5SharedColdkey",
            sha256="cc" * 32,
            composite=0.70,
            size_bytes=incumbent.size_bytes,
            eligible=[incumbent],
        )

        assert decision.held is False

    def test_exact_repack_normalized_hash_is_held(self) -> None:
        # Different bytes (sha256) but the same canonicalized source: a reformat /
        # re-comment / file-reorder repack. Held on the hash equality alone, with
        # no score proximity (a distant score must not save it).
        incumbent = _entry(
            composite=0.60,
            sha256="cc" * 32,
            normalized_source_hash="ns" * 32,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="dd" * 32,  # repackaged bytes differ
            composite=0.95,  # far from incumbent — proximity is irrelevant here
            size_bytes=999999,
            normalized_source_hash="ns" * 32,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert "repack" in (decision.reason or "")

    def test_later_upload_cannot_be_attributed_as_original(self) -> None:
        later = _entry(
            composite=0.60,
            normalized_source_hash="ns" * 32,
            first_seen=_CHALLENGER_SEEN + timedelta(hours=1),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Earlier",
            submitted_at=_CHALLENGER_SEEN,
            sha256="dd" * 32,
            composite=0.60,
            size_bytes=524288,
            normalized_source_hash="ns" * 32,
            eligible=[later],
        )
        assert decision.held is False

    def test_equal_timestamp_uses_agent_id_as_tie_break(self) -> None:
        earlier_id = uuid4()
        later_id = uuid4()
        if earlier_id.int > later_id.int:
            earlier_id, later_id = later_id, earlier_id
        original = _entry(
            composite=0.60,
            normalized_source_hash="ns" * 32,
            first_seen=_FIRST_SEEN,
            agent_id=earlier_id,
        )
        decision = evaluate_duplicate_signals(
            agent_id=later_id,
            miner_hotkey="5Later",
            submitted_at=_FIRST_SEEN,
            sha256="dd" * 32,
            composite=0.60,
            size_bytes=524288,
            normalized_source_hash="ns" * 32,
            eligible=[original],
        )
        assert decision.held is True
        assert decision.duplicate_of == earlier_id

    def test_oldest_matching_submission_is_attributed_as_original(self) -> None:
        oldest = _entry(
            composite=0.60,
            normalized_source_hash="ns" * 32,
            first_seen=_FIRST_SEEN,
        )
        newer = _entry(
            composite=0.60,
            normalized_source_hash="ns" * 32,
            first_seen=_FIRST_SEEN + timedelta(minutes=30),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Latest",
            submitted_at=_CHALLENGER_SEEN,
            sha256="dd" * 32,
            composite=0.60,
            size_bytes=524288,
            normalized_source_hash="ns" * 32,
            eligible=[newer, oldest],
        )
        assert decision.held is True
        assert decision.duplicate_of == oldest.agent_id

    def test_same_miner_repack_not_held(self) -> None:
        # A miner re-uploading their OWN agent (same normalized hash) is iterating,
        # not copying — the other-miner filter must exempt it.
        own = _entry(
            composite=0.60,
            miner="5Self",
            sha256="cc" * 32,
            normalized_source_hash="ns" * 32,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Self",
            sha256="dd" * 32,
            composite=0.61,
            size_bytes=500000,
            normalized_source_hash="ns" * 32,
            eligible=[own],
        )
        assert decision.held is False

    def test_null_normalized_hash_never_matches(self) -> None:
        # An incumbent with no stored hash (uploaded before the normalized-source hash /
        # unreadable
        # tarball) must not match a challenger that also lacks one — null is
        # "no repack match", never a hit against null.
        incumbent = _entry(
            composite=0.60, sha256="cc" * 32, normalized_source_hash=None
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="dd" * 32,
            composite=0.85,
            size_bytes=700000,
            normalized_source_hash=None,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_near_dup_from_other_miner_is_held(self) -> None:
        incumbent = _entry(composite=0.80, sha256="aa" * 32, size_bytes=500000)
        # Another miner, different bytes, near-identical size + score.
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert "near-duplicate" in (decision.reason or "")

    def test_large_improvement_not_held(self) -> None:
        incumbent = _entry(composite=0.80, sha256="aa" * 32, size_bytes=500000)
        # Same size but a big score jump => a real improvement, not a copy.
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.90,
            size_bytes=500000,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_distant_score_not_held(self) -> None:
        incumbent = _entry(composite=0.80, sha256="aa" * 32, size_bytes=500000)
        # Similar size but score gap > tol => not a near-dup.
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.75,
            size_bytes=500050,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_same_miner_improvement_not_held(self) -> None:
        # A miner iterating on THEIR OWN eligible agent (near-identical size, small
        # score bump, different bytes) is not a copier — must not be held.
        incumbent = _entry(
            composite=0.80, miner="5Mine", sha256="aa" * 32, size_bytes=500000
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Mine",  # same miner
            sha256="bb" * 32,
            composite=0.81,
            size_bytes=500200,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_copy_not_masked_by_unrelated_midscorer(self) -> None:
        # A genuine unrelated agent scoring between the copied agent and the copy
        # must not let the copy escape the gate (the false-negative A2).
        original = _entry(
            composite=0.80, miner="5Orig", sha256="aa" * 32, size_bytes=500000
        )
        midscorer = _entry(
            composite=0.804, miner="5Mid", sha256="dd" * 32, size_bytes=900000
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500050,  # matches the original's size, not the midscorer's
            eligible=[midscorer, original],
        )
        assert decision.held is True
        assert decision.duplicate_of == original.agent_id

    def test_content_dup_held_when_size_drifts(self) -> None:
        # A reformatted/locally-edited copy whose byte size moved past the size
        # tolerance, but whose shingle sketch is all but identical.
        shared = {f"{i:016x}" for i in range(20)}
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shared),
        )
        # 19 of 20 shingles shared (Jaccard 19/21 = 0.905 >= 0.75) and the tarball
        # size drifted 100 KiB past the size rule.
        copy_fp = _sk({f"{i:016x}" for i in range(19)} | {"ff" * 8})
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=600000,  # well past the 8 KiB size tolerance
            content_fingerprint=copy_fp,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert "content near-duplicate" in (decision.reason or "")

    def test_padding_held_by_containment(self) -> None:
        # A verbatim copy padded with junk files dilutes Jaccard below the tol but
        # stays fully contained => the containment arm of rule 2 holds it.
        shared = {f"{i:016x}" for i in range(20)}
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shared),
        )
        padded = _sk(shared | {f"pad{i:013x}" for i in range(40)})  # jaccard 20/60
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=900000,
            content_fingerprint=padded,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "containment" in (decision.reason or "")

    def test_structural_match_alone_never_holds(self) -> None:
        # The structural (AST) sketch is whole-crate — astfp performs no
        # reference subtraction — so it saturates between independent
        # starter-kit derivatives exactly like the pre-reference lexical
        # channel did (12 of the 66 audited holds sit at/above its
        # thresholds). Until dittobench ships reference-aware structural
        # sketches it corroborates, never triggers.
        struct = {f"{i:016x}" for i in range(30)}
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk({f"lex{i:013x}" for i in range(30)}),
            structural_fingerprint=_sk(struct),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=700000,
            content_fingerprint=_sk({f"other{i:011x}" for i in range(30)}),
            structural_fingerprint=_sk(struct),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_below_floor_lexical_does_not_fall_through_to_structural(self) -> None:
        # THE audited-corpus regression: a near-pristine kit edit gets a
        # versioned EMPTY lexical sketch (residual below the cardinality
        # floor), which matches nothing — it must not fall through to the
        # saturated whole-crate structural channel and be re-held there.
        struct = {f"{i:016x}" for i in range(30)}
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=524288,
            content_fingerprint=_sk(set()),
            structural_fingerprint=_sk(struct),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.801,
            size_bytes=524289,
            content_fingerprint=_sk(set()),
            structural_fingerprint=_sk(struct),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_structural_overlap_annotates_lexical_hold(self) -> None:
        # When the lexical channel fires, high structural overlap with the
        # matched agent is appended to the audit reason as corroboration.
        lex = {f"lex{i:013x}" for i in range(30)}
        struct = {f"{i:016x}" for i in range(30)}
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(lex),
            structural_fingerprint=_sk(struct),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=700000,
            content_fingerprint=_sk(lex),
            structural_fingerprint=_sk(struct),
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "content near-duplicate" in (decision.reason or "")
        assert "structural jaccard" in (decision.reason or "")

    def test_structural_below_tol_not_held(self) -> None:
        # Two crates sharing reference-harness AST scaffolding but well under the
        # (high) structural threshold, and lexically distinct => not held.
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk({f"lex{i:013x}" for i in range(30)}),
            structural_fingerprint=_sk({f"{i:016x}" for i in range(30)}),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=900000,
            content_fingerprint=_sk({f"z{i:015x}" for i in range(30)}),
            # 15 of 45 shingles shared => Jaccard 0.33, containment 0.5: below the
            # 0.85 / 0.98 structural tolerances.
            structural_fingerprint=_sk(
                {f"{i:016x}" for i in range(15)} | {f"s{i:015x}" for i in range(15)}
            ),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_distinct_content_not_held_despite_close_score(self) -> None:
        # Two independent harnesses that only share reference scaffolding: close
        # score but low fingerprint overlap (5 of 30) => a genuine competitor.
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk({f"{i:016x}" for i in range(20)}),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=900000,  # different size, so the size rule can't fire either
            content_fingerprint=_sk(
                {f"{i:016x}" for i in range(5)} | {f"x{i:015x}" for i in range(15)}
            ),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_content_dup_held_when_score_far(self) -> None:
        # Regression: a matching lexical fingerprint holds on its own. A large
        # score gap is NOT evidence of independence — benchmark noise moves
        # identical code further than any plausible proximity window (see
        # test_dittotop_v0_incident below), so the gate must not skip the
        # comparison just because the composites are far apart.
        shared = _sk({f"{i:016x}" for i in range(20)})
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=shared,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.90,
            size_bytes=505000,
            content_fingerprint=shared,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert "content near-duplicate" in (decision.reason or "")
        # The delta is still reported, as context rather than as a precondition.
        assert "composite delta 0.1000" in (decision.reason or "")

    def test_dittotop_v0_incident(self) -> None:
        """The bench_version 7 copy the old 0.03 score window let through.

        Production, 2026-07-25. ``ditto-v7`` (composite 0.914322, uploaded
        12:26:49Z) and ``dittoTop-v0`` (composite 0.837497, uploaded 13:16:51Z,
        another miner) were **normalized-identical**: 15 of 15 files matched once
        comments and whitespace were canonicalized. dittoTop-v0 was never held,
        because the 0.076825 composite gap fell outside the 0.03 proximity window
        — while ``cliM@X-v1`` (delta 0.0277) and ``dittoLife-v1`` (delta 0.0199),
        both *less* similar, were held. Per-agent composite_stderr across these
        four agents ran 0.016–0.020, so identical code drifts roughly 2.5x wider
        than the window assumed; detection probability was inversely related to
        how lucky the copy's re-roll was.

        Fingerprints here are identical (Jaccard 1.0), matching the production
        evidence for the pairs that *were* held.
        """
        shared = _sk({f"{i:016x}" for i in range(40)})
        ditto_v7 = _entry(
            composite=0.914322,
            miner="5Fszh8YAWYdLms139nij2QHaJVMm5EjJ2mjwvjBwDkCekSdo",
            sha256="aa" * 32,
            size_bytes=524288,
            content_fingerprint=shared,
            first_seen=datetime(2026, 7, 25, 12, 26, 49, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5F7hzMMVJhaBtWg4hHsN5g8MqbCciKr9pqK9TCqvyyGmRLw3",
            submitted_at=datetime(2026, 7, 25, 13, 16, 51, tzinfo=UTC),
            sha256="bb" * 32,
            composite=0.837497,
            # Repacked, so neither the sha256 nor the archive size matches.
            size_bytes=524288 + 64 * 1024,
            content_fingerprint=shared,
            eligible=[ditto_v7],
        )
        assert decision.held is True
        assert decision.duplicate_of == ditto_v7.agent_id
        assert "content near-duplicate" in (decision.reason or "")
        # 0.0768: wider than the retired 0.03 window and than any window a
        # 0.016-0.020 stderr would justify.
        assert "composite delta 0.0768" in (decision.reason or "")

    def test_same_miner_content_dup_not_held(self) -> None:
        # A miner iterating on their own harness shares content with themselves —
        # never a copier, so the content rule must skip same-miner entries.
        shared = _sk({f"{i:016x}" for i in range(20)})
        incumbent = _entry(
            composite=0.80, miner="5Mine", sha256="aa" * 32, content_fingerprint=shared
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Mine",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=600000,
            content_fingerprint=shared,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_missing_fingerprints_fall_back_to_size_rule(self) -> None:
        # No fingerprints anywhere (legacy rows): the content rule is inert
        # (similarity 0) and the size rule still catches a same-size near-dup.
        incumbent = _entry(composite=0.80, sha256="aa" * 32, size_bytes=500000)
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=None,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "near-duplicate" in (decision.reason or "")

    def test_negative_fingerprint_evidence_disables_size_fallback(self) -> None:
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk({f"a{i:015x}" for i in range(20)}),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Independent",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk({f"b{i:015x}" for i in range(20)}),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_reference_only_fingerprint_disables_size_fallback(self) -> None:
        # v2 records a versioned empty sketch when a valid artifact contains too
        # little miner-authored residual after reference subtraction. That is a
        # negative moderation signal, not a missing legacy fingerprint.
        reference_only = {"v": 2, "k": 256, "card": 0, "m": []}
        incumbent = _entry(
            composite=0.80,
            size_bytes=500000,
            content_fingerprint=reference_only,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            submitted_at=_CHALLENGER_SEEN,
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=reference_only,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_cross_version_comparison_routes_current_row_to_review(self) -> None:
        shared = {f"{i:016x}" for i in range(30)}
        legacy = _sk(shared)
        legacy["v"] = 1
        incumbent = _entry(
            composite=0.80,
            size_bytes=500000,
            content_fingerprint=legacy,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            submitted_at=_CHALLENGER_SEEN,
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(shared),
            eligible=[incumbent],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert "comparison inconclusive" in (decision.reason or "")
        assert "lexical" in (decision.reason or "")

    def test_reference_corpus_transition_does_not_create_a_hold(self) -> None:
        incumbent_fp = _sk({f"{i:016x}" for i in range(30)})
        incumbent_fp["corpus"] = "starter-history-before-refresh"
        challenger_fp = _sk({f"{i:016x}" for i in range(30)})
        challenger_fp["corpus"] = "starter-history-after-refresh"
        incumbent = _entry(
            composite=0.80,
            size_bytes=500000,
            content_fingerprint=incumbent_fp,
        )

        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            submitted_at=_CHALLENGER_SEEN,
            miner_hotkey="5Independent",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=challenger_fp,
            eligible=[incumbent],
        )

        assert decision.held is False

    def test_corpus_transition_still_checks_same_corpus_references(self) -> None:
        shared = {f"{i:016x}" for i in range(30)}
        stale_fp = _sk(shared)
        stale_fp["corpus"] = "starter-history-before-refresh"
        current_fp = _sk(shared)
        current_fp["corpus"] = "starter-history-after-refresh"
        stale = _entry(
            composite=0.80,
            content_fingerprint=stale_fp,
            first_seen=_FIRST_SEEN,
        )
        current = _entry(
            composite=0.80,
            content_fingerprint=current_fp,
            first_seen=_FIRST_SEEN + timedelta(minutes=1),
        )

        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            submitted_at=_CHALLENGER_SEEN,
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=None,
            content_fingerprint=current_fp,
            eligible=[stale, current],
        )

        assert decision.held is True
        assert decision.duplicate_of == current.agent_id
        assert "content near-duplicate" in (decision.reason or "")

    def test_cross_version_structural_fallback_is_not_used(self) -> None:
        legacy_content = _sk({f"a{i:015x}" for i in range(30)})
        legacy_content["v"] = 1
        shared_structural = _sk({f"{i:016x}" for i in range(30)})
        incumbent = _entry(
            composite=0.80,
            content_fingerprint=legacy_content,
            structural_fingerprint=shared_structural,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            submitted_at=_CHALLENGER_SEEN,
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=None,
            content_fingerprint=_sk({f"b{i:015x}" for i in range(30)}),
            structural_fingerprint=shared_structural,
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "comparison inconclusive" in (decision.reason or "")
        assert "lexical" in (decision.reason or "")
        assert "structural near-duplicate" not in (decision.reason or "")

    def test_cross_version_far_score_is_not_held(self) -> None:
        legacy = _sk({f"{i:016x}" for i in range(30)})
        legacy["v"] = 1
        incumbent = _entry(composite=0.60, content_fingerprint=legacy)
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            submitted_at=_CHALLENGER_SEEN,
            miner_hotkey="5Challenger",
            sha256="bb" * 32,
            composite=0.80,
            size_bytes=None,
            content_fingerprint=_sk({f"{i:016x}" for i in range(30)}),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_missing_sizes_skip_near_dup(self) -> None:
        incumbent = _entry(composite=0.80, sha256="aa" * 32, size_bytes=None)
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=None,
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_deterministic_and_self_excluded(self) -> None:
        # An entry with the same agent_id is the agent itself (re-score): neither
        # rule may match it. Verdict is repeatable.
        me = uuid4()
        entry = LedgerRow(
            miner_hotkey="5Me",
            agent_id=me,
            composite=0.80,
            tool_mean=0.80,
            memory_mean=0.80,
            first_seen=_FIRST_SEEN,
            sha256="dd" * 32,
            size_bytes=500000,
            run_id="run_1",
            seed=42,
            validator_hotkey="5Validator",
            signature=None,
            status=AgentStatus.SCORED,
        )
        d1 = evaluate_duplicate_signals(
            agent_id=me,
            miner_hotkey="5Me",
            sha256="dd" * 32,
            composite=0.80,
            size_bytes=500000,
            eligible=[entry],
        )
        d2 = evaluate_duplicate_signals(
            agent_id=me,
            miner_hotkey="5Me",
            sha256="dd" * 32,
            composite=0.80,
            size_bytes=500000,
            eligible=[entry],
        )
        assert d1.held is False
        assert d1 == d2


class TestPromptShadowSignal:
    """Prompt fingerprint in the gate: shadow mode. It never creates a hold on
    its own; it only annotates the audit reason of a hold another rule fired."""

    def test_prompt_match_alone_never_holds(self) -> None:
        # Identical prompt sketch to the incumbent, but distant score and distinct
        # sha / size / lexical: the prompt signal must NOT hold on its own.
        shared = {"pp" + f"{i:02d}" for i in range(20)}
        incumbent = _entry(
            composite=0.60,
            sha256="cc" * 32,
            size_bytes=400000,
            prompt_fingerprint=_psk(shared),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Challenger",
            sha256="dd" * 32,
            composite=0.90,  # far from incumbent
            size_bytes=700000,  # far in size
            prompt_fingerprint=_psk(shared),
            eligible=[incumbent],
        )
        assert decision.held is False

    def test_prompt_corroboration_annotates_lexical_hold(self) -> None:
        # A lexical near-dup fires (rule 2); a shared prompt sketch adds a note.
        shingles = {f"s{i:03d}" for i in range(30)}
        shared_prompt = {"pp" + f"{i:02d}" for i in range(20)}
        incumbent = _entry(
            composite=0.80,
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shingles),
            prompt_fingerprint=_psk(shared_prompt),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(shingles),  # jaccard 1.0 -> rule 2 holds
            prompt_fingerprint=_psk(shared_prompt),
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "content near-duplicate" in (decision.reason or "")
        assert "prompt jaccard" in (decision.reason or "")

    def test_hold_without_prompt_sketch_has_no_note(self) -> None:
        # Same lexical hold, but no prompt sketches: reason must not mention prompt.
        shingles = {f"s{i:03d}" for i in range(30)}
        incumbent = _entry(
            composite=0.80, content_fingerprint=_sk(shingles), size_bytes=500000
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(shingles),
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "prompt" not in (decision.reason or "")

    def test_low_prompt_overlap_not_noted(self) -> None:
        # Lexical hold fires, prompt sketches present but nearly disjoint (below the
        # advisory tolerance): no prompt note is added.
        shingles = {f"s{i:03d}" for i in range(30)}
        incumbent = _entry(
            composite=0.80,
            size_bytes=500000,
            content_fingerprint=_sk(shingles),
            prompt_fingerprint=_psk({f"a{i:02d}" for i in range(20)}),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(shingles),
            prompt_fingerprint=_psk({f"b{i:02d}" for i in range(20)}),  # disjoint
            eligible=[incumbent],
        )
        assert decision.held is True
        assert "prompt" not in (decision.reason or "")


class TestAttestedOwnerLink:
    """The owner-link exemption.

    Models the real case: a miner's submissions stalled during an evaluation
    outage, they moved to a new coldkey/hotkey and resubmitted, and their newer
    work now copy-flags against their own earlier submission because the
    same-owner exemption keys on coldkey equality and they rotated.
    """

    def test_linked_hotkey_is_not_a_copy_source(self) -> None:
        """The Jupiter case: near-duplicate of one's own pre-rotation work."""
        shingles = {f"s{i:03d}" for i in range(40)}
        incumbent = _entry(
            composite=0.80,
            miner="5OldHotkey",
            coldkey="5OldColdkey",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shingles),
        )
        kwargs = {
            "agent_id": uuid4(),
            "miner_hotkey": "5NewHotkey",
            "miner_coldkey": "5NewColdkey",  # rotated: coldkey equality cannot help
            "sha256": "bb" * 32,
            "composite": 0.805,
            "size_bytes": 500100,
            "content_fingerprint": _sk(shingles),
            "eligible": [incumbent],
        }

        # Without the attestation this is held -- today's behaviour.
        assert evaluate_duplicate_signals(**kwargs).held is True

        # With it, the miner is not a copier of their own earlier work.
        decision = evaluate_duplicate_signals(
            **kwargs, linked_owner_hotkeys=frozenset({"5OldHotkey"})
        )
        assert decision.held is False

    def test_unlinked_hotkey_is_not_exempted(self) -> None:
        """An unrelated hotkey in the set exempts nothing."""
        shingles = {f"s{i:03d}" for i in range(40)}
        incumbent = _entry(
            composite=0.80,
            miner="5OldHotkey",
            size_bytes=500000,
            content_fingerprint=_sk(shingles),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5NewHotkey",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(shingles),
            eligible=[incumbent],
            linked_owner_hotkeys=frozenset({"5SomeoneElse"}),
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id

    def test_attestation_exempts_byte_identical_resubmission(self) -> None:
        """Exact generations within a proven owner pair are not plagiarism."""
        incumbent = _entry(
            composite=0.70, miner="5OldHotkey", sha256="cc" * 32, size_bytes=500000
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5NewHotkey",
            sha256="cc" * 32,
            composite=0.70,
            size_bytes=500000,
            eligible=[incumbent],
            linked_owner_hotkeys=frozenset({"5OldHotkey"}),
        )
        assert decision.held is False

    def test_attestation_exempts_repack(self) -> None:
        """Canonicalized-source equality is also exempt for the direct pair."""
        incumbent = _entry(
            composite=0.60,
            miner="5OldHotkey",
            sha256="cc" * 32,
            normalized_source_hash="ns" * 32,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5NewHotkey",
            sha256="dd" * 32,
            composite=0.60,
            size_bytes=incumbent.size_bytes,
            normalized_source_hash="ns" * 32,
            eligible=[incumbent],
            linked_owner_hotkeys=frozenset({"5OldHotkey"}),
        )
        assert decision.held is False

    def test_attestation_does_not_shield_a_third_partys_work(self) -> None:
        """A link exempts only its counterparty.

        A miner with a genuine owner link still gets screened against every
        other miner, so a link cannot be used as blanket cover.
        """
        shingles = {f"s{i:03d}" for i in range(40)}
        own_prior = _entry(
            composite=0.50,
            miner="5OldHotkey",
            size_bytes=100000,
            content_fingerprint=_sk({f"z{i:03d}" for i in range(40)}),
        )
        stranger = _entry(
            composite=0.80,
            miner="5Stranger",
            size_bytes=500000,
            content_fingerprint=_sk(shingles),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5NewHotkey",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(shingles),
            eligible=[own_prior, stranger],
            linked_owner_hotkeys=frozenset({"5OldHotkey"}),
        )
        assert decision.held is True
        assert decision.duplicate_of == stranger.agent_id

    def test_size_fallback_also_respects_the_exemption(self) -> None:
        """Rule 3, the legacy no-fingerprint path, is exempted too."""
        incumbent = _entry(
            composite=0.80, miner="5OldHotkey", sha256="aa" * 32, size_bytes=500000
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5NewHotkey",
            sha256="bb" * 32,
            composite=0.805,
            size_bytes=500100,
            eligible=[incumbent],
            linked_owner_hotkeys=frozenset({"5OldHotkey"}),
        )
        assert decision.held is False

    def test_empty_set_is_todays_behaviour(self) -> None:
        """The default must change nothing for the 99% who never rotated."""
        incumbent = _entry(composite=0.80, sha256="aa" * 32, size_bytes=500000)
        kwargs = {
            "agent_id": uuid4(),
            "miner_hotkey": "5Copier",
            "sha256": "bb" * 32,
            "composite": 0.805,
            "size_bytes": 500100,
            "eligible": [incumbent],
        }
        assert (
            evaluate_duplicate_signals(**kwargs).held
            is evaluate_duplicate_signals(
                **kwargs, linked_owner_hotkeys=frozenset()
            ).held
        )


_EMBARGO = timedelta(hours=120)
"""Revision 3 of the subnet source-release policy (2026-07-28): five days."""


def _released(entry: LedgerRow, *, available_at: datetime) -> PublicSourceRelease:
    """Declare that the subnet published ``entry``'s source at ``available_at``."""
    return PublicSourceRelease(agent_id=entry.agent_id, available_at=available_at)


class TestPublicReleaseNoCopyOpportunity:
    """Content the subnet itself published is not content this miner stole.

    SN118 serves chain-confirmed kings' tarballs on an unauthenticated route
    once the embargo lapses, and the subnet owner has confirmed that building on
    a released agent is permitted. The gate must stop treating the consequence
    of its own publication policy as plagiarism -- without going quiet on the
    thing it exists to catch, which is a copy of an artifact that is still
    private.
    """

    def test_reference_public_before_candidate_is_not_held(self) -> None:
        shared = {f"{i:016x}" for i in range(20)}
        published = _entry(
            composite=0.80,
            miner="5Original",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shared),
            first_seen=_FIRST_SEEN,
        )
        # Published two hours before this candidate was uploaded.
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Builder",
            sha256="bb" * 32,
            composite=0.81,
            size_bytes=520000,
            content_fingerprint=_sk(shared | {"ff" * 8}),
            eligible=[published],
            submitted_at=_FIRST_SEEN + _EMBARGO + timedelta(hours=2),
            public_source_releases=[
                _released(published, available_at=_FIRST_SEEN + _EMBARGO)
            ],
        )
        assert decision.held is False
        assert decision.duplicate_of is None
        assert decision.reason is None
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.kind == "public_release"
        assert withdrawal.signal == "lexical"
        assert withdrawal.matched_agent_id == published.agent_id
        assert withdrawal.source_agent_id == published.agent_id

    def test_reference_still_inside_its_embargo_is_still_held(self) -> None:
        """The core case the screen exists for must keep firing."""
        shared = {f"{i:016x}" for i in range(20)}
        incumbent = _entry(
            composite=0.80,
            miner="5Original",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shared),
            first_seen=_FIRST_SEEN,
        )
        # Uploaded one hour before that artifact's source would have been served.
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.81,
            size_bytes=520000,
            content_fingerprint=_sk(shared | {"ff" * 8}),
            eligible=[incumbent],
            submitted_at=_FIRST_SEEN + _EMBARGO - timedelta(hours=1),
            public_source_releases=[
                _released(incumbent, available_at=_FIRST_SEEN + _EMBARGO)
            ],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id
        assert decision.no_copy_opportunity is None

    def test_disclosure_not_public_still_holds(self) -> None:
        """Under ``disclosure = never`` nothing is published, so nothing is exempt.

        The caller expresses that by passing no releases at all -- see
        :func:`ditto.db.queries.artifact_release.list_public_source_releases`,
        which returns ``{}`` whenever the policy in force does not release
        publicly.
        """
        shared = {f"{i:016x}" for i in range(20)}
        incumbent = _entry(
            composite=0.80,
            miner="5Original",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shared),
            first_seen=_FIRST_SEEN,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.81,
            size_bytes=520000,
            content_fingerprint=_sk(shared | {"ff" * 8}),
            eligible=[incumbent],
            # Long after upload: under a public policy this would be well past
            # the window. The empty release set is the whole difference.
            submitted_at=_FIRST_SEEN + timedelta(days=400),
            public_source_releases=[],
        )
        assert decision.held is True
        assert decision.duplicate_of == incumbent.agent_id

    def test_exact_byte_copy_of_a_published_artifact_is_not_held(self) -> None:
        """Equality admits no ordering: a published twin is a complete account."""
        published = _entry(
            composite=0.80,
            miner="5Original",
            sha256="cc" * 32,
            size_bytes=500000,
            first_seen=_FIRST_SEEN,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Redeployer",
            sha256="cc" * 32,
            composite=0.80,
            size_bytes=500000,
            eligible=[published],
            submitted_at=_FIRST_SEEN + _EMBARGO + timedelta(hours=1),
            public_source_releases=[
                _released(published, available_at=_FIRST_SEEN + _EMBARGO)
            ],
        )
        assert decision.held is False
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.signal == "exact_byte"

    def test_exact_byte_copy_of_a_private_artifact_is_still_held(self) -> None:
        private = _entry(
            composite=0.80,
            miner="5Original",
            sha256="cc" * 32,
            size_bytes=500000,
            first_seen=_FIRST_SEEN,
        )
        unrelated_public = _entry(
            composite=0.60,
            miner="5Someone",
            sha256="dd" * 32,
            size_bytes=400000,
            first_seen=_FIRST_SEEN,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="cc" * 32,
            composite=0.80,
            size_bytes=500000,
            eligible=[private, unrelated_public],
            submitted_at=_FIRST_SEEN + _EMBARGO + timedelta(hours=1),
            public_source_releases=[
                _released(unrelated_public, available_at=_FIRST_SEEN + _EMBARGO)
            ],
        )
        assert decision.held is True
        assert decision.duplicate_of == private.agent_id

    def test_closer_match_to_an_embargoed_artifact_still_holds(self) -> None:
        """A published near-relative does not launder a closer private match.

        Both artifacts descend from one codebase, so both clear the threshold.
        The candidate carries the still-private one's custom surface verbatim and
        only partly overlaps the published one, so it took something that was
        never published -- and the strength ordering says so.
        """
        base = {f"{i:016x}" for i in range(20)}
        published = _entry(
            composite=0.78,
            miner="5Publisher",
            sha256="aa" * 32,
            size_bytes=480000,
            content_fingerprint=_sk(base | {f"pub{i:013x}" for i in range(20)}),
            first_seen=_FIRST_SEEN,
        )
        embargoed = _entry(
            composite=0.80,
            miner="5Original",
            sha256="bb" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(base | {f"new{i:013x}" for i in range(20)}),
            first_seen=_FIRST_SEEN + timedelta(hours=1),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="cc" * 32,
            composite=0.805,
            size_bytes=500100,
            content_fingerprint=_sk(base | {f"new{i:013x}" for i in range(20)}),
            eligible=[published, embargoed],
            submitted_at=_FIRST_SEEN + _EMBARGO + timedelta(hours=2),
            public_source_releases=[
                _released(published, available_at=_FIRST_SEEN + _EMBARGO)
            ],
        )
        assert decision.held is True
        assert decision.duplicate_of == embargoed.agent_id

    def test_withdrawal_for_one_reference_does_not_excuse_another(self) -> None:
        """Withdrawal is per-pair; the loop must keep checking."""
        public_shared = {f"{i:016x}" for i in range(20)}
        private_shared = {f"secret{i:010x}" for i in range(20)}
        published = _entry(
            composite=0.70,
            miner="5Publisher",
            sha256="aa" * 32,
            size_bytes=400000,
            content_fingerprint=_sk(public_shared),
            first_seen=_FIRST_SEEN,
        )
        embargoed = _entry(
            composite=0.80,
            miner="5Original",
            sha256="bb" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(private_shared),
            first_seen=_FIRST_SEEN + timedelta(hours=1),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="cc" * 32,
            composite=0.81,
            size_bytes=520000,
            # Carries both codebases: the published one lawfully, the other not.
            content_fingerprint=_sk(public_shared | private_shared),
            eligible=[published, embargoed],
            submitted_at=_FIRST_SEEN + _EMBARGO + timedelta(hours=2),
            public_source_releases=[
                _released(published, available_at=_FIRST_SEEN + _EMBARGO)
            ],
        )
        assert decision.held is True
        assert decision.duplicate_of == embargoed.agent_id

    def test_publication_after_the_candidate_upload_does_not_exempt(self) -> None:
        """Release is judged at the candidate's upload time, not at score time."""
        shared = {f"{i:016x}" for i in range(20)}
        incumbent = _entry(
            composite=0.80,
            miner="5Original",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(shared),
            first_seen=_FIRST_SEEN,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            sha256="bb" * 32,
            composite=0.81,
            size_bytes=520000,
            content_fingerprint=_sk(shared | {"ff" * 8}),
            eligible=[incumbent],
            submitted_at=_FIRST_SEEN + timedelta(hours=6),
            public_source_releases=[
                _released(incumbent, available_at=_FIRST_SEEN + timedelta(hours=7))
            ],
        )
        assert decision.held is True

    def test_white_bolt_reproducing_published_red_dragon(self) -> None:
        """The concrete production case (both bans since reversed by hand).

        red-dragon v12 (``dc4b4c0d``) was uploaded 2026-08-04, crowned, weight-
        confirmed 11:46Z the same day, and its source went public 2026-08-09
        11:46Z under the 120-hour policy. white-bolt v1 was uploaded 2026-08-10
        and v2 on 2026-08-11 -- both after publication -- and both were held and
        then banned for reproducing red-dragon's engine.

        The detail production got wrong twice over: the hold on white-bolt v1
        named red-dragon **v16** (``e86e42f1``, uploaded 2026-08-07), the
        *nearest* earlier match, which was never published. The reference in the
        hold record was private while the content was not, so the withdrawal has
        to be driven by whichever artifact accounts for the shared content, not
        by whichever one the ledger happened to name.
        """
        engine = {f"engine{i:010x}" for i in range(30)}
        v12 = _entry(
            composite=0.90,
            miner="5DcpbvmTro",
            sha256="12" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(engine),
            first_seen=datetime(2026, 8, 4, 6, 21, 53, tzinfo=UTC),
        )
        v16 = _entry(
            composite=0.93,
            miner="5DcpbvmTro",
            sha256="16" * 32,
            size_bytes=505000,
            content_fingerprint=_sk(engine | {f"v16{i:013x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 7, 14, 12, 32, tzinfo=UTC),
        )
        v12_public_at = datetime(2026, 8, 9, 11, 46, 31, tzinfo=UTC)
        white_bolt_v1 = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5HTUbRK5bN",
            sha256="wb" * 32,
            composite=0.91,
            size_bytes=510000,
            content_fingerprint=_sk(engine | {f"wb{i:014x}" for i in range(2)}),
            eligible=[v12, v16],
            submitted_at=datetime(2026, 8, 10, 18, 19, 33, tzinfo=UTC),
            public_source_releases=[_released(v12, available_at=v12_public_at)],
        )
        assert white_bolt_v1.held is False
        assert white_bolt_v1.duplicate_of is None
        withdrawal = white_bolt_v1.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.kind == "public_release"
        assert withdrawal.source_agent_id == v12.agent_id

        white_bolt_v2 = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5HTUbRK5bN",
            sha256="w2" * 32,
            composite=0.92,
            size_bytes=512000,
            content_fingerprint=_sk(engine | {f"w2{i:014x}" for i in range(3)}),
            eligible=[v12, v16],
            submitted_at=datetime(2026, 8, 11, 14, 39, 45, tzinfo=UTC),
            public_source_releases=[_released(v12, available_at=v12_public_at)],
        )
        assert white_bolt_v2.held is False


class TestSelfLineagePrecedence:
    """An originator must not be held against its own downstream copy.

    When a published codebase spreads, the nearest earlier match is often the
    newest recipient rather than the source. red-dragon v18 was held against
    astrion-v9 -- whose entire history is two submissions -- while red-dragon had
    nineteen. Content this owner already shipped cannot have come from a
    submission that did not yet exist.
    """

    def test_owner_row_predating_the_match_withdraws_the_hold(self) -> None:
        engine = {f"engine{i:010x}" for i in range(30)}
        own_prior = _entry(
            composite=0.93,
            miner="5DcpbvmTro",
            coldkey="5RedDragonCold",
            sha256="17" * 32,
            size_bytes=505000,
            content_fingerprint=_sk(engine),
            first_seen=datetime(2026, 8, 9, 15, 49, 16, tzinfo=UTC),
        )
        recipient = _entry(
            composite=0.94,
            miner="5CkuRmNC5R",
            coldkey="5AstrionCold",
            sha256="a9" * 32,
            size_bytes=507000,
            content_fingerprint=_sk(engine | {f"as{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 17, 37, 48, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5DcpbvmTro",
            miner_coldkey="5RedDragonCold",
            sha256="18" * 32,
            composite=0.95,
            size_bytes=509000,
            content_fingerprint=_sk(engine | {f"v18{i:013x}" for i in range(2)}),
            eligible=[own_prior, recipient],
            submitted_at=datetime(2026, 8, 12, 10, 40, 3, tzinfo=UTC),
        )
        assert decision.held is False
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.kind == "self_lineage"
        assert withdrawal.matched_agent_id == recipient.agent_id
        assert withdrawal.source_agent_id == own_prior.agent_id

    def test_owner_row_postdating_the_match_is_no_alibi(self) -> None:
        """Order is the whole argument: a later own row proves nothing.

        If this owner only acquired the shared surface *after* the reference
        existed, its own earlier generation cannot explain where the surface
        came from, and the hold stands.
        """
        engine = {f"engine{i:010x}" for i in range(30)}
        stranger = _entry(
            composite=0.90,
            miner="5Stranger",
            coldkey="5StrangerCold",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(engine),
            first_seen=_FIRST_SEEN,
        )
        own_prior = _entry(
            composite=0.91,
            miner="5Copier",
            coldkey="5CopierCold",
            sha256="bb" * 32,
            size_bytes=502000,
            content_fingerprint=_sk(engine),
            first_seen=_FIRST_SEEN + timedelta(hours=1),
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            miner_coldkey="5CopierCold",
            sha256="cc" * 32,
            composite=0.92,
            size_bytes=503000,
            content_fingerprint=_sk(engine),
            eligible=[stranger, own_prior],
            submitted_at=_FIRST_SEEN + timedelta(hours=2),
        )
        assert decision.held is True
        assert decision.duplicate_of == stranger.agent_id

    def test_weaker_own_row_is_no_alibi(self) -> None:
        """The owner's own history must actually account for the shared surface."""
        engine = {f"engine{i:010x}" for i in range(30)}
        stranger = _entry(
            composite=0.90,
            miner="5Stranger",
            coldkey="5StrangerCold",
            sha256="aa" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(engine),
            first_seen=_FIRST_SEEN + timedelta(hours=1),
        )
        unrelated_own = _entry(
            composite=0.50,
            miner="5Copier",
            coldkey="5CopierCold",
            sha256="bb" * 32,
            size_bytes=300000,
            content_fingerprint=_sk({f"mine{i:012x}" for i in range(30)}),
            first_seen=_FIRST_SEEN,
        )
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="5Copier",
            miner_coldkey="5CopierCold",
            sha256="cc" * 32,
            composite=0.905,
            size_bytes=501000,
            content_fingerprint=_sk(engine),
            eligible=[stranger, unrelated_own],
            submitted_at=_FIRST_SEEN + timedelta(hours=2),
        )
        assert decision.held is True
        assert decision.duplicate_of == stranger.agent_id


def _id(prefix: str) -> UUID:
    """A stable UUID carrying a production agent id's real 8-hex prefix.

    The four inversion cases below and the pinned true positive are real
    production adjudications. Only the first octet of each agent id was recorded
    in the incident write-up, so the remainder is deterministic padding; the
    prefix is what ties a fixture to the artifact it reproduces.
    """
    return UUID(f"{prefix}-0000-4000-8000-000000000000")


class TestEarliestSourceAttribution:
    """A surviving hold must name the earliest artifact carrying the match.

    Four production holds were adjudicated by hand because the gate named the
    wrong party. All four share one mechanism: ``list_eligible_ledger`` returns
    one representative row per payment coldkey, so the nearest *visible* earlier
    match is routinely an intermediate recipient of a spreading codebase rather
    than its source — and the owner's own earlier generations, which prove they
    had the surface first, are not in that view at all.

    Every case below therefore puts the owner's real history in
    ``eligible_history`` and only the representative rows in ``eligible``, which
    is what the score path actually sees.
    """

    # Shared custom module set: the surface these submissions have in common.
    ENGINE = {f"engine{i:010x}" for i in range(40)}

    def test_red_dragon_v18_is_not_a_duplicate_of_astrion_v1(self) -> None:
        """Case 1: a 14-day developer held as a copy of a two-day-old newcomer.

        red-dragon v18 (``69bf2cc5``) was held naming astrion-v9 v1
        (``516f2a9b``) — an owner whose entire history is two submissions, the
        first of which postdates red-dragon v17 by two days. red-dragon's own
        v17 (``dd8bd390``) already carried the complete shared module set, and
        the same coldkey had been shipping it as ``kingbear-mem-v1`` since
        2026-07-25. The owner's representative row is a *later* generation, so
        the per-pair self-lineage test cannot see the alibi; only the full
        history can.
        """
        kingbear = _entry(
            agent_id=_id("6b17b3a4"),
            composite=0.91,
            miner="5KingbearHk",
            coldkey="5HgisASb3W",
            sha256="1a" * 32,
            size_bytes=498000,
            content_fingerprint=_sk(self.ENGINE),
            first_seen=datetime(2026, 7, 25, 9, 12, 0, tzinfo=UTC),
        )
        red_dragon_v17 = _entry(
            agent_id=_id("dd8bd390"),
            composite=0.93,
            miner="5DcpbvmTro",
            coldkey="5HgisASb3W",
            sha256="17" * 32,
            size_bytes=505000,
            content_fingerprint=_sk(self.ENGINE | {f"v17{i:013x}" for i in range(3)}),
            first_seen=datetime(2026, 8, 9, 15, 49, 16, tzinfo=UTC),
        )
        astrion_v1 = _entry(
            agent_id=_id("516f2a9b"),
            composite=0.94,
            miner="5CkuRmNC5R",
            coldkey="5AstrionCold",
            sha256="a9" * 32,
            size_bytes=507000,
            content_fingerprint=_sk(self.ENGINE | {f"as{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 17, 37, 48, tzinfo=UTC),
        )
        # What owner reduction actually leaves visible for red-dragon's coldkey:
        # its best generation, which postdates astrion v1.
        red_dragon_rep = _entry(
            agent_id=_id("aa11bb22"),
            composite=0.945,
            miner="5DcpbvmTro",
            coldkey="5HgisASb3W",
            sha256="2b" * 32,
            size_bytes=508000,
            content_fingerprint=_sk(self.ENGINE | {f"rep{i:013x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 20, 5, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("69bf2cc5"),
            miner_hotkey="5DcpbvmTro",
            miner_coldkey="5HgisASb3W",
            sha256="18" * 32,
            composite=0.95,
            size_bytes=509000,
            content_fingerprint=_sk(self.ENGINE | {f"v18{i:013x}" for i in range(2)}),
            eligible=[astrion_v1, red_dragon_rep],
            eligible_history=[kingbear, red_dragon_v17, astrion_v1, red_dragon_rep],
            submitted_at=datetime(2026, 8, 12, 10, 40, 3, tzinfo=UTC),
        )
        assert decision.held is False
        assert decision.duplicate_of is None
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.kind == "self_lineage"
        assert withdrawal.matched_agent_id == astrion_v1.agent_id
        # The earliest artifact carrying the shared engine is this owner's own,
        # reached across two hotkeys on one payment coldkey. red-dragon v17 would
        # alibi it alone; kingbear is named because it is older still.
        assert withdrawal.source_agent_id == kingbear.agent_id
        assert red_dragon_v17.agent_id != withdrawal.source_agent_id

    def test_red_dragon_case_still_inverts_without_full_history(self) -> None:
        """The same inputs minus the history are exactly the production bug.

        Pinned so a regression that stops threading ``eligible_history`` fails
        loudly here rather than silently re-accusing the originator.
        """
        astrion_v1 = _entry(
            agent_id=_id("516f2a9b"),
            composite=0.94,
            miner="5CkuRmNC5R",
            coldkey="5AstrionCold",
            content_fingerprint=_sk(self.ENGINE | {f"as{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 17, 37, 48, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("69bf2cc5"),
            miner_hotkey="5DcpbvmTro",
            miner_coldkey="5HgisASb3W",
            sha256="18" * 32,
            composite=0.95,
            size_bytes=509000,
            content_fingerprint=_sk(self.ENGINE | {f"v18{i:013x}" for i in range(2)}),
            eligible=[astrion_v1],
            submitted_at=datetime(2026, 8, 12, 10, 40, 3, tzinfo=UTC),
        )
        assert decision.held is True
        assert decision.duplicate_of == astrion_v1.agent_id

    def test_kaelith_v3_is_not_a_duplicate_of_banblackycat_v7(self) -> None:
        """Case 2: held against a reference its own v1 predates by 4h52m.

        Kaelith-ditto-miner v3 (``14fa4404``) was held naming banblackycat v7
        (``98d56bdf``, 2026-08-11T19:05). Kaelith's own v1 (``6fd58c8d``,
        2026-08-11T14:13) already carried the whole codebase. banblackycat v6 is
        in the history and is older still, but it is not in ``eligible`` and
        history is never a trigger surface, so it must not become a replacement
        accusation.
        """
        banblackycat_v6 = _entry(
            agent_id=_id("9603b036"),
            composite=0.88,
            miner="5Dvj3htj",
            coldkey="5BanColdkey",
            sha256="b6" * 32,
            size_bytes=494000,
            content_fingerprint=_sk(self.ENGINE),
            first_seen=datetime(2026, 8, 9, 3, 10, 0, tzinfo=UTC),
        )
        kaelith_v1 = _entry(
            agent_id=_id("6fd58c8d"),
            composite=0.90,
            miner="5EnPyord",
            coldkey="5KaelithCold",
            sha256="3c" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(self.ENGINE | {f"ka{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 14, 13, 0, tzinfo=UTC),
        )
        banblackycat_v7 = _entry(
            agent_id=_id("98d56bdf"),
            composite=0.92,
            miner="5Dvj3htj",
            coldkey="5BanColdkey",
            sha256="b7" * 32,
            size_bytes=503000,
            content_fingerprint=_sk(self.ENGINE | {f"bb{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 19, 5, 0, tzinfo=UTC),
        )
        kaelith_v2 = _entry(
            agent_id=_id("cc33dd44"),
            composite=0.925,
            miner="5EnPyord",
            coldkey="5KaelithCold",
            sha256="4d" * 32,
            size_bytes=504000,
            content_fingerprint=_sk(self.ENGINE | {f"k2{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 21, 0, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("14fa4404"),
            miner_hotkey="5EnPyord",
            miner_coldkey="5KaelithCold",
            sha256="5e" * 32,
            composite=0.93,
            size_bytes=506000,
            content_fingerprint=_sk(self.ENGINE | {f"k3{i:014x}" for i in range(2)}),
            # Owner reduction leaves the *later* v2 as Kaelith's representative.
            eligible=[banblackycat_v7, kaelith_v2],
            eligible_history=[
                banblackycat_v6,
                kaelith_v1,
                banblackycat_v7,
                kaelith_v2,
            ],
            submitted_at=datetime(2026, 8, 12, 8, 30, 0, tzinfo=UTC),
        )
        assert decision.held is False
        assert decision.duplicate_of is None
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.matched_agent_id == banblackycat_v7.agent_id
        assert withdrawal.source_agent_id == kaelith_v1.agent_id

    def test_whitycatboss_v3_is_not_a_duplicate_of_kaelith_v1(self) -> None:
        """Case 3: the other half of a bidirectional pair.

        whitycatboss v3 (``0dbb3d4e``, hotkey ``5Dvj3htj``) was held naming
        Kaelith v1. That same hotkey's banblackycat v6 (``9603b036``,
        2026-08-09T03:10) predates the reference by 2.5 days. Cases 2 and 3
        together are two operators developing a common ancestor in parallel,
        each accused of copying the other; both accusations must be withdrawn.
        """
        banblackycat_v6 = _entry(
            agent_id=_id("9603b036"),
            composite=0.88,
            miner="5Dvj3htj",
            coldkey="5BanColdkey",
            sha256="b6" * 32,
            size_bytes=494000,
            content_fingerprint=_sk(self.ENGINE),
            first_seen=datetime(2026, 8, 9, 3, 10, 0, tzinfo=UTC),
        )
        kaelith_v1 = _entry(
            agent_id=_id("6fd58c8d"),
            composite=0.90,
            miner="5EnPyord",
            coldkey="5KaelithCold",
            sha256="3c" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(self.ENGINE | {f"ka{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 14, 13, 0, tzinfo=UTC),
        )
        whity_rep = _entry(
            agent_id=_id("ee55ff66"),
            composite=0.925,
            miner="5Dvj3htj",
            coldkey="5BanColdkey",
            sha256="6f" * 32,
            size_bytes=505000,
            content_fingerprint=_sk(self.ENGINE | {f"w2{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 22, 0, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("0dbb3d4e"),
            miner_hotkey="5Dvj3htj",
            miner_coldkey="5BanColdkey",
            sha256="7a" * 32,
            composite=0.93,
            size_bytes=507000,
            content_fingerprint=_sk(self.ENGINE | {f"w3{i:014x}" for i in range(2)}),
            eligible=[kaelith_v1, whity_rep],
            eligible_history=[banblackycat_v6, kaelith_v1, whity_rep],
            submitted_at=datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC),
        )
        assert decision.held is False
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.matched_agent_id == kaelith_v1.agent_id
        assert withdrawal.source_agent_id == banblackycat_v6.agent_id

    def test_kaelith_v2_earlier_pair_is_not_held(self) -> None:
        """Case 4: the same inversion one generation earlier.

        Kaelith v2 held against a banblackycat generation that postdates
        Kaelith's own v1. Same shape, same withdrawal.
        """
        kaelith_v1 = _entry(
            agent_id=_id("6fd58c8d"),
            composite=0.90,
            miner="5EnPyord",
            coldkey="5KaelithCold",
            sha256="3c" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(self.ENGINE | {f"ka{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 14, 13, 0, tzinfo=UTC),
        )
        banblackycat_v7 = _entry(
            agent_id=_id("98d56bdf"),
            composite=0.92,
            miner="5Dvj3htj",
            coldkey="5BanColdkey",
            sha256="b7" * 32,
            size_bytes=503000,
            content_fingerprint=_sk(self.ENGINE | {f"bb{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 19, 5, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("cc33dd44"),
            miner_hotkey="5EnPyord",
            miner_coldkey="5KaelithCold",
            sha256="4d" * 32,
            composite=0.925,
            size_bytes=504000,
            content_fingerprint=_sk(self.ENGINE | {f"k2{i:014x}" for i in range(2)}),
            # Kaelith's representative is the held v2 itself, so nothing of this
            # owner's own history survives owner reduction into `eligible`.
            eligible=[banblackycat_v7],
            eligible_history=[kaelith_v1, banblackycat_v7],
            submitted_at=datetime(2026, 8, 11, 21, 0, 0, tzinfo=UTC),
        )
        assert decision.held is False
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.source_agent_id == kaelith_v1.agent_id

    def test_beking_v1_copy_of_gggggggg_v3_is_still_held(self) -> None:
        """The pinned true positive: this must keep firing.

        beking-v1 (``16eddfaf``) uploaded 67 minutes after gggggggg v3
        (``81266c17``) under a different coldkey, with ``src/baseline.rs`` at
        0.9943 similarity — +6/-10 of 1,408 lines. That figure is the file-level
        diff measurement; the gate compares 256-member bottom-k sketches, which
        at this edit distance agree completely, so the reason prints 1.000. The
        shingle counts below keep the production scale. beking has no prior
        submission, so no owner history can excuse it.
        """
        shared = {f"baseline{i:012x}" for i in range(1398)}
        gggggggg_v3 = _entry(
            agent_id=_id("81266c17"),
            composite=0.8871,
            miner="5Gggggggg",
            coldkey="5GgggColdkey",
            sha256="8b" * 32,
            size_bytes=511000,
            content_fingerprint=_sk(shared | {f"gonly{i:011x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 10, 11, 2, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("16eddfaf"),
            miner_hotkey="5Beking",
            miner_coldkey="5BekingColdkey",
            sha256="9c" * 32,
            composite=0.8904,
            size_bytes=511400,
            content_fingerprint=_sk(shared | {f"bonly{i:011x}" for i in range(6)}),
            eligible=[gggggggg_v3],
            eligible_history=[gggggggg_v3],
            submitted_at=datetime(2026, 8, 10, 12, 9, 0, tzinfo=UTC),
        )
        assert decision.held is True
        assert decision.duplicate_of == gggggggg_v3.agent_id
        assert decision.no_copy_opportunity is None
        assert "content near-duplicate" in (decision.reason or "")

    def test_hold_retargets_to_the_originator_not_the_intermediate(self) -> None:
        """The positive half of earliest-source selection.

        A genuine copier with no owner history still gets held — but named
        against the *earliest* artifact carrying the surface, not the
        intermediate recipient that happened to be nearest in the ledger. This
        is the harm case 1 describes, with the copier's alibi removed.
        """
        originator = _entry(
            agent_id=_id("0a0a0a0a"),
            composite=0.90,
            miner="5Originator",
            coldkey="5OriginCold",
            sha256="0a" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(self.ENGINE),
            first_seen=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
        )
        intermediate = _entry(
            agent_id=_id("0b0b0b0b"),
            composite=0.93,
            miner="5Intermediate",
            coldkey="5InterCold",
            sha256="0b" * 32,
            size_bytes=502000,
            content_fingerprint=_sk(self.ENGINE | {f"im{i:014x}" for i in range(2)}),
            first_seen=datetime(2026, 8, 11, 8, 0, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("0c0c0c0c"),
            miner_hotkey="5Newcomer",
            miner_coldkey="5NewcomerCold",
            sha256="0c" * 32,
            composite=0.94,
            size_bytes=503000,
            content_fingerprint=_sk(self.ENGINE | {f"nc{i:014x}" for i in range(2)}),
            # Only the intermediate survives owner reduction as a visible match.
            eligible=[intermediate],
            eligible_history=[originator, intermediate],
            submitted_at=datetime(2026, 8, 12, 8, 0, 0, tzinfo=UTC),
        )
        assert decision.held is True
        assert decision.duplicate_of == originator.agent_id
        assert "earliest artifact in the matching cluster" in (decision.reason or "")

    def test_history_alone_never_opens_a_new_hold(self) -> None:
        """History is an attribution and alibi surface, never a trigger surface.

        An artifact that matches only a *history* row — one owner reduction hid
        from the pool — must not be held. Widening what the gate can see must
        never widen what it accuses.
        """
        hidden = _entry(
            agent_id=_id("0d0d0d0d"),
            composite=0.90,
            miner="5Hidden",
            coldkey="5HiddenCold",
            sha256="0d" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(self.ENGINE),
            first_seen=datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC),
        )
        unrelated = _entry(
            agent_id=_id("0e0e0e0e"),
            composite=0.60,
            miner="5Unrelated",
            coldkey="5UnrelatedCold",
            sha256="0e" * 32,
            size_bytes=300000,
            content_fingerprint=_sk({f"other{i:011x}" for i in range(40)}),
            first_seen=datetime(2026, 8, 2, 8, 0, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("0f0f0f0f"),
            miner_hotkey="5Candidate",
            miner_coldkey="5CandidateCold",
            sha256="0f" * 32,
            composite=0.91,
            size_bytes=501000,
            content_fingerprint=_sk(self.ENGINE),
            eligible=[unrelated],
            eligible_history=[hidden, unrelated],
            submitted_at=datetime(2026, 8, 12, 8, 0, 0, tzinfo=UTC),
        )
        assert decision.held is False
        assert decision.no_copy_opportunity is None

    def test_owner_holding_the_earliest_cluster_member_is_withdrawn(self) -> None:
        """``self_origin``: the backstop when pairwise ranking is too strict.

        The per-pair alibi requires this owner's earlier row to resemble the
        candidate *at least as much as the reference does*. An owner whose
        codebase has since moved on fails that bar against a nearer reference
        while still holding the earliest artifact in the cluster — and being
        earliest than everything that matches is the stronger argument, because
        it rules out every member of the cluster at once rather than one pair.
        """
        base = {f"shared{i:012x}" for i in range(100)}
        own_first = _entry(
            agent_id=_id("1a1a1a1a"),
            composite=0.80,
            miner="5Owner",
            coldkey="5OwnerCold",
            sha256="1a" * 32,
            size_bytes=500000,
            content_fingerprint=_sk(base | {f"own{i:013x}" for i in range(10)}),
            first_seen=datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC),
        )
        reference = _entry(
            agent_id=_id("1b1b1b1b"),
            composite=0.92,
            miner="5Other",
            coldkey="5OtherCold",
            sha256="1b" * 32,
            size_bytes=502000,
            content_fingerprint=_sk(base | {f"ref{i:013x}" for i in range(7)}),
            first_seen=datetime(2026, 8, 1, 8, 0, 0, tzinfo=UTC),
        )
        decision = evaluate_duplicate_signals(
            agent_id=_id("1c1c1c1c"),
            miner_hotkey="5Owner",
            miner_coldkey="5OwnerCold",
            sha256="1c" * 32,
            composite=0.93,
            size_bytes=503000,
            content_fingerprint=_sk(base | {f"cand{i:012x}" for i in range(18)}),
            eligible=[reference],
            eligible_history=[own_first, reference],
            submitted_at=datetime(2026, 8, 12, 8, 0, 0, tzinfo=UTC),
        )
        assert decision.held is False
        withdrawal = decision.no_copy_opportunity
        assert withdrawal is not None
        assert withdrawal.kind == "self_origin"
        assert withdrawal.matched_agent_id == reference.agent_id
        assert withdrawal.source_agent_id == own_first.agent_id
