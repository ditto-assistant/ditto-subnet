"""Offline calibration harness for the anti-copy signal stack.

See ``docs/SEMANTIC-CLONE-PREVENTION.md`` §6. Given a labeled corpus of agent
**pairs** (clone vs. independent), it scores each anti-copy *signal* over the
corpus and reports, per signal, the precision/recall it achieves across a
threshold sweep — so thresholds are chosen from the data (the convergence
false-positive floor), never guessed.

The pieces:

- **Obfuscation ladder** (:func:`obfuscate`) — takes a crate and applies a tier of
  copy-hiding transforms. Each tier is a harder clone: tier 1 is cosmetic
  (reformat + recomment + file reorder/rename), which the normalized-source
  hash must still catch; tier 2 adds identifier renaming, which defeats the
  normalized-source hash and
  should fall to the AST / behavioral layers.
- **Signals** (:class:`Signal`) — a named ``(tar_a, tar_b) -> similarity`` in
  ``[0, 1]``. This version wires the in-process signals: normalized-source
  hash, lexical Jaccard/containment, and prompt Jaccard/containment (the
  prompt-surface fingerprint, which — unlike lexical/normalized-source — survives
  identifier renaming
  because it hashes string *contents*). Structural (dittobench), code-embedding,
  and behavioral signals plug in later as more ``Signal``\\s.
- **Corpus** (:func:`build_corpus`) — clone pairs (a seed vs. its ladder variant)
  and independent pairs (distinct seeds). The demo corpus also carries the hard
  *convergent* case: two independent agents on the same reference harness that
  share a scaffolding prompt preamble but differ in strategy — the false-positive
  surface that makes the prompt fingerprint a review-band signal rather than an
  autoreject.
- **Evaluation** (:func:`evaluate`) — a precision/recall threshold sweep per
  signal, plus the best-F1 operating point.

Everything is pure + deterministic (fixed tar mtime, seeded transforms), so a run
is reproducible and unit-testable. Value objects are frozen dataclasses per the
code-quality standard; no pydantic here (nothing crosses the HTTP boundary).
"""

from __future__ import annotations

import io
import re
import tarfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ditto.api_server.embedding import cosine
from ditto.api_server.fingerprint import (
    compute_content_fingerprint,
    compute_embedding_input,
    compute_normalized_source_hash,
    compute_prompt_fingerprint,
    content_similarity,
)

# A crate as an in-memory {path: source-bytes} map — the unit the transforms and
# corpus builder work on before packing to the tar.gz bytes the signals consume.
Crate = Mapping[str, bytes]


# ── Packing ──────────────────────────────────────────────────────────────────
def pack(crate: Crate) -> bytes:
    """Pack ``{path: bytes}`` into deterministic tar.gz bytes (fixed mtime).

    Members are emitted in sorted-path order with ``mtime=0`` so the *same* crate
    always yields byte-identical archives — a repack that only reorders files is
    therefore invisible at the tar level too, matching what a real copier does.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for path in sorted(crate):
            data = crate[path]
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(tar_gz: bytes) -> dict[str, bytes]:
    """Inverse of :func:`pack` — regular files only."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            if f is not None:
                out[member.name] = f.read()
    return out


# ── Obfuscation ladder ───────────────────────────────────────────────────────
def t_reformat(crate: Crate) -> dict[str, bytes]:
    """Cosmetic reformat: double interior indentation, inject blank lines."""
    out: dict[str, bytes] = {}
    for path, data in crate.items():
        lines = data.decode("utf-8", "replace").splitlines()
        reflowed = []
        for line in lines:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            reflowed.append(" " * (indent * 2) + stripped)
            reflowed.append("")  # extra blank line
        out[path] = ("\n".join(reflowed) + "\n").encode()
    return out


def t_recomment(crate: Crate) -> dict[str, bytes]:
    """Inject ``//`` and ``/* */`` comments a normalized hash must see through."""
    out: dict[str, bytes] = {}
    for i, (path, data) in enumerate(sorted(crate.items())):
        text = data.decode("utf-8", "replace")
        commented = (
            f"// auto-generated header {i}\n"
            f"/* block\n   comment {i} */\n"
            + re.sub(r"\n", f"  // note {i}\n", text, count=1)
        )
        out[path] = commented.encode()
    return out


def t_reorder_rename_files(crate: Crate) -> dict[str, bytes]:
    """Rename + reorder files (content unchanged) — a repack a copier does free."""
    # Deterministic new names in reverse sorted order; content is preserved.
    items = sorted(crate.items(), reverse=True)
    return {f"src/mod_{i}.rs": data for i, (_old, data) in enumerate(items)}


