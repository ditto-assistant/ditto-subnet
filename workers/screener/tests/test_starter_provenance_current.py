"""Keep the newest starter manifest equal to the monorepo kit, and staged.

Source review exempts exact ``path + sha256`` starter matches from parts of its
scrutiny and attributes everything else to the miner. A stale manifest makes an
unmodified kit look miner-authored, so every kit change ships the next manifest
version. That manifest is staged outside the runtime-loaded set: trusting it is
a separate, reviewed activation change.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterable
from pathlib import Path

import pytest

from ditto_screener.l2_review import L2_STARTER_MANIFESTS, InProcessAnalyzerHarness
from ditto_screener.source_review import (
    OpenRouterSourceReviewAgent,
    _load_provenance_manifest,
)
from scripts import generate_starter_provenance as generator
from scripts.generate_starter_provenance import (
    ORIGIN,
    RUNTIME_MANIFESTS,
    STAGED_MANIFESTS,
    is_submittable,
    manifest_drift,
    manifest_number,
    manifests_in,
    newest_manifest,
    regenerate_command,
    starter_files,
)

SCREENER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SCREENER_ROOT.parents[1]
STARTER_KIT = REPOSITORY_ROOT / "miners" / "dittobench-starter-kit"
GENERATOR = SCREENER_ROOT / "scripts" / "generate_starter_provenance.py"
# The report-only source control fixture (docs/canonical-starter-source-control.md)
# packages the unchanged kit at v0.330.5, kit tree 9ffd5370e21b.
CONTROL_FIXTURE = (
    REPOSITORY_ROOT
    / "apps/platform/ditto/api_server/data/canonical-starter-v0.330.5.tgz"
)


def starter_drift_failure(kit: Path, manifest: Path) -> str | None:
    """Explain how ``manifest`` differs from ``kit``, or return None if exact."""
    expected = json.loads(manifest.read_text())["files"]
    drift = manifest_drift(expected, starter_files(kit))
    if not drift:
        return None
    details = "; ".join(f"{kind}: {', '.join(paths)}" for kind, paths in drift.items())
    return (
        f"{manifest.name} does not match {kit.name} ({details}). Stage the "
        "next manifest: activating a stale one would attribute these "
        "first-party kit files to the miner. Commit the kit change, then run "
        "from the repository root:\n"
        f"  {regenerate_command(manifest_number(manifest) + 1)}"
    )


def test_newest_manifest_covers_current_starter_kit() -> None:
    if not STARTER_KIT.is_dir():
        pytest.skip("the monorepo starter kit is not part of this checkout")
    newest = newest_manifest(RUNTIME_MANIFESTS, STAGED_MANIFESTS)
    failure = starter_drift_failure(STARTER_KIT, newest)
    if failure is not None:
        pytest.fail(failure, pytrace=False)


def test_newest_manifest_is_a_loadable_monorepo_manifest() -> None:
    newest = newest_manifest(RUNTIME_MANIFESTS, STAGED_MANIFESTS)
    manifest = _load_provenance_manifest(newest)
    files = manifest["files"]
    assert isinstance(files, dict) and files
    assert manifest["version"] == 1
    assert manifest["origin"] == ORIGIN
    assert len(str(manifest["revision"])) == 40
    # Nothing the starter ``submit`` packager excludes is ever trusted.
    assert [path for path in files if not is_submittable(path)] == []


def test_staged_and_runtime_manifests_share_one_version_sequence() -> None:
    runtime = manifests_in(RUNTIME_MANIFESTS)
    staged = manifests_in(STAGED_MANIFESTS)
    numbers = [manifest_number(path) for path in (*runtime, *staged)]
    # Activation moves a staged file without renaming it, so a version lives
    # in exactly one directory.
    assert len(numbers) == len(set(numbers))
    runtime_revisions = {
        _load_provenance_manifest(path)["revision"] for path in runtime
    }
    for path in staged:
        manifest = _load_provenance_manifest(path)
        assert manifest["version"] == 1
        assert manifest["origin"] == ORIGIN
        assert manifest["revision"] not in runtime_revisions


def test_v6_describes_exactly_the_canonical_starter_control_fixture() -> None:
    if not CONTROL_FIXTURE.is_file():
        pytest.skip("the canonical starter control fixture is not in this checkout")
    (manifest,) = [
        path
        for path in manifests_in(RUNTIME_MANIFESTS, STAGED_MANIFESTS)
        if manifest_number(path) == 6
    ]
    fixture: dict[str, str] = {}
    with tarfile.open(CONTROL_FIXTURE, mode="r:gz") as archive:
        for member in archive:
            extracted = archive.extractfile(member) if member.isfile() else None
            if extracted is not None:
                name = member.name.removeprefix("./")
                fixture[name] = hashlib.sha256(extracted.read()).hexdigest()

    # Evidence from reviewing that fixture covers v6 exactly: the same
    # files with the same bytes, and nothing else.
    assert json.loads(manifest.read_text())["files"] == fixture


def _copied_sources(dockerfile: Path, context: Path) -> list[str]:
    """Build-context sources of every ``COPY``/``ADD`` that reads the context."""
    sources: list[str] = []
    for line in dockerfile.read_text().replace("\\\n", " ").splitlines():
        if line.split(maxsplit=1)[:1] not in (["COPY"], ["ADD"]):
            continue
        arguments = shlex.split(line)[1:]
        if any(word.startswith("--from=") for word in arguments):
            continue
        paths = [word for word in arguments if not word.startswith("--")]
        sources.extend(str((context / path).resolve()) for path in paths[:-1])
    return sources


def _copied(path: Path, sources: list[str]) -> bool:
    resolved = str(path.resolve())
    return any(
        fnmatch.fnmatchcase(resolved, source) or resolved.startswith(source + "/")
        for source in sources
    )


def _same_manifests(loaded: Iterable[Path | str], expected: list[Path]) -> bool:
    """Compare manifest sets, never orders.

    Runtime loaders sort their glob by name, so v10 lands before v3, while
    ``manifests_in`` sorts by version number. Trust is the union of the set.
    """
    paths = [Path(path).resolve() for path in loaded]
    return len(paths) == len(set(paths)) and set(paths) == {
        path.resolve() for path in expected
    }


def test_manifest_sets_compare_across_name_and_version_order(tmp_path: Path) -> None:
    for number in (1, 3, 9, 10):
        (tmp_path / f"starter-kit-provenance-v{number}.json").write_text("{}")
    by_name = sorted(tmp_path.glob("starter-kit-provenance-*.json"))
    by_version = manifests_in(tmp_path)

    # The orders really differ once a version reaches two digits, and the
    # comparison still holds.
    assert by_name != by_version
    assert _same_manifests(by_name, by_version)
    assert not _same_manifests(by_name[:-1], by_version)
    assert not _same_manifests([*by_name, by_name[0]], by_version)


def test_no_runtime_loader_or_image_sees_a_staged_manifest(tmp_path: Path) -> None:
    runtime = manifests_in(RUNTIME_MANIFESTS)
    staged = manifests_in(STAGED_MANIFESTS)
    assert runtime
    # The staging directory lives outside the ``ditto_screener`` package.
    assert not STAGED_MANIFESTS.resolve().is_relative_to(
        (SCREENER_ROOT / "ditto_screener").resolve()
    )

    # L2's starter revisions, the L1 provenance block and the static
    # preflight's default trust set, and the in-process analyzer.
    assert _same_manifests(L2_STARTER_MANIFESTS, runtime)
    key = tmp_path / "key"
    key.write_text("unused")
    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="unused",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=1,
        max_steps=1,
    )
    assert _same_manifests(agent._provenance_manifest_files, runtime)
    assert (
        InProcessAnalyzerHarness()._manifests.resolve() == RUNTIME_MANIFESTS.resolve()
    )

    # The analyzer image bakes exactly the runtime set, and neither image
    # copies the staging directory.
    analyzer = _copied_sources(
        SCREENER_ROOT / "deploy" / "l2-analyzer.Dockerfile", SCREENER_ROOT
    )
    screener = _copied_sources(SCREENER_ROOT / "Dockerfile", REPOSITORY_ROOT)
    assert _same_manifests(
        (
            path
            for path in RUNTIME_MANIFESTS.iterdir()
            if path.suffix == ".json" and _copied(path, analyzer)
        ),
        runtime,
    )
    # A two-digit activated version is baked too; a staged one never is.
    assert _copied(RUNTIME_MANIFESTS / "starter-kit-provenance-v10.json", analyzer)
    for path in (*staged, STAGED_MANIFESTS / "starter-kit-provenance-v10.json"):
        assert not _copied(path, analyzer), path
        assert not _copied(path, screener), path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=provenance-test",
            "-c",
            "user.email=provenance-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _starter_repository(tmp_path: Path) -> Path:
    """A monorepo-shaped fixture: a kit directory beside unrelated history."""
    repository = tmp_path / "monorepo"
    kit = repository / "kit"
    for relative, content in {
        "src/lib.rs": "pub fn tracked() {}\n",
        "scripts/run.sh": "#!/bin/sh\nexec true\n",
        ".env.example": "OPENROUTER_API_KEY=\n",
        "config/.env.production": "OPENROUTER_API_KEY=secret\n",
        ".env": "OPENROUTER_API_KEY=secret\n",
        ".env.local": "OPENROUTER_API_KEY=secret\n",
        ".agents/skills/mine/SKILL.md": "skill\n",
        "target/release/tracked-artifact": "build output\n",
        "fixtures/local.db": "db\n",
        "fixtures/local.db-wal": "wal\n",
        "submission.tgz": "archive\n",
        "fixtures/sample.tar": "packaged by submit\n",
    }.items():
        path = kit / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (kit / "scripts/run.sh").chmod(0o755)
    (kit / ".claude").mkdir()
    (kit / ".claude/skills").symlink_to("../.agents/skills")
    (kit / "linked.rs").symlink_to("src/lib.rs")
    (repository / "README.md").write_text("outside the kit\n")
    _git(repository, "init", "-q")
    _git(repository, "add", "-f", ".")
    _git(repository, "commit", "-q", "-m", "kit")
    (kit / "untracked.rs").write_text("pub fn untracked() {}\n")
    (kit / "target/debug").mkdir(parents=True)
    (kit / "target/debug/local-build").write_bytes(os.urandom(64))
    return kit


def _generate(kit: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--starter-dir",
            str(kit),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_generator_trusts_only_tracked_submittable_regular_files(
    tmp_path: Path,
) -> None:
    kit = _starter_repository(tmp_path)

    files = starter_files(kit)

    # Symlinks, dev skills, secrets, local DBs, submission tarballs, build
    # output, and untracked files are never trusted. Starter ``submit``
    # excludes ``.env.*``, so the committed ``.env.example`` template is not
    # trusted either. A plain .tar, which ``submit`` does package, is.
    assert sorted(files) == [
        "fixtures/sample.tar",
        "scripts/run.sh",
        "src/lib.rs",
    ]
    assert files["src/lib.rs"] == hashlib.sha256(b"pub fn tracked() {}\n").hexdigest()


def test_drift_guard_fails_on_one_byte_with_the_regenerate_command(
    tmp_path: Path,
) -> None:
    kit = _starter_repository(tmp_path)
    manifest = tmp_path / "starter-kit-provenance-v6.json"
    assert _generate(kit, manifest).returncode == 0
    assert starter_drift_failure(kit, manifest) is None

    source = kit / "src/lib.rs"
    source.write_bytes(source.read_bytes().replace(b"tracked", b"trackee"))
    (kit / "scripts/run.sh").unlink()
    (kit / "src/extra.rs").write_text("pub fn extra() {}\n")
    _git(kit, "add", "src/extra.rs")

    failure = starter_drift_failure(kit, manifest)

    assert failure is not None
    assert "modified: src/lib.rs" in failure
    assert "added: src/extra.rs" in failure
    assert "removed: scripts/run.sh" in failure
    assert regenerate_command(7) in failure
    assert (
        "--output workers/screener/staged-starter-provenance/"
        "starter-kit-provenance-v7.json"
    ) in failure


def test_generator_is_reproducible_and_pins_the_last_kit_commit(
    tmp_path: Path,
) -> None:
    kit = _starter_repository(tmp_path)
    kit_commit = _git(kit, "rev-parse", "HEAD")
    (kit.parent / "README.md").write_text("an unrelated monorepo change\n")
    _git(kit.parent, "commit", "-q", "-am", "unrelated")
    assert _git(kit, "rev-parse", "HEAD") != kit_commit
    first, second = tmp_path / "first.json", tmp_path / "second.json"

    assert _generate(kit, first).returncode == 0
    assert _generate(kit, second).returncode == 0
    assert _generate(kit, first).returncode == 0

    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text())
    assert payload["revision"] == kit_commit
    assert payload["origin"] == ORIGIN
    assert payload["version"] == 1
    assert _load_provenance_manifest(first) == payload


def test_generator_refuses_uncommitted_kit_and_rewriting_a_manifest(
    tmp_path: Path,
) -> None:
    kit = _starter_repository(tmp_path)
    published = tmp_path / "starter-kit-provenance-v6.json"
    assert _generate(kit, published).returncode == 0
    before = published.read_bytes()

    (kit / "src/lib.rs").write_text("pub fn uncommitted() {}\n")
    dirty = _generate(kit, tmp_path / "next.json")
    assert dirty.returncode != 0
    assert "commit starter-kit changes" in dirty.stderr
    assert not (tmp_path / "next.json").exists()

    _git(kit, "commit", "-q", "-am", "kit change")
    rewrite = _generate(kit, published)
    assert rewrite.returncode == 1
    assert "refusing to rewrite" in rewrite.stderr
    assert published.read_bytes() == before
    assert _generate(kit, tmp_path / "starter-kit-provenance-v7.json").returncode == 0


def test_generator_never_writes_into_the_runtime_manifest_set(
    tmp_path: Path,
) -> None:
    kit = _starter_repository(tmp_path)
    before = set(RUNTIME_MANIFESTS.iterdir())
    active = newest_manifest(RUNTIME_MANIFESTS)
    target = RUNTIME_MANIFESTS / (
        f"starter-kit-provenance-v{manifest_number(active) + 100}.json"
    )

    nested = RUNTIME_MANIFESTS / "nested" / target.name

    try:
        # Trusting a manifest is an explicit activation change, never
        # generator output: not a runtime file, not anywhere else in the
        # shipped package, and not an active version number.
        into_runtime = _generate(kit, target)
        into_package = _generate(kit, nested)
        reused = _generate(kit, tmp_path / active.name)
    finally:
        for created in set(RUNTIME_MANIFESTS.iterdir()) - before:
            if created.is_dir():
                shutil.rmtree(created)
            else:
                created.unlink()

    for refused in (into_runtime, into_package):
        assert refused.returncode == 1
        assert "runtime-loaded trust set" in refused.stderr
    assert not target.exists()
    assert not nested.parent.exists()
    assert reused.returncode == 1
    assert "already an active manifest" in reused.stderr
    assert not (tmp_path / active.name).exists()


def test_generator_recreates_only_the_emptied_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    kit = _starter_repository(tmp_path)
    # Activation moves the last staged manifest away, and Git does not keep
    # the emptied directory, so a fresh checkout has no staging directory.
    staged = tmp_path / "staged-starter-provenance"
    monkeypatch.setattr(generator, "STAGED_MANIFESTS", staged)
    output = staged / "starter-kit-provenance-v999.json"

    assert generator.main(["--starter-dir", str(kit), "--output", str(output)]) == 0
    assert _load_provenance_manifest(output)["files"] == starter_files(kit)

    # Any other missing directory is refused plainly and never created.
    elsewhere = tmp_path / "missing" / "starter-kit-provenance-v999.json"
    assert generator.main(["--starter-dir", str(kit), "--output", str(elsewhere)]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not elsewhere.parent.exists()


def test_newest_manifest_orders_versions_numerically(tmp_path: Path) -> None:
    runtime, staged = tmp_path / "runtime", tmp_path / "staged"
    runtime.mkdir()
    staged.mkdir()
    for number in (1, 9, 2):
        (runtime / f"starter-kit-provenance-v{number}.json").write_text("{}")
    (staged / "starter-kit-provenance-v10.json").write_text("{}")
    (runtime / "bench-categories-v1.json").write_text("{}")

    assert newest_manifest(runtime).name == "starter-kit-provenance-v9.json"
    assert newest_manifest(runtime, staged) == (
        staged / "starter-kit-provenance-v10.json"
    )
