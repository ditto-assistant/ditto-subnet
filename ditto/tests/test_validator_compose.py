"""Integration tests for the Compose wrapper's verified dittobench-api pin."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
COMPOSE_WRAPPER = ROOT / "scripts/validator-compose.sh"


FAKE_WRAPPER_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
capture = os.environ.get("FAKE_COMPOSE_CAPTURE")
if capture:
    try:
        with open(capture) as handle:
            recorded = json.load(handle)
    except (OSError, ValueError):
        recorded = {"calls": []}
    entry = {
        "args": args,
        "image": os.environ.get("DITTO_SUBNET_IMAGE"),
        "wallets_dir": os.environ.get("DITTO_BITTENSOR_WALLETS_DIR"),
    }
    recorded.setdefault("docker_calls", []).append(entry)
    if args[:1] == ["compose"] and args != ["compose", "version", "--short"]:
        recorded["calls"].append(entry)
    recorded.update(entry)
    with open(capture, "w") as handle:
        json.dump(recorded, handle)

if args == ["compose", "version", "--short"]:
    print("2.40.0")
elif args == ["info"] or args == ["buildx", "version"]:
    pass
elif args[:2] == ["kill", "--signal=USR1"]:
    with open(os.environ["FAKE_VALIDATOR_STATE"], "w") as handle:
        handle.write('{"platform_accepted":true,"state":"drained"}')
elif args[:2] == ["kill", "--signal=USR2"]:
    with open(os.environ["FAKE_VALIDATOR_STATE"], "w") as handle:
        handle.write('{"platform_accepted":true,"state":"ready"}')
elif args[:1] == ["exec"]:
    with open(os.environ["FAKE_VALIDATOR_STATE"]) as handle:
        sys.stdout.write(handle.read())
elif args[:2] == ["inspect", "--format"]:
    if ".Config.Env" in args[2]:
        print(
            "VALIDATOR_BOOTSTRAP_TOKEN="
            + os.environ.get("FAKE_VALIDATOR_BOOTSTRAP_TOKEN", "manual")
        )
    elif ".State.Running" in args[2]:
        print("true")
    elif ".State.Health" in args[2]:
        print(os.environ.get("FAKE_SCORER_HEALTH", "healthy"))
    else:
        raise SystemExit("unhandled docker inspect format: " + repr(args))
elif args[:1] == ["compose"]:
    if "build" in args and os.environ.get("FAKE_COMPOSE_BUILD_FAIL") == "true":
        raise SystemExit(1)
    if "ps" in args:
        if args[-1] == "ditto-subnet":
            sys.stdout.write(os.environ.get("FAKE_VALIDATOR_PS_OUTPUT", ""))
        else:
            sys.stdout.write(os.environ.get("FAKE_COMPOSE_PS_OUTPUT", ""))
    elif (
        "up" in args
        and "--no-deps" not in args
        and os.environ.get("FAKE_COMPOSE_UP_FAIL") == "true"
    ):
        if os.environ.get("FAKE_COMPOSE_UP_LEAVES_DRAINED") == "true":
            with open(os.environ["FAKE_VALIDATOR_STATE"], "w") as handle:
                handle.write('{"platform_accepted":true,"state":"drained"}')
        raise SystemExit(1)
else:
    raise SystemExit("unhandled wrapper docker command: " + repr(args))
"""


FAKE_WRAPPER_GIT = r"""#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if "remote" in args and "get-url" in args:
    print("https://github.com/ditto-assistant/dittobench-api.git")
elif "rev-parse" in args:
    if args[-1] == "FETCH_HEAD":
        print(
            os.environ.get(
                "FAKE_DITTOBENCH_REF_CHECKSUM",
                os.environ["FAKE_DITTOBENCH_CHECKSUM"],
            )
        )
    else:
        print(os.environ["FAKE_DITTOBENCH_CHECKSUM"])
elif "merge-base" in args:
    if os.environ.get("FAKE_DITTOBENCH_IS_MAIN", "true") != "true":
        raise SystemExit(1)
elif "ls-tree" in args:
    records = [b"100644 Dockerfile"]
    executable = os.environ.get("FAKE_EXECUTABLE_PATH")
    if executable:
        records.append(b"100755 " + executable.encode())
    sys.stdout.buffer.write(b"\0".join(records) + b"\0")
elif "hash-object" in args:
    sys.stdin.buffer.read()
    print("a" * 40)
elif "fetch" in args:
    if os.environ.get("FAKE_DITTOBENCH_FETCH_FAIL") == "true":
        raise SystemExit(1)
elif (
    "status" in args
    or "cat-file" in args
    or "checkout" in args
):
    pass
else:
    raise SystemExit("unhandled wrapper git command: " + repr(args))
"""


