"""Deploy-time metadata check for the hosted-v2 control signer seed placement.

The seed below is the public synthetic test key, never production configuration.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import bittensor
import pytest

from ditto.api_server import coding_hosted_signer_preflight as preflight
from ditto.api_server.coding_hosted_signer import load_hosted_control_signer
from ditto.api_server.coding_hosted_signer_config import HostedControlSignerConfig
from ditto.api_server.errors import ApiServerConfigError

PLATFORM_ROOT = Path(__file__).parents[3]
SEED = bytes.fromhex("11" * 32)  # Public synthetic test key, never production config.
KEY = bittensor.Keypair.create_from_seed(SEED.hex())
MODULE = "ditto.api_server.coding_hosted_signer_preflight"
ENV_KEYS = (
    "DITTO_CODING_HOSTED_CONTROL_ENABLED",
    "DITTO_CODING_HOSTED_SIGNER_SEED_FILE",
    "DITTO_CODING_HOSTED_SIGNER_HOTKEY",
)
# Exits the child before it can read anything if the seed path is ever opened.
AUDITED_RUN = """
import os, runpy, sys
MODULE = "ditto.api_server.coding_hosted_signer_preflight"
seed = os.environ["DITTO_CODING_HOSTED_SIGNER_SEED_FILE"]
def audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.basename(os.fsdecode(args[0])) == os.path.basename(seed):
            os._exit(97)
sys.addaudithook(audit)
args = sys.argv[1:]
if args[:1] == ["--read-control"]:
    args = args[1:]
    open(seed, "rb").close()
