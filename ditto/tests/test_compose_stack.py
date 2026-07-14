"""Regression checks for production Compose invariants."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION

COMPOSE_PATH = Path(__file__).parents[2] / "docker-compose.yml"
COMPOSE_WRAPPER_PATH = Path(__file__).parents[2] / "scripts/validator-compose.sh"
SANDBOX_DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile.sandbox-docker"
DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile"
RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"


def test_ollama_is_pinned_with_functional_embedding_healthcheck() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    ollama = compose["services"]["ollama"]

    assert ollama["image"] == (
        "docker.io/ollama/ollama:0.11.10@"
        "sha256:a5409cb903d30f9cd67e9f430dd336ddc9274e16fd78f75b675c42065991b4fd"
    )
    probe = " ".join(ollama["healthcheck"]["test"])
    assert "/api/embed" in probe
    assert "embeddinggemma" in probe
    assert " 200 " in probe


def test_sandbox_image_pins_dind_and_installs_curl() -> None:
    dockerfile = SANDBOX_DOCKERFILE_PATH.read_text()

    assert dockerfile.startswith(
        "FROM docker.io/library/docker:29-dind@"
        "sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515"
    )
    assert "RUN apk add --no-cache curl socat" in dockerfile


def test_sandbox_health_and_validator_preflight_use_forwarded_embedding_route() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    sandbox = compose["services"]["sandbox-docker"]
    validator = compose["services"]["ditto-subnet"]

    probe = " ".join(sandbox["healthcheck"]["test"])
    assert "curl --silent --show-error --max-time 10" in probe
    assert "nc -w" not in probe
    assert "127.0.0.1:11434/api/embed" in probe
    assert "/api/embed" in probe
    assert "embeddinggemma" in probe
    assert "%{http_code}" in probe
    assert "^200$$" in probe
    assert validator["environment"]["VALIDATOR_EMBED_PREFLIGHT_URL"] == (
        "http://sandbox-docker:11434/api/embed"
    )
    entrypoint = (
        COMPOSE_PATH.parent / "scripts/sandbox-docker-entrypoint.sh"
    ).read_text()
    assert "TCP-LISTEN:11434" in entrypoint
    assert "TCP:ollama:11434" in entrypoint
    assert validator["environment"]["VALIDATOR_DITTOBENCH_API_URL"] == (
        "http://sandbox-docker:8000"
    )


def test_dittobench_context_has_one_full_ref_checksum_pin() -> None:
    raw_compose = COMPOSE_PATH.read_text()
    context_prefix = "${DITTOBENCH_BUILD_CONTEXT:-"
    context_line = next(
        line for line in raw_compose.splitlines() if context_prefix in line
    )
    remote_context = context_line.split(context_prefix, 1)[1].removesuffix("}")
    parsed = urlsplit(remote_context)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/ditto-assistant/dittobench-api.git"
    assert query.get("ref") == ["refs/heads/main"]
    assert set(query) == {"ref", "checksum"}
    assert len(query["checksum"]) == 1
    checksum = query["checksum"][0]
    assert len(checksum) == 40
    assert checksum == checksum.lower()
    assert all(character in "0123456789abcdef" for character in checksum)

    compose = yaml.safe_load(raw_compose)
    expected = compose["x-dittobench-build-context"]
    assert compose["services"]["model-relay"]["build"]["context"] == expected
    assert compose["services"]["dittobench-api"]["build"]["context"] == expected
    assert 'git -C "$checkout" fetch' in COMPOSE_WRAPPER_PATH.read_text()


def test_validator_hotkey_access_is_read_only_and_service_scoped() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    services = compose["services"]
    validator = services["ditto-subnet"]

    assert validator["cap_drop"] == ["ALL"]
    assert validator["cap_add"] == ["DAC_READ_SEARCH"]
    assert "cap_add" not in services["pylon"]

    assert len(validator["volumes"]) == 1
    wallet_mount = validator["volumes"][0]
    assert wallet_mount["type"] == "bind"
    assert wallet_mount["read_only"] is True
    assert wallet_mount["bind"]["create_host_path"] is False
    assert wallet_mount["source"].endswith(
        "/wallets/${VALIDATOR_WALLET_NAME}/hotkeys/${VALIDATOR_WALLET_HOTKEY}"
    )
    assert wallet_mount["target"].endswith(
        "/wallets/${VALIDATOR_WALLET_NAME}/hotkeys/${VALIDATOR_WALLET_HOTKEY}"
    )


def test_only_validator_is_an_explicit_auto_update_target() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    services = compose["services"]
    targeted = [
        name
        for name, service in services.items()
        if service.get("labels", {}).get("io.heyditto.validator.auto-update-target")
        == "true"
    ]

    assert targeted == ["ditto-subnet"]
    validator = services["ditto-subnet"]
    assert validator["image"].endswith("ditto-subnet-validator:local}")
    assert validator["pull_policy"] == "build"
    assert validator["build"]["context"] == "."
    assert validator["build"]["args"]["VALIDATOR_COMPATIBILITY_EPOCH"] == "1"
    assert int(validator["build"]["args"]["VALIDATOR_HEARTBEAT_PROTOCOL"]) == (
        HEARTBEAT_PROTOCOL_VERSION
    )
    assert validator["environment"]["VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH"] == "1"
    assert validator["stop_grace_period"] == "80m"
    assert validator["environment"]["VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS"] == "4500"

    # These services are deliberately outside updater scope and retain their
    # existing deployment boundaries/pins.
    for name in (
        "pylon",
        "sandbox-docker",
        "ollama",
        "model-relay",
        "dittobench-api",
    ):
        assert "io.heyditto.validator.auto-update-target" not in services[name].get(
            "labels", {}
        )


def test_validator_image_and_release_channel_share_compatibility_metadata() -> None:
    dockerfile = DOCKERFILE_PATH.read_text()
    workflow = RELEASE_WORKFLOW_PATH.read_text()

    assert "ARG VALIDATOR_COMPATIBILITY_EPOCH=1" in dockerfile
    assert (
        f"ARG VALIDATOR_HEARTBEAT_PROTOCOL={HEARTBEAT_PROTOCOL_VERSION}" in dockerfile
    )
    assert (
        'io.heyditto.validator.compatibility-epoch="$VALIDATOR_COMPATIBILITY_EPOCH"'
        in dockerfile
    )
    assert 'io.heyditto.validator.update-protocol="1"' in dockerfile
    assert (
        'io.heyditto.validator.heartbeat-protocol="$VALIDATOR_HEARTBEAT_PROTOCOL"'
        in dockerfile
    )
    assert 'io.heyditto.validator.compose-schema="1"' in dockerfile

    assert 'COMPATIBILITY_EPOCH: "1"' in workflow
    assert "ditto-subnet-validator" in workflow
    assert ":compat-${{ env.COMPATIBILITY_EPOCH }}" in workflow
    assert ":sha-${{ needs.release.outputs.commit_sha }}" in workflow
    assert "packages: write" in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    assert ":latest" not in workflow
