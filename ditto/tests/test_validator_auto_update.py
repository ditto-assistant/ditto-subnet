"""Integration tests for the host-side validator updater's safety gates."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
UPDATER = ROOT / "scripts/validator-auto-update.sh"
IMAGE_REPOSITORY = "ghcr.io/ditto-assistant/ditto-subnet-validator"
CHANNEL = f"{IMAGE_REPOSITORY}:compat-1"
DIGEST = f"{IMAGE_REPOSITORY}@sha256:" + "2" * 64
FAILED_DIGEST = f"{IMAGE_REPOSITORY}@sha256:" + "3" * 64
SOURCE = "https://github.com/ditto-assistant/ditto-subnet"


def _labels(version: str, revision: str, *, epoch: str = "1") -> dict[str, str]:
    return {
        "io.heyditto.validator-service": "true",
        "io.heyditto.validator.compatibility-epoch": epoch,
        "io.heyditto.validator.heartbeat-protocol": "4",
        "io.heyditto.validator.update-protocol": "1",
        "io.heyditto.validator.compose-schema": "1",
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
    }


def _initial_state() -> dict[str, Any]:
    old = {
        "id": "sha256:" + "1" * 64,
        "labels": _labels("0.6.6", "a" * 40),
        "repo_digests": [],
    }
    new = {
        "id": "sha256:" + "2" * 64,
        "labels": _labels("0.6.7", "b" * 40),
        "repo_digests": [DIGEST],
    }
    return {
        "calls": [],
        "compose_calls": [],
        "drain_mode": "success",
        "fail_candidate": False,
        "fail_resume": False,
        "fail_stop": False,
        "interrupt_resume_image": None,
        "images": {old["id"]: old, CHANNEL: new, DIGEST: new},
        "container": {
            "id": "validator-1",
            "image": old["id"],
            "running": True,
            "labels": {
                "com.docker.compose.service": "ditto-subnet",
                "io.heyditto.validator.auto-update-target": "true",
            },
            "runtime_state": {
                "compatibility_epoch": 1,
                "heartbeat_protocol": 4,
                "platform_accepted": True,
                "state": "working",
                "update_protocol": 1,
            },
        },
    }


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import re
import signal
import sys

path = os.environ["FAKE_DOCKER_STATE"]
with open(path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["calls"].append(args)

def save():
    with open(path, "w") as handle:
        json.dump(state, handle)

def image(ref):
    if ref in state["images"]:
        return state["images"][ref]
    for item in state["images"].values():
        if item["id"] == ref:
            return item
    raise SystemExit(1)

if args[:1] == ["pull"]:
    save()
elif args[:2] == ["image", "inspect"]:
    fmt = args[args.index("--format") + 1]
    item = image(args[-1])
    if ".Id" in fmt:
        print(item["id"])
    elif "RepoDigests" in fmt:
        print("\n".join(item.get("repo_digests", [])))
    else:
        match = re.search(r'Labels "([^"]+)', fmt)
        value = "<no value>"
        if match:
            value = item["labels"].get(match.group(1), value)
        print(value)
    save()
elif args[:1] == ["inspect"]:
    fmt = args[args.index("--format") + 1]
    container = state["container"]
    if ".State.Running" in fmt:
        print("true" if container["running"] else "false")
    elif ".Image" in fmt:
        print(container["image"])
    else:
        match = re.search(r'Labels "([^"]+)', fmt)
        value = "<no value>"
        if match:
            value = container["labels"].get(match.group(1), value)
        print(value)
    save()
elif args[:1] == ["exec"]:
    print(json.dumps(state["container"]["runtime_state"], separators=(",", ":")))
    save()
elif args[:1] == ["kill"]:
    signal_name = next(
        value.split("=", 1)[1] for value in args if value.startswith("--signal=")
    )
    if signal_name == "USR2" and state["fail_resume"]:
        save()
        raise SystemExit(1)
    if signal_name == "USR1" and state["drain_mode"] == "success":
        state["container"]["runtime_state"]["state"] = "drained"
    elif signal_name == "USR2":
        state["container"]["runtime_state"]["state"] = "working"
        if state.get("interrupt_resume_image") == state["container"]["image"]:
            state["interrupt_resume_image"] = None
            save()
            os.kill(os.getppid(), signal.SIGTERM)
    save()
elif args[:2] == ["image", "tag"]:
    state["images"][args[3]] = image(args[2])
    save()
elif args[:1] == ["stop"]:
    if state["fail_stop"]:
        save()
        raise SystemExit(1)
    state["container"]["running"] = False
    save()
else:
    save()
    raise SystemExit("unhandled fake docker command: " + repr(args))
"""


