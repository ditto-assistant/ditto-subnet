"""Deterministic source-language inventory for anti-copy provenance (#436).

The anti-copy channels in :mod:`ditto.api_server.fingerprint` were built around
the Rust starter kit: the normalized-source hash strips only ``//`` and
``/* */`` comments, and prompt extraction reads Rust-shaped string literals. A
Python, TypeScript, or Go artifact therefore gets weaker evidence from those
channels, and nothing recorded *how much* of an artifact they could read
properly. This module answers that question without changing any channel: it
classifies each fingerprinted member by language and counts files and bytes per
language, so a reviewer can see that a comparison involved, say, a mostly
Python artifact before reading a normalized-source miss as exculpatory.

**Detection rules.** The path table mirrors the screener's masking table
(``workers/screener/ditto_screener/source_masking.py``: ``_LANGUAGE_BY_SUFFIX``,
``_LANGUAGE_BY_NAME`` and ``language_for_path``) so both components name a
file's language identically; a contract test pins that every screener mapping
is reproduced here unchanged. On top of it this inventory adds a few
documentation/data labels the screener has no lexer for, and reads a ``#!``
interpreter line for files the path table does not name. Anything else is
``unknown``. The labels are a closed set, so the inventory never carries a path,
a file name, or any submitted content: only counts keyed by fixed labels.

The inventory is provenance, not evidence. It never feeds a hold, a clear, or a
score, and a change to these rules bumps :data:`LANGUAGE_INVENTORY_VERSION`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Bump whenever a label or detection rule changes, so inventories recorded
# under different rules are never read as comparable.
LANGUAGE_INVENTORY_VERSION = "lang1"
UNKNOWN_LANGUAGE = "unknown"

# Mirrors ``ditto_screener.source_masking._LANGUAGE_BY_SUFFIX`` exactly.
_SCREENER_LANGUAGE_BY_SUFFIX: Mapping[str, str] = {
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".cxx": "c",
    ".hh": "c",
    ".hpp": "c",
    ".hxx": "c",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".dart": "dart",
    ".php": "php",
    ".zig": "zig",
    ".swift": "swift",
    ".scala": "scala",
    ".sc": "scala",
    ".groovy": "groovy",
    ".gradle": "groovy",
    ".fs": "fsharp",
    ".fsi": "fsharp",
    ".fsx": "fsharp",
    ".mm": "c",
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".pyx": "python",
    ".pxd": "python",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ksh": "shell",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".mk": "make",
    ".ini": "ini",
    ".cfg": "ini",
}
# Mirrors ``ditto_screener.source_masking._LANGUAGE_BY_NAME`` exactly.
_SCREENER_LANGUAGE_BY_NAME: Mapping[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "go.mod": "go",
    "go.work": "go",
    "makefile": "make",
    "gnumakefile": "make",
    "pipfile": "toml",
    "cargo.lock": "toml",
    "poetry.lock": "toml",
    "uv.lock": "toml",
    ".env": "dotenv",
}
# Inventory-only labels: documentation and data files the screener has no
# lexer for. Naming them keeps a README or a JSON fixture out of ``unknown``,
# which a reviewer would otherwise have to guess at.
_INVENTORY_ONLY_SUFFIXES: Mapping[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".rst": "restructuredtext",
    ".txt": "text",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}
_LANGUAGE_BY_SUFFIX: Mapping[str, str] = {
    **_INVENTORY_ONLY_SUFFIXES,
    **_SCREENER_LANGUAGE_BY_SUFFIX,
}

# ``#!`` interpreters, by basename with any trailing version stripped
# (``python3.12`` -> ``python``). Only interpreters whose language already has
# a label are named; anything else stays ``unknown``.
_LANGUAGE_BY_INTERPRETER: Mapping[str, str] = {
    "python": "python",
    "pypy": "python",
    "node": "javascript",
    "nodejs": "javascript",
    "ts-node": "typescript",
    "tsx": "typescript",
    "sh": "shell",
    "bash": "shell",
    "dash": "shell",
    "zsh": "shell",
    "ksh": "shell",
}
_INTERPRETER_VERSION = re.compile(r"[\d.]+$")
_SHEBANG_MAX_BYTES = 256

LANGUAGE_LABELS = frozenset(
    {
        *_LANGUAGE_BY_SUFFIX.values(),
        *_SCREENER_LANGUAGE_BY_NAME.values(),
        "dockerfile",
        "requirements",
        "dotenv",
        *_LANGUAGE_BY_INTERPRETER.values(),
        UNKNOWN_LANGUAGE,
    }
)


def language_for_path(path: str) -> str | None:
    """Return the screener's language for ``path``, or ``None`` when unknown.

    Same rules, in the same order, as the screener's ``language_for_path``,
    plus the inventory-only documentation/data suffixes.
    """
    name = path.rsplit("/", 1)[-1].casefold()
    language = _SCREENER_LANGUAGE_BY_NAME.get(name)
    if language is not None:
        return language
    if name.startswith(("dockerfile.", "containerfile.")) or name.endswith(
        (".dockerfile", ".containerfile")
    ):
        return "dockerfile"
    if name.startswith(("requirements", "constraints")) and name.endswith(".txt"):
        return "requirements"
    if name.startswith(".env."):
        return "dotenv"
    dot = name.rfind(".")
    return _LANGUAGE_BY_SUFFIX.get(name[dot:]) if dot > 0 else None


def language_for_shebang(raw: bytes) -> str | None:
    """Return the language a leading ``#!`` line names, or ``None``.

    ``#!/usr/bin/env -S python3 -u`` and ``#!/usr/local/bin/python3.12`` both
    read as ``python``. ``env`` options and ``NAME=value`` assignments before
    the interpreter are skipped.
    """
    if not raw.startswith(b"#!"):
        return None
    line = raw[2:_SHEBANG_MAX_BYTES].split(b"\n", 1)[0]
    words = line.decode("utf-8", "replace").split()
    if not words:
        return None
    interpreter = words[0].rsplit("/", 1)[-1]
    if interpreter == "env":
        rest = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
        if not rest:
            return None
        interpreter = rest[0].rsplit("/", 1)[-1]
    interpreter = _INTERPRETER_VERSION.sub("", interpreter.casefold())
    return _LANGUAGE_BY_INTERPRETER.get(interpreter)


def language_for_member(path: str, raw: bytes) -> str:
    """Classify one archive member: path rules first, then its ``#!`` line."""
    return language_for_path(path) or language_for_shebang(raw) or UNKNOWN_LANGUAGE


