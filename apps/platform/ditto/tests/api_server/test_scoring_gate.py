"""Unit tests for the pure anti-copy gate :mod:`ditto.api_server.scoring_gate`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.fingerprint import _FP_VERSION, _MINHASH_K, _PROMPT_VERSION
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