def _wrapper_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "wrapper-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    git = fake_bin / "git"
    docker.write_text(FAKE_WRAPPER_DOCKER)
    git.write_text(FAKE_WRAPPER_GIT)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    git.chmod(git.stat().st_mode | stat.S_IXUSR)
    checksum_match = re.search(
        r"checksum=([0-9a-f]{40})", (ROOT / "docker-compose.yml").read_text()
    )
    assert checksum_match is not None
    checksum = checksum_match.group(1)
    cache = tmp_path / "cache"
    checkout = cache / "dittobench-api" / checksum
    (checkout / ".git").mkdir(parents=True)
    (checkout / "Dockerfile").write_text("FROM scratch\n")
    capture = tmp_path / "compose-capture.json"
    state_dir = tmp_path / "wrapper-state"
    state_dir.mkdir()
    validator_state = tmp_path / "validator-state.json"
    validator_state.write_text('{"platform_accepted":true,"state":"working"}')
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        # Exercise main-ancestry wrapper behavior independently of the
        # deployment pin. A dependent draft may temporarily point Compose at
        # an exact feature-branch commit; the dedicated unmerged-smoke test
        # overrides this context explicitly.
        "DITTOBENCH_BUILD_CONTEXT": (
            "https://github.com/ditto-assistant/dittobench-api.git?"
            f"ref=refs/heads/main&checksum={checksum}"
        ),
        "DITTO_SUBNET_BUILD_CACHE": str(cache),
        "DITTO_VALIDATOR_UPDATE_STATE_DIR": str(state_dir),
        "FAKE_COMPOSE_CAPTURE": str(capture),
        "FAKE_DITTOBENCH_CHECKSUM": checksum,
        "FAKE_VALIDATOR_STATE": str(validator_state),
        "HOME": str(tmp_path / "operator-home"),
    }
    return env, capture, state_dir


def _compose_calls(capture: Path) -> list[list[str]]:
    return [call["args"] for call in json.loads(capture.read_text())["calls"]]


def _docker_calls(capture: Path) -> list[list[str]]:
    return [call["args"] for call in json.loads(capture.read_text())["docker_calls"]]


def _built_revision_marker(env: dict[str, str]) -> Path:
    return Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "dittobench-built-revision"


def test_compose_config_forwards_wallets_dir(tmp_path: Path) -> None:
    env, capture, _ = _wrapper_env(tmp_path)

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "config", "--quiet"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = json.loads(capture.read_text())
    assert captured["wallets_dir"] == str(tmp_path / "operator-home/.bittensor/wallets")


def test_compose_wrapper_repairs_git_tracked_executable_modes(
    tmp_path: Path,
) -> None:
    env, _, _ = _wrapper_env(tmp_path)
    env.pop("DITTOBENCH_ALLOW_UNMERGED_SMOKE", None)
    env.pop("SUBTENSOR_NETWORK", None)
    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    executable = (
        Path(env["DITTO_SUBNET_BUILD_CACHE"]) / "dittobench-api" / checksum / "run.sh"
    )
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o644)
    env["FAKE_EXECUTABLE_PATH"] = "run.sh"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert executable.stat().st_mode & stat.S_IXUSR


def test_compose_wrapper_rejects_checksum_that_is_not_on_main(tmp_path: Path) -> None:
    env, _, _ = _wrapper_env(tmp_path)
    env["FAKE_DITTOBENCH_IS_MAIN"] = "false"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "is not in refs/heads/main history" in result.stderr


def test_compose_wrapper_caches_successful_main_ancestry(tmp_path: Path) -> None:
    env, _, _ = _wrapper_env(tmp_path)

    first = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert first.returncode == 0, first.stderr

    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    marker = (
        Path(env["DITTO_SUBNET_BUILD_CACHE"])
        / "dittobench-api"
        / f"{checksum}.ref-verified"
    )
    assert marker.read_text() == f"refs/heads/main {checksum}\n"

    # A later invocation trusts the immutable cached evidence and does not
    # repeat a fetch or ancestry command (the fake would fail either now).
    env["FAKE_DITTOBENCH_IS_MAIN"] = "false"
    env["FAKE_DITTOBENCH_FETCH_FAIL"] = "true"
    second = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert second.returncode == 0, second.stderr


