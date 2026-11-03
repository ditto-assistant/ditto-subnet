"""Structural (AST-shape) fingerprint of an uploaded harness, computed by Platform.

This is a byte-for-byte port of the scorer's ``internal/astfp`` package
(``services/dittobench-api/internal/astfp/astfp.go``). It used to run only on
validator hosts, which therefore had to download and unpack every miner's
source tarball just to produce this moderation signal. Platform already holds
the trusted upload, so it computes the sketch here and validators score from
the screened image alone.

The sketch must equal the Go output for the same artifact: the anti-copy gate
compares stored sketches across agents, and the existing pool was produced by
validators. So this module mirrors Go exactly, including:

* the extractor's file set (regular members only, last duplicate wins) and the
  Docker build-context choice (root ``Dockerfile`` or one top-level directory
  holding it);
* ``filepath.WalkDir`` order, which matters only once a walk limit trips;
* Go's rune decoding (one U+FFFD per invalid byte) and ``unicode`` classes;
* tree-sitter-rust ``v0.21.2``, the grammar the Go binding vendors. The Python
  dependency is pinned to that release; a different grammar emits different
  node types and silently splits the comparison pool.

Bump nothing here without bumping ``astfp.Version`` in Go and backfilling.
"""

from __future__ import annotations

import codecs
import gzip
import hashlib
import io
import logging
import posixpath
import tarfile
import unicodedata
import warnings
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

logger = logging.getLogger(__name__)

VERSION = 1
_SHINGLE_NODES = 6
_GENERIC_SHINGLE_TOKENS = 10
_MINHASH_K = 256
_HASH_HEX = 16
_MAX_FILES = 5000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SHINGLES = 500_000
_MAX_NODES_PER_FILE = 2_000_000
# Extractor guards (internal/sandbox/tarball.go). A tarball the scorer would
# refuse to unpack never produced a sketch there, so it produces none here.
_MAX_TARBALL_BYTES = 20 << 20
_MAX_EXTRACTED_BYTES = 160 << 20
_MAX_TARBALL_FILES = 20000

_SOURCE_KINDS = {
    ".rs": "rust", ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".mts": "typescript", ".cts": "typescript", ".go": "go", ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin", ".c": "c", ".h": "c", ".cc": "cpp",
    ".cpp": "cpp", ".cxx": "cpp", ".hh": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sc": "scala", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".hrl": "erlang", ".fs": "fsharp", ".fsx": "fsharp", ".lua": "lua",
    ".dart": "dart", ".zig": "zig", ".sh": "shell", ".bash": "shell",
    ".zsh": "shell", ".vue": "web", ".svelte": "web",
}  # fmt: skip

_STRUCTURAL_KEYWORDS = frozenset({
    "as", "async", "await", "break", "case", "catch", "class", "const", "continue",
    "def", "defer", "do", "else", "elif", "enum", "except", "export", "extends",
    "false", "finally", "fn", "for", "foreach", "from", "func", "function", "go",
    "if", "implements", "import", "in", "interface", "let", "loop", "match", "mod",
    "new", "nil", "none", "null", "package", "pass", "private", "protected",
    "public", "raise", "return", "select", "self", "static", "struct", "super",
    "switch", "this", "throw", "trait", "true", "try", "type", "typeof", "union",
    "unsafe", "use", "var", "while", "with", "yield",
})  # fmt: skip
_OPERATOR_PAIRS = frozenset({
    "==", "!=", "<=", ">=", "->", "=>", "::", ":=", "&&", "||", "++", "--", "+=",
    "-=", "*=", "/=", "**", "??", "?.", "<<", ">>",
})  # fmt: skip
_HASH_COMMENT_KINDS = frozenset({"python", "ruby", "shell", "php"})
_REPLACEMENT_CHAR = chr(0xFFFD)
# Go unicode.IsSpace: the Latin-1 set plus the Unicode White_Space property.
# Python's str.isspace also accepts U+001C..U+001F, which Go does not.
_GO_SPACE = frozenset(
    chr(code)
    for code in (
        *(0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680),
        *range(0x2000, 0x200B),
        *(0x2028, 0x2029, 0x202F, 0x205F, 0x3000),
    )
)


