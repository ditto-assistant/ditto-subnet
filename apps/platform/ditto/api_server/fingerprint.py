"""Content fingerprint for the anti-copy gate.

The anti-copy gate in :mod:`ditto.api_server.scoring_gate` starts from two cheap
byte-level signals — an exact ``sha256`` match and a size+score-proximity
heuristic — both of which a copier defeats by re-indenting, renaming files, or
nudging the tarball size. This module adds a *content*-level signal that survives
reformatting and localized edits.

**How it works.** Each regular file's text is normalized (decoded leniently, all
intra-line whitespace removed, blank lines dropped) so indentation,
tabs-vs-spaces, line-endings, and operator/keyword-spacing reformatting all
wash out. The normalized lines are cut into overlapping **k-line shingles**
(:data:`_SHINGLE_LINES`); every shingle is hashed, and the shingles from all files
are unioned into one set. Shingling is per-file then unioned, so renaming or
reordering files is invisible and a *localized* edit only disturbs the handful of
shingles that span it — unlike a whole-file hash, which any single edit voids.
Before sketching, shingles found in the complete official starter-kit mainline
history are removed. Shared reference scaffolding is therefore neutral evidence;
only the miner-authored residual contributes to a near-copy comparison.

**Storage — a MinHash (bottom-k) sketch.** Keeping the full shingle set would grow
with harness size; instead the fingerprint stores the ``k`` smallest shingle
hashes (a KMV / bottom-k MinHash sketch) plus the true set cardinality. That is a
*fixed*-size summary (:data:`_MINHASH_K` hashes) from which :func:`content_similarity`
estimates both Jaccard and containment. For a harness whose shingle set is smaller
than ``k`` (the common case) the sketch is the whole set, so the estimate is
*exact*; the approximation only engages for unusually large trees.

**What it catches / misses.** Jaccard catches sprinkled small edits; containment
(the symmetric overlap coefficient) catches a copy that pads itself with junk
files to dilute Jaccard. It is still a *lexical* signal: it does **not** defeat
identifier renaming or statement reordering *within* a shingle window — that needs
language-aware AST/token analysis, which belongs in the screener/dittobench where
the crate is already unpacked and its Rust toolchain is available (the platform
has no Rust parser). See ``docs`` handoff for that layer.

Computed here because ``/upload/agent`` already holds the whole tarball in memory
(streamed for the size cap + sha256), so the platform gets the signal without a
second unpack. Everything is pure + deterministic: the same tarball always yields
the same sketch, and the gate the same verdict.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import sys
import tarfile
from array import array
from bisect import bisect_left
from collections.abc import Iterator
from functools import lru_cache
from importlib.resources import files as resource_files

logger = logging.getLogger(__name__)

# Bump when the normalization / shingling / sketch format changes so stored
# fingerprints from an older algorithm are not silently compared against new ones
# (a cross-version Jaccard is meaningless). :func:`content_similarity` returns no
# match unless both sketches carry the same version.
_FP_VERSION = 2

# Version of the normalized-source-hash canonicalization
# (:func:`compute_normalized_source_hash`). Bumped independently of ``_FP_VERSION``
# so a change to comment/whitespace/file canonicalization doesn't silently compare
# across formats.
_NSH_VERSION = 2

# A shingle is this many consecutive normalized lines. Small enough that a
# one-line edit disturbs only a few shingles (robust to sprinkled edits), large
# enough that a shingle is distinctive (common single lines like a lone ``}`` do
# not collide across unrelated crates).
_SHINGLE_LINES = 4
# Bottom-k sketch size. 256 keeps the stored fingerprint ~4 KB and, because most
# harness shingle sets are smaller than this, makes the similarity estimate exact
# for the common case (the sketch is then the whole set).
_MINHASH_K = 256
# Width of each shingle hash: 64 bits as zero-padded hex. Fixed width so the hex
# strings sort in the same order as the integers they encode (bottom-k == the k
# lexicographically-smallest), and JSON-safe (no >2^53 bigints on the wire).
_HASH_HEX = 16

# Reference-aware v2 fingerprints remove shingles found anywhere in the official
# starter-kit history before sketching. Below the floor the sketch is emptied:
# a residual smaller than this is statistically nothing to compare (and nothing
# a copier needs to steal — the exact-equality rules still catch literal
# resubmissions). The floor is EIGHT shingles — two full edited regions, since a
# shingle spans _SHINGLE_LINES(=4) normalized lines and one edited line disturbs
# at most that many shingles:
#
# - 16 was measured to miss real theft: a 12-line innovation block yields a
#   residual of ~12 shingles, so BOTH the original and a verbatim copy of it
#   sketched empty and the copy escaped every channel.
# - 4 (one region) is too coincidence-prone: two honest agents sharing a single
#   verbatim boilerplate window (a pasted community fix, a common config stanza)
#   would match on it alone.
# - Estimator instability is not a factor in this range: for card < k the
#   sketch IS the full set, so Jaccard/containment are exact — a verbatim
#   subset scores exactly 1.0 and a one-shingle mismatch drops cleanly below
#   the 0.95 containment hold either way.
#
# Prompt remains an advisory channel with the same eight-shingle floor. The
# exact normalized-source channel has a smaller floor because equality of the
# complete residual set is stronger than a fuzzy ratio.
_MIN_CONTENT_SHINGLES = 8
_MIN_PROMPT_SHINGLES = 8
_MIN_NSH_SHINGLES = 4
_REFERENCE_BUNDLES = {
    "lexical": "reference_lexical_v2.bin",
    "normalized": "reference_normalized_v2.bin",
    "prompt": "reference_prompt_v2.bin",
}


@lru_cache(maxsize=1)
def _reference_corpus_provenance() -> tuple[str, str, str, str]:
    manifest = json.loads(
        resource_files("ditto.anticopy")
        .joinpath("reference_manifest_v2.json")
        .read_text()
    )
    return (
        str(manifest["source"]),
        str(manifest["revision"]),
        str(manifest["commit_set_sha256"]),
        "starter-kit-mainline-history",
    )


def reference_corpus_provenance() -> dict[str, str]:
    """Return the immutable identity fields of the packaged starter-kit corpus.

    The returned mapping is safe to expose as comparison provenance: it contains
    only the public canonical repository, the exact mainline revision, and the
    digest of the included commit set. It never contains submitted artifact data.
    """
    source, revision, corpus_id, exclusion_mode = _reference_corpus_provenance()
    return {
        "source": source,
        "revision": revision,
        "corpus_id": corpus_id,
        "exclusion_mode": exclusion_mode,
    }


def _reference_corpus_id() -> str:
    return _reference_corpus_provenance()[2]


# Decompression-bomb + work guards. The upload cap bounds the *compressed* tarball
# at 20 MiB by default, but gzip/tar inflate far past that, so fingerprinting
# reads through its own limits. Tripping any of them yields ``None``
# ("unfingerprintable"), which the gate reads as no content match (the sha256 +
# size signals still apply).
_MAX_TOTAL_BYTES = 64 * 1024 * 1024  # matches the dittobench sandbox extract cap
_MAX_FILE_BYTES = 8 * 1024 * 1024
# Bound on *every* iterated tar member, not just regular files: a tar of hundreds
# of thousands of directory/symlink headers is a cheap CPU-DoS otherwise (the
# member walk is O(headers) regardless of file content).
_MAX_MEMBERS = 20000
# Hard ceiling on distinct shingles before we give up (paired with the byte caps).
_MAX_SHINGLES = 500_000

# --- prompt fingerprint (see docs/SEMANTIC-CLONE-PREVENTION.md §4) ----------
# Version of the prompt-sketch format. A *string* so it can never collide with the
# integer ``_FP_VERSION`` / structural versions in :func:`content_similarity`'s
# ``v`` equality check — a prompt sketch is a distinct channel, stored apart, and
# must never be compared against a lexical/structural one.
_PROMPT_VERSION = "p2"
# A string literal must have at least this many whitespace-split words to count as
# "prompt-like". Filters out identifiers, format specifiers, config keys, and short
# messages (the shared-scaffolding strings that would otherwise false-match across
# independent agents), leaving substantial instruction text.
_PROMPT_MIN_WORDS = 8
# Prompt literals are shingled at the *word* level (not line, like the lexical
# channel) so a copied prompt still matches after light reflowing/editing while a
# genuine paraphrase — which changes most n-grams — diverges. 5 words is
# distinctive enough that unrelated prose rarely shares a shingle.
_PROMPT_SHINGLE_WORDS = 5

# --- code-embedding input (see docs/SEMANTIC-CLONE-PREVENTION.md §4) ---------
# Character cap on the text handed to the code-embedding model
# (:func:`compute_embedding_input`). Sized to fit the smaller supported backend's
# context window (jina-embeddings-v2-base-code, 8192 tokens ≈ ~24k chars of code);
# the primary backend (Qwen3-Embedding-0.6B, 32k tokens) has ample headroom. Deter-
# ministic prefix truncation: the sorted-file concatenation is stable, so the same
# crate always yields the same (possibly truncated) input.
_EMBED_INPUT_MAX_CHARS = 24000


@lru_cache(maxsize=len(_REFERENCE_BUNDLES))
def _reference_shingles(channel: str) -> array[int]:
    """Load one sorted uint64 starter-history shingle bundle.

    Bundles are generated from every blob reachable in the official starter-kit
    history by ``scripts/build_reference_fingerprints.py``.  Keeping the hashes in
    a sorted native array uses a few megabytes rather than the much larger Python
    ``set[str]`` representation; membership remains fast via binary search.
    """
    name = _REFERENCE_BUNDLES[channel]
    raw = resource_files("ditto.anticopy").joinpath(name).read_bytes()
    if len(raw) % 8:
        raise RuntimeError(f"invalid reference-fingerprint bundle: {name}")
    values = array("Q")
    values.frombytes(raw)
    if sys.byteorder == "little":
        values.byteswap()
    return values


def _without_reference(shingles: set[str], channel: str) -> set[str]:
    """Return ``shingles`` minus official starter-history shingles."""
    reference = _reference_shingles(channel)
    residual: set[str] = set()
    for shingle in shingles:
        value = int(shingle, 16)
        i = bisect_left(reference, value)
        if i == len(reference) or reference[i] != value:
            residual.add(shingle)
    return residual


def compute_content_fingerprint(tar_gz_bytes: bytes) -> dict | None:
    """Return a MinHash shingle sketch of the tarball's source, or ``None``.

    The returned dict is ``{"v", "corpus", "k", "card", "m"}`` — algorithm
    version, canonical reference-corpus identity, sketch budget, true residual
    cardinality, and the sorted bottom-``k`` residual hashes — JSON-serializable
    for the ``agents.content_fingerprint`` column and consumed by
    :func:`content_similarity`.

    Returns ``None`` when the bytes are not a readable tar.gz, contain no source
    lines, or trip the bomb/work guards. A valid artifact with too little
    post-reference content returns a versioned empty sketch, which is deliberately
    incomparable but lets the backfill distinguish processed rows. Fingerprinting
    is a best-effort moderation signal layered on an already-verified upload, so it
    never raises into the upload path: a hostile or corrupt tarball simply gets no
    content signal (the validator/screener still reject a broken harness downstream).
    """
    shingles: set[str] = set()
    total = 0
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tar:
            for member in tar:
                members += 1
                if members > _MAX_MEMBERS:
                    logger.warning("fingerprint: >%d members, skipping", _MAX_MEMBERS)
                    return None
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:  # e.g. a hardlink carrying no data
                    continue
                # Bounded read: +1 so an over-cap file is detected rather than
                # silently truncated into a hash a smaller honest file could hit.
                raw = extracted.read(_MAX_FILE_BYTES + 1)
                if len(raw) > _MAX_FILE_BYTES:
                    logger.warning("fingerprint: file exceeds per-file cap")
                    return None
                total += len(raw)
                if total > _MAX_TOTAL_BYTES:
                    logger.warning("fingerprint: >%d bytes, skipping", _MAX_TOTAL_BYTES)
                    return None
                for shingle in _file_shingles(raw):
                    shingles.add(shingle)
                    if len(shingles) > _MAX_SHINGLES:
                        logger.warning("fingerprint: >%d shingles", _MAX_SHINGLES)
                        return None
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as e:
        logger.info("fingerprint: unreadable tarball (%s)", type(e).__name__)
        return None

    if not shingles:
        return None
    shingles = _without_reference(shingles, "lexical")
    if len(shingles) < _MIN_CONTENT_SHINGLES:
        return {
            "v": _FP_VERSION,
            "corpus": _reference_corpus_id(),
            "k": _MINHASH_K,
            "card": len(shingles),
            "m": [],
        }
    return {
        "v": _FP_VERSION,
        "corpus": _reference_corpus_id(),
        "k": _MINHASH_K,
        "card": len(shingles),
        "m": sorted(shingles)[:_MINHASH_K],
    }


def compute_normalized_source_hash(tar_gz_bytes: bytes) -> str | None:
    """Return a single hash of the tarball's *canonicalized* source, or ``None``.

    This is the **"exact-repack"** signal (see
    ``docs/SEMANTIC-CLONE-PREVENTION.md``): a copy that only reformats, re-comments,
    or reorders/renames files normalizes to the **same** hash even though its
    ``sha256`` (and, slightly, its shingle sketch) differ. Unlike
    :func:`content_similarity` this is an *equality* signal — a match means "the
    same source, repackaged", the cheapest strong copy evidence after ``sha256``.

    Canonicalization, per regular file: strip ``//`` line and ``/* */`` block
    comments (string-``"``-aware so a ``//`` inside a URL literal survives),
    remove all intra-line whitespace, drop blank lines, cut the result into
    shingles, and subtract official starter-history shingles. The residual
    shingles are then **sorted** (so renaming/reordering files is invisible) and
    hashed together. Identifier renaming and statement reordering are *not*
    canonicalized away — that is the AST / behavioral layer's job; this layer only
    promises to see through cosmetic repackaging.

    Returns ``None`` ("no usable hash", read by the gate as no repack match) on an
    unreadable tarball, empty source, or a bomb/work-guard trip — same contract
    and guards as :func:`compute_content_fingerprint`. Pure + deterministic.

    Note: ``'``-delimited char literals are not tracked (Rust lifetimes such as
    ``'a`` have no closing quote and would eat spans), so a char literal
    containing ``"`` or a comment marker may mis-normalize. This only costs a rare
    missed match (the copy falls through to lexical/structural) — never a false match,
    since a
    collision still requires near-identical code.
    """
    shingles: set[str] = set()
    total = 0
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tar:
            for member in tar:
                members += 1
                if members > _MAX_MEMBERS:
                    logger.warning("nsh: >%d members, skipping", _MAX_MEMBERS)
                    return None
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read(_MAX_FILE_BYTES + 1)
                if len(raw) > _MAX_FILE_BYTES:
                    logger.warning("nsh: file exceeds per-file cap")
                    return None
                total += len(raw)
                if total > _MAX_TOTAL_BYTES:
                    logger.warning("nsh: >%d bytes, skipping", _MAX_TOTAL_BYTES)
                    return None
                shingles.update(_normalized_source_shingles(raw))
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as e:
        logger.info("nsh: unreadable tarball (%s)", type(e).__name__)
        return None

    shingles = _without_reference(shingles, "normalized")
    if len(shingles) < _MIN_NSH_SHINGLES:
        return None
    digest = hashlib.sha256()
    corpus_id = _reference_corpus_id()
    digest.update(f"nsh{_NSH_VERSION}:{corpus_id}\x00".encode())
    for shingle in sorted(shingles):
        digest.update(bytes.fromhex(shingle))
    return f"nsh{_NSH_VERSION}:{corpus_id}:{digest.hexdigest()}"


def compute_prompt_fingerprint(tar_gz_bytes: bytes) -> dict | None:
    """Return a word-shingle sketch of the crate's prompt literals, or ``None``.

    This is the first component of the **prompt-surface (strategy/asset) fingerprint**
    (see
    ``docs/SEMANTIC-CLONE-PREVENTION.md`` §4): it fingerprints the *prompt surface*
    — the substantial string literals a copier must preserve to keep the champion's
    score — independently of the surrounding code. A submission that refactors or
    renames everything but reuses the prompt keeps a high overlap here even when the
    lexical (:func:`compute_content_fingerprint`) and normalized-source
    (:func:`compute_normalized_source_hash`) channels diverge, because identifier
    renaming and reformatting do not touch string *contents*.

    Extraction, per regular file: every string literal — ordinary ``"..."`` (with
    ``\\`` escapes) and Rust raw strings ``r"..."`` / ``r#"..."#`` (multi-line
    prompts are usually raw) — is collected; those with at least
    :data:`_PROMPT_MIN_WORDS` words (lowercased, whitespace-collapsed) are cut into
    overlapping :data:`_PROMPT_SHINGLE_WORDS`-word shingles and unioned into a
    bottom-``k`` MinHash sketch, the same ``{v, corpus, k, card, m}`` shape as the
    lexical channel so :func:`content_similarity` compares it directly. The ``v``
    is a string (:data:`_PROMPT_VERSION`) so a prompt sketch never matches a
    lexical/structural sketch.

    Returns ``None`` ("no prompt fingerprint", read as no match) when the bytes are
    unreadable, carry no prompt-length literal, or trip the shared bomb/work guards.
    A valid prompt surface below the residual-cardinality floor returns a versioned
    empty sketch, matching the lexical channel's backfill marker.

    the prompt fingerprint is a *review-band* signal: honest agents on the same
    reference harness share
    scaffolding prompts, so a prompt match alone is not copy evidence. It is
    calibrated (``ditto.anticopy.calibration``) and fused with an orthogonal signal
    before it can hold an agent; this function only produces the sketch.
    """
    shingles: set[str] = set()
    total = 0
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tar:
            for member in tar:
                members += 1
                if members > _MAX_MEMBERS:
                    logger.warning("prompt-fp: >%d members, skipping", _MAX_MEMBERS)
                    return None
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read(_MAX_FILE_BYTES + 1)
                if len(raw) > _MAX_FILE_BYTES:
                    logger.warning("prompt-fp: file exceeds per-file cap")
                    return None
                total += len(raw)
                if total > _MAX_TOTAL_BYTES:
                    logger.warning("prompt-fp: >%d bytes, skipping", _MAX_TOTAL_BYTES)
                    return None
                for shingle in _prompt_shingles(raw):
                    shingles.add(shingle)
                    if len(shingles) > _MAX_SHINGLES:
                        logger.warning("prompt-fp: >%d shingles", _MAX_SHINGLES)
                        return None
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as e:
        logger.info("prompt-fp: unreadable tarball (%s)", type(e).__name__)
        return None

    if not shingles:
        return None
    shingles = _without_reference(shingles, "prompt")
    if len(shingles) < _MIN_PROMPT_SHINGLES:
        return {
            "v": _PROMPT_VERSION,
            "corpus": _reference_corpus_id(),
            "k": _MINHASH_K,
            "card": len(shingles),
            "m": [],
        }
    return {
        "v": _PROMPT_VERSION,
        "corpus": _reference_corpus_id(),
        "k": _MINHASH_K,
        "card": len(shingles),
        "m": sorted(shingles)[:_MINHASH_K],
    }


def compute_embedding_input(tar_gz_bytes: bytes) -> str | None:
    """Return the canonical source text to feed the code-embedding model, or None.

    This is the deterministic *input builder* for the **code-embedding** signal
    (see ``docs/SEMANTIC-CLONE-PREVENTION.md`` §4). It does not embed anything — the
    embedding model is a self-hosted service (Qwen3-Embedding-0.6B primary,
    jina-embeddings-v2-base-code CPU fallback) called separately — it only produces
    the stable text that service embeds, so the input is reproducible and unit-
    testable without the model.

    Unlike those canonicalizations, this preserves readable code: comments and
    blank lines are dropped (a copier changes those freely, and they carry little
    logic), but identifiers, structure, and indentation are kept, because the model
    reasons over natural code and derives its rename/refactor invariance
    *semantically* — that invariance is the point of the code-embedding signal and is
    the model's job, not
    the input's. Per-file cleaned texts are sorted by content and joined with a blank
    line (no path names), so renaming or reordering files does not change the input.
    The result is prefix-truncated to :data:`_EMBED_INPUT_MAX_CHARS` to fit the
    model's context window; the sorted concatenation makes that truncation stable.

    Returns ``None`` ("no embedding input", read downstream as no code-embedding signal)
    on an
    unreadable tarball, empty source, or a bomb/work-guard trip — same contract and
    guards as the other extractors. Pure + deterministic.
    """
    files: list[str] = []
    total = 0
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tar:
            for member in tar:
                members += 1
                if members > _MAX_MEMBERS:
                    logger.warning("embed-input: >%d members, skipping", _MAX_MEMBERS)
                    return None
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read(_MAX_FILE_BYTES + 1)
                if len(raw) > _MAX_FILE_BYTES:
                    logger.warning("embed-input: file exceeds per-file cap")
                    return None
                total += len(raw)
                if total > _MAX_TOTAL_BYTES:
                    logger.warning("embed-input: >%d bytes, skipping", _MAX_TOTAL_BYTES)
                    return None
                cleaned = _embedding_source(raw)
                if cleaned:
                    files.append(cleaned)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as e:
        logger.info("embed-input: unreadable tarball (%s)", type(e).__name__)
        return None

    if not files:
        return None
    files.sort()
    return "\n\n".join(files)[:_EMBED_INPUT_MAX_CHARS]


def _embedding_source(raw: bytes) -> str:
    """Canonicalize one file for embedding: comments + blank lines dropped, code kept.

    Keeps each surviving line verbatim (indentation and identifiers intact) so the
    model sees natural code; only comments (via :func:`_strip_comments`) and
    blank/whitespace-only lines are removed.
    """
    text = _strip_comments(raw.decode("utf-8", errors="replace"))
    return "\n".join(line for line in text.splitlines() if line.strip())


def _prompt_shingles(raw: bytes) -> list[str]:
    """Return the hashed word-shingles of a file's prompt-length string literals.

    Each qualifying literal (``>= _PROMPT_MIN_WORDS`` words after lowercasing +
    whitespace collapse) is shingled into overlapping ``_PROMPT_SHINGLE_WORDS``-word
    windows so a light edit disturbs only a few shingles. Shingles carry a ``p:``
    prefix before hashing, keeping the prompt hash-space disjoint from the lexical
    line-shingle space as a second guard against cross-channel collision.
    """
    text = raw.decode("utf-8", errors="replace")
    out: list[str] = []
    for literal in _extract_string_literals(text):
        words = literal.lower().split()
        if len(words) < _PROMPT_MIN_WORDS:
            continue
        w = _PROMPT_SHINGLE_WORDS
        for i in range(len(words) - w + 1):
            out.append(_hash_shingle("p:" + " ".join(words[i : i + w])))
    return out


def _extract_string_literals(text: str) -> Iterator[str]:
    """Yield the contents of every string literal in ``text``.

    Handles ordinary ``"..."`` literals (honoring ``\\`` escapes so an escaped quote
    does not end the literal) and Rust raw strings ``r"..."`` / ``r#"..."#`` /
    ``r##"..."##`` … (no escapes; the literal ends at ``"`` followed by the same
    number of ``#`` that opened it). Line ``//`` and block ``/* */`` comments are
    skipped so a ``"`` inside a comment does not open a spurious literal. Char /
    lifetime ``'`` is not tracked (a ``"`` inside a char literal is rare and only
    costs a spurious literal that fails the word-count gate). Best-effort and
    tolerant: it never raises on malformed input, it just yields what it can.
    """
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        # Skip comments so a `"` inside them doesn't open a literal.
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return
            i = nl + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        # Raw string: r"..." or r#"..."# with a matching run of hashes.
        if c == "r" and i + 1 < n and text[i + 1] in ('"', "#"):
            j = i + 1
            hashes = 0
            while j < n and text[j] == "#":
                hashes += 1
                j += 1
            if j < n and text[j] == '"':
                close = '"' + "#" * hashes
                end = text.find(close, j + 1)
                if end == -1:
                    yield text[j + 1 :]
                    return
                yield text[j + 1 : end]
                i = end + len(close)
                continue
        # Ordinary double-quoted string.
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                cj = text[j]
                if cj == "\\" and j + 1 < n:  # escape: consume the next char raw
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if cj == '"':
                    break
                buf.append(cj)
                j += 1
            yield "".join(buf)
            i = j + 1
            continue
        i += 1


def _normalized_source(raw: bytes) -> str:
    """Canonicalize one file's text: comments stripped, whitespace/blanks removed.

    Line boundaries are kept (lines joined with ``\\n``) so tokens on adjacent
    lines don't merge into a new token; only *intra*-line whitespace is removed.
    """
    text = _strip_comments(raw.decode("utf-8", errors="replace"))
    lines = [norm for line in text.splitlines() if (norm := "".join(line.split()))]
    return "\n".join(lines)


def _normalized_source_shingles(raw: bytes) -> list[str]:
    """Hash normalized-source windows for reference-aware exact equality."""
    normalized = _normalized_source(raw)
    if not normalized:
        return []
    lines = normalized.splitlines()
    k = _SHINGLE_LINES
    if len(lines) <= k:
        return [_hash_shingle("\n".join(lines))]
    return [
        _hash_shingle("\n".join(lines[i : i + k])) for i in range(len(lines) - k + 1)
    ]


def _strip_comments(text: str) -> str:
    """Remove ``//`` line and ``/* */`` block comments, preserving string literals.

    A single-pass scanner that tracks double-quoted string state (so ``//`` and
    ``/*`` inside a ``"..."`` literal are left intact). Char/lifetime ``'`` is
    intentionally not tracked — see :func:`compute_normalized_source_hash`.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:  # escape — keep the next char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":  # line comment
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl  # keep the newline (appended next iteration)
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":  # block comment
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _file_shingles(raw: bytes) -> list[str]:
    """Return the hashed k-line shingles of one file's normalized text.

    Normalization decodes leniently (``errors="replace"`` so binary blobs still
    hash stably), removes *all* intra-line whitespace, and drops blank lines — so
    indentation, tabs-vs-spaces, line-endings, and operator/keyword spacing
    reformatting (what a formatter like rustfmt churns) all normalize away. Token
    identity, ordering, and case within a line are preserved (defeating those is
    the AST layer's job). Two distinct files would have to share whitespace-free
    4-line windows to collide, which in practice means the same code. A file with
    fewer than :data:`_SHINGLE_LINES` non-blank lines yields one whole-file shingle.
    """
    text = raw.decode("utf-8", errors="replace")
    lines = [norm for line in text.splitlines() if (norm := "".join(line.split()))]
    if not lines:
        return []
    k = _SHINGLE_LINES
    if len(lines) <= k:
        return [_hash_shingle("\n".join(lines))]
    return [
        _hash_shingle("\n".join(lines[i : i + k])) for i in range(len(lines) - k + 1)
    ]


def _hash_shingle(shingle: str) -> str:
    """Hash one shingle to a fixed-width hex string (top 64 bits of sha256)."""
    return hashlib.sha256(shingle.encode("utf-8")).hexdigest()[:_HASH_HEX]


def content_similarity(a: dict | None, b: dict | None) -> tuple[float, float]:
    """Estimate ``(jaccard, containment)`` of two fingerprint sketches in ``[0, 1]``.

    ``jaccard`` = ``|A ∩ B| / |A ∪ B|`` (dilutes when a copy pads itself with junk
    files). ``containment`` = ``|A ∩ B| / min(|A|, |B|)`` (the symmetric overlap
    coefficient — ~1.0 when the smaller shingle set is essentially a subset of the
    larger, so it still fires on a padded copy). Both are estimated from the
    bottom-k sketches and are exact when both sets are small enough to be fully
    retained (``card <= k``).

    Returns ``(0.0, 0.0)`` when either sketch is missing, empty, or carries a
    different sketch-format version or reference corpus (either comparison is
    meaningless), so the gate can threshold without special-casing ``None``. The
    two channels (lexical / structural) are isolated by storage column, and each
    compares only within its own version — hence the equality check on ``v`` rather
    than a hard-coded constant, so a channel can version its format independently.
    """
    if not a or not b:
        return (0.0, 0.0)
    va = a.get("v")
    if va is None or va != b.get("v"):
        return (0.0, 0.0)
    corpus_a, corpus_b = a.get("corpus"), b.get("corpus")
    if corpus_a != corpus_b:
        return (0.0, 0.0)
    ma, mb = set(a.get("m", ())), set(b.get("m", ()))
    if not ma or not mb:
        return (0.0, 0.0)
    ka, kb = int(a.get("k", _MINHASH_K)), int(b.get("k", _MINHASH_K))
    card_a, card_b = int(a.get("card", 0)), int(b.get("card", 0))

    # Jaccard (KMV): over the k smallest hashes of the union (each guaranteed to sit
    # in one of the two bottom-k sketches), the fraction lying in *both* sketches —
    # i.e. in the intersection — estimates the Jaccard index.
    sample = sorted(ma | mb)[: min(ka, kb)]
    if not sample:
        return (0.0, 0.0)
    jaccard = sum(1 for v in sample if v in ma and v in mb) / len(sample)

    # Containment estimated DIRECTLY, not derived from Jaccard: the algebraic
    # I = J*(|A|+|B|)/(1+J) is exact for the true J but multiplies the KMV sample
    # error by (|A|+|B|), which is huge and one-sided exactly in the asymmetric
    # (padded-copy) regime — biasing containment upward into false holds. Instead
    # condition on the SMALLER set's min-hashes restricted to the range the larger
    # set's sketch actually observes: for a min-hash x of the smaller set with
    # x <= the larger set's k-th smallest hash, membership in the larger set is
    # fully determined (x in larger  iff  x in larger's sketch). The shared fraction
    # of those observable min-hashes estimates containment unbiasedly, and is exact
    # when the larger set is fully retained.
    if card_a <= card_b:
        small, large, large_k, large_card = ma, mb, kb, card_b
    else:
        small, large, large_k, large_card = mb, ma, ka, card_a
    if large_card <= large_k:
        observable = small  # larger set fully retained: every membership is known
    else:
        tau = max(large)  # the larger set's k-th smallest hash
        observable = {x for x in small if x <= tau}
    if not observable:
        return (jaccard, 0.0)
    containment = sum(1 for x in observable if x in large) / len(observable)
    return (jaccard, containment)