_IDENT_DEF = re.compile(r"\b(?:fn|let|struct|const|mut)\s+([a-zA-Z_]\w*)")


def _rename_map(crate: Crate) -> dict[str, str]:
    """Collect defined identifiers across the crate → deterministic new names."""
    names: set[str] = set()
    for data in crate.values():
        for m in _IDENT_DEF.finditer(data.decode("utf-8", "replace")):
            names.add(m.group(1))
    # Stable order so the rename is reproducible.
    return {name: f"r_{i}" for i, name in enumerate(sorted(names))}


def t_rename_idents(
    crate: Crate, mapping: Mapping[str, str] | None = None
) -> dict[str, bytes]:
    """Consistently rename defined identifiers (defeats the exact-repack hash)."""
    m = dict(mapping) if mapping is not None else _rename_map(crate)
    out: dict[str, bytes] = {}
    for path, data in crate.items():
        text = data.decode("utf-8", "replace")
        for old, new in m.items():
            text = re.sub(rf"\b{re.escape(old)}\b", new, text)
        out[path] = text.encode()
    return out


# Ladder tiers, each building on the previous. Keep in sync with the doc's §6
# ladder; tiers 3+ (control-flow rewrite, re-implementation) need real crates and
# are added to the corpus as they are collected, not synthesized here.
def obfuscate(crate: Crate, tier: int) -> dict[str, bytes]:
    """Apply the copy-hiding transforms up to ``tier`` (1 = cosmetic, 2 = +rename)."""
    out: dict[str, bytes] = dict(crate)
    if tier >= 1:
        out = t_reorder_rename_files(t_recomment(t_reformat(out)))
    if tier >= 2:
        out = t_rename_idents(out)
    return out


# ── Corpus ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ClonePair:
    """One labeled corpus pair of packed tar.gz crates."""

    a: bytes
    b: bytes
    is_clone: bool
    tier: int  # obfuscation tier for clones; 0 for independents
    note: str = ""


def build_corpus(seeds: Sequence[Crate], *, max_tier: int = 2) -> list[ClonePair]:
    """Build labeled pairs: each seed vs. its ladder variants (clones) + all
    distinct seed pairs (independents).

    Independents include the *convergent* case when two seeds are similar-but-
    distinct — that is exactly the false-positive floor a signal must clear.
    """
    pairs: list[ClonePair] = []
    packed = [pack(s) for s in seeds]
    for seed, base in zip(seeds, packed, strict=True):
        for tier in range(1, max_tier + 1):
            pairs.append(
                ClonePair(base, pack(obfuscate(seed, tier)), True, tier, f"tier{tier}")
            )
    for i in range(len(packed)):
        for j in range(i + 1, len(packed)):
            pairs.append(ClonePair(packed[i], packed[j], False, 0, "independent"))
    return pairs


# ── Signals ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Signal:
    """A named pairwise similarity in ``[0, 1]`` (higher = more likely a clone)."""

    name: str
    score: Callable[[bytes, bytes], float]


def _sig_normalized_hash(a: bytes, b: bytes) -> float:
    ha = compute_normalized_source_hash(a)
    hb = compute_normalized_source_hash(b)
    return 1.0 if (ha is not None and ha == hb) else 0.0


def _lexical(a: bytes, b: bytes) -> tuple[float, float]:
    return content_similarity(
        compute_content_fingerprint(a), compute_content_fingerprint(b)
    )


def _prompt(a: bytes, b: bytes) -> tuple[float, float]:
    return content_similarity(
        compute_prompt_fingerprint(a), compute_prompt_fingerprint(b)
    )


def embedding_signal(
    embed: Callable[[str], list[float] | None],
    *,
    name: str = "code_embedding_cosine",
) -> Signal:
    """Build the code-embedding cosine signal from an injected text embedder.

    ``embed`` maps the crate's canonical source
    (:func:`ditto.api_server.fingerprint.compute_embedding_input`) to a vector — in
    production the self-hosted service (:mod:`ditto.api_server.embedding`), in tests
    a deterministic fake. It is *not* in :func:`default_signals` because the offline
    harness has no live model; wire it explicitly once an embedder is available:

        sig = embedding_signal(lambda text: my_embedder_call(text))

    A crate that yields no embedding input embeds to ``None`` and scores ``0.0``.
    """

    def score(a: bytes, b: bytes) -> float:
        ta, tb = compute_embedding_input(a), compute_embedding_input(b)
        va = embed(ta) if ta else None
        vb = embed(tb) if tb else None
        return cosine(va, vb)

    return Signal(name, score)