def _go_rune_errors(error: UnicodeError) -> tuple[str | bytes, int]:
    """Replace each undecodable byte with U+FFFD, as Go's []rune(string) does."""
    if not isinstance(error, UnicodeDecodeError):
        raise error
    return _REPLACEMENT_CHAR * (error.end - error.start), error.end


codecs.register_error("ditto-go-runes", _go_rune_errors)


def compute_structural_fingerprint(tar_gz_bytes: bytes) -> dict | None:
    """Return the ``{v, k, card, m}`` structural sketch, or ``None``.

    ``None`` means no structural signal: an archive the scorer could not unpack
    or build-context, no supported source file, or a tripped work guard. Never
    raises — this is a best-effort moderation signal on an accepted upload.
    """
    try:
        files = _build_context_files(tar_gz_bytes)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError, ValueError) as e:
        logger.info("structural fingerprint: unusable tarball (%s)", type(e).__name__)
        return None
    if files is None:
        return None
    try:
        return _fingerprint_files(files)
    except Exception:
        logger.exception("structural fingerprint failed")
        return None


def _build_context_files(tar_gz_bytes: bytes) -> list[tuple[str, bytes]] | None:
    """Mirror ``fetchTarball`` + ``buildContextDir``: context files in walk order."""
    if len(tar_gz_bytes) > _MAX_TARBALL_BYTES:
        return None
    files: dict[str, bytes] = {}
    top_dirs: set[str] = set()
    total = 0
    entries = 0
    with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tar:
        for member in tar:
            entries += 1
            if entries > _MAX_TARBALL_FILES:
                return None
            rel = _safe_rel(member.name)
            if rel is None:
                return None
            if member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
                extracted = tar.extractfile(member)
                raw = extracted.read() if extracted is not None else b""
                total += len(raw)
                if total > _MAX_EXTRACTED_BYTES:
                    return None
                files[rel] = raw
                if "/" in rel:
                    top_dirs.add(rel.split("/", 1)[0])
            else:
                total += max(member.size, 0)
                if total > _MAX_EXTRACTED_BYTES:
                    return None
                if member.isdir() and rel != ".":
                    top_dirs.add(rel.split("/", 1)[0])
    if "Dockerfile" in files:
        prefix = ""
    elif len(top_dirs) == 1 and f"{next(iter(top_dirs))}/Dockerfile" in files:
        prefix = f"{next(iter(top_dirs))}/"
    else:
        return None  # the scorer refuses an ambiguous build context
    scoped = [(path, raw) for path, raw in files.items() if path.startswith(prefix)]
    scoped.sort(
        key=lambda item: tuple(
            part.encode("utf-8", "surrogateescape") for part in item[0].split("/")
        )
    )
    return scoped


def _safe_rel(name: str) -> str | None:
    """Go ``safeJoin``: reject empty, absolute, and escaping member names."""
    if not name.strip() or name.startswith(("/", "\\")):
        return None
    rel = posixpath.normpath(name)
    if rel == ".." or rel.startswith("../"):
        return None
    return rel


def _go_ext(path: str) -> str:
    """Go ``filepath.Ext``: the suffix from the final dot of the last element."""
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:] if dot >= 0 else ""


def _fingerprint_files(files: list[tuple[str, bytes]]) -> dict | None:
    shingles: set[str] = set()
    seen = 0
    parser: Parser | None = None
    for path, raw in files:
        kind = _SOURCE_KINDS.get(_go_ext(path).lower())
        if kind is None:
            continue
        seen += 1
        if seen > _MAX_FILES:
            break
        if len(raw) > _MAX_FILE_BYTES:
            continue
        if kind == "rust":
            parser = parser or _rust_parser()
            hashes = _rust_shingles(parser, raw)
        else:
            hashes = _generic_shingles(kind, raw)
        overflow = False
        for value in hashes:
            shingles.add(value)
            if len(shingles) > _MAX_SHINGLES:
                overflow = True
                break
        if overflow:
            break
    if not shingles:
        return None
    ordered = sorted(shingles)
    return {
        "v": VERSION,
        "k": _MINHASH_K,
        "card": len(shingles),
        "m": ordered[:_MINHASH_K],
    }


