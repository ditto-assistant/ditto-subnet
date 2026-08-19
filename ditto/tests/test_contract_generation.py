"""Guard: contract generators cannot emit from a stale protocol install.

``ditto-screening-protocol`` is a path dependency installed as a *built* copy,
so a plain ``uv sync`` reports "Audited" and keeps serving the previously-built
copy after its source changes. Generating against that copy does not raise --
it writes an artifact describing the older contract, which lands as a clean,
plausible diff. A v11 rollout regenerated ``bench_version: 9 | 10`` back down to
``9`` exactly this way.

The deployment scripts already reinstall for this reason
(``workers/screener/tests/test_deployment.py`` pins that); these pin the same
protection on the paths that generate committed artifacts.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
BACKROOM_GENERATOR = ROOT / "apps/backroom/scripts/platform-contract/generate.sh"
GOLDEN_GENERATOR = ROOT / "scripts/gen_validator_contract.py"
FRESHNESS_MODULE = ROOT / "scripts/screening_protocol_freshness.py"
REINSTALL = "uv sync --reinstall-package ditto-screening-protocol"

# The generator lived in two places until the leaner Platform-side copy was
# deleted. That copy wrote the mirror goldens without ``assert_fresh()`` and
# without ``miner_contract.json``, so reaching for the nearer script silently
# opted out of both protections this module exists to pin.
RETIRED_GENERATOR = ROOT / "apps/platform/scripts/gen_validator_contract.py"


def test_backroom_client_generator_reinstalls_before_dumping_the_schema() -> None:
    script = BACKROOM_GENERATOR.read_text()

    assert REINSTALL in script
    # Order is the whole point: reinstalling after the dump protects nothing.
    assert script.index(REINSTALL) < script.index("create_api_server")


def test_golden_generator_refuses_to_run_against_a_stale_install() -> None:
    script = GOLDEN_GENERATOR.read_text()

    assert "assert_fresh()" in script
    # Must gate the writes, not merely be imported.
    assert script.index("assert_fresh()") < script.index(".write_text(")


def test_exactly_one_generator_writes_the_contract_goldens() -> None:
    """A second generator is a way to bypass every guard above it."""
    assert not RETIRED_GENERATOR.exists(), (
        f"{RETIRED_GENERATOR.relative_to(ROOT)} is back; the goldens have one "
        "generator so the staleness guard and the miner contract cannot be "
        "skipped by running the nearer script"
    )
    generators = {
        path.relative_to(ROOT)
        for path in ROOT.rglob("gen_validator_contract.py")
        if ".venv" not in path.parts and "node_modules" not in path.parts
    }
    assert generators == {GOLDEN_GENERATOR.relative_to(ROOT)}


def _import_generator() -> ModuleType:
    """Import the generator by path; ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "gen_validator_contract_under_test", GOLDEN_GENERATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_generator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, authoritative: bool
) -> tuple[Path, Path]:
    generator = _import_generator()
    monkeypatch.setattr(generator, "assert_fresh", lambda: None)
    monkeypatch.setattr(
        generator, "_models_are_the_subnet_copy", lambda: not authoritative
    )
    out = tmp_path / "subnet"
    mirror = tmp_path / "mirror"
    out.mkdir()
    mirror.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_validator_contract.py",
            "--out",
            str(out / "validator_contract.json"),
            "--confirmation-out",
            str(out / "confirmation_contract.json"),
            "--miner-out",
            str(out / "miner_contract.json"),
            "--mirror-dir",
            str(mirror),
        ],
    )
    generator.main()
    return out, mirror


def test_golden_generator_writes_both_monorepo_contract_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The byte-identical mirror is produced, not merely asserted afterwards.

    ``test_monorepo_validator_goldens_match_platform_byte_for_byte`` requires
    the two committed copies to match. Deleting the Platform-side generator
    removed the only other writer of the mirror, so this one writes both from a
    single computation -- and by default, since an opt-in mirror is one nobody
    remembers to ask for.
    """
    generator = _import_generator()
    assert generator._PLATFORM_CONTRACT_DIR == (
        ROOT / "apps/platform/ditto/tests/contract"
    )

    out, mirror = _run_generator(monkeypatch, tmp_path, authoritative=True)

    for filename in ("validator_contract.json", "confirmation_contract.json"):
        assert (mirror / filename).read_bytes() == (out / filename).read_bytes()
    # The miner golden has no committed Platform-side copy to keep in step.
    assert (out / "miner_contract.json").exists()
    assert not (mirror / "miner_contract.json").exists()


def test_golden_generator_refuses_to_mirror_the_subnets_own_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regenerating from the client copy must not overwrite the golden.

    The Platform-side golden exists to catch this repo's hand-maintained models
    drifting from it. Writing the mirror from those same models would make the
    guard agree with whatever the client currently says -- green on precisely
    the change it exists to fail.
    """
    out, mirror = _run_generator(monkeypatch, tmp_path, authoritative=False)

    assert (out / "validator_contract.json").exists()
    assert list(mirror.iterdir()) == []


def test_freshness_check_stays_quiet_when_it_cannot_prove_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It may only speak when it can prove divergence.

    Deliberately hermetic rather than asserting on this machine's venv: a check
    that reads the ambient environment would fail whenever a developer edits the
    protocol source before syncing, which is noise dressed up as a regression.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import screening_protocol_freshness as freshness

    # No monorepo source tree to compare against -> nothing provable.
    monkeypatch.setattr(freshness, "SOURCE", tmp_path / "absent")
    assert freshness.stale_install_message() is None

    # An editable install resolves to the source itself -> always fresh.
    import ditto_screening_protocol

    installed = Path(ditto_screening_protocol.__file__).resolve().parent
    monkeypatch.setattr(freshness, "SOURCE", installed)
    assert freshness.stale_install_message() is None

    # A package that is not importable at all -> nothing to say.
    monkeypatch.setattr(freshness, "PACKAGE", "ditto_screening_protocol_absent")
    monkeypatch.setattr(freshness, "SOURCE", tmp_path)
    assert freshness.stale_install_message() is None


def test_freshness_module_detects_a_divergent_tree(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from screening_protocol_freshness import _tree_digest

    source = tmp_path / "source"
    installed = tmp_path / "installed"
    for directory in (source, installed):
        (directory / "sub").mkdir(parents=True)
        (directory / "__init__.py").write_text("VERSION = 1\n")
        (directory / "sub" / "models.py").write_text("bench_version = [9, 10]\n")

    assert _tree_digest(source) == _tree_digest(installed)

    # The exact shape of the real regression: the installed copy lags a version.
    (installed / "sub" / "models.py").write_text("bench_version = [9]\n")
    assert _tree_digest(source) != _tree_digest(installed)

    # Content, not just names: a same-length edit must still register.
    (installed / "sub" / "models.py").write_text("bench_version = [9, 11]\n")
    assert _tree_digest(source) != _tree_digest(installed)