sys.argv = ["preflight", *args]
runpy.run_module(MODULE, run_name="__main__", alter_sys=True)
"""


def _placement(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    directory = tmp_path / "coding-hosted-signer"
    directory.mkdir(mode=0o700)
    seed = directory / "seed"
    seed.write_bytes(SEED)
    seed.chmod(0o600)
    return seed


def _unsafe(seed: Path, fault: str) -> Path:
    directory = seed.parent
    if fault == "missing":
        seed.unlink()
    elif fault == "group-readable":
        seed.chmod(0o640)
    elif fault == "symlink":
        real = directory / "real"
        seed.rename(real)
        seed.symlink_to(real.name)
    elif fault == "hardlink":
        os.link(seed, directory.parent / "second-link")
    elif fault == "hex-text":
        seed.write_text("11" * 32)
    elif fault == "truncated":
        seed.write_bytes(SEED[:16])
    elif fault == "fifo":
        seed.unlink()
        os.mkfifo(seed, 0o600)
    elif fault == "directory-mode":
        directory.chmod(0o750)
    elif fault == "symlinked-directory":
        real = directory.parent / "real-signer"
        directory.rename(real)
        directory.symlink_to(real.name)
    elif fault == "writable-ancestor":
        directory.parent.chmod(0o770)
    elif fault == "relative":
        return seed.relative_to(seed.parents[1])
    elif fault == "dot-dot":
        return directory / ".." / directory.name / "seed"
    else:
        raise AssertionError(fault)
    return seed


FAULTS = (
    "missing",
    "group-readable",
    "symlink",
    "hardlink",
    "hex-text",
    "truncated",
    "fifo",
    "directory-mode",
    "symlinked-directory",
    "writable-ancestor",
    "relative",
    "dot-dot",
)


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _run(seed: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in set(ENV_KEYS)}
    env.update(
        DITTO_CODING_HOSTED_CONTROL_ENABLED="true",
        DITTO_CODING_HOSTED_SIGNER_SEED_FILE=str(seed),
        DITTO_CODING_HOSTED_SIGNER_HOTKEY=KEY.ss58_address,
    )
    return subprocess.run(
        [sys.executable, "-c", AUDITED_RUN, *args],
        cwd=PLATFORM_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_disabled_settings_never_inspect_the_seed_path(monkeypatch, capsys):
    _env(
        monkeypatch,
        DITTO_CODING_HOSTED_CONTROL_ENABLED="false",
        DITTO_CODING_HOSTED_SIGNER_SEED_FILE="/nonexistent/staged/seed",
    )
    monkeypatch.setattr(
        preflight,
        "private_file_metadata",
        lambda *_args: pytest.fail("disabled preflight inspected the seed path"),
    )

    assert preflight.main(["--check-metadata"]) == 0
    assert capsys.readouterr().out == (
        "hosted-v2 control signer disabled; seed path not inspected\n"
    )


def test_enabled_safe_placement_passes_and_the_loader_agrees(tmp_path):
    seed = _placement(tmp_path)
    config = HostedControlSignerConfig(True, seed, KEY.ss58_address)

    assert preflight.check_hosted_signer_seed_metadata(config) is True
    signer = load_hosted_control_signer(config, process_role="platform")
    assert signer is not None
    signer.close()


@pytest.mark.parametrize("fault", FAULTS)
def test_every_unsafe_placement_fails_metadata_and_the_loader(tmp_path, fault):
    seed = _unsafe(_placement(tmp_path), fault)
    config = HostedControlSignerConfig(True, seed, KEY.ss58_address)

    with pytest.raises(ApiServerConfigError) as metadata:
        preflight.check_hosted_signer_seed_metadata(config)
    assert str(tmp_path) not in str(metadata.value)
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(config, process_role="platform")


def test_content_checks_remain_startup_only(tmp_path):
    """Metadata cannot see content: a mismatched or zero seed passes it."""
    seed = _placement(tmp_path)
    other = bittensor.Keypair.create_from_seed("22" * 32).ss58_address
    mismatched = HostedControlSignerConfig(True, seed, other)
    assert preflight.check_hosted_signer_seed_metadata(mismatched) is True
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(mismatched, process_role="platform")

    seed.write_bytes(bytes(32))
    zero = HostedControlSignerConfig(True, seed, KEY.ss58_address)
    assert preflight.check_hosted_signer_seed_metadata(zero) is True
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(zero, process_role="platform")


@pytest.mark.parametrize(
    ("enabled", "hotkey"),
    [
        ("yes", KEY.ss58_address),
        ("", KEY.ss58_address),
        ("true", ""),
        ("true", KEY.ss58_address + "\n0"),
    ],
)
def test_invalid_settings_fail_before_any_path_check(
    tmp_path, monkeypatch, capsys, enabled, hotkey
):
    seed = _placement(tmp_path)
    _env(
        monkeypatch,
        DITTO_CODING_HOSTED_CONTROL_ENABLED=enabled,
        DITTO_CODING_HOSTED_SIGNER_SEED_FILE=str(seed),
        DITTO_CODING_HOSTED_SIGNER_HOTKEY=hotkey,
    )
    monkeypatch.setattr(
        preflight,
        "private_file_metadata",
        lambda *_args: pytest.fail("invalid settings reached the path checks"),
    )

    assert preflight.main(["--check-metadata"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("hosted-v2 control signer preflight failed: ")
    assert str(seed) not in captured.err


def test_the_only_mode_must_be_named():
    with pytest.raises(SystemExit) as exit_info:
        preflight.main([])
    assert exit_info.value.code == 2


def test_command_line_never_opens_the_seed(tmp_path):
    seed = _placement(tmp_path)

    # Control: the audit hook does catch a real open of the seed.
    assert _run(seed, "--read-control", "--check-metadata").returncode == 97

    accepted = _run(seed, "--check-metadata")
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout == (
        "hosted-v2 control signer seed metadata ok (seed not read)\n"
    )

    seed.chmod(0o644)
    refused = _run(seed, "--check-metadata")
    assert refused.returncode == 1, refused.stderr
    assert refused.stderr == (
        "hosted-v2 control signer preflight failed: "
        "hosted Coding signer seed placement is missing or unsafe\n"
    )


def test_a_process_that_does_not_own_the_seed_is_refused(tmp_path, monkeypatch):
    """deploy (or a relay) cannot use a ditto-api-owned placement.

    The loader and the metadata check compare the seed's and its directory's
    owner with the effective UID of the process, so a placement that belongs to
    ditto-api fails closed for any other user even if its permissions were
    widened. On a host every ancestor is root-owned; here the ancestors are
    reported as root-owned and the effective UID is simulated, so only the
    placement's own owner checks can refuse. Nothing is opened.
    """
    seed = _placement(tmp_path)
    config = HostedControlSignerConfig(True, seed, KEY.ss58_address)
    owner = os.geteuid()
    placement = seed.parent
    real_lstat = Path.lstat
    real_open = os.open

    def root_owned_ancestors(self, *args, **kwargs):
        info = real_lstat(self, *args, **kwargs)
        if self not in (placement, seed) and placement.is_relative_to(self):
            fields = list(info[:10])
            fields[4] = 0
            return os.stat_result(fields)
        return info

    def guarded_open(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(seed):
            pytest.fail("a non-owner opened the seed")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", root_owned_ancestors)
    monkeypatch.setattr(os, "open", guarded_open)

    # Control: the owning process accepts the same placement.
    assert preflight.check_hosted_signer_seed_metadata(config) is True

    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(ApiServerConfigError):
        preflight.check_hosted_signer_seed_metadata(config)
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(config, process_role="platform")


def test_update_script_never_checks_or_opens_the_seed_as_the_deploy_user():
    """The metadata preflight runs as ditto-api, never from update.sh."""
    updater = (PLATFORM_ROOT / "scripts" / "update.sh").read_text()

    assert MODULE not in updater
    assert "DITTO_CODING_HOSTED_SIGNER_SEED_FILE" not in updater
    refusal = updater.index("ditto-api would run under pm2")
    assert updater.index("\n. ./.env.deploy\n") < refusal
    assert refusal < updater.index('deploy_stage="infra"')
    assert refusal < updater.index("uv run alembic upgrade head")
    assert refusal < updater.index(
        "pm2 jlist 2>/dev/null | node scripts/pm2_deploy_plan"
    )
    assert 'case "${DITTO_CODING_HOSTED_CONTROL_ENABLED-false}" in' in updater
