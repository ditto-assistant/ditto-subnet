"""Contract tests for immutable, whole-stack validator releases.

The host updater is exercised below through process-level fakes so the tests
assert the Docker/Compose boundary rather than shell implementation details.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
BUILD_RELEASE = ROOT / "scripts/build-stack-release.py"
COMPOSE = ROOT / "docker-compose.yml"
UPDATER = ROOT / "scripts/validator-stack-auto-update.sh"
STACK_COMPOSE = ROOT / "scripts/validator-stack-compose.sh"
STACK_INSTALLER = ROOT / "scripts/install-validator-stack-auto-update.sh"
STACK_REPOSITORY = "ghcr.io/ditto-assistant/ditto-subnet-stack"
STACK_CHANNEL = f"{STACK_REPOSITORY}:compat-2"
STACK_DIGEST = f"{STACK_REPOSITORY}@sha256:" + "f" * 64
OLD_STACK_DIGEST = f"{STACK_REPOSITORY}@sha256:" + "d" * 64
REVISION = "a" * 40

MANAGED_IMAGES = {
    "VALIDATOR_IMAGE": "ghcr.io/ditto-assistant/ditto-subnet-validator",
    "SANDBOX_DOCKER_IMAGE": "ghcr.io/ditto-assistant/ditto-subnet-sandbox-docker",
    "DITTOBENCH_API_IMAGE": "ghcr.io/ditto-assistant/dittobench-api-sandbox",
    "MODEL_RELAY_IMAGE": "ghcr.io/ditto-assistant/dittobench-api-relay",
    "PYLON_IMAGE": "docker.io/backenddevelopersltd/bittensor-pylon",
    "OLLAMA_IMAGE": "docker.io/ollama/ollama",
}
SERVICE_IMAGE_KEYS = {
    "ditto-subnet": "VALIDATOR_IMAGE",
    "sandbox-docker": "SANDBOX_DOCKER_IMAGE",
    "dittobench-api": "DITTOBENCH_API_IMAGE",
    "model-relay": "MODEL_RELAY_IMAGE",
    "pylon": "PYLON_IMAGE",
    "ollama": "OLLAMA_IMAGE",
}


def _images() -> dict[str, str]:
    return {
        key: f"{repository}@sha256:{index:064x}"
        for index, (key, repository) in enumerate(MANAGED_IMAGES.items(), start=1)
    }


def _build_command(output: Path, **overrides: str) -> list[str]:
    values = {
        "version": "0.10.0",
        "revision": REVISION,
        "dittobench-revision": REVISION,
        "compatibility-epoch": "2",
        "update-protocol": "1",
        "compose-schema": "1",
        "heartbeat-protocol": "6",
        **{key.lower().replace("_", "-"): value for key, value in _images().items()},
        **overrides,
    }
    command = [
        sys.executable,
        str(BUILD_RELEASE),
        "--compose",
        str(COMPOSE),
        "--output",
        str(output),
    ]
    for key, value in values.items():
        command.extend((f"--{key}", value))
    return command


def _render_release(tmp_path: Path, **overrides: str) -> Path:
    output = tmp_path / "release"
    result = subprocess.run(
        _build_command(output, **overrides),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return output


def _manifest(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        assert separator == "="
        assert key and key not in result
        result[key] = value
    return result


def test_release_builder_renders_one_image_only_compose_bundle(tmp_path: Path) -> None:
    output = _render_release(tmp_path)
    manifest = _manifest(output / "manifest.env")
    compose = yaml.safe_load((output / "compose.yml").read_text())

    assert manifest == {
        "STACK_FORMAT_VERSION": "1",
        "STACK_VERSION": "0.10.0",
        "STACK_REVISION": REVISION,
        "DITTOBENCH_REVISION": REVISION,
        "COMPATIBILITY_EPOCH": "2",
        "UPDATE_PROTOCOL": "1",
        "HEARTBEAT_PROTOCOL": "6",
        "COMPOSE_SCHEMA": "1",
        **_images(),
    }
    assert "x-dittobench-build-context" not in compose
    for service, image_key in SERVICE_IMAGE_KEYS.items():
        rendered = compose["services"][service]
        assert rendered["image"] == manifest[image_key]
        assert rendered["pull_policy"] == "never"
        assert "build" not in rendered

    validator_environment = compose["services"]["ditto-subnet"]["environment"]
    assert validator_environment == {
        **yaml.safe_load(COMPOSE.read_text())["services"]["ditto-subnet"][
            "environment"
        ],
        "VALIDATOR_STACK_MODE": "managed",
        "VALIDATOR_STACK_DESCRIPTOR_REF": (
            "${VALIDATOR_STACK_DESCRIPTOR_REF:?validated descriptor ref required}"
        ),
        "VALIDATOR_STACK_VERSION": "0.10.0",
        "VALIDATOR_STACK_REVISION": REVISION,
        "VALIDATOR_STACK_DITTOBENCH_REVISION": REVISION,
        "VALIDATOR_STACK_COMPOSE_SCHEMA": "1",
        "VALIDATOR_STACK_UPDATE_PROTOCOL": "1",
        "VALIDATOR_STACK_COMPONENT_DITTO_SUBNET": manifest["VALIDATOR_IMAGE"],
        "VALIDATOR_STACK_COMPONENT_DITTOBENCH_API": manifest["DITTOBENCH_API_IMAGE"],
        "VALIDATOR_STACK_COMPONENT_SANDBOX_DOCKER": manifest["SANDBOX_DOCKER_IMAGE"],
        "VALIDATOR_STACK_COMPONENT_MODEL_RELAY": manifest["MODEL_RELAY_IMAGE"],
        "VALIDATOR_STACK_COMPONENT_PYLON": manifest["PYLON_IMAGE"],
        "VALIDATOR_STACK_COMPONENT_OLLAMA": manifest["OLLAMA_IMAGE"],
    }

    # A release must be usable without a Git checkout or a build daemon on the
    # validator host. No remote/local build contexts may survive rendering.
    raw = (output / "compose.yml").read_text()
    assert "build:" not in raw
    assert "DITTOBENCH_BUILD_CONTEXT" not in raw
    assert "github.com/ditto-assistant" not in raw


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("version", "v0.10.0", "unprefixed semantic version"),
        ("version", "0.10", "unprefixed semantic version"),
        ("revision", "A" * 40, "full lowercase Git SHA"),
        ("revision", "a" * 39, "full lowercase Git SHA"),
        ("dittobench-revision", "b" * 39, "full lowercase Git SHA"),
        ("compatibility-epoch", "0", "positive integer"),
        ("update-protocol", "-1", "positive integer"),
        ("compose-schema", "invalid", "positive integer"),
        ("heartbeat-protocol", "0", "positive integer"),
        (
            "validator-image",
            "ghcr.io/ditto-assistant/ditto-subnet-validator:latest",
            "immutable sha256 image reference",
        ),
        (
            "dittobench-api-image",
            "ghcr.io/ditto-assistant/dittobench-api-sandbox@sha256:ABC",
            "immutable sha256 image reference",
        ),
    ],
)
def test_release_builder_rejects_ambiguous_identity_fields(
    tmp_path: Path, argument: str, value: str, message: str
) -> None:
    result = subprocess.run(
        _build_command(tmp_path / "release", **{argument: value}),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not (tmp_path / "release/manifest.env").exists()


def test_release_builder_requires_every_managed_service(tmp_path: Path) -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    del compose["services"]["model-relay"]
    incomplete = tmp_path / "incomplete.yml"
    incomplete.write_text(yaml.safe_dump(compose, sort_keys=False))
    command = _build_command(tmp_path / "release")
    command[command.index("--compose") + 1] = str(incomplete)

    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "missing managed services" in result.stderr
    assert "model-relay" in result.stderr
    assert not (tmp_path / "release/manifest.env").exists()


def test_source_compose_cannot_claim_managed_release_identity() -> None:
    validator_environment = yaml.safe_load(COMPOSE.read_text())["services"][
        "ditto-subnet"
    ]["environment"]

    assert validator_environment["VALIDATOR_STACK_MODE"] == "source"
    assert not any(
        key.startswith("VALIDATOR_STACK_") and key != "VALIDATOR_STACK_MODE"
        for key in validator_environment
    )


FAKE_WRAPPER_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["info"]:
    raise SystemExit(0)
if args == ["compose", "version", "--short"]:
    print("2.24.0")
    raise SystemExit(0)
if args[:1] == ["compose"]:
    with open(os.environ["WRAPPER_CAPTURE"], "w") as handle:
        json.dump({
            "descriptor": os.environ.get("VALIDATOR_STACK_DESCRIPTOR_REF"),
            "args": args,
        }, handle)
    raise SystemExit(0)
raise SystemExit("unexpected docker arguments: " + repr(args))
"""