FAKE_COMPOSE = r"""#!/usr/bin/env python3
import json
import os
import sys

path = os.environ["FAKE_DOCKER_STATE"]
with open(path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["compose_calls"].append(args)
if args[:2] == ["ps", "-q"]:
    print(state["container"]["id"])
elif args[:1] == ["up"]:
    ref = os.environ["DITTO_SUBNET_IMAGE"]
    item = state["images"][ref]
    state["container"]["id"] = "validator-2"
    state["container"]["image"] = item["id"]
    state["container"]["running"] = True
    failed = state["fail_candidate"] and item["id"] == "sha256:" + "2" * 64
    state["container"]["runtime_state"]["state"] = "drained"
    state["container"]["runtime_state"]["platform_accepted"] = not failed
else:
    raise SystemExit("unhandled fake compose command: " + repr(args))
with open(path, "w") as handle:
    json.dump(state, handle)
"""


@pytest.fixture
def updater_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    state_path = tmp_path / "docker-state.json"
    state_path.write_text(json.dumps(_initial_state()))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    compose = fake_bin / "validator-compose"
    docker.write_text(FAKE_DOCKER)
    compose.write_text(FAKE_COMPOSE)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    compose.chmod(compose.stat().st_mode | stat.S_IXUSR)
    env_file = tmp_path / ".env"
    env_file.write_text("VALIDATOR_AUTO_UPDATE=true\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state_path),
        "DITTO_VALIDATOR_COMPOSE": str(compose),
        "DITTO_SUBNET_ENV_FILE": str(env_file),
        "DITTO_VALIDATOR_UPDATE_STATE_DIR": str(tmp_path / "updater-state"),
        "VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS": "1",
        "VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS": "1",
        "VALIDATOR_AUTO_UPDATE_CHECK_SECONDS": "1",
    }
    return env, state_path, env_file


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(UPDATER), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _read_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_disabled_mode_does_not_touch_docker(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, env_file = updater_env
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")

    result = _run(env, "run")

    assert result.returncode == 0
    state = _read_state(state_path)
    assert state["calls"] == []
    assert not any(call[0] == "up" for call in state["compose_calls"])


def test_timeout_budget_is_bounded_by_operator_drain_and_readiness_settings(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    env["VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS"] = "5400"
    env["VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS"] = "600"

    result = _run(env, "budget")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "TIMEOUT_START_SECONDS=8104",
        "TIMEOUT_STOP_SECONDS=2103",
    ]
    assert _read_state(state_path)["calls"] == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS", "0"),
        ("VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS", "invalid"),
        ("VALIDATOR_AUTO_UPDATE_CHECK_SECONDS", "-1"),
    ],
)
def test_timeout_budget_rejects_non_positive_settings(
    updater_env: tuple[dict[str, str], Path, Path], name: str, value: str
) -> None:
    env, state_path, _ = updater_env
    env[name] = value

    result = _run(env, "budget")

    assert result.returncode == 1
    assert "must be a positive integer" in result.stderr
    assert _read_state(state_path)["calls"] == []


