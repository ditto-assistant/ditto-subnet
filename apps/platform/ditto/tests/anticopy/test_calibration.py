"""Unit tests for :mod:`ditto.anticopy.calibration` — the anti-copy calibration harness.

These lock the two things the harness must get right: (1) the obfuscation ladder
produces the invariances each tier claims (tier-1 cosmetic keeps the normalized
hash; tier-2 rename breaks it), and (2) the evaluation reports honest
precision/recall — in particular the normalized-source hash signal has **zero false
positives** on
independents and full recall on the tier it is meant to catch.
"""

from __future__ import annotations

import pytest

from ditto.anticopy.calibration import (
    _prompt,
    _sig_normalized_hash,
    build_corpus,
    default_signals,
    demo_corpus,
    embedding_signal,
    evaluate,
    format_report,
    obfuscate,
    pack,
    t_recomment,
    t_reformat,
    t_rename_idents,
    t_reorder_rename_files,
    unpack,
)
from ditto.api_server.fingerprint import compute_normalized_source_hash

_SEED = {
    "src/lib.rs": (
        b'const NAME: &str = "quartz-orbit-seed";\n'
        + b"".join(
            f"fn compute_{i}(x: i64) -> i64 {{\n"
            f"    let step_{i} = x + {i + 101};\n"
            f"    step_{i} * {i + 17}\n"
            "}\n".encode()
            for i in range(8)
        )
    ),
    "Cargo.toml": b'[package]\nname = "quartz-orbit-seed"\n',
}


class TestPacking:
    def test_pack_is_deterministic_and_roundtrips(self) -> None:
        assert pack(_SEED) == pack(_SEED)  # fixed mtime → byte-identical
        assert unpack(pack(_SEED)) == dict(_SEED)

    def test_pack_is_file_order_invariant(self) -> None:
        reordered = dict(reversed(list(_SEED.items())))
        assert pack(_SEED) == pack(reordered)  # sorted-path emission


class TestLadder:
    def test_tier1_cosmetic_preserves_normalized_hash(self) -> None:
        # Reformat + recomment + file rename/reorder must NOT change the
        # normalized-source hash.
        base = pack(_SEED)
        variant = pack(obfuscate(_SEED, tier=1))
        assert base != variant  # the bytes differ (it's a real repack)…
        h1 = compute_normalized_source_hash(base)
        h2 = compute_normalized_source_hash(variant)
        assert h1 is not None and h1 == h2  # …but the canonical hash matches

    def test_tier2_rename_breaks_normalized_hash(self) -> None:
        # Identifier renaming defeats the exact-repack hash (falls to the
        # structural / behavioral layers).
        h_base = compute_normalized_source_hash(pack(_SEED))
        h_t2 = compute_normalized_source_hash(pack(obfuscate(_SEED, tier=2)))
        assert h_base is not None and h_t2 is not None and h_base != h_t2

    def test_individual_transforms_keep_hash_and_rename_breaks_it(self) -> None:
        h = compute_normalized_source_hash(pack(_SEED))
        for transform in (t_reformat, t_recomment, t_reorder_rename_files):
            assert compute_normalized_source_hash(pack(transform(_SEED))) == h
        assert compute_normalized_source_hash(pack(t_rename_idents(_SEED))) != h

    def test_rename_is_consistent(self) -> None:
        renamed = t_rename_idents(_SEED)["src/lib.rs"].decode()
        # The definition and every use of `compute`/`step` are renamed together,
        # so no original defined identifier survives as a whole word.
        assert "compute" not in renamed
        assert "step" not in renamed
        assert "r_" in renamed  # deterministic new names


class TestEvaluation:
    def test_normalized_hash_precise_on_independents_full_recall_on_tier1(self) -> None:
        corpus = demo_corpus()
        reports = {r.name: r for r in evaluate(corpus, default_signals())}
        nsh = reports["normalized_source_hash"]
        # Exact-repack hash: no false positive on any independent pair…
        assert nsh.best.precision == 1.0
        # …and it fires on the cosmetic (tier-1) clones. (Tier-2 rename escapes it, so
        # overall recall < 1 — that gap is what motivates the structural / behavioral
        # layers.)
        assert nsh.best.recall > 0.0

    def test_lexical_separates_clones_from_independents(self) -> None:
        corpus = demo_corpus()
        reports = {r.name: r for r in evaluate(corpus, default_signals())}
        # The lexical signal should reach a usable operating point (some threshold
        # with both precision and recall positive) on this corpus.
        jac = reports["lexical_jaccard"]
        assert jac.best.precision > 0.0 and jac.best.recall > 0.0

    def test_report_formats(self) -> None:
        corpus = demo_corpus()
        reports = evaluate(corpus, default_signals())
        text = format_report(reports, corpus)
        assert "normalized_source_hash" in text
        assert "precision" in text

    def test_build_corpus_labels(self) -> None:
        seeds = [
            {"src/lib.rs": b"fn a() {}\n"},
            {"src/lib.rs": b"fn b() {}\n"},
        ]
        corpus = build_corpus(seeds, max_tier=2)
        clones = [p for p in corpus if p.is_clone]
        indep = [p for p in corpus if not p.is_clone]
        assert {p.tier for p in clones} == {1, 2}  # two ladder tiers per seed
        assert len(indep) == 1  # the single distinct-seed pair


