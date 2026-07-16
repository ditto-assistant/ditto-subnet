"""Integration tests for the host-side validator updater's safety gates."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
UPDATER = ROOT / "scripts/validator-auto-update.sh"
COMPOSE_WRAPPER = ROOT / "scripts/validator-compose.sh"
IMAGE_REPOSITORY = "ghcr.io/ditto-assistant/ditto-subnet-validator"
CHANNEL = f"{IMAGE_REPOSITORY}:compat-2"
DIGEST = f"{IMAGE_REPOSITORY}@sha256:" + "2" * 64
OLD_DIGEST = f"{IMAGE_REPOSITORY}@sha256:" + "1" * 64
FAILED_DIGEST = f"{IMAGE_REPOSITORY}@sha256:" + "3" * 64
SOURCE = "https://github.com/ditto-assistant/ditto-subnet"


def _labels(
    version: str,
    revision: str,
    *,
    epoch: str = "2",
    heartbeat_protocol: str = "4",
) -> dict[str, str]:
    return {
        "io.heyditto.validator-service": "true",
        "io.heyditto.validator.compatibility-epoch": epoch,
        "io.heyditto.validator.heartbeat-protocol": heartbeat_protocol,
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
        "compose_images": [],
        "compose_bootstrap_tokens": [],
        "drain_mode": "success",
        "fail_drain_signal": False,
        "fail_candidate": False,
        "fail_resume": False,
        "fail_resume_image": None,
        "resume_state_stuck_image": None,
        "stop_after_resume_image": None,
        "fail_stop": False,
        "interrupt_stop_before_effect": False,
        "interrupt_resume_image": None,
        "interrupt_compose_image": None,
        "resume_effect_then_error_image": None,
        "fail_sidecar_reconcile": False,
        "sidecar_reconciles": 0,
        "runtime_heartbeat_protocol_overrides": {},
        "images": {old["id"]: old, OLD_DIGEST: old, CHANNEL: new, DIGEST: new},
        "registry_images": {OLD_DIGEST: old, DIGEST: new},
        "container": {
            "id": "validator-1",
            "image": old["id"],
            "running": True,
            "labels": {
                "com.docker.compose.service": "ditto-subnet",
                "io.heyditto.validator.auto-update-target": "true",
            },
            "runtime_state": {
                "compatibility_epoch": 2,
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
    ref = args[1]
    if ref not in state["images"] and ref in state.get("registry_images", {}):
        state["images"][ref] = state["registry_images"][ref]
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
    if signal_name == "USR1" and state.get("fail_drain_signal"):
        save()
        raise SystemExit(1)
    if (
        signal_name == "USR2"
        and state.get("resume_effect_then_error_image") == state["container"]["image"]
    ):
        state["container"]["runtime_state"]["state"] = "working"
        save()
        raise SystemExit(1)
    if signal_name == "USR2" and (
        state["fail_resume"]
        or state.get("fail_resume_image") == state["container"]["image"]
    ):
        save()
        raise SystemExit(1)
    if signal_name == "USR1" and state["drain_mode"] == "success":
        state["container"]["runtime_state"]["state"] = "drained"
    elif signal_name == "USR2":
        if state.get("resume_state_stuck_image") != state["container"]["image"]:
            state["container"]["runtime_state"]["state"] = "working"
        if state.get("stop_after_resume_image") == state["container"]["image"]:
            state["stop_after_resume_image"] = None
            state["container"]["running"] = False
        if state.get("interrupt_resume_image") == state["container"]["image"]:
            state["interrupt_resume_image"] = None
            save()
            os.kill(os.getppid(), signal.SIGTERM)
    save()
elif args[:2] == ["image", "tag"]:
    state["images"][args[3]] = image(args[2])
    save()
elif args[:1] == ["stop"]:
    if state["interrupt_stop_before_effect"]:
        state["interrupt_stop_before_effect"] = False
        save()
        os.kill(os.getppid(), signal.SIGTERM)
        raise SystemExit(0)
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
import signal
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
    state["compose_images"].append(ref)
    state["compose_bootstrap_tokens"].append(os.environ["VALIDATOR_BOOTSTRAP_TOKEN"])
    item = state["images"][ref]
    state["container"]["id"] = "validator-2"
    state["container"]["image"] = item["id"]
    state["container"]["running"] = True
    failed = state["fail_candidate"] and item["id"] == "sha256:" + "2" * 64
    runtime_heartbeat = state["runtime_heartbeat_protocol_overrides"].get(
        item["id"], item["labels"]["io.heyditto.validator.heartbeat-protocol"]
    )
    state["container"]["runtime_state"]["compatibility_epoch"] = int(
        item["labels"]["io.heyditto.validator.compatibility-epoch"]
    )
    state["container"]["runtime_state"]["heartbeat_protocol"] = int(
        runtime_heartbeat
    )
    state["container"]["runtime_state"]["update_protocol"] = int(
        item["labels"]["io.heyditto.validator.update-protocol"]
    )
    state["container"]["runtime_state"]["state"] = "drained"
    state["container"]["runtime_state"]["platform_accepted"] = not failed
elif args == ["managed-reconcile"]:
    if state["container"]["runtime_state"]["state"] != "drained":
        raise SystemExit("sidecars reconciled while validator was not drained")
    state["sidecar_reconciles"] += 1
    if state["fail_sidecar_reconcile"]:
        with open(path, "w") as handle:
            json.dump(state, handle)
        raise SystemExit("simulated partial sidecar reconciliation failure")
else:
    raise SystemExit("unhandled fake compose command: " + repr(args))
with open(path, "w") as handle:
    json.dump(state, handle)
if (
    args[:1] == ["up"]
    and state.get("interrupt_compose_image") == state["container"]["image"]
):
    state["interrupt_compose_image"] = None
    with open(path, "w") as handle:
        json.dump(state, handle)
    os.kill(os.getppid(), signal.SIGTERM)
"""