def test_scope_label_failure_stops_before_any_container_mutation(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["container"]["labels"]["io.heyditto.validator.auto-update-target"] = "false"
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    state = _read_state(state_path)
    assert not any(call[0] in {"kill", "stop"} for call in state["calls"])
    assert not any(call[0] == "up" for call in state["compose_calls"])


def test_incompatible_candidate_fails_before_drain(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    for image in (CHANNEL, DIGEST):
        state["images"][image]["labels"][
            "io.heyditto.validator.compatibility-epoch"
        ] = "2"
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    state = _read_state(state_path)
    assert not any(call[0] in {"kill", "stop"} for call in state["calls"])


def test_heartbeat_protocol_mismatch_fails_before_drain(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    for image in (CHANNEL, DIGEST):
        state["images"][image]["labels"]["io.heyditto.validator.heartbeat-protocol"] = (
            "5"
        )
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert not any(
        call[0] in {"kill", "stop"} for call in _read_state(state_path)["calls"]
    )


def test_minor_release_crossing_requires_supervised_migration(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    for image in (CHANNEL, DIGEST):
        state["images"][image]["labels"]["org.opencontainers.image.version"] = "0.7.0"
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "supervised migration required" in result.stderr
    assert not any(
        call[0] in {"kill", "stop"} for call in _read_state(state_path)["calls"]
    )


def test_starting_validator_is_deferred_without_signal(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["container"]["runtime_state"]["state"] = "starting"
    state["container"]["runtime_state"]["platform_accepted"] = False
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 0
    assert "deferring update" in result.stderr
    state = _read_state(state_path)
    assert not any(call[0] in {"kill", "stop"} for call in state["calls"])


def test_never_drained_work_resumes_without_replacement(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["drain_mode"] = "timeout"
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 0
    state = _read_state(state_path)
    signals = [call[1] for call in state["calls"] if call[0] == "kill"]
    assert signals == ["--signal=USR1", "--signal=USR2"]
    assert not any(call[0] == "stop" for call in state["calls"])
    assert not any(call[0] == "up" for call in state["compose_calls"])
    assert state["container"]["image"] == "sha256:" + "1" * 64


def test_term_before_drain_ack_cancels_usr1(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["drain_mode"] = "timeout"
    state_path.write_text(json.dumps(state))
    env["VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS"] = "30"
    process = subprocess.Popen(
        [str(UPDATER), "run"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(500):
        try:
            calls = _read_state(state_path)["calls"]
        except json.JSONDecodeError:
            calls = []
        if any(call[:2] == ["kill", "--signal=USR1"] for call in calls):
            break
        time.sleep(0.01)
    else:
        process.kill()
        pytest.fail("updater never requested drain")

    process.terminate()
    process.communicate(timeout=10)

    state = _read_state(state_path)
    signals = [call[1] for call in state["calls"] if call[0] == "kill"]
    assert signals == ["--signal=USR1", "--signal=USR2"]
    assert state["container"]["runtime_state"]["state"] == "working"
    assert not (
        Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "transaction.env"
    ).exists()


def test_timeout_fails_loud_when_resume_cannot_be_delivered(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["drain_mode"] = "timeout"
    state["fail_resume"] = True
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "could not verify" in result.stderr
    assert not any(call[0] == "stop" for call in _read_state(state_path)["calls"])


def test_post_drain_stop_failure_resumes_and_restores_old_image(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["fail_stop"] = True
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    state = _read_state(state_path)
    signals = [call[1] for call in state["calls"] if call[0] == "kill"]
    assert signals == ["--signal=USR1", "--signal=USR2"]
    assert state["container"]["running"] is True
    assert state["container"]["runtime_state"]["state"] == "working"
    assert state["container"]["image"] == "sha256:" + "1" * 64
    assert not any(call[0] == "up" for call in state["compose_calls"])


def test_success_replaces_only_validator_and_retains_rollback(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    state = _read_state(state_path)
    assert state["container"]["image"] == "sha256:" + "2" * 64
    stop_calls = [call for call in state["calls"] if call[0] == "stop"]
    assert stop_calls == [["stop", "--time", "30", "validator-1"]]
    up_calls = [call for call in state["compose_calls"] if call[0] == "up"]
    assert len(up_calls) == 1
    assert up_calls[0] == [
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
        "ditto-subnet",
    ]
    assert state["container"]["runtime_state"]["state"] == "working"
    assert [call[1] for call in state["calls"] if call[0] == "kill"] == [
        "--signal=USR1",
        "--signal=USR2",
    ]
    record = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "last-update.env"
    assert "PREVIOUS_IMAGE=ditto-subnet-validator-rollback:" in record.read_text()
    assert f"CURRENT_IMAGE={DIGEST}" in record.read_text()


def test_term_at_committed_boundary_resumes_candidate_from_journal(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["interrupt_resume_image"] = "sha256:" + "2" * 64
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 143, result.stderr
    interrupted = _read_state(state_path)
    assert interrupted["container"]["image"] == "sha256:" + "2" * 64
    assert interrupted["container"]["runtime_state"]["state"] == "working"
    transaction = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "transaction.env"
    assert "PHASE=committed" in transaction.read_text()

    retry = _run(env, "run")
    assert retry.returncode == 0, retry.stderr
    assert not transaction.exists()
    assert "already running validator" in retry.stderr


def test_term_at_rollback_ready_boundary_resumes_previous_image_from_journal(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["fail_candidate"] = True
    state["interrupt_resume_image"] = "sha256:" + "1" * 64
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 143, result.stderr
    interrupted = _read_state(state_path)
    assert interrupted["container"]["image"] == "sha256:" + "1" * 64
    assert interrupted["container"]["runtime_state"]["state"] == "working"
    updater_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    transaction = updater_state / "transaction.env"
    assert "PHASE=rollback_ready" in transaction.read_text()
    assert not (updater_state / "failed-candidate").exists()

    retry = _run(env, "run")
    assert retry.returncode == 0, retry.stderr
    assert "candidate is suppressed" in retry.stderr
    assert (updater_state / "failed-candidate").read_text().strip() == DIGEST
    assert not transaction.exists()


def test_failed_candidate_readiness_automatically_restores_previous_image(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["fail_candidate"] = True
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    state = _read_state(state_path)
    assert state["container"]["image"] == "sha256:" + "1" * 64
    up_calls = [call for call in state["compose_calls"] if call[0] == "up"]
    assert len(up_calls) == 2
    assert "previous image was restored" in result.stderr
    updater_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    assert (updater_state / "failed-candidate").read_text().strip() == DIGEST

    # The same bad digest is not allowed to flap the validator every 15 minutes.
    state["fail_candidate"] = False
    state["compose_calls"] = []
    # A power loss after rollback commit but before USR2 must be recovered even
    # though the bad digest is already suppressed.
    state["container"]["runtime_state"]["state"] = "drained"
    state_path.write_text(json.dumps(state))
    retry = _run(env, "run")
    assert retry.returncode == 0
    assert "candidate is suppressed" in retry.stderr
    recovered = _read_state(state_path)
    assert not any(call[0] == "up" for call in recovered["compose_calls"])
    assert recovered["container"]["runtime_state"]["state"] == "working"


def test_deferred_new_channel_keeps_older_failed_digest_suppressed(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    updater_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    updater_state.mkdir()
    failed_file = updater_state / "failed-candidate"
    failed_file.write_text(FAILED_DIGEST + "\n")
    state = _read_state(state_path)
    failed_image = {
        **state["images"][DIGEST],
        "id": "sha256:" + "3" * 64,
        "repo_digests": [FAILED_DIGEST],
    }
    state["images"][FAILED_DIGEST] = failed_image
    state["container"]["runtime_state"]["state"] = "starting"
    state["container"]["runtime_state"]["platform_accepted"] = False
    state_path.write_text(json.dumps(state))

    deferred = _run(env, "run")

    assert deferred.returncode == 0, deferred.stderr
    assert "deferring update" in deferred.stderr
    assert failed_file.read_text().strip() == FAILED_DIGEST

    state = _read_state(state_path)
    state["images"][CHANNEL] = failed_image
    state["container"]["runtime_state"]["state"] = "working"
    state["container"]["runtime_state"]["platform_accepted"] = True
    state["calls"] = []
    state["compose_calls"] = []
    state_path.write_text(json.dumps(state))

    rollback = _run(env, "run")

    assert rollback.returncode == 0, rollback.stderr
    assert "candidate is suppressed" in rollback.stderr
    final = _read_state(state_path)
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any(call[0] == "up" for call in final["compose_calls"])


def test_power_loss_journal_restores_uncommitted_candidate_before_suppression(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    new_id = "sha256:" + "2" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    state["container"]["image"] = new_id
    state["container"]["runtime_state"]["state"] = "drained"
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    update_state.mkdir()
    (update_state / "failed-candidate").write_text(DIGEST + "\n")
    (update_state / "transaction.env").write_text(
        "\n".join(
            [
                "PHASE=candidate_ready",
                f"PREVIOUS_IMAGE={rollback}",
                f"PREVIOUS_IMAGE_ID={old_id}",
                f"CURRENT_IMAGE={DIGEST}",
                f"CURRENT_IMAGE_ID={new_id}",
                "CURRENT_VERSION=0.6.7",
                f"CURRENT_REVISION={'b' * 40}",
                "",
            ]
        )
    )

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    recovered = _read_state(state_path)
    assert recovered["container"]["image"] == old_id
    assert recovered["container"]["runtime_state"]["state"] == "working"
    up_calls = [call for call in recovered["compose_calls"] if call[0] == "up"]
    assert len(up_calls) == 1
    assert "candidate is suppressed" in result.stderr
    assert not (update_state / "transaction.env").exists()


def test_pre_ack_power_loss_journal_always_delivers_resume(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    new_id = "sha256:" + "2" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    update_state.mkdir()
    (update_state / "failed-candidate").write_text(DIGEST + "\n")
    (update_state / "transaction.env").write_text(
        "\n".join(
            [
                "PHASE=prepared",
                f"PREVIOUS_IMAGE={rollback}",
                f"PREVIOUS_IMAGE_ID={old_id}",
                f"CURRENT_IMAGE={DIGEST}",
                f"CURRENT_IMAGE_ID={new_id}",
                "CURRENT_VERSION=0.6.7",
                f"CURRENT_REVISION={'b' * 40}",
                "",
            ]
        )
    )

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    recovered = _read_state(state_path)
    assert [call[1] for call in recovered["calls"] if call[0] == "kill"] == [
        "--signal=USR2"
    ]
    assert not any(call[0] == "up" for call in recovered["compose_calls"])
    assert not (update_state / "transaction.env").exists()