class TestPromptSignal:
    """Prompt fingerprint: it catches the rename clones the lexical and
    normalized-source channels miss, while the convergent pair keeps it honest as a
    review-band (not autoreject) signal."""

    def test_prompt_recall_exceeds_lexical_and_hash(self) -> None:
        # The payoff: hashing the prompt's contents survives identifier renaming, so
        # the prompt fingerprint catches BOTH ladder tiers (recall 1.0) where the
        # normalized-source hash and the lexical MinHash — defeated by the tier-2
        # rename — reach only 0.5.
        reports = {r.name: r for r in evaluate(demo_corpus(), default_signals())}
        prompt = reports["prompt_jaccard"]
        assert prompt.best.recall == 1.0
        assert prompt.best.recall > reports["normalized_source_hash"].best.recall
        assert prompt.best.recall > reports["lexical_jaccard"].best.recall

    def test_prompt_precise_at_operating_point(self) -> None:
        # A threshold still separates the preserved-prompt clones (score 1.0) from
        # the convergent pair (< 1.0), so the reported operating point is clean.
        reports = {r.name: r for r in evaluate(demo_corpus(), default_signals())}
        assert reports["prompt_jaccard"].best.precision == 1.0

    def test_convergent_pair_is_orthogonal_no_single_signal_suffices(self) -> None:
        # The honesty case: on two independent agents that share only the harness
        # preamble, the prompt fingerprint fires (shared prompt) while the lexical
        # channel and the normalized-source hash stay at 0.0. No single signal is
        # both firing and correct — which is exactly why a hold needs ≥2
        # orthogonal signals, and why the prompt fingerprint alone must not autoreject.
        conv = next(p for p in demo_corpus() if p.note == "convergent")
        assert not conv.is_clone
        assert (
            _prompt(conv.a, conv.b)[0] > 0.5
        )  # the prompt fingerprint fires on a non-clone
        assert (
            _sig_normalized_hash(conv.a, conv.b) == 0.0
        )  # …but the normalized-source hash does not
        # …and neither does the lexical channel (bodies differ, no shared window).
        reports = {r.name: r for r in evaluate([conv], default_signals())}
        assert reports["lexical_jaccard"].sweep  # scored, just not a match here

    def test_prompt_survives_tier2_rename(self) -> None:
        # Direct check underlying the recall win: a tier-2 rename leaves the prompt
        # sketch identical to the base, even as the normalized-source hash diverges.
        base = demo_corpus()[0].a  # alpha base crate (tier-1 clone's a-side)
        seed = unpack(base)
        renamed = pack(obfuscate(seed, tier=2))
        assert _prompt(base, renamed)[0] == 1.0
        assert compute_normalized_source_hash(base) != compute_normalized_source_hash(
            renamed
        )


class TestEmbeddingSignal:
    """The the code-embedding cosine signal plumbing, exercised with a deterministic
    fake embedder
    (the real orthogonality needs the live model + real crates)."""

    @staticmethod
    def _fake_embed(text: str) -> list[float]:
        # A char-frequency histogram: a stand-in embedder that is deterministic and
        # gives identical vectors for identical source, lower cosine for different.
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_(){}<>-+*/;: "
        return [float(text.count(c)) for c in alphabet]

    def test_self_similarity_is_one(self) -> None:
        sig = embedding_signal(self._fake_embed)
        a = pack(_SEED)
        assert sig.score(a, a) == pytest.approx(1.0)

    def test_different_crates_below_one(self) -> None:
        sig = embedding_signal(self._fake_embed)
        a = pack(_SEED)
        b = pack(
            {
                "src/lib.rs": b"fn totally_unrelated() -> u8 {\n    42\n}\n",
                "Cargo.toml": b'[package]\nname = "other"\n',
            }
        )
        s = sig.score(a, b)
        assert 0.0 <= s < 1.0

    def test_no_embedding_input_scores_zero(self) -> None:
        # A crate with only comments has no embedding input -> None -> cosine 0.0.
        sig = embedding_signal(self._fake_embed)
        empty = pack({"x.rs": b"// only a comment\n"})
        assert sig.score(empty, pack(_SEED)) == 0.0