def default_signals() -> list[Signal]:
    """The in-process signals available today: normalized-source hash, lexical
    MinHash, and the prompt fingerprint. Structural, code-embedding, and behavioral
    signals append here as they come online.

    The prompt fingerprint is the rename-resistant complement to the lexical and
    normalized-source channels: it hashes the prompt's string *contents*, so a copy
    that renames every identifier — defeating those two — still overlaps here. It is a
    review-band signal (honest agents share reference-harness scaffolding prompts;
    see the convergent pair in :func:`demo_corpus`), so calibration reports where a
    threshold separates preserved-prompt clones from that convergence.
    """
    return [
        Signal("normalized_source_hash", _sig_normalized_hash),
        Signal("lexical_jaccard", lambda a, b: _lexical(a, b)[0]),
        Signal("lexical_containment", lambda a, b: _lexical(a, b)[1]),
        Signal("prompt_jaccard", lambda a, b: _prompt(a, b)[0]),
        Signal("prompt_containment", lambda a, b: _prompt(a, b)[1]),
    ]


# ── Evaluation ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Operating:
    """Precision/recall at one threshold (predict clone iff score >= threshold)."""

    threshold: float
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class SignalReport:
    """A signal's threshold sweep + its best-F1 operating point over a corpus."""

    name: str
    best: Operating
    sweep: tuple[Operating, ...] = field(default_factory=tuple)


def _operating(scores: Sequence[tuple[float, bool]], threshold: float) -> Operating:
    tp = fp = fn = 0
    for score, is_clone in scores:
        predicted = score >= threshold
        if predicted and is_clone:
            tp += 1
        elif predicted and not is_clone:
            fp += 1
        elif not predicted and is_clone:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return Operating(threshold, precision, recall, f1)


def evaluate(
    pairs: Iterable[ClonePair],
    signals: Iterable[Signal],
    *,
    precision_floor: float = 0.99,
) -> list[SignalReport]:
    """Score every signal over the corpus and report its precision/recall sweep.

    Threshold candidates are the distinct observed scores (plus a hair above the
    max, i.e. "never fire"), so the sweep hits every achievable operating point.

    The reported ``best`` is chosen to match the design's operating goal — **max
    recall subject to ``precision >= precision_floor``** — because in a
    winner-take-all system a false positive (flagging an *independent* agent) is
    the costly error, so we hold precision and take the recall we can. This
    deliberately avoids raw-F1 selection, which on a clone-heavy corpus is
    maximized by the degenerate "predict everything" threshold. If no threshold
    clears the floor, fall back to the most-precise point.
    """
    corpus = list(pairs)
    reports: list[SignalReport] = []
    for sig in signals:
        scored = [(sig.score(p.a, p.b), p.is_clone) for p in corpus]
        max_score = max((s for s, _ in scored), default=0.0)
        candidates = sorted({s for s, _ in scored} | {max_score + 1e-9})
        sweep = tuple(_operating(scored, t) for t in candidates)
        eligible = [
            o for o in sweep if o.precision >= precision_floor and o.recall > 0.0
        ]
        if eligible:
            best = max(eligible, key=lambda o: (o.recall, o.threshold))
        else:
            best = max(sweep, key=lambda o: (o.precision, o.recall, o.threshold))
        reports.append(SignalReport(sig.name, best, sweep))
    return reports


def format_report(reports: Sequence[SignalReport], corpus: Sequence[ClonePair]) -> str:
    """A compact text table for the CLI: each signal's best-F1 operating point."""
    n_clone = sum(1 for p in corpus if p.is_clone)
    n_indep = len(corpus) - n_clone
    tiers = sorted({p.tier for p in corpus if p.is_clone})
    lines = [
        f"calibration: {len(corpus)} pairs ({n_clone} clone / {n_indep} independent), "
        f"clone tiers {tiers}",
        f"{'signal':<26} {'thr':>6} {'precision':>10} {'recall':>8} {'f1':>6}",
        "-" * 60,
    ]
    for r in reports:
        b = r.best
        lines.append(
            f"{r.name:<26} {b.threshold:>6.3f} {b.precision:>10.3f} "
            f"{b.recall:>8.3f} {b.f1:>6.3f}"
        )
    return "\n".join(lines)


