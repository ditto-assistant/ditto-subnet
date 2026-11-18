"""Exercise the real cpm-scorer updater with a fake registry, cosign, Docker
and service manager. Mirrors test_generator_scorer_auto_update.py."""

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/cpm-scorer-auto-update.sh"
REPOSITORY = "ghcr.io/ditto-assistant/ditto-subnet-stack"
REVISION = "a" * 40
NEW = f"{REPOSITORY}@sha256:" + "c" * 64
OLD = f"{REPOSITORY}@sha256:" + "b" * 64
NEW_ID = "sha256:" + "1" * 64
OLD_ID = "sha256:" + "2" * 64

# One python shim stands in for every external command. State lives in files
# under the release root so each invocation sees the previous one's effects:
# `running` holds the image ID of the live container (empty: none).
SHIM = f"""#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["CPM_SCORER_RELEASE_ROOT"])
fault = os.environ["TEST_FAULT"]
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(name + " " + repr(args) + "\\n")
running_file = root / "running"
running = running_file.read_text() if running_file.exists() else ""
ids = {{{NEW!r}: {NEW_ID!r}, {OLD!r}: {OLD_ID!r}}}

def current_id():
    text = (root / "current.env").read_text()
    return ids[text.split("=", 1)[1].strip()]

def start():
    # Restart=always brings up whatever current.env names; the faulty
    # candidate never stays up.
    image = current_id()
    good = not (fault == "startup" and image == {NEW_ID!r})
    running_file.write_text(image if good else "")

if name in ("cosign",):
    sys.exit(1 if fault == "signature" else 0)
if name in ("flock", "sleep"):
    sys.exit(0)
if name == "docker":
    if args[0] == "pull":
        sys.exit(0)
    if args[:2] == ["image", "inspect"]:
        fmt = args[args.index("--format") + 1]
        if "RepoDigests" in fmt:
            print({NEW!r})
        elif ".Id" in fmt:
            print(ids[args[-1]])
        elif "update-protocol" in fmt:
            print("1")
        else:
            print(os.environ["TEST_LABEL_REVISION"])
        sys.exit(0)
    if args[0] == "inspect":
        if not running:
            sys.exit(1)
        fmt = args[args.index("--format") + 1]
        print(running if ".Image" in fmt else "container-" + running[-8:])
        sys.exit(0)
    if args[0] == "run":
        if "--entrypoint" in args:
            print("#!/usr/bin/env bash\\nexit 0")
        else:
            revision = os.environ["TEST_IDENTITY_REVISION"]
            print(json.dumps({{"scorer_version": "0.1.0+" + revision,
                               "revision": revision, "cpm_score_version": 1}}))
        sys.exit(0)
    if args[0] == "kill":
        if not running:
            sys.exit(1)
        (root / "drained").touch()
        start()
        sys.exit(0)
    if args[0] in ("tag", "image"):
        sys.exit(0)
    sys.exit(f"unexpected docker call {{args}}")
if name == "systemctl":
    if args[0] == "is-active":
        sys.exit(0 if running else 3)
    if args[0] == "start":
        start()
    sys.exit(0)
sys.exit(f"unexpected command {{name}}")
"""


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def harness(tmp_path: Path):
    tools = tmp_path / "bin"
    tools.mkdir()
    shim = tools / "shim"
    shim.write_text(SHIM)
    shim.chmod(0o755)
    for name in ("docker", "cosign", "flock", "systemctl", "sleep"):
        (tools / name).symlink_to(shim)
    root = tmp_path / "release"
    root.mkdir()
    (root / "current.env").write_text(f"CPM_SCORER_IMAGE={OLD}\n")
    (root / "last-good.env").write_text(f"CPM_SCORER_IMAGE={OLD}\n")
    (root / "running").write_text(OLD_ID)
    env = os.environ | {
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "CPM_SCORER_RELEASE_ROOT": str(root),
        "CPM_SCORER_UPDATER_PATH": str(tmp_path / "installed-updater"),
        "CPM_SCORER_ADOPTION_POLLS": "5",
        "CPM_SCORER_STARTUP_SECONDS": "0",
        "TEST_FAULT": "none",
        "TEST_LOG": str(tmp_path / "calls"),
        "TEST_LABEL_REVISION": REVISION,
        "TEST_IDENTITY_REVISION": REVISION,
    }
    return root, env, tmp_path


def _image(root: Path, name: str) -> str:
    return (root / name).read_text().split("=", 1)[1].strip()