@lru_cache(maxsize=1)
def _rust_language():  # noqa: ANN202 - tree_sitter is an optional import
    import tree_sitter_rust
    from tree_sitter import Language

    with warnings.catch_warnings():
        # tree-sitter-rust 0.21.2 hands back a raw pointer; the pin is deliberate.
        warnings.simplefilter("ignore", DeprecationWarning)
        return Language(tree_sitter_rust.language())


def _rust_parser() -> Parser:
    from tree_sitter import Parser

    return Parser(_rust_language())


def _hash_shingle(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_HEX]


def _rust_shingles(parser: Parser, src: bytes) -> list[str]:
    """Hashed 6-node shingles of the pre-order named-node-type stream."""
    root = parser.parse(src).root_node
    out: list[str] = []
    window: list[str] = []
    stack: list[Node] = [root]
    nodes = 0
    while stack:
        node = stack.pop()
        if node.is_named:
            nodes += 1
            if nodes > _MAX_NODES_PER_FILE:
                return []
            window.append(node.type)
            if len(window) > _SHINGLE_NODES:
                del window[0]
            if len(window) == _SHINGLE_NODES:
                out.append(_hash_shingle(" ".join(window)))
        stack.extend(reversed(node.named_children))
    if out:
        return out
    if not window:
        return []
    return [_hash_shingle(" ".join(window))]


def _generic_shingles(kind: str, raw: bytes) -> list[str]:
    tokens = _generic_tokens(kind, raw.decode("utf-8", "ditto-go-runes"))
    if not tokens:
        return []
    prefix = f"generic:{kind}:"
    if len(tokens) < _GENERIC_SHINGLE_TOKENS:
        return [_hash_shingle(prefix + " ".join(tokens))]
    return [
        _hash_shingle(prefix + " ".join(tokens[i : i + _GENERIC_SHINGLE_TOKENS]))
        for i in range(len(tokens) - _GENERIC_SHINGLE_TOKENS + 1)
    ]


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "L"


def _is_digit(ch: str) -> bool:
    return unicodedata.category(ch) == "Nd"


def _go_lower(word: str) -> str:
    # Go's per-rune ToLower maps U+0130 to "i"; Python's full mapping adds a
    # combining dot. Only keyword membership reads the result.
    return word.replace(chr(0x130), "i").lower()


def _generic_tokens(kind: str, src: str) -> list[str]:  # noqa: C901, PLR0912
    """Literal port of Go ``genericTokens``; keep the control flow identical."""
    tokens: list[str] = []
    hash_comment = kind in _HASH_COMMENT_KINDS
    n = len(src)
    i = 0
    while i < n and len(tokens) < _MAX_NODES_PER_FILE:
        r = src[i]
        if r in _GO_SPACE:
            i += 1
            continue
        if hash_comment and r == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if r == "/" and i + 1 < n and src[i + 1] == "/":
            i += 2
            while i < n and src[i] != "\n":
                i += 1
            continue
        if r == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            if i + 1 < n:
                i += 2
            continue
        if r in "'\"`":
            quote = r
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            tokens.append("literal")
            continue
        if _is_digit(r):
            i += 1
            while i < n and (_is_digit(src[i]) or _is_letter(src[i]) or src[i] in "._"):
                i += 1
            tokens.append("number")
            continue
        if r == "_" or _is_letter(r):
            start = i
            i += 1
            while i < n and (src[i] == "_" or _is_letter(src[i]) or _is_digit(src[i])):
                i += 1
            word = _go_lower(src[start:i])
            tokens.append(word if word in _STRUCTURAL_KEYWORDS else "identifier")
            continue
        if i + 1 < n and src[i : i + 2] in _OPERATOR_PAIRS:
            tokens.append(src[i : i + 2])
            i += 2
            continue
        tokens.append(r)
        i += 1
    return tokens