FAKE_FLOCK = r"""#!/usr/bin/env python3
import fcntl
import sys

fd = int(sys.argv[-1])
operation = fcntl.LOCK_UN if "-u" in sys.argv else fcntl.LOCK_EX | fcntl.LOCK_NB
try:
    fcntl.flock(fd, operation)
except BlockingIOError:
    raise SystemExit(1)
"""


FAKE_WRAPPER_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["compose", "version", "--short"]:
    print("2.40.0")
elif args == ["info"] or args == ["buildx", "version"]:
    pass
elif args[:1] == ["compose"]:
    with open(os.environ["FAKE_COMPOSE_CAPTURE"], "w") as handle:
        json.dump(
            {
                "args": args,
                "image": os.environ.get("DITTO_SUBNET_IMAGE"),
                "wallets_dir": os.environ.get("DITTO_BITTENSOR_WALLETS_DIR"),
            },
            handle,
        )
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
    print(os.environ["FAKE_DITTOBENCH_CHECKSUM"])
elif "ls-tree" in args:
    records = [b"100644 Dockerfile"]
    executable = os.environ.get("FAKE_EXECUTABLE_PATH")
    if executable:
        records.append(b"100755 " + executable.encode())
    sys.stdout.buffer.write(b"\0".join(records) + b"\0")
elif "status" in args or "cat-file" in args or "checkout" in args:
    pass
else:
    raise SystemExit("unhandled wrapper git command: " + repr(args))
