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
INSTALLER_PATH = Path(__file__).parents[2] / "scripts/install-validator-auto-update.sh"


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


def test_scorer_capability_probe_needs_no_operator_secret() -> None:
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]

    assert (
        services["ditto-subnet"]["environment"][
            "VALIDATOR_DITTOBENCH_CAPABILITIES_TIMEOUT_SECONDS"
        ]
        == "3"
    )
    assert all(
        "DITTOBENCH_CAPABILITIES_TOKEN" not in service.get("environment", {})
        for service in services.values()
    )


def test_sandbox_daemon_prunes_old_unused_build_data() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    sandbox = compose["services"]["sandbox-docker"]
    entrypoint = (
        COMPOSE_PATH.parent / "scripts/sandbox-docker-entrypoint.sh"
    ).read_text()

    assert sandbox["volumes"] == ["sandbox-docker-rootful-data:/var/lib/docker"]
    assert "docker system prune --all --force --filter 'until=24h'" in entrypoint
    assert "docker volume prune --all --force" in entrypoint
    assert "sleep 21600" in entrypoint
    assert "/var/run/docker.sock" not in COMPOSE_PATH.read_text()


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
    assert services["pylon"]["cap_drop"] == ["ALL"]
    assert services["pylon"]["cap_add"] == ["DAC_READ_SEARCH"]

    assert len(validator["volumes"]) == 2
    bootstrap_mount = next(
        mount
        for mount in validator["volumes"]
        if mount["target"] == "/var/lib/ditto-validator-update"
    )
    assert bootstrap_mount == {
        "type": "volume",
        "source": "validator-update-bootstrap",
        "target": "/var/lib/ditto-validator-update",
        "read_only": False,
    }
    assert "validator-update-bootstrap" in compose["volumes"]

    wallet_mount = next(
        mount for mount in validator["volumes"] if mount["type"] == "bind"
    )
    assert wallet_mount["type"] == "bind"
    assert wallet_mount["read_only"] is True
    assert wallet_mount["bind"]["create_host_path"] is False
    assert wallet_mount["source"] == (
        "${DITTO_BITTENSOR_WALLETS_DIR:-~/.bittensor/wallets}/"
        "${VALIDATOR_WALLET_NAME}/hotkeys/${VALIDATOR_WALLET_HOTKEY}"
    )
    assert wallet_mount["target"].endswith(
        "/wallets/${VALIDATOR_WALLET_NAME}/hotkeys/${VALIDATOR_WALLET_HOTKEY}"
    )

    pylon_wallet_mount = next(
        mount
        for mount in services["pylon"]["volumes"]
        if mount["target"] == "/root/.bittensor/wallets"
    )
    assert pylon_wallet_mount == {
        "type": "bind",
        "source": "${DITTO_BITTENSOR_WALLETS_DIR:-~/.bittensor/wallets}",
        "target": "/root/.bittensor/wallets",
        "read_only": True,
        "bind": {"create_host_path": False},
    }


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
    assert validator["build"]["args"]["VALIDATOR_COMPATIBILITY_EPOCH"] == "2"
    assert int(validator["build"]["args"]["VALIDATOR_HEARTBEAT_PROTOCOL"]) == (
        HEARTBEAT_PROTOCOL_VERSION
    )
    assert validator["environment"]["VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH"] == "2"
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

    assert "ARG VALIDATOR_COMPATIBILITY_EPOCH=2" in dockerfile
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

    assert 'COMPATIBILITY_EPOCH: "2"' in workflow
    assert f'HEARTBEAT_PROTOCOL: "{HEARTBEAT_PROTOCOL_VERSION}"' in workflow
    assert (
        "io.heyditto.validator.heartbeat-protocol" in workflow
        and '"$exact")" = "$HEARTBEAT_PROTOCOL"' in workflow
    )
    assert "ditto-subnet-validator" in workflow
    assert "STACK_REPOSITORY: ghcr.io/ditto-assistant/ditto-subnet-stack" in workflow
    assert '--tag "$STACK_REPOSITORY:compat-$COMPATIBILITY_EPOCH"' in workflow
    assert ":sha-${{ needs.release.outputs.commit_sha }}" in workflow
    assert "packages: write" in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    assert ":latest" not in workflow
    assert "--read-only --tmpfs /tmp \\" in workflow
    assert "--cap-drop ALL --cap-add DAC_READ_SEARCH" in workflow
    assert "target=/var/lib/ditto-validator-update" in workflow
    assert (
        "docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130" in workflow
    )
    # Validator, sandbox daemon, scorer, relay, and the final signed stack
    # descriptor are independently built and published from the exact release.
    build_action_count = sum(
        workflow.count(action)
        for action in (
            "docker/build-push-action@",
            "useblacksmith/build-push-action@",
        )
    )
    assert build_action_count == 5
    for repository in (
        "ghcr.io/ditto-assistant/ditto-subnet-validator",
        "ghcr.io/ditto-assistant/ditto-subnet-sandbox-docker",
        "ghcr.io/ditto-assistant/dittobench-api-sandbox",
        "ghcr.io/ditto-assistant/dittobench-api-relay",
        "ghcr.io/ditto-assistant/ditto-subnet-stack",
    ):
        assert repository in workflow
    assert (
        'raw="$(docker buildx imagetools inspect --raw "$repository@$digest")"'
        in workflow
    )
    assert 'child="$repository@$amd64_digest"' in workflow
    assert 'docker pull --platform linux/amd64 "$child"' in workflow
    assert 'exact="$STACK_REPOSITORY@$STACK_DIGEST"' in workflow
    assert 'child_exact="$STACK_REPOSITORY@$child_digest"' in workflow
    assert 'docker pull --platform "$platform" "$child_exact"' in workflow
    assert "for platform in linux/amd64 linux/arm64" in workflow
    assert "Promote only the authenticated stack descriptor" in workflow