def _run_stack_compose_wrapper(
    tmp_path: Path,
    *,
    descriptor_ref: str = STACK_DIGEST,
    managed_ref: str | None = None,
    transaction_ref: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    state_dir = tmp_path / "state"
    release_dir = state_dir / "current"
    release_dir.mkdir(parents=True)
    rendered = _render_release(tmp_path / "render")
    shutil.copy2(rendered / "manifest.env", release_dir / "manifest.env")
    shutil.copy2(rendered / "compose.yml", release_dir / "compose.yml")
    if descriptor_ref:
        (release_dir / ".descriptor-ref").write_text(descriptor_ref + "\n")
    (state_dir / "managed-release.env").write_text(
        f"STACK_RELEASE={managed_ref or descriptor_ref}\n"
    )
    if transaction_ref:
        (state_dir / "transaction.env").write_text(
            "PHASE=committed\n"
            f"PREVIOUS_RELEASE={managed_ref or OLD_STACK_DIGEST}\n"
            f"CANDIDATE_RELEASE={transaction_ref}\n"
        )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "VALIDATOR_STACK_DESCRIPTOR_REF=operator-spoof\n"
        "VALIDATOR_STACK_VERSION=operator-spoof\n"
    )
    fake_bin = tmp_path / "wrapper-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(FAKE_WRAPPER_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    capture = tmp_path / "wrapper-capture.json"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DITTO_VALIDATOR_STACK_UPDATE_STATE_DIR": str(state_dir),
        "DITTO_SUBNET_ENV_FILE": str(env_file),
        "WRAPPER_CAPTURE": str(capture),
    }
    result = subprocess.run(
        [str(STACK_COMPOSE), str(release_dir), "config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, capture


def test_stack_wrapper_exports_only_recorded_verified_descriptor_state(
    tmp_path: Path,
) -> None:
    result, capture = _run_stack_compose_wrapper(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["descriptor"] == STACK_DIGEST


def test_stack_wrapper_rejects_stale_current_descriptor_state(tmp_path: Path) -> None:
    result, capture = _run_stack_compose_wrapper(tmp_path, managed_ref=OLD_STACK_DIGEST)

    assert result.returncode != 0
    assert "does not match installed state" in result.stderr
    assert not capture.exists()


def test_stack_wrapper_accepts_committed_verified_candidate_before_recording(
    tmp_path: Path,
) -> None:
    result, capture = _run_stack_compose_wrapper(
        tmp_path,
        managed_ref=OLD_STACK_DIGEST,
        transaction_ref=STACK_DIGEST,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["descriptor"] == STACK_DIGEST


def test_stack_wrapper_rejects_unverified_release_directory(tmp_path: Path) -> None:
    result, capture = _run_stack_compose_wrapper(tmp_path, descriptor_ref="")

    assert result.returncode != 0
    assert "no regular .descriptor-ref" in result.stderr
    assert not capture.exists()


def test_stack_updater_contract_is_installed() -> None:
    """Give an immediate, readable failure while the updater is being built."""

    assert UPDATER.is_file(), "whole-stack updater entrypoint is missing"
    assert os.access(UPDATER, os.X_OK), "whole-stack updater must be executable"


def test_stack_bootstrap_persists_registry_wallet_and_signature_context() -> None:
    installer = STACK_INSTALLER.read_text()
    compose = STACK_COMPOSE.read_text()

    assert "command -v cosign" in installer
    assert 'Environment="DOCKER_CONFIG=$docker_config"' in installer
    assert 'Environment="DITTO_BITTENSOR_WALLETS_DIR=$wallets_dir"' in installer
    assert "VALIDATOR_STACK_AUTO_UPDATE=true" in installer
    assert "DITTO_BITTENSOR_WALLETS_DIR" in compose
    assert (
        "managed stack mutation must run through validator-stack-auto-update.sh"
        in compose
    )


FAKE_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import shutil
import sys

state_path = os.environ["FAKE_STACK_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["calls"].append(args)

def save():
    with open(state_path, "w") as handle:
        json.dump(state, handle)

def die(message="fake docker failure"):
    save()
    raise SystemExit(message)

if args[:1] == ["pull"]:
    if args[1] == state.get("fail_pull"):
        die()
elif args[:1] == ["create"]:
    state["extracting"] = args[1]
    print("descriptor-container")
elif args[:1] == ["cp"]:
    source = state["descriptor_dirs"][state["extracting"]]
    destination = args[2]
    for item in os.listdir(source):
        source_path = os.path.join(source, item)
        destination_path = os.path.join(destination, item)
        if os.path.isdir(source_path):
            shutil.copytree(source_path, destination_path)
        else:
            shutil.copy2(source_path, destination_path)
elif args[:2] == ["image", "inspect"]:
    ref = args[-1]
    if "--format" not in args:
        if ref not in state["images"]:
            die("missing image")
    else:
        fmt = args[args.index("--format") + 1]
        if "RepoDigests" in fmt:
            print(state["channel_digest"])
        elif ".Id" in fmt:
            print(state["images"][ref])
        elif "Labels" in fmt:
            label = fmt.split('Labels "', 1)[1].split('"', 1)[0]
            print(state["descriptor_labels"].get(ref, {}).get(label, "<no value>"))
elif args[:1] == ["inspect"]:
    container = state["containers"][args[-1]]
    fmt = args[args.index("--format") + 1]
    if ".Image" in fmt:
        print(container["image"])
    elif ".State.Running" in fmt:
        print("true" if container["running"] else "false")
    elif ".State.Health" in fmt:
        print(container.get("health", "none"))
elif args[:1] == ["exec"]:
    print(json.dumps(state["runtime_state"], separators=(",", ":")))
elif args[:1] == ["kill"]:
    signal_name = next(
        value.split("=", 1)[1]
        for value in args
        if value.startswith("--signal=")
    )
    state["runtime_state"]["state"] = "drained" if signal_name == "USR1" else "working"
elif args[:1] == ["stop"]:
    state["containers"][args[-1]]["running"] = False
elif args[:2] == ["rm", "-f"]:
    pass
else:
    die("unhandled fake docker call: " + repr(args))
save()
"""


FAKE_COMPOSE = r"""#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["FAKE_STACK_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
release_dir = sys.argv[1]
args = sys.argv[2:]
state["compose_calls"].append([release_dir, *args])
state_dir = os.path.realpath(os.environ["DITTO_VALIDATOR_STACK_UPDATE_STATE_DIR"])
release_dir = os.path.realpath(release_dir)
allowed = {os.path.join(state_dir, name) for name in ("current", "previous", "staged")}
if release_dir not in allowed:
    raise SystemExit("release directory is not updater-validated state")
descriptor_path = os.path.join(release_dir, ".descriptor-ref")
if not os.path.isfile(descriptor_path):
    raise SystemExit("validated release has no regular .descriptor-ref")
with open(descriptor_path) as handle:
    descriptor_ref = handle.read().strip()
if not descriptor_ref.startswith(
    "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:"
):
    raise SystemExit("validated release descriptor reference is malformed")
if args[:2] == ["ps", "-q"]:
    print(args[2] + "-container")
elif args[:1] == ["up"]:
    manifest = {}
    with open(os.path.join(release_dir, "manifest.env")) as handle:
        for line in handle:
            key, value = line.rstrip().split("=", 1)
            manifest[key] = value
    mapping = {
        "pylon": "PYLON_IMAGE", "sandbox-docker": "SANDBOX_DOCKER_IMAGE",
        "model-relay": "MODEL_RELAY_IMAGE", "ollama": "OLLAMA_IMAGE",
        "dittobench-api": "DITTOBENCH_API_IMAGE", "ditto-subnet": "VALIDATOR_IMAGE",
    }
    services = [value for value in args if value in mapping]
    for service in services:
        container = state["containers"][service + "-container"]
        container["image"] = state["images"][manifest[mapping[service]]]
        container["running"] = True
        is_candidate = manifest["STACK_VERSION"] == "0.10.1"
        container["health"] = (
            "unhealthy"
            if is_candidate and state.get("fail_health_service") == service
            else "healthy"
        )
    if "ditto-subnet" in services:
        state["runtime_state"]["state"] = "drained"
        state["runtime_state"]["platform_accepted"] = not (
            is_candidate and state.get("fail_validator_acceptance", False)
        )
        state["current_install_version"] = manifest["STACK_VERSION"]
elif args[:2] == ["config", "--services"]:
    print("\n".join((
        "pylon", "sandbox-docker", "model-relay", "ollama",
        "dittobench-api", "ditto-subnet",
    )))
elif args[:2] == ["config", "--images"]:
    with open(os.path.join(release_dir, "manifest.env")) as handle:
        values = dict(line.rstrip().split("=", 1) for line in handle)
    print("\n".join(values[key] for key in (
        "PYLON_IMAGE", "SANDBOX_DOCKER_IMAGE", "MODEL_RELAY_IMAGE",
        "OLLAMA_IMAGE", "DITTOBENCH_API_IMAGE", "VALIDATOR_IMAGE",
    )))
elif args == ["config"]:
    print("services: {}")
else:
    raise SystemExit("unhandled fake compose call: " + repr(args))
with open(state_path, "w") as handle:
    json.dump(state, handle)
"""


FAKE_FLOCK = "#!/bin/sh\nexit 0\n"

FAKE_MV = r"""#!/usr/bin/env python3
import json
import os
import sys

path = os.environ["FAKE_STACK_STATE"]
with open(path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
if state.get("fail_stage_move") and args and args[-1].endswith("/staged"):
    state["failed_stage_moves"] = state.get("failed_stage_moves", 0) + 1
    with open(path, "w") as handle:
        json.dump(state, handle)
    raise SystemExit("injected staged move failure")
os.execv("/bin/mv", ["mv", *args])
"""

FAKE_COSIGN = r"""#!/usr/bin/env python3
import json
import os
import sys

path = os.environ["FAKE_STACK_STATE"]
with open(path) as handle:
    state = json.load(handle)
state.setdefault("cosign_calls", []).append(sys.argv[1:])
with open(path, "w") as handle:
    json.dump(state, handle)
if state.get("fail_cosign"):
    raise SystemExit("signature verification failed")
"""


def _descriptor_labels(version: str) -> dict[str, str]:
    return {
        "io.heyditto.validator.stack-release": "true",
        "org.opencontainers.image.source": (
            "https://github.com/ditto-assistant/ditto-subnet"
        ),
        "io.heyditto.validator.compatibility-epoch": "2",
        "io.heyditto.validator.update-protocol": "1",
        "io.heyditto.validator.compose-schema": "1",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": REVISION,
    }


def _write_release(path: Path, version: str, images: dict[str, str]) -> None:
    path.mkdir()
    manifest = {
        "STACK_FORMAT_VERSION": "1",
        "STACK_VERSION": version,
        "STACK_REVISION": REVISION,
        "DITTOBENCH_REVISION": REVISION,
        "COMPATIBILITY_EPOCH": "2",
        "UPDATE_PROTOCOL": "1",
        "HEARTBEAT_PROTOCOL": "6",
        "COMPOSE_SCHEMA": "1",
        **images,
    }
    (path / "manifest.env").write_text(
        "".join(f"{key}={value}\n" for key, value in manifest.items())
    )
    (path / "compose.yml").write_text("services: {}\n")


@pytest.fixture
def stack_updater_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, contents in {
        "docker": FAKE_DOCKER,
        "stack-compose": FAKE_COMPOSE,
        "flock": FAKE_FLOCK,
        "cosign": FAKE_COSIGN,
        "mv": FAKE_MV,
    }.items():
        executable = fake_bin / name
        executable.write_text(contents)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    old_release = tmp_path / "old-release"
    new_release = tmp_path / "new-release"
    old_images = _images()
    new_images = {
        key: value.rsplit(":", 1)[0] + ":" + "e" * 64
        for key, value in old_images.items()
    }
    _write_release(old_release, "0.10.0", old_images)
    _write_release(new_release, "0.10.1", new_images)
    image_ids = {
        ref: "sha256:" + ("1" if ref in old_images.values() else "2") * 64 + f"-{index}"
        for index, ref in enumerate([*old_images.values(), *new_images.values()])
    }
    containers = {}
    for service in SERVICE_IMAGE_KEYS:
        ref = old_images[SERVICE_IMAGE_KEYS[service]]
        containers[f"{service}-container"] = {
            "image": image_ids[ref],
            "running": True,
            "health": "healthy",
        }
    labels = {
        OLD_STACK_DIGEST: _descriptor_labels("0.10.0"),
        STACK_DIGEST: _descriptor_labels("0.10.1"),
    }
    for version, images in (("0.10.0", old_images), ("0.10.1", new_images)):
        for key, source in {
            "VALIDATOR_IMAGE": "https://github.com/ditto-assistant/ditto-subnet",
            "SANDBOX_DOCKER_IMAGE": ("https://github.com/ditto-assistant/ditto-subnet"),
            "DITTOBENCH_API_IMAGE": (
                "https://github.com/ditto-assistant/dittobench-api"
            ),
            "MODEL_RELAY_IMAGE": ("https://github.com/ditto-assistant/dittobench-api"),
        }.items():
            labels[images[key]] = {
                "org.opencontainers.image.source": source,
                "org.opencontainers.image.revision": REVISION,
                "org.opencontainers.image.version": version,
            }
            if key == "VALIDATOR_IMAGE":
                labels[images[key]]["io.heyditto.validator.heartbeat-protocol"] = "6"
                labels[image_ids[images[key]]] = labels[images[key]]
    state = {
        "calls": [],
        "compose_calls": [],
        "cosign_calls": [],
        "channel_digest": STACK_DIGEST,
        "descriptor_dirs": {
            OLD_STACK_DIGEST: str(old_release),
            STACK_DIGEST: str(new_release),
        },
        "descriptor_labels": labels,
        "images": image_ids,
        "containers": containers,
        "runtime_state": {
            "compatibility_epoch": 2,
            "heartbeat_protocol": 6,
            "platform_accepted": True,
            "state": "working",
            "update_protocol": 1,
        },
        "current_install_version": "0.9.6",
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    state_dir = tmp_path / "updater-state"
    env_file = tmp_path / ".env"
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=false\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_STACK_STATE": str(state_path),
        "DITTO_VALIDATOR_STACK_COMPOSE": str(fake_bin / "stack-compose"),
        "DITTO_VALIDATOR_STACK_UPDATE_STATE_DIR": str(state_dir),
        "DITTO_SUBNET_ENV_FILE": str(env_file),
        "VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS": "1",
        "VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS": "1",
        "VALIDATOR_AUTO_UPDATE_CHECK_SECONDS": "1",
    }
    return env, state_path, state_dir, env_file


def _run_updater(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(UPDATER), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_disabled_stack_updater_does_not_touch_docker(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, _, _ = stack_updater_env

    result = _run_updater(env, "run")

    assert result.returncode == 0, result.stderr
    state = json.loads(state_path.read_text())
    assert state["calls"] == []
    assert state["compose_calls"] == []


def test_supervised_adoption_binds_every_running_service(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, state_dir, _ = stack_updater_env

    result = _run_updater(env, "adopt", OLD_STACK_DIGEST)

    assert result.returncode == 0, result.stderr
    assert (state_dir / "managed-release.env").read_text() == (
        f"STACK_RELEASE={OLD_STACK_DIGEST}\n"
    )
    assert (state_dir / "current/manifest.env").is_file()
    state = json.loads(state_path.read_text())
    inspected = [
        call[-1] for call in state["calls"] if call[:2] == ["image", "inspect"]
    ]
    assert set(_images().values()).issubset(inspected)
    assert not any(call[0] in {"kill", "stop"} for call in state["calls"])


def test_supervised_migration_stages_validated_descriptor_before_drain(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, state_dir, _ = stack_updater_env

    result = _run_updater(env, "migrate", OLD_STACK_DIGEST)

    assert result.returncode == 0, result.stderr
    assert (state_dir / "managed-release.env").read_text() == (
        f"STACK_RELEASE={OLD_STACK_DIGEST}\n"
    )
    assert (state_dir / "current/.descriptor-ref").read_text().strip() == (
        OLD_STACK_DIGEST
    )
    assert not (state_dir / "staged").exists()
    state = json.loads(state_path.read_text())
    assert state["current_install_version"] == "0.10.0"
    signals = [call[1] for call in state["calls"] if call[:1] == ["kill"]]
    assert signals[-2:] == ["--signal=USR1", "--signal=USR2"]
    assert all(
        Path(call[0]).name in {"current", "previous", "staged"}
        for call in state["compose_calls"]
    )


@pytest.mark.parametrize(
    "failure", ["missing", "extra-file", "extra-directory", "invalid"]
)
def test_invalid_extracted_descriptor_is_cleaned_before_migration_drain(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
    failure: str,
) -> None:
    env, state_path, state_dir, _ = stack_updater_env
    state = json.loads(state_path.read_text())
    release = Path(state["descriptor_dirs"][OLD_STACK_DIGEST])
    if failure == "missing":
        (release / "compose.yml").unlink()
    elif failure == "extra-file":
        (release / "unexpected.txt").write_text("unexpected\n")
    elif failure == "extra-directory":
        (release / "unexpected").mkdir()
    else:
        (release / "manifest.env").write_text("not-a-manifest\n")
    before = state["containers"]

    result = _run_updater(env, "migrate", OLD_STACK_DIGEST)

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    assert final["current_install_version"] == "0.9.6"
    assert final["containers"] == before
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any("up" in call for call in final["compose_calls"])
    assert not (state_dir / "managed-release.env").exists()
    assert not (state_dir / "staged").exists()
    assert not list(state_dir.glob("staged.tmp.*"))


def test_staged_descriptor_move_failure_cleans_up_without_drain(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, state_dir, _ = stack_updater_env
    state = json.loads(state_path.read_text())
    before = state["containers"]
    state["fail_stage_move"] = True
    state_path.write_text(json.dumps(state))

    result = _run_updater(env, "migrate", OLD_STACK_DIGEST)

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    assert final["failed_stage_moves"] == 1
    assert final["current_install_version"] == "0.9.6"
    assert final["containers"] == before
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert final["compose_calls"] == []
    assert not (state_dir / "managed-release.env").exists()
    assert not (state_dir / "staged").exists()
    assert not list(state_dir.glob("staged.tmp.*"))


def test_component_pull_failure_happens_before_validator_drain(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, _, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    state["calls"] = []
    state["compose_calls"] = []
    state["fail_pull"] = (
        next(iter(_images().values())).rsplit(":", 1)[0] + ":" + "e" * 64
    )
    state_path.write_text(json.dumps(state))
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any("up" in call for call in final["compose_calls"])


def test_invalid_descriptor_signature_fails_before_validator_drain(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, _, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    state["calls"] = []
    state["compose_calls"] = []
    state["cosign_calls"] = []
    state["fail_cosign"] = True
    state_path.write_text(json.dumps(state))
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    assert final["cosign_calls"]
    assert any(STACK_DIGEST in call for call in final["cosign_calls"])
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any("up" in call for call in final["compose_calls"])


def test_success_replaces_and_commits_the_complete_stack(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, state_dir, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode == 0, result.stderr
    assert (state_dir / "managed-release.env").read_text() == (
        f"STACK_RELEASE={STACK_DIGEST}\n"
    )
    assert _manifest(state_dir / "current/manifest.env")["STACK_VERSION"] == "0.10.1"
    assert _manifest(state_dir / "previous/manifest.env")["STACK_VERSION"] == "0.10.0"
    state = json.loads(state_path.read_text())
    signals = [call[1] for call in state["calls"] if call[:1] == ["kill"]]
    assert signals[-2:] == ["--signal=USR1", "--signal=USR2"]
    up_calls = [call for call in state["compose_calls"] if "up" in call]
    assert len(up_calls) == 2
    assert "ditto-subnet" not in up_calls[0]
    assert up_calls[1][-1] == "ditto-subnet"
    assert state["runtime_state"]["state"] == "working"
    assert "unrelated-container" not in {call[-1] for call in state["calls"] if call}


@pytest.mark.parametrize(
    "failure_service",
    ["pylon", "sandbox-docker", "model-relay", "ollama", "dittobench-api"],
)
def test_unhealthy_candidate_sidecar_rolls_back_every_service_and_is_suppressed(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
    failure_service: str,
) -> None:
    env, state_path, state_dir, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    old_container_images = {
        name: value["image"] for name, value in state["containers"].items()
    }
    state["containers"]["unrelated-container"] = {
        "image": "sha256:unrelated",
        "running": True,
        "health": "healthy",
    }
    state["fail_health_service"] = failure_service
    state_path.write_text(json.dumps(state))
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    for container, image in old_container_images.items():
        assert final["containers"][container]["image"] == image
    assert final["containers"]["unrelated-container"] == {
        "image": "sha256:unrelated",
        "running": True,
        "health": "healthy",
    }
    assert final["runtime_state"]["state"] == "working"
    assert (state_dir / "managed-release.env").read_text() == (
        f"STACK_RELEASE={OLD_STACK_DIGEST}\n"
    )
    assert (state_dir / "failed-candidate").read_text().strip() == STACK_DIGEST
    assert not (state_dir / "transaction.env").exists()

    final["calls"] = []
    final["compose_calls"] = []
    final["fail_health_service"] = None
    state_path.write_text(json.dumps(final))
    retry = _run_updater(env, "run")
    assert retry.returncode == 0, retry.stderr
    assert "candidate suppressed" in retry.stderr
    suppressed = json.loads(state_path.read_text())
    assert not any(call[0] in {"kill", "stop"} for call in suppressed["calls"])
    assert not any("up" in call for call in suppressed["compose_calls"])


def test_candidate_validator_rejection_rolls_back_complete_stack(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, state_dir, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    old_images = {name: value["image"] for name, value in state["containers"].items()}
    state["fail_validator_acceptance"] = True
    state_path.write_text(json.dumps(state))
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    assert {name: value["image"] for name, value in final["containers"].items()} == (
        old_images
    )
    assert final["runtime_state"]["platform_accepted"] is True
    assert final["runtime_state"]["state"] == "working"
    assert (state_dir / "failed-candidate").read_text().strip() == STACK_DIGEST


def test_same_release_digest_is_a_safe_noop(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, _, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    state["channel_digest"] = OLD_STACK_DIGEST
    state["calls"] = []
    state["compose_calls"] = []
    state_path.write_text(json.dumps(state))
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode == 0, result.stderr
    assert "already running stack" in result.stderr
    final = json.loads(state_path.read_text())
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any("up" in call for call in final["compose_calls"])


@pytest.mark.parametrize(
    ("field", "value"),
    [("STACK_VERSION", "1.0.0"), ("COMPOSE_SCHEMA", "2")],
)
def test_incompatible_candidate_is_rejected_before_drain(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
    field: str,
    value: str,
) -> None:
    env, state_path, _, env_file = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    candidate = Path(state["descriptor_dirs"][STACK_DIGEST])
    manifest = _manifest(candidate / "manifest.env")
    manifest[field] = value
    (candidate / "manifest.env").write_text(
        "".join(f"{key}={item}\n" for key, item in manifest.items())
    )
    if field == "STACK_VERSION":
        state["descriptor_labels"][STACK_DIGEST]["org.opencontainers.image.version"] = (
            value
        )
        for ref in manifest.values():
            labels = state["descriptor_labels"].get(ref)
            if labels and "org.opencontainers.image.version" in labels:
                labels["org.opencontainers.image.version"] = value
    state["calls"] = []
    state["compose_calls"] = []
    state_path.write_text(json.dumps(state))
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode != 0
    final = json.loads(state_path.read_text())
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any("up" in call for call in final["compose_calls"])


def test_status_is_network_free_and_lists_the_installed_release_images(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, _, _ = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    state = json.loads(state_path.read_text())
    state["calls"] = []
    state["compose_calls"] = []
    state["cosign_calls"] = []
    state_path.write_text(json.dumps(state))

    result = _run_updater(env, "status")

    assert result.returncode == 0, result.stderr
    assert OLD_STACK_DIGEST in result.stdout
    for image in _images().values():
        assert image in result.stdout
    final = json.loads(state_path.read_text())
    assert final["calls"] == []
    assert final["compose_calls"] == []
    assert final["cosign_calls"] == []


def test_legacy_validator_only_state_is_not_silently_adopted(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, state_path, state_dir, env_file = stack_updater_env
    state_dir.mkdir()
    (state_dir / "managed-image.env").write_text(
        "DITTO_SUBNET_IMAGE=ghcr.io/ditto-assistant/ditto-subnet-validator@"
        f"sha256:{'1' * 64}\n"
    )
    env_file.write_text("VALIDATOR_STACK_AUTO_UPDATE=true\n")

    result = _run_updater(env, "run")

    assert result.returncode != 0
    assert "not adopted" in result.stderr
    assert not (state_dir / "managed-release.env").exists()
    final = json.loads(state_path.read_text())
    assert not any(call[0] in {"kill", "stop"} for call in final["calls"])
    assert not any("up" in call for call in final["compose_calls"])


@pytest.mark.parametrize(
    "phase", ["old_stopped", "candidate_started", "rollback_pending"]
)
def test_recover_restores_the_complete_previous_stack_after_crash_phase(
    stack_updater_env: tuple[dict[str, str], Path, Path, Path],
    phase: str,
) -> None:
    env, state_path, state_dir, _ = stack_updater_env
    adopted = _run_updater(env, "adopt", OLD_STACK_DIGEST)
    assert adopted.returncode == 0, adopted.stderr
    shutil.copytree(state_dir / "current", state_dir / "previous")
    (state_dir / "transaction.env").write_text(
        f"PHASE={phase}\nPREVIOUS_RELEASE={OLD_STACK_DIGEST}\n"
        f"CANDIDATE_RELEASE={STACK_DIGEST}\n"
    )
    state = json.loads(state_path.read_text())
    for container in state["containers"].values():
        container["running"] = False
    state["runtime_state"]["state"] = "drained"
    state["calls"] = []
    state["compose_calls"] = []
    state_path.write_text(json.dumps(state))

    result = _run_updater(env, "recover")

    assert result.returncode == 0, result.stderr
    assert not (state_dir / "transaction.env").exists()
    assert (state_dir / "failed-candidate").read_text().strip() == STACK_DIGEST
    final = json.loads(state_path.read_text())
    assert all(container["running"] for container in final["containers"].values())
    assert final["runtime_state"]["state"] == "working"
    assert len([call for call in final["compose_calls"] if "up" in call]) == 2
