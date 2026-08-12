"""The stock-kit digest bundle: history coverage, extra revisions, determinism."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from pathlib import Path

from ditto.api_server.fingerprint import _normalized_source
from scripts.build_kit_file_digests import build_kit_file_digests


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Kit Digest Test",
            "-c",
            "user.email=kit-digest-test@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


def _kit_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "monorepo"
    kit = repo / "miners" / "dittobench-starter-kit"
    kit.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (kit / "src.rs").write_text("fn kit_v1() {\n    let value = 1;\n}\n")
    _commit(repo, "import the kit")
    (kit / "src.rs").write_text("fn kit_v2() {\n    let value = 2;\n}\n")
    (kit / "model.bin").write_bytes(b"\x00\x01\x02\xff not utf-8")
    _commit(repo, "update the kit")
    return repo


def _baseline_bundle(output: Path, texts: tuple[str, ...]) -> None:
    """A minimal operator baseline bundle carrying an upstream lineage."""
    bundle = {
        "format": "starter-kit-baseline-v1",
        "source": "https://example.invalid/kit",
        "revision": "0" * 40,
        "commits": ["0" * 40],
        "historical_sha256": sorted(
            hashlib.sha256(text.encode()).hexdigest() for text in texts
        ),
        "historical_normalized_sha256": sorted(
            hashlib.sha256(_normalized_source(text.encode()).encode()).hexdigest()
            for text in texts
        ),
    }
    (output / "starter_kit_baseline_v1.json.gz").write_bytes(
        gzip.compress(json.dumps(bundle).encode())
    )


def test_covers_every_revision_of_the_kit_path_including_binaries(
    tmp_path: Path,
) -> None:
    repo = _kit_repo(tmp_path)
    output = tmp_path / "anticopy"
    output.mkdir()

    bundle = build_kit_file_digests(repo=repo, output=output)

    exact = set(bundle.raw_sha256)
    # Both revisions of the source file, not just the tip: miners fork anywhere.
    for content in (
        b"fn kit_v1() {\n    let value = 1;\n}\n",
        b"fn kit_v2() {\n    let value = 2;\n}\n",
        b"\x00\x01\x02\xff not utf-8",
    ):
        assert hashlib.sha256(content).hexdigest() in exact
    # A non-UTF-8 blob contributes no normalized digest, so binary fixtures can
    # never collide with source through the canonicalized channel.
    assert len(bundle.normalized_sha256) == 2


def test_inherits_the_upstream_lineage_from_the_baseline_bundle(
    tmp_path: Path,
) -> None:
    """The pre-import revisions live only in the review bundle; use them."""
    repo = _kit_repo(tmp_path)
    output = tmp_path / "anticopy"
    output.mkdir()
    upstream = "fn pre_import_kit() {\n    let value = 0;\n}\n"
    _baseline_bundle(output, (upstream,))

    bundle = build_kit_file_digests(repo=repo, output=output)

    assert hashlib.sha256(upstream.encode()).hexdigest() in set(bundle.raw_sha256)
    assert any(
        source.kind == "starter-kit-baseline-bundle" for source in bundle.sources
    )


def test_extra_tree_and_curated_revisions_are_merged(tmp_path: Path) -> None:
    repo = _kit_repo(tmp_path)
    output = tmp_path / "anticopy"
    output.mkdir()
    tree = tmp_path / "kit-v0.3.1"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "old.rs").write_text("fn ancient_kit() {}\n")
    curated = hashlib.sha256(b"a revision nobody can check out any more").hexdigest()
    (output / "kit_revisions_extra.json").write_text(
        json.dumps(
            {
                "format": "kit-revisions-extra-v1",
                "revisions": [
                    {"label": "v0.1.0", "note": "deleted tag", "raw_sha256": [curated]}
                ],
            }
        )
    )

    bundle = build_kit_file_digests(repo=repo, output=output, extra_trees=(tree,))

    exact = set(bundle.raw_sha256)
    assert hashlib.sha256(b"fn ancient_kit() {}\n").hexdigest() in exact
    assert curated in exact
    kinds = {source.kind for source in bundle.sources}
    assert {"extra-tree", "curated-revision"} <= kinds


def test_output_is_deterministic(tmp_path: Path) -> None:
    repo = _kit_repo(tmp_path)
    output = tmp_path / "anticopy"
    output.mkdir()

    first = build_kit_file_digests(repo=repo, output=output)
    written = (output / "kit_file_digests_v1.json").read_bytes()
    second = build_kit_file_digests(repo=repo, output=output)

    assert first.identity == second.identity
    assert (output / "kit_file_digests_v1.json").read_bytes() == written
