"""Hashed generator-template n-gram corpus shared by the screener and its builder.

The DittoBench generator renders every user-facing surface from closed template
pools (``datagen.go`` category templates, ``memory_v2.go`` declarative
acknowledgement sentences, story oracle phrasings, world record shapes). Miner
fixtures that assert those exact sentences are a cheap review lead: the harness
was tuned against the generator surface rather than against a product.

The corpus is a deterministic hashed index of that generator surface, NOT a
secrecy boundary: ``research/dittobench-datagen`` is public, so anyone can
regenerate the same hashes in one command. It is kept as unsalted SHA-256
prefixes of normalized word n-grams only so that a fixture hit is reported as a
location-only lead and no matched text ever appears in a finding or a note.

This module owns normalization, hashing, and corpus loading so the build script
(``scripts/build_generator_ngram_corpus.py``) and the runtime scanner cannot
drift apart. Nothing here is a verdict: a hit routes the reviewer to the served
path (``_is_non_runtime_path`` keeps fixture locations inadmissible as
citations).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from functools import lru_cache
from importlib import resources

NGRAM_SIZE = 5
HASH_HEX_CHARS = 16
CORPUS_RESOURCE = "data/generator_ngram_corpus.json"
CORPUS_SCHEMA_VERSION = 1
# A gram made only of function words ("what is on my the") is English, not a
# generator fingerprint; require at least this many content tokens.
MIN_CONTENT_TOKENS = 2

# Go fmt verbs (%s, %q, %02d, %.2f), Go grammar slots (#slot#), brace and
# angle-bracket slots. Every placeholder splits the surrounding text into
# independent segments so no gram ever spans a filled value.
_PLACEHOLDER = re.compile(
    r"%[-+# 0]*\d*(?:\.\d+)?[a-zA-Z]|#[a-z][a-z0-9_]*#|\{[^{}\n]*\}|<[a-z][a-z0-9_ ]*>"
)
_TOKEN = re.compile(r"[a-z0-9]+")
_APOSTROPHE = re.compile(r"[’']")
# A Go rune or Rust char literal: one character or one escape (``\n``,
# ``\xHH``, ``\u{...}``, Go ``\uXXXX`` / ``\UXXXXXXXX`` / octal); anything
# else after an apostrophe is a Rust lifetime marker and is stepped over.
_CHAR_LITERAL = re.compile(
    r"'(?:[^'\\\n]|\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}"
    r"|U[0-9a-fA-F]{8}|[0-7]{3}|[^\n]))'"
)

_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "now",
        "of",
        "on",
        "one",
        "or",
        "our",
        "out",
        "please",
        "she",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "whats",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


def segment_tokens(text: str) -> list[list[str]]:
    """Split text at placeholders and tokenize each segment to lowercase words."""
    segments: list[list[str]] = []
    for raw in _PLACEHOLDER.split(text):
        cleaned = _APOSTROPHE.sub("", raw.casefold())
        tokens = _TOKEN.findall(cleaned)
        if tokens:
            segments.append(tokens)
    return segments


def _informative(gram: Sequence[str]) -> bool:
    return sum(token not in _STOPWORDS for token in gram) >= MIN_CONTENT_TOKENS


def iter_grams(text: str, *, n: int = NGRAM_SIZE) -> Iterator[tuple[str, ...]]:
    """Yield every informative word n-gram of ``text`` (segments never merge)."""
    for tokens in segment_tokens(text):
        if len(tokens) < n:
            continue
        for start in range(len(tokens) - n + 1):
            gram = tuple(tokens[start : start + n])
            if _informative(gram):
                yield gram


def gram_hash(gram: Sequence[str]) -> str:
    """Return the fixed-width SHA-256 prefix that represents one gram."""
    digest = hashlib.sha256(" ".join(gram).encode("utf-8")).hexdigest()
    return digest[:HASH_HEX_CHARS]


def hash_text(text: str, *, n: int = NGRAM_SIZE) -> set[str]:
    """Return the hash set of every informative n-gram in ``text``."""
    return {gram_hash(gram) for gram in iter_grams(text, n=n)}


def go_string_literals(source: str) -> list[str]:
    """Return every Go (or Rust) string literal in ``source`` outside comments.

    Interpreted strings keep their escape sequences decoded for the common
    cases (``\\"``, ``\\n``, ``\\t``, ``\\\\``); raw backtick strings are taken
    verbatim. Rune / char literals and ``//`` / ``/* */`` comments are skipped,
    which also covers the Rust starter kit's public template literals; a Rust
    lifetime apostrophe is stepped over so ``fn f<'a>(s: &'a str)`` never hides
    the literal that follows it.
    """
    literals: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "/":
            end = source.find("\n", index)
            index = length if end == -1 else end
            continue
        if char == "/" and nxt == "*":
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char == "`":
            end = source.find("`", index + 1)
            if end == -1:
                break
            literals.append(source[index + 1 : end])
            index = end + 1
            continue
        if char == '"':
            chars: list[str] = []
            index += 1
            while index < length and source[index] != '"':
                current = source[index]
                if current == "\\" and index + 1 < length:
                    escaped = source[index + 1]
                    chars.append({"n": "\n", "t": "\t"}.get(escaped, escaped))
                    index += 2
                    continue
                if current == "\n":
                    break
                chars.append(current)
                index += 1
            literals.append("".join(chars))
            index += 1
            continue
        if char == "'":
            # Only a genuine rune / char literal (``'x'``, ``'\\n'``, ``'\\u{1F}'``)
            # is consumed as a span. A Rust lifetime (``&'static str``, ``<'a>``)
            # has no closing quote nearby; treating it as a literal would swallow
            # every ``"..."`` template up to the next apostrophe.
            literal = _CHAR_LITERAL.match(source, index)
            index = literal.end() if literal is not None else index + 1
            continue
        index += 1
    return literals


def build_corpus_hashes(sources: Iterable[str], *, n: int = NGRAM_SIZE) -> set[str]:
    """Hash every informative n-gram of every string literal in Go sources."""
    hashes: set[str] = set()
    for source in sources:
        for literal in go_string_literals(source):
            hashes.update(hash_text(literal, n=n))
    return hashes


def build_text_hashes(texts: Iterable[str], *, n: int = NGRAM_SIZE) -> set[str]:
    """Hash every informative n-gram of whole documents (public prose, JSON)."""
    hashes: set[str] = set()
    for text in texts:
        for line in text.splitlines():
            hashes.update(hash_text(line, n=n))
    return hashes


def corpus_document(
    hashes: Iterable[str],
    *,
    source_files: Sequence[str],
    excluded_public_files: Sequence[str] = (),
) -> dict:
    """Return the canonical on-disk corpus document (sorted, versioned)."""
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "ngram_size": NGRAM_SIZE,
        "hash_hex_chars": HASH_HEX_CHARS,
        "min_content_tokens": MIN_CONTENT_TOKENS,
        "source_files": list(source_files),
        "excluded_public_files": list(excluded_public_files),
        "hashes": sorted(set(hashes)),
    }


def dumps_corpus(document: dict) -> str:
    """Serialize a corpus document byte-stably."""
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


def parse_corpus(text: str) -> frozenset[str]:
    """Validate a corpus document and return its hash set."""
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("generator n-gram corpus must be a JSON object")
    if document.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise ValueError("generator n-gram corpus schema_version mismatch")
    if document.get("ngram_size") != NGRAM_SIZE:
        raise ValueError("generator n-gram corpus ngram_size mismatch")
    if document.get("hash_hex_chars") != HASH_HEX_CHARS:
        raise ValueError("generator n-gram corpus hash width mismatch")
    hashes = document.get("hashes")
    if not isinstance(hashes, list) or not all(
        isinstance(item, str) and len(item) == HASH_HEX_CHARS for item in hashes
    ):
        raise ValueError("generator n-gram corpus hashes are malformed")
    return frozenset(hashes)


@lru_cache(maxsize=1)
def load_corpus() -> frozenset[str]:
    """Load the packaged hashed corpus (empty when the resource is absent)."""
    resource = resources.files("ditto_screener").joinpath(CORPUS_RESOURCE)
    try:
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return frozenset()
    return parse_corpus(text)


__all__ = [
    "CORPUS_RESOURCE",
    "HASH_HEX_CHARS",
    "MIN_CONTENT_TOKENS",
    "NGRAM_SIZE",
    "build_corpus_hashes",
    "build_text_hashes",
    "corpus_document",
    "dumps_corpus",
    "go_string_literals",
    "gram_hash",
    "hash_text",
    "iter_grams",
    "load_corpus",
    "parse_corpus",
    "segment_tokens",
]
