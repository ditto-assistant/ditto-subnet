"""Unit tests for :mod:`ditto.api_server.source_languages` (#436)."""

from __future__ import annotations

import ast
import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from ditto.api_server import source_languages
from ditto.api_server.fingerprint import compute_content_fingerprint
from ditto.api_server.source_languages import (
    LANGUAGE_INVENTORY_VERSION,
    UNKNOWN_LANGUAGE,
    inventory_for_members,
    language_for_member,
    language_for_path,
    language_for_shebang,
    stored_language_inventory,
)

_SCREENER_MASKING = (
    Path(__file__).resolve().parents[5]
    / "workers"
    / "screener"
    / "ditto_screener"
    / "source_masking.py"
)


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _screener_table(name: str) -> dict[str, str]:
    tree = ast.parse(_SCREENER_MASKING.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(value)
    raise AssertionError(f"{name} not found in {_SCREENER_MASKING}")


class TestScreenerParity:
    """The inventory names a file's language exactly as the screener does."""

    @pytest.mark.skipif(
        not _SCREENER_MASKING.exists(), reason="screener worker not in this checkout"
    )
    @pytest.mark.parametrize(
        ("screener_name", "mirror"),
        [
            ("_LANGUAGE_BY_SUFFIX", source_languages._SCREENER_LANGUAGE_BY_SUFFIX),
            ("_LANGUAGE_BY_NAME", source_languages._SCREENER_LANGUAGE_BY_NAME),
        ],
    )
    def test_mirrors_screener_tables(self, screener_name, mirror) -> None:
        assert dict(mirror) == _screener_table(screener_name)

    def test_inventory_only_labels_never_override_a_screener_mapping(self) -> None:
        overlap = set(source_languages._INVENTORY_ONLY_SUFFIXES) & set(
            source_languages._SCREENER_LANGUAGE_BY_SUFFIX
        )
        assert overlap == set()


class TestLanguageDetection:
    @pytest.mark.parametrize(
        ("path", "language"),
        [
            ("agent/src/main.rs", "rust"),
            ("agent/app.py", "python"),
            ("agent/web/index.TSX", "typescript"),
            ("agent/web/util.mjs", "javascript"),
            ("agent/cmd/main.go", "go"),
            ("agent/go.mod", "go"),
            ("agent/Dockerfile", "dockerfile"),
            ("agent/Dockerfile.dev", "dockerfile"),
            ("agent/requirements-dev.txt", "requirements"),
            ("agent/.env.local", "dotenv"),
            ("agent/README.md", "markdown"),
            ("agent/notes.txt", "text"),
            ("agent/fixtures/cases.json", "json"),
            ("agent/Cargo.toml", "toml"),
            ("agent/.gitignore", None),
            ("agent/run", None),
            ("agent/data.bin", None),
        ],
    )
    def test_path_rules(self, path, language) -> None:
        assert language_for_path(path) == language

    @pytest.mark.parametrize(
        ("head", "language"),
        [
            (b"#!/usr/bin/env python3\nprint(1)\n", "python"),
            (b"#!/usr/local/bin/python3.12 -u\n", "python"),
            (b"#!/usr/bin/env -S PYTHONPATH=src python3 -u\n", "python"),
            (b"#!/usr/bin/env node\n", "javascript"),
            (b"#!/usr/bin/env ts-node\n", "typescript"),
            (b"#!/bin/bash\nset -e\n", "shell"),
            (b"#!/bin/sh\n", "shell"),
            (b"#!/usr/bin/env ruby\n", None),
            (b"#!\n", None),
            (b"#!/usr/bin/env\n", None),
            (b"print(1)\n", None),
        ],
    )
    def test_shebang_rules(self, head, language) -> None:
        assert language_for_shebang(head) == language

    def test_path_wins_over_shebang(self) -> None:
        assert language_for_member("tool.rs", b"#!/usr/bin/env python3\n") == "rust"
        assert language_for_member("tool", b"#!/usr/bin/env python3\n") == "python"
        assert language_for_member("tool", b"\x00\x01binary") == UNKNOWN_LANGUAGE

    def test_every_label_is_declared(self) -> None:
        produced = {
            *source_languages._LANGUAGE_BY_SUFFIX.values(),
            *source_languages._SCREENER_LANGUAGE_BY_NAME.values(),
            *source_languages._LANGUAGE_BY_INTERPRETER.values(),
            "dockerfile",
            "requirements",
            "dotenv",
            UNKNOWN_LANGUAGE,
        }
        assert produced == source_languages.LANGUAGE_LABELS


class TestInventory:
    def test_counts_files_and_bytes_by_language(self) -> None:
        inventory = inventory_for_members(
            [
                ("a/main.py", b"x" * 10),
                ("a/util.py", b"y" * 5),
                ("a/web/app.ts", b"z" * 7),
                ("a/run", b"#!/bin/sh\necho\n"),
                ("a/LICENSE", b"MIT"),
            ],
            excluded=[b"k" * 100, b"lock" * 4],
        )
        assert inventory == {
            "v": LANGUAGE_INVENTORY_VERSION,
            "files": {"python": 2, "shell": 1, "typescript": 1, "unknown": 1},
            "bytes": {"python": 15, "shell": 15, "typescript": 7, "unknown": 3},
            "excluded_files": 2,
            "excluded_bytes": 116,
        }

    def test_is_deterministic_and_carries_no_paths(self) -> None:
        members = [("secret/dir/agent_core.py", b"code"), ("x/y.rs", b"fn")]
        a = inventory_for_members(members)
        b = inventory_for_members(list(reversed(members)))
        assert json.dumps(a) == json.dumps(b)
        serialized = json.dumps(a)
        for leaked in ("secret", "agent_core", "y.rs", "code"):
            assert leaked not in serialized


class TestStoredInventory:
    def test_round_trips_a_recorded_inventory(self) -> None:
        recorded = inventory_for_members([("a.go", b"package main")])
        assert stored_language_inventory({"v": 2, "languages": recorded}) == {
            "version": LANGUAGE_INVENTORY_VERSION,
            "files": {"go": 1},
            "bytes": {"go": 12},
            "excluded_files": 0,
            "excluded_bytes": 0,
        }

    @pytest.mark.parametrize(
        "fingerprint",
        [
            None,
            {"v": 2, "m": []},
            {"v": 2, "languages": "python"},
            {"v": 2, "languages": {"files": {}, "bytes": {}}},
            {"v": 2, "languages": {"v": "lang1", "files": [], "bytes": {}}},
        ],
    )
    def test_absent_or_malformed_reads_as_not_recorded(self, fingerprint) -> None:
        assert stored_language_inventory(fingerprint) is None

    def test_drops_unknown_labels_and_bad_counts(self) -> None:
        stored = stored_language_inventory(
            {
                "languages": {
                    "v": "lang1",
                    "files": {
                        "python": 3,
                        "src/secret_module.py": 1,
                        "rust": True,
                        "go": "7",
                        "c": -4,
                    },
                    "bytes": {"python": 2**60},
                    "excluded_files": "many",
                }
            }
        )
        assert stored is not None
        assert stored["files"] == {"c": 0, "python": 3}
        assert stored["bytes"] == {"python": 1 << 40}
        assert stored["excluded_files"] is None
        assert stored["excluded_bytes"] is None


class TestContentFingerprintInventory:
    def test_fingerprint_records_language_mix_of_authored_members(self) -> None:
        body = "\n".join(
            f"def handler_{i}(payload):\n    return compute_value(payload, {i})"
            for i in range(30)
        ).encode()
        fp = compute_content_fingerprint(
            _tar_gz(
                {
                    "agent/app.py": body,
                    "agent/web/index.ts": b"export const answer = 42;\n" * 3,
                    "agent/uv.lock": b"version = 1\n",
                }
            )
        )
        assert fp is not None
        languages = fp["languages"]
        assert languages["v"] == LANGUAGE_INVENTORY_VERSION
        assert languages["files"] == {"python": 1, "typescript": 1}
        assert languages["bytes"]["python"] == len(body)
        # The lockfile is excluded from every channel and counted only as such.
        assert languages["excluded_files"] == 1
        assert languages["excluded_bytes"] == len(b"version = 1\n")

    def test_inventory_does_not_change_the_sketch(self) -> None:
        body = "\n".join(f"let value_{i} = compute({i});" for i in range(40)).encode()
        rust = compute_content_fingerprint(_tar_gz({"agent/src/lib.rs": body}))
        python = compute_content_fingerprint(_tar_gz({"agent/src/lib.py": body}))
        assert rust is not None and python is not None
        assert rust["languages"]["files"] == {"rust": 1}
        assert python["languages"]["files"] == {"python": 1}
        strip = lambda fp: {k: v for k, v in fp.items() if k != "languages"}  # noqa: E731
        assert strip(rust) == strip(python)

    def test_unreadable_artifact_records_nothing(self) -> None:
        assert compute_content_fingerprint(gzip.compress(b"not a tar")) is None