def test_systemd_unit_pins_runtime_settings_to_its_timeout_budget() -> None:
    installer = INSTALLER_PATH.read_text()

    for setting in (
        "VALIDATOR_AUTO_UPDATE_DRAIN_TIMEOUT_SECONDS",
        "VALIDATOR_AUTO_UPDATE_READY_TIMEOUT_SECONDS",
        "VALIDATOR_AUTO_UPDATE_CHECK_SECONDS",
    ):
        assert f"Environment={setting}=" in installer
        assert (f"{setting}= " + "\\") in installer
    assert "Environment=DITTO_SUBNET_ENV_FILE=" in installer
    assert 'Environment="HOME=$service_home"' in installer
    assert 'Environment="DOCKER_CONFIG=$docker_config"' in installer
    assert 'Environment="XDG_CONFIG_HOME=$xdg_config_home"' in installer
    assert 'Environment="DITTO_BITTENSOR_WALLETS_DIR=$wallets_dir"' in installer
    assert "infer_running_wallets_dir" in installer
    assert "io.heyditto.validator.auto-update-target=true" in installer
    assert "*$'\\n'* | *$'\\r'* | *'\"'* | *\\\\* | *'%'*)" in installer
    assert "*'\\\\'*" not in installer
    assert "TimeoutStartSec=${start_timeout_seconds}s" in installer
    assert "TimeoutStopSec=${stop_timeout_seconds}s" in installer


def test_installer_repairs_private_updater_state_ownership() -> None:
    installer = INSTALLER_PATH.read_text()

    assert 'install -d -m 0700 -o "$service_user" -g "$service_group"' in installer
    assert 'find "$state_dir" -type l -print -quit' in installer
    assert 'chown -R "$service_user:$service_group" "$state_dir"' in installer
    assert 'find "$state_dir" -type d -exec chmod 0700 {} +' in installer
    assert 'find "$state_dir" -type f -exec chmod 0600 {} +' in installer