class LanguageInventory:
    """Accumulate per-language file and byte counts for one artifact."""

    __slots__ = ("_bytes", "_excluded_bytes", "_excluded_files", "_files")

    def __init__(self) -> None:
        self._files: dict[str, int] = {}
        self._bytes: dict[str, int] = {}
        self._excluded_files = 0
        self._excluded_bytes = 0

    def add(self, path: str, raw: bytes) -> None:
        """Count one fingerprinted (miner-authored) member."""
        language = language_for_member(path, raw)
        self._files[language] = self._files.get(language, 0) + 1
        self._bytes[language] = self._bytes.get(language, 0) + len(raw)

    def exclude(self, raw: bytes) -> None:
        """Count one member dropped as stock kit content or a lockfile."""
        self._excluded_files += 1
        self._excluded_bytes += len(raw)

    def to_json(self) -> dict:
        """Return the JSON-ready inventory; keys are fixed labels, never paths."""
        return {
            "v": LANGUAGE_INVENTORY_VERSION,
            "files": dict(sorted(self._files.items())),
            "bytes": dict(sorted(self._bytes.items())),
            "excluded_files": self._excluded_files,
            "excluded_bytes": self._excluded_bytes,
        }


def inventory_for_members(
    members: Iterable[tuple[str, bytes]], *, excluded: Iterable[bytes] = ()
) -> dict:
    """Pure helper: the inventory for ``(path, raw)`` members plus exclusions."""
    inventory = LanguageInventory()
    for path, raw in members:
        inventory.add(path, raw)
    for raw in excluded:
        inventory.exclude(raw)
    return inventory.to_json()


_MAX_LABELS = len(LANGUAGE_LABELS)
_MAX_COUNT = 1 << 40


def _counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for key, count in value.items():
        if key not in LANGUAGE_LABELS or isinstance(count, bool):
            continue
        if not isinstance(count, int):
            continue
        out[key] = max(0, min(count, _MAX_COUNT))
        if len(out) >= _MAX_LABELS:
            break
    return dict(sorted(out.items()))


def stored_language_inventory(content_fingerprint: dict | None) -> dict | None:
    """Return a bounded, label-only copy of a stored inventory, or ``None``.

    Rows fingerprinted before the inventory existed, or carrying a malformed
    one, read as ``None`` ("not recorded"), never as an empty artifact. Only
    the fixed labels survive, so nothing a stored row could smuggle into this
    JSON reaches an operator surface.
    """
    if not isinstance(content_fingerprint, dict):
        return None
    stored = content_fingerprint.get("languages")
    if not isinstance(stored, dict):
        return None
    version = stored.get("v")
    files = _counts(stored.get("files"))
    sizes = _counts(stored.get("bytes"))
    if not isinstance(version, str) or files is None or sizes is None:
        return None
    excluded_files = stored.get("excluded_files")
    excluded_bytes = stored.get("excluded_bytes")
    return {
        "version": version[:32],
        "files": files,
        "bytes": sizes,
        "excluded_files": (
            max(0, min(excluded_files, _MAX_COUNT))
            if isinstance(excluded_files, int) and not isinstance(excluded_files, bool)
            else None
        ),
        "excluded_bytes": (
            max(0, min(excluded_bytes, _MAX_COUNT))
            if isinstance(excluded_bytes, int) and not isinstance(excluded_bytes, bool)
            else None
        ),
    }