"""


@pytest.fixture
def updater_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    state_path = tmp_path / "docker-state.json"
    state_path.write_text(json.dumps(_initial_state()))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    compose = fake_bin / "validator-compose"
    flock = fake_bin / "flock"
    docker.write_text(FAKE_DOCKER)
    compose.write_text(FAKE_COMPOSE)
    flock.write_text(FAKE_FLOCK)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    compose.chmod(compose.stat().st_mode | stat.S_IXUSR)
    flock.chmod(flock.stat().st_mode | stat.S_IXUSR)
    env_file = tmp_path / ".env"
    env_file.write_text("VALIDATOR_AUTO_UPDATE=true\n")
    updater_state = tmp_path / "updater-state"
    updater_state.mkdir(exist_ok=True)
    (updater_state / "managed-image.env").write_text(
        f"DITTO_SUBNET_IMAGE={OLD_DIGEST}\n"
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state_path),
        "DITTO_VALIDATOR_COMPOSE": str(compose),
        "DITTO_SUBNET_ENV_FILE": str(env_file),
        "DITTO_VALIDATOR_UPDATE_STATE_DIR": str(updater_state),
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


def _write_transaction(state_dir: Path, phase: str, rollback: str) -> Path:
    transaction = state_dir / "transaction.env"
    transaction.write_text(
        "\n".join(
            [
                f"PHASE={phase}",
                f"PREVIOUS_IMAGE={rollback}",
                f"PREVIOUS_IMAGE_ID=sha256:{'1' * 64}",
                f"CURRENT_IMAGE={DIGEST}",
                f"CURRENT_IMAGE_ID=sha256:{'2' * 64}",
                "CURRENT_VERSION=0.6.7",
                f"CURRENT_REVISION={'b' * 40}",
                "SUPPRESS_CANDIDATE=true",
                "",
            ]
        )
    )
    return transaction


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
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DITTO_SUBNET_BUILD_CACHE": str(cache),
        "DITTO_VALIDATOR_UPDATE_STATE_DIR": str(state_dir),
        "FAKE_COMPOSE_CAPTURE": str(capture),
        "FAKE_DITTOBENCH_CHECKSUM": checksum,
        "HOME": str(tmp_path / "operator-home"),
    }
    return env, capture, state_dir


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


def test_enabled_mode_requires_persisted_managed_image_before_docker_mutation(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    (Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "managed-image.env").unlink()

    result = _run(env, "run")

    assert result.returncode == 1
    assert "not adopted" in result.stderr
    state = _read_state(state_path)
    assert state["calls"] == []
    assert state["compose_calls"] == []


def test_missing_local_digest_alias_is_restored_before_identity_check(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    del state["images"][OLD_DIGEST]
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    state = _read_state(state_path)
    assert ["pull", OLD_DIGEST] in state["calls"]
    assert state["container"]["image"] == "sha256:" + "2" * 64


def test_managed_compose_config_uses_the_persisted_immutable_image(
    tmp_path: Path,
) -> None:
    env, capture, state_dir = _wrapper_env(tmp_path)
    (state_dir / "managed-image.env").write_text(f"DITTO_SUBNET_IMAGE={DIGEST}\n")

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
    assert captured["image"] == DIGEST
    assert captured["wallets_dir"] == str(tmp_path / "operator-home/.bittensor/wallets")


def test_compose_wrapper_repairs_git_tracked_executable_modes(
    tmp_path: Path,
) -> None:
    env, _, state_dir = _wrapper_env(tmp_path)
    (state_dir / "managed-image.env").write_text(f"DITTO_SUBNET_IMAGE={DIGEST}\n")
    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    executable = (
        Path(env["DITTO_SUBNET_BUILD_CACHE"]) / "dittobench-api" / checksum / "run.sh"
    )
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o644)
    env["FAKE_EXECUTABLE_PATH"] = "run.sh"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "config", "--quiet"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert executable.stat().st_mode & stat.S_IXUSR


def test_managed_compose_blocks_broad_mutation_and_image_override(
    tmp_path: Path,
) -> None:
    env, _, state_dir = _wrapper_env(tmp_path)
    (state_dir / "managed-image.env").write_text(f"DITTO_SUBNET_IMAGE={DIGEST}\n")

    broad = subprocess.run(
        [str(COMPOSE_WRAPPER), "up", "-d", "--build"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert broad.returncode == 1
    assert "blocks broad Compose mutation" in broad.stderr

    override = subprocess.run(
        [str(COMPOSE_WRAPPER), "config", "--quiet"],
        env={**env, "DITTO_SUBNET_IMAGE": "ditto-subnet-validator:local"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert override.returncode == 1
    assert "cannot override" in override.stderr


def test_managed_reconcile_excludes_the_validator_service(tmp_path: Path) -> None:
    env, capture, state_dir = _wrapper_env(tmp_path)
    (state_dir / "managed-image.env").write_text(f"DITTO_SUBNET_IMAGE={DIGEST}\n")

    direct = subprocess.run(
        [str(COMPOSE_WRAPPER), "managed-reconcile"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert direct.returncode == 1
    assert "must run through validator-auto-update.sh" in direct.stderr

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "managed-reconcile"],
        env={**env, "DITTO_ALLOW_MANAGED_SIDECAR_RECONCILE": "true"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    args = json.loads(capture.read_text())["args"]
    assert args[-12:] == [
        "up",
        "-d",
        "--build",
        "--no-deps",
        "--wait",
        "--wait-timeout",
        "180",
        "pylon",
        "sandbox-docker",
        "model-relay",
        "ollama",
        "dittobench-api",
    ]
    assert "ditto-subnet" not in args


def test_sidecar_reconcile_drains_and_resumes_without_recreating_validator(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, env_file = updater_env
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")

    result = _run(env, "reconcile-sidecars")

    assert result.returncode == 0, result.stderr
    state = _read_state(state_path)
    assert state["sidecar_reconciles"] == 1
    assert [call[1] for call in state["calls"] if call[0] == "kill"] == [
        "--signal=USR1",
        "--signal=USR2",
    ]
    assert not any(call[0] == "stop" for call in state["calls"])
    assert not any(call[0] == "up" for call in state["compose_calls"])
    assert state["container"]["runtime_state"]["state"] == "working"


def test_failed_sidecar_reconcile_stays_drained_until_explicit_recovery(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, env_file = updater_env
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")
    state = _read_state(state_path)
    state["fail_sidecar_reconcile"] = True
    state_path.write_text(json.dumps(state))

    result = _run(env, "reconcile-sidecars")

    assert result.returncode == 1
    assert "remains drained" in result.stderr
    failed = _read_state(state_path)
    assert failed["container"]["runtime_state"]["state"] == "drained"
    assert [call[1] for call in failed["calls"] if call[0] == "kill"] == [
        "--signal=USR1"
    ]
    assert failed["compose_images"] == []

    failed["fail_sidecar_reconcile"] = False
    state_path.write_text(json.dumps(failed))
    recovery = _run(env, "recover")
    assert recovery.returncode == 0, recovery.stderr
    assert _read_state(state_path)["container"]["runtime_state"]["state"] == "working"


def test_timeout_budget_is_bounded_by_operator_drain_and_readiness_settings(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    env["VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS"] = "5400"
    env["VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS"] = "600"

    result = _run(env, "budget")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "DRAIN_TIMEOUT_SECONDS=5400",
        "READY_TIMEOUT_SECONDS=600",
        "CHECK_SECONDS=1",
        "TIMEOUT_START_SECONDS=9606",
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
        ] = "1"
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    state = _read_state(state_path)
    assert not any(call[0] in {"kill", "stop"} for call in state["calls"])


def test_heartbeat_protocol_upgrade_replaces_and_resumes_candidate(
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

    assert result.returncode == 0, result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == "sha256:" + "2" * 64
    assert final["container"]["runtime_state"]["heartbeat_protocol"] == 5
    assert final["container"]["runtime_state"]["state"] == "working"


def test_invalid_heartbeat_protocol_fails_before_drain(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    for image in (CHANNEL, DIGEST):
        state["images"][image]["labels"]["io.heyditto.validator.heartbeat-protocol"] = (
            "invalid"
        )
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert not any(
        call[0] in {"kill", "stop"} for call in _read_state(state_path)["calls"]
    )


def test_candidate_runtime_heartbeat_mismatch_rolls_back(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    candidate_id = "sha256:" + "2" * 64
    for image in (CHANNEL, DIGEST):
        state["images"][image]["labels"]["io.heyditto.validator.heartbeat-protocol"] = (
            "5"
        )
    state["runtime_heartbeat_protocol_overrides"][candidate_id] = "4"
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "candidate failed readiness" in result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == "sha256:" + "1" * 64
    assert final["container"]["runtime_state"]["heartbeat_protocol"] == 4
    assert final["container"]["runtime_state"]["state"] == "working"


def test_heartbeat_protocol_downgrade_requires_supervised_migration(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    current_id = "sha256:" + "1" * 64
    state["images"][current_id]["labels"][
        "io.heyditto.validator.heartbeat-protocol"
    ] = "5"
    state["images"][OLD_DIGEST]["labels"][
        "io.heyditto.validator.heartbeat-protocol"
    ] = "5"
    state["container"]["runtime_state"]["heartbeat_protocol"] = 5
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "heartbeat protocol 4 is older" in result.stderr
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


def test_failed_drain_signal_aborts_before_container_mutation(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["fail_drain_signal"] = True
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "could not request cooperative drain" in result.stderr
    state = _read_state(state_path)
    assert not any(call[0] == "stop" for call in state["calls"])
    assert not any(call[0] == "up" for call in state["compose_calls"])
    assert state["container"]["running"] is True


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


def test_term_during_stop_with_ambiguous_resume_never_recreates_old_image(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    state["interrupt_stop_before_effect"] = True
    state["resume_state_stuck_image"] = old_id
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 143, result.stderr
    interrupted = _read_state(state_path)
    assert interrupted["container"]["image"] == old_id
    assert interrupted["compose_images"] == []
    transaction = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "transaction.env"
    assert "PHASE=prepared" in transaction.read_text()


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
    managed = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "managed-image.env"
    assert managed.read_text() == f"DITTO_SUBNET_IMAGE={DIGEST}\n"


def test_adopt_records_only_the_matching_healthy_registry_digest(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, env_file = updater_env
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")
    (Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "managed-image.env").unlink()
    state = _read_state(state_path)
    state["container"]["image"] = "sha256:" + "2" * 64
    state_path.write_text(json.dumps(state))

    result = _run(env, "adopt", DIGEST)

    assert result.returncode == 0, result.stderr
    managed = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "managed-image.env"
    assert managed.read_text() == f"DITTO_SUBNET_IMAGE={DIGEST}\n"


def test_adopt_rejects_a_digest_that_is_not_running(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, env_file = updater_env
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")
    (Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "managed-image.env").unlink()

    result = _run(env, "adopt", DIGEST)

    assert result.returncode == 1
    assert "does not match" in result.stderr
    assert not (
        Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "managed-image.env"
    ).exists()


def test_kernel_lock_rejects_a_concurrent_update_without_mutation(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["drain_mode"] = "timeout"
    state_path.write_text(json.dumps(state))
    env["VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS"] = "30"
    first = subprocess.Popen(
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
        first.kill()
        pytest.fail("first updater never acquired the lock and requested drain")

    second = _run(env, "run")

    assert second.returncode == 1
    assert "already running" in second.stderr
    first.terminate()
    first.communicate(timeout=10)


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


def test_committed_candidate_that_cannot_resume_is_rolled_back_and_suppressed(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["fail_resume_image"] = "sha256:" + "2" * 64
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    final = _read_state(state_path)
    assert final["container"]["image"] == "sha256:" + "1" * 64
    assert final["container"]["runtime_state"]["state"] == "working"
    updater_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    assert (updater_state / "failed-candidate").read_text().strip() == DIGEST
    assert not (updater_state / "transaction.env").exists()

    final["calls"] = []
    final["compose_calls"] = []
    state_path.write_text(json.dumps(final))
    retry = _run(env, "run")
    assert retry.returncode == 0, retry.stderr
    assert "candidate is suppressed" in retry.stderr
    assert not any(call[0] == "up" for call in _read_state(state_path)["compose_calls"])


def test_delivered_but_unverified_candidate_resume_never_forces_rollback(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["resume_state_stuck_image"] = "sha256:" + "2" * 64
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "may be working after USR2" in result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == "sha256:" + "2" * 64
    assert final["compose_images"] == [DIGEST]
    updater_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    assert "PHASE=committed" in (updater_state / "transaction.env").read_text()
    assert not (updater_state / "failed-candidate").exists()

    final["resume_state_stuck_image"] = None
    final["container"]["runtime_state"]["state"] = "working"
    state_path.write_text(json.dumps(final))
    env_file = Path(env["DITTO_SUBNET_ENV_FILE"])
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")
    recovery = _run(env, "recover")
    assert recovery.returncode == 0, recovery.stderr
    assert not (updater_state / "transaction.env").exists()
    assert (updater_state / "managed-image.env").read_text() == (
        f"DITTO_SUBNET_IMAGE={DIGEST}\n"
    )


def test_resume_effect_with_docker_cli_error_never_recreates_candidate(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    state["resume_effect_then_error_image"] = "sha256:" + "2" * 64
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == "sha256:" + "2" * 64
    assert final["compose_images"] == [DIGEST]
    assert final["container"]["runtime_state"]["state"] == "working"


def test_restart_after_resume_signal_never_authorizes_recreation(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    new_id = "sha256:" + "2" * 64
    state["resume_state_stuck_image"] = new_id
    state["stop_after_resume_image"] = new_id
    state_path.write_text(json.dumps(state))

    result = _run(env, "run")

    assert result.returncode == 1
    assert "may be working after USR2" in result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == new_id
    assert final["compose_images"] == [DIGEST]
    transaction = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"]) / "transaction.env"
    assert "PHASE=committed" in transaction.read_text()


def test_committed_recovery_never_redeploys_an_unresumable_candidate(
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
    state["fail_resume_image"] = new_id
    state_path.write_text(json.dumps(state))
    updater_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    (updater_state / "transaction.env").write_text(
        "\n".join(
            [
                "PHASE=committed",
                f"PREVIOUS_IMAGE={rollback}",
                f"PREVIOUS_IMAGE_ID={old_id}",
                f"CURRENT_IMAGE={DIGEST}",
                f"CURRENT_IMAGE_ID={new_id}",
                "CURRENT_VERSION=0.6.7",
                f"CURRENT_REVISION={'b' * 40}",
                "SUPPRESS_CANDIDATE=true",
                "",
            ]
        )
    )

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == old_id
    assert final["compose_images"] == [rollback]
    assert final["container"]["runtime_state"]["state"] == "working"
    assert (updater_state / "failed-candidate").read_text().strip() == DIGEST
    assert not (updater_state / "transaction.env").exists()


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
    assert len(set(state["compose_bootstrap_tokens"])) == 2
    assert all(
        re.fullmatch(r"[0-9a-f]{32}", token)
        for token in state["compose_bootstrap_tokens"]
    )
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
    updater_state.mkdir(exist_ok=True)
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
    update_state.mkdir(exist_ok=True)
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
    update_state.mkdir(exist_ok=True)
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


def test_term_during_prepared_recovery_still_cancels_the_drain(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    new_id = "sha256:" + "2" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    state["container"]["runtime_state"]["state"] = "drained"
    state["interrupt_resume_image"] = old_id
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
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
                "SUPPRESS_CANDIDATE=true",
                "",
            ]
        )
    )

    result = _run(env, "run")

    assert result.returncode == 143, result.stderr
    recovered = _read_state(state_path)
    assert recovered["container"]["image"] == old_id
    assert recovered["container"]["runtime_state"]["state"] == "working"
    assert not (update_state / "transaction.env").exists()


@pytest.mark.parametrize("phase", ["stopped", "candidate_ready"])
def test_term_during_uncommitted_recovery_still_restores_previous_image(
    updater_env: tuple[dict[str, str], Path, Path], phase: str
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    new_id = "sha256:" + "2" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    if phase == "stopped":
        state["container"]["running"] = False
    else:
        state["container"]["image"] = new_id
        state["container"]["runtime_state"]["state"] = "drained"
    state["interrupt_compose_image"] = old_id
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    (update_state / "transaction.env").write_text(
        "\n".join(
            [
                f"PHASE={phase}",
                f"PREVIOUS_IMAGE={rollback}",
                f"PREVIOUS_IMAGE_ID={old_id}",
                f"CURRENT_IMAGE={DIGEST}",
                f"CURRENT_IMAGE_ID={new_id}",
                "CURRENT_VERSION=0.6.7",
                f"CURRENT_REVISION={'b' * 40}",
                "SUPPRESS_CANDIDATE=true",
                "",
            ]
        )
    )

    result = _run(env, "run")

    assert result.returncode == 143, result.stderr
    recovered = _read_state(state_path)
    assert recovered["container"]["image"] == old_id
    assert recovered["container"]["runtime_state"]["state"] == "working"
    assert recovered["compose_images"] == [rollback]
    assert (update_state / "transaction.env").exists()

    env_file = Path(env["DITTO_SUBNET_ENV_FILE"])
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")
    retry = _run(env, "recover")
    assert retry.returncode == 0, retry.stderr
    assert not (update_state / "transaction.env").exists()


@pytest.mark.parametrize("phase", ["stopped", "candidate_ready", "rollback_pending"])
def test_term_during_journal_resume_never_recreates_a_possibly_working_target(
    updater_env: tuple[dict[str, str], Path, Path], phase: str
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    state["container"]["runtime_state"]["state"] = "drained"
    state["interrupt_resume_image"] = old_id
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    transaction = _write_transaction(update_state, phase, rollback)

    result = _run(env, "run")

    assert result.returncode == 143, result.stderr
    interrupted = _read_state(state_path)
    assert interrupted["container"]["image"] == old_id
    assert interrupted["container"]["runtime_state"]["state"] == "working"
    assert interrupted["compose_images"] == []
    assert transaction.exists()


def test_term_during_no_journal_recovery_never_recreates_validator(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, env_file = updater_env
    env_file.write_text("VALIDATOR_AUTO_UPDATE=false\n")
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    state["container"]["runtime_state"]["state"] = "drained"
    state["interrupt_resume_image"] = old_id
    state_path.write_text(json.dumps(state))

    result = _run(env, "recover")

    assert result.returncode == 143, result.stderr
    interrupted = _read_state(state_path)
    assert interrupted["container"]["runtime_state"]["state"] == "working"
    assert interrupted["compose_images"] == []


def test_prepared_journal_restores_recorded_image_when_old_container_is_stopped(
    updater_env: tuple[dict[str, str], Path, Path],
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    state["container"]["running"] = False
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    transaction = _write_transaction(update_state, "prepared", rollback)
    (update_state / "failed-candidate").write_text(DIGEST + "\n")

    result = _run(env, "run")

    assert result.returncode == 0, result.stderr
    recovered = _read_state(state_path)
    assert recovered["container"]["image"] == old_id
    assert recovered["container"]["runtime_state"]["state"] == "working"
    assert recovered["compose_images"] == [rollback]
    assert not transaction.exists()


@pytest.mark.parametrize("phase", ["committed", "stopped"])
def test_running_unaccepted_journal_target_is_never_recreated(
    updater_env: tuple[dict[str, str], Path, Path], phase: str
) -> None:
    env, state_path, _ = updater_env
    state = _read_state(state_path)
    old_id = "sha256:" + "1" * 64
    new_id = "sha256:" + "2" * 64
    rollback = "ditto-subnet-validator-rollback:0-6-6-" + "1" * 12
    state["images"][rollback] = state["images"][old_id]
    state["container"]["image"] = new_id if phase == "committed" else old_id
    state["container"]["runtime_state"]["state"] = "working"
    state["container"]["runtime_state"]["platform_accepted"] = False
    state_path.write_text(json.dumps(state))
    update_state = Path(env["DITTO_VALIDATOR_UPDATE_STATE_DIR"])
    transaction = _write_transaction(update_state, phase, rollback)

    result = _run(env, "run")

    assert result.returncode == 1, result.stderr
    final = _read_state(state_path)
    assert final["container"]["image"] == (new_id if phase == "committed" else old_id)
    assert final["compose_images"] == []
    assert transaction.exists()
