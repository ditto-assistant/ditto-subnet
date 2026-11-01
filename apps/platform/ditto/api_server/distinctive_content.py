"""Language-agnostic distinctive-content items for the unreleased-copy signal.

The lexical, line, structural and prompt fingerprints in
:mod:`ditto.api_server.fingerprint` all measure *code shape*. A port to another
language keeps none of it: a Rust rewrite of a Python agent shares no 4-line
window, no line, and no AST shape with its source, and the 2026-10-29
Python-to-Rust port shared 0-4 fingerprint hashes with the agent it took. What a
port does keep is the *content* the score depends on: prompt and instruction
text, cue and keyword lists, note and header constants, and the long
descriptive names given to them. This module extracts that content from any
source file as normalized **items** so two artifacts in different languages can
be compared item by item.

**Extraction** (per regular text file, :func:`extract_items`):

* every string literal, in any of the quoting forms the supported languages
  use (``"..."``, ``'...'``, triple quotes, raw strings, Go/JS backticks),
  with adjacent literals joined the way the compiler would join them;
* each literal is normalized: escapes, format placeholders (``{name}``,
  ``${x}``, ``%s``, ``{}``), punctuation, quoting and case are stripped, leaving
  lower-case words;
* a literal yields ``text`` items (one per sentence or line with at least
  :data:`_MIN_TEXT_WORDS` words), one ``short`` item (2-3 words), and every
  window of :data:`_SEQ_WINDOW` consecutive literals in a file yields a ``seq``
  item: an ordered cue or keyword list survives a port in the same order even
  when every element is short;
* identifiers of at least :data:`_MIN_IDENT_WORDS` words are split on
  ``snake_case`` / ``camelCase`` boundaries into an ``ident`` item, so
  ``FILED_ORDER_HEADER``, ``filed_order_header`` and ``filedOrderHeader`` are
  the same item.

Comments, Markdown documentation, vendored trees, fixtures, tests, lockfiles,
published starter-kit files and oversized data files are skipped. Each item
carries the ``path:line`` of its
first occurrence, which is what an operator needs to confirm a match quickly.

**What it is not.** An item is evidence only after everything public is
removed: the packaged public corpus (:func:`public_item_keys`: every starter
kit, ditto-harness and the public generator vocabulary) and, at comparison
time, every agent the subnet released before the candidate was uploaded.
Comparison lives in :mod:`ditto.api_server.unreleased_copy`.

The module is pure, deterministic and stdlib-only at import time so the
corpus builder can load it standalone.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

# Bump when extraction or normalization changes. Items from different
# versions are never compared (the index stores the version per agent).
ITEMS_VERSION = "dc2"

KIND_TEXT = "text"
KIND_SHORT = "short"
KIND_SEQ = "seq"
KIND_IDENT = "ident"
# A window of single-word literals (``"integer" | "number" | "boolean"``) is
# usually schema or protocol vocabulary, not authored content: kept, but as a
# separate, low-weight kind.
KIND_WORD_SEQ = "wordseq"
# A personal external namespace (a Hugging Face or GitHub account, a container
# registry user) referenced anywhere in the artifact. Never scored: two owners
# sharing one is a same-owner hint, not copy evidence.
KIND_RESOURCE = "resource"
KINDS = (KIND_TEXT, KIND_SHORT, KIND_SEQ, KIND_WORD_SEQ, KIND_IDENT, KIND_RESOURCE)

_MIN_TEXT_WORDS = 4
_MIN_TEXT_CHARS = 20
_SEQ_WINDOW = 3
_MIN_SEQ_CHARS = 18
_MIN_IDENT_WORDS = 3
_MIN_IDENT_CHARS = 14
_PREVIEW_CHARS = 120

# Work guards. Upload already bounds the compressed tarball; these bound the
# inflated walk exactly like the fingerprint channels do.
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_MEMBERS = 20000
# A file larger than this is data (a word list, a model, a fixture), not the
# authored surface this signal is about.
_MAX_ITEM_FILE_BYTES = 256 * 1024
_MAX_ITEMS = 200_000

_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "target",
        "vendor",
        "third_party",
        "thirdparty",
        "fixtures",
        "testdata",
        "dist",
        ".venv",
        "venv",
    }
)

_HASH_COMMENT = frozenset(
    {".py", ".pyi", ".rb", ".sh", ".bash", ".zsh", ".toml", ".yaml", ".yml"}
)
_C_COMMENT = frozenset(
    {
        ".rs",
        ".go",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".mts",
        ".tsx",
        ".java",
        ".kt",
        ".cs",
        ".swift",
        ".scala",
        ".dart",
        ".php",
        ".zig",
        ".json",
    }
)
# Prompt files an agent loads at runtime. Markdown/reST documentation is not
# served content, and lineage declarations copy their base's prose verbatim.
_PROSE = frozenset({".txt", ".prompt", ".tmpl", ".j2", ".jinja"})
# Languages where a single quote opens a string rather than a char/rune/lifetime.
_SINGLE_QUOTE_STRINGS = frozenset(
    {
        ".py",
        ".pyi",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".mts",
        ".tsx",
        ".php",
        ".dart",
        ".toml",
        ".yaml",
        ".yml",
    }
)
_BACKTICK_STRINGS = frozenset(
    {".go", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".tsx"}
)

_ESCAPE_RE = re.compile(r"\\(?:u\{?[0-9a-fA-F]{1,6}\}?|x[0-9a-fA-F]{2}|.)", re.S)
_PLACEHOLDER_RE = re.compile(
    r"\$\{[^{}]*\}|\{[^{}\s]*\}|%[-+ #0]*\d*(?:\.\d+)?[sdifgxXvqrcoeEbtTpuw]"
)
_WORD_RE = re.compile(r"[^\W_]+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_JOIN_GAP_RE = re.compile(r"[\s+\\]*")
_PY_PREFIX = frozenset("rRbBuUfF")


@dataclass(frozen=True)
class Item:
    """One distinctive item and where it first occurs in an artifact."""

    kind: str
    key: int
    """Signed 64-bit hash of ``kind`` + normalized text (a Postgres ``bigint``)."""
    path: str
    line: int
    preview: str
    """The normalized text, truncated: what the operator reads beside path:line."""
    text: str = ""
    """The full normalized text (never stored; used for fuzzy release checks)."""


_KIND_CODES = {kind: code for code, kind in enumerate(KINDS)}
_KIND_MASK = 0x7


def item_key(kind: str, normalized: str) -> int:
    """Stable signed 64-bit key for one normalized item.

    The low three bits carry the kind, so a stored ``bigint[]`` of keys is the
    whole index: :func:`key_kind` recovers what each shared key weighs.
    """
    digest = hashlib.sha256(f"{ITEMS_VERSION}:{kind}:{normalized}".encode()).digest()
    value = int.from_bytes(digest[:8], "big", signed=True)
    return (value & ~_KIND_MASK) | _KIND_CODES[kind]


def key_kind(key: int) -> str:
    """The item kind packed into ``key`` by :func:`item_key`."""
    return KINDS[key & _KIND_MASK]


def normalize_text(raw: str) -> str:
    """Lower-case words of a literal with quoting, escapes and placeholders gone."""
    text = _ESCAPE_RE.sub(" ", raw)
    text = _PLACEHOLDER_RE.sub(" ", text)
    return " ".join(_WORD_RE.findall(text.lower()))


def identifier_words(name: str) -> list[str]:
    """Split ``snake_case`` / ``SCREAMING_CASE`` / ``camelCase`` into words."""
    words: list[str] = []
    for part in name.split("_"):
        if part:
            words.extend(w.lower() for w in _CAMEL_RE.findall(part))
    return words


def _suffix(path: str) -> str:
    p = PurePosixPath(path)
    if p.name == "Dockerfile":
        return ".sh"
    return p.suffix.lower()


_TEST_FILE_RE = re.compile(
    r"^(?:test_.*\.py|.*_test\.(?:py|go)|tests?\.rs|.*_tests?\.rs"
    r"|.*\.(?:test|spec)\.[cm]?[jt]sx?|conftest\.py)$"
)


# Package manifests are templates: names, paths, feature flags and dependency
# lists that every port of the same base declares the same way.
_MANIFESTS = frozenset(
    {
        "Cargo.toml",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "go.sum",
        "setup.cfg",
        "setup.py",
        "requirements.txt",
        "requirements-dev.txt",
    }
)


def _skipped_path(path: str) -> bool:
    """Vendored, generated, fixture, test and manifest files carry no served content."""
    parts = PurePosixPath(path.removeprefix("./")).parts
    if parts and parts[-1] in _MANIFESTS:
        return True
    if any(part in _SKIP_DIRS or part in {"tests", "test"} for part in parts[:-1]):
        return True
    return bool(parts) and bool(_TEST_FILE_RE.match(parts[-1]))


def _scan(text: str, suffix: str) -> Iterator[tuple[str, str, int, int, int]]:
    """Yield ``("str"|"ident", value, line, start, end)`` tokens of one file.

    A tolerant single-pass scanner: comments are skipped, every string form the
    language family uses is decoded to its raw contents, and identifiers are
    reported between them. It never raises; malformed input yields what it can.
    """
    hash_comments = suffix in _HASH_COMMENT
    c_comments = suffix in _C_COMMENT
    single = suffix in _SINGLE_QUOTE_STRINGS
    backtick = suffix in _BACKTICK_STRINGS
    python_like = suffix in {".py", ".pyi"}
    rust = suffix == ".rs"
    n = len(text)
    i = 0
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c_comments and c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                end = text.find("\n", i)
                i = n if end == -1 else end
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                stop = n if end == -1 else end + 2
                line += text.count("\n", i, stop)
                i = stop
                continue
        if hash_comments and c == "#":
            end = text.find("\n", i)
            i = n if end == -1 else end
            continue
        if rust and c == "r" and i + 1 < n and text[i + 1] in '#"':
            j = i + 1
            hashes = 0
            while j < n and text[j] == "#":
                hashes += 1
                j += 1
            if j < n and text[j] == '"':
                close = '"' + "#" * hashes
                end = text.find(close, j + 1)
                stop = n if end == -1 else end
                yield ("str", text[j + 1 : stop], line, i, stop + len(close))
                line += text.count("\n", i, stop)
                i = n if end == -1 else stop + len(close)
                continue
        if c == "'" and not single:
            # char / rune / lifetime: skip a closed one-char literal, else step.
            m = re.match(r"'(?:\\.[^']{0,8}|[^'\\\n])'", text[i : i + 12])
            i += len(m.group(0)) if m else 1
            continue
        if c in "\"'" or (c == "`" and backtick):
            start = i
            triple = python_like and text.startswith(c * 3, i)
            quote = c * 3 if triple else c
            j = i + len(quote)
            buf_start = j
            raw_mode = c == "`"
            while j < n:
                if not raw_mode and text[j] == "\\":
                    j += 2
                    continue
                if text.startswith(quote, j):
                    break
                if text[j] == "\n" and not (triple or raw_mode):
                    break
                j += 1
            value = text[buf_start:j]
            yield ("str", value, line, start, min(n, j + len(quote)))
            line += value.count("\n")
            i = min(n, j + len(quote))
            continue
        if (c.isascii() and c.isalpha()) or c == "_":
            m = _IDENT_RE.match(text, i)
            assert m is not None
            word = m.group(0)
            # A Python string prefix (f"", rb'') belongs to the literal.
            if (
                python_like
                and len(word) <= 2
                and all(ch in _PY_PREFIX for ch in word)
                and m.end() < n
                and text[m.end()] in "\"'"
            ):
                i = m.end()
                continue
            # Rust byte strings: ``b"..."`` continues at the quote, ``br#"..."#``
            # at the ``r`` so the raw-string branch reads it.
            if rust and word in {"b", "br"} and m.end() < n and text[m.end()] in '"#':
                i = m.end() - 1 if word == "br" else m.end()
                continue
            yield ("ident", word, line, i, m.end())
            i = m.end()
            continue
        i += 1


def _literals(text: str, suffix: str) -> Iterator[tuple[str, str, int, int, int]]:
    """Yield ``(kind, raw, line, start, end)``: joined literals and identifiers."""
    pending: list[str] = []
    pending_line = 0
    pending_start = -1
    pending_end = -1
    for kind, value, line, start, end in _scan(text, suffix):
        if kind == "str":
            gap = text[pending_end:start] if pending else ""
            if pending and _JOIN_GAP_RE.fullmatch(gap):
                pending.append(value)
            else:
                if pending:
                    yield (
                        "str",
                        "".join(pending),
                        pending_line,
                        pending_start,
                        pending_end,
                    )
                pending = [value]
                pending_line = line
                pending_start = start
            pending_end = end
            continue
        if pending:
            yield ("str", "".join(pending), pending_line, pending_start, pending_end)
            pending = []
        yield ("ident", value, line, start, end)
    if pending:
        yield ("str", "".join(pending), pending_line, pending_start, pending_end)


# --- module-level constants ---------------------------------------------------
# A Python port interpolates its prompt constants (``f"... {BLOCKED} ..."``)
# where a Rust or Go port inlines them, so constants are resolved into
# ``{NAME}`` / ``${NAME}`` interpolations before normalizing.
_CONST_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_CONST_GAP_RE = re.compile(
    r"\s*(?::[^=\n;{}()]*)?=\s*(?:[rbuRBUfF]{0,2}|r#*|b|br#*)?\s*", re.S
)
_NUM_CONST_RE = re.compile(
    r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+|static\s+)?"
    r"([A-Z][A-Z0-9_]{2,})\s*(?::\s*[\w&'<>\[\]; ]+?)?\s*=\s*"
    r"(-?\d[\d_]*(?:\.\d+)?)\s*[;,]?\s*(?:#.*|//.*)?$"
)
_INTERP_RE = re.compile(r"\$?\{([A-Z][A-Z0-9_]{2,})(?:[!:][^{}]*)?\}")


def file_constants(path: str, raw: bytes) -> dict[str, str]:
    """String and number constants a file defines (``NAME = "..."``, ``const``)."""
    suffix = _suffix(path)
    if suffix not in _HASH_COMMENT and suffix not in _C_COMMENT:
        return {}
    text = raw.decode("utf-8", errors="replace")
    out: dict[str, str] = {}
    previous: tuple[str, int] | None = None
    for kind, value, _line, start, end in _literals(text, suffix):
        if kind == "ident":
            previous = (value, end) if _CONST_NAME_RE.match(value) else None
            continue
        if previous is not None and _CONST_GAP_RE.fullmatch(text[previous[1] : start]):
            out.setdefault(previous[0], value)
        previous = None
    for name, number in _NUM_CONST_RE.findall(text):
        out.setdefault(name, number.replace("_", ""))
    return out


def _resolve(value: str, constants: dict[str, str]) -> str:
    if not constants or "{" not in value:
        return value
    for _ in range(3):
        resolved = _INTERP_RE.sub(
            lambda m: constants.get(m.group(1), m.group(0)), value
        )
        if resolved == value:
            break
        value = resolved
    return value


# --- chunking -------------------------------------------------------------------
# Real and escaped newlines are the same thing to a port: a Python triple-quoted
# prompt hard-wraps where a Rust literal writes ``\n`` or one long line. Chunks
# break at blank lines and sentence ends only; a single newline is a space.
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?;:])\s+")


def _chunks(value: str) -> Iterator[tuple[str, int]]:
    """``(chunk, newline offset)`` sentences of one literal or prose text."""
    unescaped = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    offset = 0
    for paragraph in _PARAGRAPH_RE.split(unescaped):
        flat = re.sub(r"\s*\n\s*", " ", paragraph)
        for sentence in _SENTENCE_END_RE.split(flat):
            yield sentence, offset
        offset += paragraph.count("\n") + 2


# --- external namespaces --------------------------------------------------------
_RESOURCE_PATTERNS = (
    (
        "hf",
        re.compile(
            r"(?:huggingface\.co|hf\.co)/(?:(?:datasets|spaces|models)/)?"
            r"([A-Za-z0-9][\w.-]{1,95})/[\w.-]+",
            re.I,
        ),
    ),
    (
        "gh",
        re.compile(
            r"(?:github\.com|raw\.githubusercontent\.com)/([A-Za-z0-9][A-Za-z0-9-]{0,38})/",
            re.I,
        ),
    ),
    (
        "oci",
        re.compile(
            r"(?:ghcr\.io|quay\.io|docker\.io|registry\.gitlab\.com)/([a-z0-9][a-z0-9._-]*)/",
            re.I,
        ),
    ),
)
_FROM_RE = re.compile(r"(?im)^\s*FROM\s+(?:--\S+\s+)*(\S+)")


def _resource_items(path: str, text: str) -> Iterator[Item]:
    seen: set[str] = set()
    for site, pattern in _RESOURCE_PATTERNS:
        for match in pattern.finditer(text):
            seen.add(f"{site}:{match.group(1).lower()}")
    if PurePosixPath(path).name.startswith("Dockerfile"):
        for image in _FROM_RE.findall(text):
            parts = image.split("@", 1)[0].split(":", 1)[0].split("/")
            if len(parts) >= 3 or (
                len(parts) == 2 and ("." in parts[0] or ":" in parts[0])
            ):
                namespace = parts[1] if len(parts) >= 3 else ""
            elif len(parts) == 2:
                namespace = parts[0]
            else:
                namespace = ""
            if namespace and namespace != "library":
                seen.add(f"oci:{namespace.lower()}")
    for name in sorted(seen):
        yield Item(KIND_RESOURCE, item_key(KIND_RESOURCE, name), path, 1, name, name)


def _text_items(path: str, value: str, line: int) -> Iterator[Item]:
    for chunk, newline_offset in _chunks(value):
        norm = normalize_text(chunk)
        if len(norm) >= _MIN_TEXT_CHARS and norm.count(" ") + 1 >= _MIN_TEXT_WORDS:
            yield Item(
                KIND_TEXT,
                item_key(KIND_TEXT, norm),
                path,
                line + newline_offset,
                norm[:_PREVIEW_CHARS],
                norm,
            )


def _prose_items(path: str, text: str) -> Iterator[Item]:
    yield from _text_items(path, text, 1)


def file_items(
    path: str, raw: bytes, constants: dict[str, str] | None = None
) -> list[Item]:
    """Every distinctive item of one source file, in source order.

    ``constants`` (from :func:`file_constants` over the whole artifact) are
    resolved into interpolations before normalizing.
    """
    suffix = _suffix(path)
    text = raw.decode("utf-8", errors="replace")
    resources = list(_resource_items(path, text))
    if (
        suffix not in _HASH_COMMENT
        and suffix not in _C_COMMENT
        and suffix not in _PROSE
    ):
        return resources
    if suffix in _PROSE:
        return resources + list(_prose_items(path, text))
    constants = constants or {}
    out: list[Item] = resources
    window: list[tuple[str, int]] = []
    for kind, value, line, _start, _end in _literals(text, suffix):
        if kind == "ident":
            parts = identifier_words(value)
            joined = " ".join(parts)
            if len(parts) >= _MIN_IDENT_WORDS and len(joined) >= _MIN_IDENT_CHARS:
                out.append(
                    Item(
                        KIND_IDENT,
                        item_key(KIND_IDENT, joined),
                        path,
                        line,
                        joined[:_PREVIEW_CHARS],
                        joined,
                    )
                )
            continue
        value = _resolve(value, constants)
        whole = normalize_text(value)
        if not whole:
            continue
        window.append((whole, line))
        if len(window) > _SEQ_WINDOW:
            window.pop(0)
        if len(window) == _SEQ_WINDOW:
            seq = " | ".join(w for w, _ in window)
            if len(seq) >= _MIN_SEQ_CHARS:
                kind = (
                    KIND_WORD_SEQ if all(" " not in w for w, _ in window) else KIND_SEQ
                )
                out.append(
                    Item(
                        kind,
                        item_key(kind, seq),
                        path,
                        window[0][1],
                        seq[:_PREVIEW_CHARS],
                        seq,
                    )
                )
        words = whole.count(" ") + 1
        if 2 <= words < _MIN_TEXT_WORDS:
            out.append(
                Item(
                    KIND_SHORT,
                    item_key(KIND_SHORT, whole),
                    path,
                    line,
                    whole[:_PREVIEW_CHARS],
                    whole,
                )
            )
            continue
        out.extend(_text_items(path, value, line))
    return out


@dataclass(frozen=True)
class ArtifactItems:
    """First occurrence of every distinct item in one artifact."""

    version: str
    items: dict[int, Item]


def extract_items(
    tar_gz_bytes: bytes, *, excluded: Callable[[str, bytes], bool] | None = None
) -> ArtifactItems | None:
    """Distinct items of a ``.tar.gz`` artifact, or ``None`` if unreadable.

    ``excluded(path, raw)`` drops whole members (published starter-kit files,
    generated lockfiles); the platform passes
    :func:`ditto.api_server.fingerprint._excluded_from_fingerprints`. The
    first occurrence of an item (sorted path order, then line) is kept so the
    evidence is deterministic. Best-effort: a hostile or corrupt tarball returns
    ``None`` and never raises into the caller.
    """
    found: dict[int, Item] = {}
    total = 0
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_gz_bytes), mode="r:gz") as tar:
            entries: list[tuple[str, bytes]] = []
            for member in tar:
                members += 1
                if members > _MAX_MEMBERS:
                    return None
                if not member.isfile() or member.size > _MAX_ITEM_FILE_BYTES:
                    continue
                path = member.name.removeprefix("./")
                if _skipped_path(path):
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read(_MAX_ITEM_FILE_BYTES + 1)
                total += len(raw)
                if total > _MAX_TOTAL_BYTES:
                    return None
                if excluded is not None and excluded(path, raw):
                    continue
                entries.append((path, raw))
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError, ValueError):
        return None
    constants: dict[str, str] = {}
    for path, raw in sorted(entries, key=lambda e: e[0]):
        for name, value in file_constants(path, raw).items():
            constants.setdefault(name, value)
    for path, raw in sorted(entries, key=lambda e: e[0]):
        for item in file_items(path, raw, constants):
            if item.key not in found:
                found[item.key] = item
                if len(found) > _MAX_ITEMS:
                    return None
    return ArtifactItems(version=ITEMS_VERSION, items=found)


@lru_cache(maxsize=1)
def public_item_keys() -> frozenset[int]:
    """Keys of every item in the packaged public corpus (see the builder)."""
    from importlib.resources import files as resource_files

    raw = resource_files("ditto.anticopy").joinpath(PUBLIC_CORPUS_BUNDLE).read_bytes()
    data = gzip.decompress(raw)
    if len(data) % 8:
        raise RuntimeError(f"invalid public item bundle: {PUBLIC_CORPUS_BUNDLE}")
    return frozenset(
        int.from_bytes(data[i : i + 8], "big", signed=True)
        for i in range(0, len(data), 8)
    )


PUBLIC_CORPUS_BUNDLE = f"public_items_{ITEMS_VERSION}.bin.gz"