def test_signed_release_drains_activates_and_self_refreshes(harness) -> None:
    root, env, tmp_path = harness
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert _image(root, "current.env") == NEW
    assert _image(root, "last-good.env") == NEW
    assert (root / "active-descriptor").read_text().strip() == NEW
    assert (root / "drained").exists()
    assert (tmp_path / "installed-updater").exists()
    calls = (tmp_path / "calls").read_text()
    assert "--signal=SIGUSR1" in calls
    # The identity check runs offline, from the local verified image only.
    assert "'run', '--rm', '--network', 'none', '--pull', 'never'" in calls
    assert "SIGTERM" not in calls and "SIGKILL" not in calls
    # Old and new images stay local for a --pull never rollback.
    assert "ditto-cpm-scorer-local:previous" in calls

    (tmp_path / "calls").write_text("")
    again = _run(env)
    assert again.returncode == 0, again.stderr
    assert "already running" in again.stdout
    assert "kill" not in (tmp_path / "calls").read_text()


@pytest.mark.parametrize("fault", ["signature", "label", "identity", "expected"])
def test_unverified_release_never_touches_the_service(harness, fault) -> None:
    root, env, tmp_path = harness
    env["TEST_FAULT"] = fault
    if fault == "label":
        env["TEST_LABEL_REVISION"] = "not-a-revision"
    if fault == "identity":
        env["TEST_IDENTITY_REVISION"] = "d" * 40
    if fault == "expected":
        env["CPM_SCORER_EXPECTED_REVISION"] = "e" * 40
    result = _run(env)
    assert result.returncode != 0
    assert _image(root, "current.env") == OLD
    calls = (tmp_path / "calls").read_text()
    assert "systemctl" not in calls and "kill" not in calls
    if fault == "signature":
        assert "'run'" not in calls  # nothing from an unsigned image executes


def test_bootstrap_pins_an_immutable_tag_and_revision(harness) -> None:
    root, env, tmp_path = harness
    env["CPM_SCORER_REF"] = f"{REPOSITORY}:cpm-scorer-sha-{REVISION}"
    env["CPM_SCORER_EXPECTED_REVISION"] = REVISION
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert f"cpm-scorer-sha-{REVISION}" in (tmp_path / "calls").read_text()
    assert _image(root, "current.env") == NEW

    for ref in (f"{REPOSITORY}:latest", "docker.io/attacker/cpm:cpm-scorer-stable-1"):
        env["CPM_SCORER_REF"] = ref
        refused = _run(env)
        assert refused.returncode != 0
        assert "refusing reference" in refused.stderr


def test_first_activation_starts_an_inactive_service(harness) -> None:
    root, env, _tmp_path = harness
    (root / "running").write_text("")
    (root / "last-good.env").unlink()
    (root / "current.env").unlink()
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert _image(root, "current.env") == NEW
    assert (root / "running").read_text() == NEW_ID


def test_failed_startup_rolls_back_and_suppresses_the_candidate(harness) -> None:
    root, env, tmp_path = harness
    env["TEST_FAULT"] = "startup"
    result = _run(env)
    assert result.returncode == 1, result.stderr
    assert "rollback requested" in result.stderr
    assert _image(root, "current.env") == OLD
    assert _image(root, "last-good.env") == OLD
    assert (root / "running").read_text() == OLD_ID
    assert (root / "failed-candidate").read_text().strip() == NEW
    assert not (tmp_path / "installed-updater").exists()

    # The same candidate is suppressed for a day, then retried.
    (tmp_path / "calls").write_text("")
    retry = _run(env)
    assert retry.returncode == 0, retry.stderr
    assert "failed candidate suppressed" in retry.stdout
    assert "systemctl" not in (tmp_path / "calls").read_text()

    expired = time.time() - 25 * 3600
    os.utime(root / "failed-candidate", (expired, expired))
    (tmp_path / "calls").write_text("")
    expired_retry = _run(env)
    assert expired_retry.returncode == 1
    assert "--signal=SIGUSR1" in (tmp_path / "calls").read_text()
    assert _image(root, "current.env") == OLD


def test_identity_payload_is_json_the_updater_parses() -> None:
    # The updater's identity check reads cpm-scorer version's JSON keys.
    text = SCRIPT.read_text()
    for key in ("revision", "cpm_score_version", "scorer_version"):
        assert f"identity['{key}']" in text