# ── A tiny synthetic corpus so `python -m ditto.anticopy.calibration` runs today ──
# A reference-harness scaffolding prompt every honest agent embeds. Two independent
# agents that share it (but differ in strategy) are the convergence surface the prompt
# fingerprint must
# not treat as copy evidence on its own — the reason it is review-band, not
# autoreject. Kept clear of any identifier the rename transform touches (``step`` /
# ``NAME`` / ``SYSTEM_PROMPT`` / the fn prefixes) so a tier-2 rename cannot perturb
# it inside the string. Single line so the reformat transform leaves it byte-intact.
_HARNESS_PREAMBLE = (
    "You have access to a persistent memory store and tools that search it. "
    "Before answering, query the store for relevant saved notes and gather the "
    "surrounding context so the reply is grounded in what the user has told you."
)
# Distinct per-seed strategy prompts (genuinely different, not paraphrases), so a
# preserved-prompt clone overlaps fully while two distinct honest seeds do not.
_SEED_PROMPTS = {
    "alpha": (
        "Prioritise exact recall. Return the most relevant saved note verbatim, "
        "attach its identifier, and decline to answer when nothing matches."
    ),
    "beta": (
        "Favour brevity. Collect the top few memories, fold them into a compact "
        "briefing, and surface only the facts that change the decision."
    ),
    "gamma": (
        "Plan explicitly. Split the request into parts, resolve each against the "
        "saved notes, and assemble the pieces into an ordered final answer."
    ),
}


def _synthetic_seed(
    fn_prefix: str, n_fns: int, *, flavor: int = 0, prompt: str | None = None
) -> dict[str, bytes]:
    """A small, plausibly-structured Rust-ish crate for the demo corpus.

    ``prompt`` is embedded as a single-line raw-string const so the prompt
    fingerprint has a surface; it defaults to ``_SEED_PROMPTS[fn_prefix]``.
    """
    text = prompt if prompt is not None else _SEED_PROMPTS[fn_prefix]
    fns = "\n\n".join(
        f"fn {fn_prefix}{i}(x: i64) -> i64 {{\n"
        f"    let step = x + {i + flavor};\n"
        f"    step * {2 + flavor}\n"
        f"}}"
        for i in range(n_fns)
    )
    lib = (
        f"// crate {fn_prefix}\n"
        f'const NAME: &str = "{fn_prefix}";\n'
        f'const SYSTEM_PROMPT: &str = r#"{text}"#;\n\n'
        f"{fns}\n"
    )
    cargo = f'[package]\nname = "{fn_prefix}"\nversion = "0.1.0"\n'
    return {"src/lib.rs": lib.encode(), "Cargo.toml": cargo.encode()}


def _convergent_pair() -> ClonePair:
    """Two independent agents on the same harness: shared prompt preamble, distinct
    strategy tail — the prompt fingerprint false-positive surface, labeled non-clone.

    The bodies differ (different prefixes / sizes / flavors), so the lexical and
    normalized-source channels see them as unrelated; only the shared preamble
    gives the prompt fingerprint a partial (< 1.0) overlap, which is exactly the
    convergence a
    threshold must sit above.
    """
    a = _synthetic_seed(
        "delta", 5, flavor=3, prompt=_HARNESS_PREAMBLE + " Then draft a direct reply."
    )
    b = _synthetic_seed(
        "epsilon", 7, flavor=4, prompt=_HARNESS_PREAMBLE + " Then list the key points."
    )
    return ClonePair(pack(a), pack(b), is_clone=False, tier=0, note="convergent")


def demo_corpus() -> list[ClonePair]:
    """Distinct seeds (independents) + their ladder variants (clones), plus the
    convergent-independent pair that stresses the prompt signal."""
    seeds = [
        _synthetic_seed("alpha", 6, flavor=0),
        _synthetic_seed("beta", 5, flavor=1),
        _synthetic_seed("gamma", 7, flavor=2),
    ]
    corpus = build_corpus(seeds, max_tier=2)
    corpus.append(_convergent_pair())
    return corpus


def main() -> None:  # pragma: no cover - thin CLI wrapper
    corpus = demo_corpus()
    reports = evaluate(corpus, default_signals())
    print(format_report(reports, corpus))


if __name__ == "__main__":  # pragma: no cover
    main()


# Silence "imported but unused" for the re-export convenience used by tests.
__all__ = [
    "ClonePair",
    "Crate",
    "Operating",
    "Signal",
    "SignalReport",
    "build_corpus",
    "default_signals",
    "demo_corpus",
    "embedding_signal",
    "evaluate",
    "format_report",
    "obfuscate",
    "pack",
    "t_recomment",
    "t_rename_idents",
    "t_reorder_rename_files",
    "t_reformat",
    "unpack",
]
