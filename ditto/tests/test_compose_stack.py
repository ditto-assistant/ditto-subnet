"""Regression checks for production Compose invariants."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

COMPOSE_PATH = Path(__file__).parents[2] / "docker-compose.yml"
COMPOSE_WRAPPER_PATH = Path(__file__).parents[2] / "scripts/validator-compose.sh"


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