def test_compose_wrapper_allows_exact_unmerged_ref_only_for_local_smoke(
    tmp_path: Path,
) -> None:
    env, _, _ = _wrapper_env(tmp_path)
    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    env["DITTOBENCH_BUILD_CONTEXT"] = (
        "https://github.com/ditto-assistant/dittobench-api.git?"
        "ref=refs/heads/test/unmerged-smoke&"
        f"checksum={checksum}"
    )

    denied = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert denied.returncode == 1
    assert "require local network smoke opt-in" in denied.stderr

    for partial_opt_in in (
        {"DITTOBENCH_ALLOW_UNMERGED_SMOKE": "true"},
        {"SUBTENSOR_NETWORK": "local"},
    ):
        partially_allowed = subprocess.run(
            [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
            env={**env, **partial_opt_in},
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert partially_allowed.returncode == 1
        assert "require local network smoke opt-in" in partially_allowed.stderr

    smoke_env = {
        **env,
        "DITTOBENCH_ALLOW_UNMERGED_SMOKE": "true",
        "SUBTENSOR_NETWORK": "local",
    }
    wrong_tip = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env={
            **smoke_env,
            "FAKE_DITTOBENCH_REF_CHECKSUM": "f" * 40,
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert wrong_tip.returncode == 1
    assert "is not the current" in wrong_tip.stderr

    allowed = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=smoke_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr


def test_stack_update_rebuilds_and_replaces_a_stale_scorer_image(
    tmp_path: Path,
) -> None:
    """The v0.29.3 failure: `up` reused the cached scorer image.

    Compose recreated dittobench-api with the new pin's environment and kept the
    old binary, so the scorer advertised a revision it was not built from. A pin
    the host has never built must therefore force a real `build --pull` AND
    replace the running containers, not just re-render their environment.
    """
    env, capture, _ = _wrapper_env(tmp_path)
    env["FAKE_COMPOSE_PS_OUTPUT"] = "scorer-container\n"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _compose_calls(capture)
    build = next(call for call in calls if "build" in call)
    assert build[-3:] == ["build", "--pull", "dittobench-api"]
    recreate = next(call for call in calls if "--no-deps" in call)
    assert recreate[-5:] == [
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "dittobench-api",
    ]
    # The operator's own command still runs after the targeted replacement.
    operator_up = next(
        index for index, call in enumerate(calls) if call[-2:] == ["up", "-d"]
    )
    assert operator_up > calls.index(recreate)
    assert (
        _built_revision_marker(env).read_text().strip()
        == env["FAKE_DITTOBENCH_CHECKSUM"]
    )


def test_live_scorer_replacement_drains_and_resumes_validator(tmp_path: Path) -> None:
    env, capture, _ = _wrapper_env(tmp_path)
    env["FAKE_COMPOSE_PS_OUTPUT"] = "scorer-container\n"
    env["FAKE_VALIDATOR_PS_OUTPUT"] = "validator-container\n"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _docker_calls(capture)
    drain_index = calls.index(["kill", "--signal=USR1", "validator-container"])
    replace_index = next(
        index
        for index, call in enumerate(calls)
        if call[-5:]
        == [
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "dittobench-api",
        ]
    )
    resume_index = calls.index(["kill", "--signal=USR2", "validator-container"])
    assert drain_index < replace_index < resume_index
    assert (
        "draining validator before reconciling the live scoring stack" in result.stderr
    )


def test_unhealthy_rebuilt_scorer_keeps_validator_drained(tmp_path: Path) -> None:
    env, capture, _ = _wrapper_env(tmp_path)
    env["FAKE_COMPOSE_PS_OUTPUT"] = "scorer-container\n"
    env["FAKE_VALIDATOR_PS_OUTPUT"] = "validator-container\n"
    env["FAKE_SCORER_HEALTH"] = "unhealthy"
    env["DITTO_VALIDATOR_COMPOSE_READY_TIMEOUT_SECONDS"] = "1"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "validator remains drained" in result.stderr
    assert ["kill", "--signal=USR1", "validator-container"] in _docker_calls(capture)
    assert ["kill", "--signal=USR2", "validator-container"] not in _docker_calls(
        capture
    )
    assert not _built_revision_marker(env).exists()


def test_interrupted_reconciliation_resumes_only_after_full_health_verification(
    tmp_path: Path,
) -> None:
    """An outer source updater may time out just after Compose creates services.

    The wrapper must not strand that platform-accepted validator in bootstrap
    drain when the complete replacement is already healthy.
    """
    env, capture, _ = _wrapper_env(tmp_path)
    _built_revision_marker(env).write_text(f"{env['FAKE_DITTOBENCH_CHECKSUM']}\n")
    env["FAKE_COMPOSE_PS_OUTPUT"] = "service-container\n"
    env["FAKE_VALIDATOR_PS_OUTPUT"] = "validator-container\n"
    env["FAKE_COMPOSE_UP_FAIL"] = "true"
    env["FAKE_COMPOSE_UP_LEAVES_DRAINED"] = "true"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert (
        "verified reconciled source stack and resumed validator after interruption"
        in result.stderr
    )
    assert ["kill", "--signal=USR2", "validator-container"] in _docker_calls(capture)


def test_interrupted_reconciliation_stays_drained_when_a_component_is_missing(
    tmp_path: Path,
) -> None:
    env, capture, _ = _wrapper_env(tmp_path)
    _built_revision_marker(env).write_text(f"{env['FAKE_DITTOBENCH_CHECKSUM']}\n")
    env["FAKE_VALIDATOR_PS_OUTPUT"] = "validator-container\n"
    env["FAKE_COMPOSE_UP_FAIL"] = "true"
    env["FAKE_COMPOSE_UP_LEAVES_DRAINED"] = "true"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "validator remains drained for safe operator recovery" in result.stderr
    assert ["kill", "--signal=USR2", "validator-container"] not in _docker_calls(
        capture
    )


def test_unchanged_pin_starts_the_stack_without_a_rebuild(tmp_path: Path) -> None:
    """A no-op restart must stay fast and independent of the network.

    Restarts are routine — after a host reboot, after every stack updater
    tick — so keying the rebuild on the pin rather than on "did we run `up`"
    is what keeps this fix from becoming a rebuild on every restart.
    """
    env, capture, _ = _wrapper_env(tmp_path)
    _built_revision_marker(env).write_text(f"{env['FAKE_DITTOBENCH_CHECKSUM']}\n")

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _compose_calls(capture)
    assert not any("build" in call for call in calls)
    operator_calls = [call for call in calls if call[-2:] == ["up", "-d"]]
    assert len(operator_calls) == 1


def test_unchanged_live_stack_does_not_drain_for_noop_up(tmp_path: Path) -> None:
    env, capture, _ = _wrapper_env(tmp_path)
    _built_revision_marker(env).write_text(f"{env['FAKE_DITTOBENCH_CHECKSUM']}\n")
    env["FAKE_COMPOSE_PS_OUTPUT"] = "scorer-container\n"
    env["FAKE_VALIDATOR_PS_OUTPUT"] = "validator-container\n"
    env["FAKE_VALIDATOR_BOOTSTRAP_TOKEN"] = f"source-{'a' * 40}"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = _docker_calls(capture)
    assert ["kill", "--signal=USR1", "validator-container"] not in calls
    assert ["kill", "--signal=USR2", "validator-container"] not in calls


def test_scorer_rebuild_failure_stops_the_stack_command(tmp_path: Path) -> None:
    """Never fall through to `up` on a failed rebuild.

    Continuing would start the very thing this change exists to prevent: the
    previous scorer image, wearing the new pin's environment.
    """
    env, capture, _ = _wrapper_env(tmp_path)
    env["FAKE_COMPOSE_BUILD_FAIL"] = "true"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "could not rebuild dittobench-api" in result.stderr
    assert [call for call in _compose_calls(capture) if call[-2:] == ["up", "-d"]] == []
    # An unwritten marker keeps the next attempt honest.
    assert not _built_revision_marker(env).exists()


def test_read_only_commands_never_trigger_a_rebuild(tmp_path: Path) -> None:
    env, capture, _ = _wrapper_env(tmp_path)

    for command in (["ps"], ["logs", "--since", "10m", "ditto-subnet"]):
        result = subprocess.run(
            [str(COMPOSE_WRAPPER), *command],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    assert not any("build" in call for call in _compose_calls(capture))
    assert not _built_revision_marker(env).exists()
