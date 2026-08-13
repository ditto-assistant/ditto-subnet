"""Regression checks for production Compose invariants."""

import re
from pathlib import Path

import yaml

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION

COMPOSE_PATH = Path(__file__).parents[2] / "docker-compose.yml"
COMPOSE_WRAPPER_PATH = Path(__file__).parents[2] / "scripts/validator-compose.sh"
SANDBOX_DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile.sandbox-docker"
DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile"
RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"
SANDBOX_ENTRYPOINT_PATH = (
    Path(__file__).parents[2] / "scripts/sandbox-docker-entrypoint.sh"
)


def _compose_default(expression: str) -> str:
    """Return the ``:-`` fallback baked into a ``${VAR:-default}`` expression.

    The fleet runs the compose default, so a knob whose default is empty ships
    as a no-op. Tests assert on this value, never on a `.env.example` line.
    """
    _, _, remainder = expression.partition(":-")
    return remainder.rstrip("}")


def test_retired_local_inference_sidecars_are_absent() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    assert "ollama" not in compose["services"]
    assert "model-relay" not in compose["services"]


def test_sandbox_image_pins_compatible_rootful_dind_and_installs_guard_tools() -> None:
    dockerfile = SANDBOX_DOCKERFILE_PATH.read_text()

    assert dockerfile.startswith(
        "FROM docker.io/library/docker:29-dind@"
        "sha256:66d292e5c26bd33a6f6f61cacb880de2186339a524ecba1ce098dbbaceed6515"
    )
    assert "RUN apk add --no-cache curl socat" in dockerfile


def test_sandbox_health_reports_the_isolated_daemon() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    sandbox = compose["services"]["sandbox-docker"]
    probe = " ".join(sandbox["healthcheck"]["test"])
    assert "docker info" in probe
    assert "docker info >/dev/null 2>&1" in probe


def test_v9_private_projection_volume_is_initialized_before_scorer_health() -> None:
    """The scorer must not finish 351 V9 cases and then discover no storage.

    The trusted rootful sandbox service initializes one durable named volume;
    the scorer receives that exact volume and path only after the sandbox is
    healthy. No miner or public validator container can mount the replay keys.
    """
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    services = compose["services"]
    volume_name = "dittobench-private-artifacts"
    target = "/var/lib/dittobench-private-artifacts"

    assert volume_name in compose["volumes"]
    for service_name in ("sandbox-docker", "dittobench-api"):
        service = services[service_name]
        assert service["environment"]["DITTOBENCH_PRIVATE_ARTIFACT_DIR"] == target
        assert f"{volume_name}:{target}" in service["volumes"]

    for service_name, service in services.items():
        if service_name in {"sandbox-docker", "dittobench-api"}:
            continue
        serialized = "\n".join(str(item) for item in service.get("volumes", []))
        assert volume_name not in serialized
        assert target not in serialized

    scorer = services["dittobench-api"]
    assert scorer["read_only"] is True
    assert scorer["depends_on"]["sandbox-docker"]["condition"] == "service_healthy"

    entrypoint = SANDBOX_ENTRYPOINT_PATH.read_text()
    assert 'chown 65532:65532 "$private_artifact_dir"' in entrypoint
    assert 'chmod 0700 "$private_artifact_dir"' in entrypoint
    assert entrypoint.index('chmod 0700 "$private_artifact_dir"') < entrypoint.index(
        "exec dockerd-entrypoint.sh"
    )


def test_compatibility_executor_does_not_require_host_specific_outer_limits() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    sandbox = compose["services"]["sandbox-docker"]

    assert "mem_limit" not in sandbox
    assert "cpus" not in sandbox
    assert "pids_limit" not in sandbox


def test_validator_probes_the_live_sandbox_network_namespace() -> None:
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    validator = services["ditto-subnet"]["environment"]

    assert (
        validator["VALIDATOR_SANDBOX_DOCKER_PROBE_URL"]
        == "http://sandbox-docker:8000/health"
    )
    assert ":11434" not in validator["VALIDATOR_SANDBOX_DOCKER_PROBE_URL"]


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


def test_inference_control_plane_shares_one_self_supplied_token() -> None:
    """The validator can only reach the scorer's control plane with a bearer.

    dittobench-api joins sandbox-docker's network namespace; the validator does
    not, so its ``/v1/inference/session`` call arrives from the Compose bridge
    and never on loopback. v0.29.5 shipped with the token unset on both sides,
    which 401'd every v7 activation. Both sides must therefore carry the SAME
    value, and it must have a default: validators auto-update without an
    operator ever touching the host, so a fix that needs a hand-set secret
    would never take effect.
    """
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    scorer = services["dittobench-api"]["environment"][
        "DITTOBENCH_BROKER_CONTROL_TOKEN"
    ]
    validator = services["ditto-subnet"]["environment"][
        "VALIDATOR_DITTOBENCH_CONTROL_TOKEN"
    ]

    assert scorer == validator
    # One interpolation with a non-empty default: operator-overridable, but
    # never empty on an untouched host.
    assert scorer.startswith("${DITTOBENCH_BROKER_CONTROL_TOKEN:-")
    assert scorer.endswith("}")
    assert len(scorer.removeprefix("${DITTOBENCH_BROKER_CONTROL_TOKEN:-")[:-1]) >= 16


def test_scorer_allows_only_verified_screened_image_downloads() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    environment = compose["services"]["dittobench-api"]["environment"]

    assert environment["DITTOBENCH_ALLOW_SCREENED_IMAGES"] == "1"
    assert "DITTOBENCH_ALLOW_PRIVATE_HARNESS" not in environment


def test_sandbox_daemon_prunes_old_unused_build_data() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    sandbox = compose["services"]["sandbox-docker"]
    entrypoint = (
        COMPOSE_PATH.parent / "scripts/sandbox-docker-entrypoint.sh"
    ).read_text()

    assert set(sandbox["volumes"]) == {
        "sandbox-docker-rootful-data:/var/lib/docker",
        "openrouter-shim-ca:/var/lib/dittobench-openrouter-shim",
        "dittobench-private-artifacts:/var/lib/dittobench-private-artifacts",
    }
    assert "security_opt" not in sandbox
    # `docker system prune` also removes unused networks, which would delete the
    # idle ditto-sandbox bridge between benchmarks and break every harness run.
    # Reclaim containers, images, and build cache explicitly; never system-prune.
    assert "docker system prune" not in entrypoint
    assert "docker network prune" not in entrypoint
    assert "docker container prune --force" in entrypoint
    assert "docker image prune --all --force --filter 'until=24h'" in entrypoint
    assert "docker builder prune --all --force" in entrypoint
    assert "docker volume prune --all --force" in entrypoint
    assert "sleep 21600" in entrypoint
    assert "dockerd-entrypoint.sh --feature containerd-snapshotter=false" in entrypoint
    assert entrypoint.index("ensure_sandbox_network") < entrypoint.index(
        "docker container prune --force"
    )
    assert "--dport 11436 -j ACCEPT" in entrypoint
    assert '--dport "$openrouter_shim_port" -j ACCEPT' in entrypoint
    assert "--dport 11434 -j ACCEPT" not in entrypoint
    assert "--dport 11435 -j ACCEPT" not in entrypoint


def test_sandbox_egress_policy_installs_before_dockerd_starts() -> None:
    entrypoint = SANDBOX_ENTRYPOINT_PATH.read_text()

    policy_install = "iptables -I DOCKER-USER 1 -i ditto-sandbox0"
    daemon_start = "exec dockerd-entrypoint.sh"
    assert policy_install in entrypoint
    assert daemon_start in entrypoint
    assert entrypoint.index(policy_install) < entrypoint.index(daemon_start)
    assert "docker network prune" not in entrypoint


def test_untrusted_runtime_fails_closed_and_uses_restricted_network() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    service = compose["services"]["dittobench-api"]
    env = service["environment"]
    assert env["DITTOBENCH_ALLOW_SCREENED_IMAGES"] == "1"
    assert "DITTOBENCH_REQUIRE_SCREENED_IMAGE" not in env
    assert env["DITTOBENCH_SANDBOX_HARDEN"] == "1"
    assert env["DITTOBENCH_SANDBOX_EGRESS_NETWORK"] == "ditto-sandbox"
    # The API and daemon share one outer namespace. The rootful compatibility
    # daemon resolves the broker through the namespace-local host gateway.
    assert "extra_hosts" not in service
    assert compose["services"]["sandbox-docker"]["extra_hosts"] == [
        "host.docker.internal:127.0.0.1"
    ]
    assert env["DITTOBENCH_REQUIRE_TICKET_INFERENCE"] == "true"
    assert env["DITTOBENCH_PLATFORM_INFERENCE_PROXY_URL"] == (
        "${DITTOBENCH_PLATFORM_INFERENCE_PROXY_URL:-"
        "https://dittobench.ai/api/v1/inference/chat/completions}"
    )
    assert env["DITTOBENCH_PLATFORM_INFERENCE_TRANSPORT_URL"] == (
        "${DITTOBENCH_PLATFORM_INFERENCE_TRANSPORT_URL:-"
        "https://platform-api.heyditto.ai/api/v1/inference/chat/completions}"
    )
    assert env["DITTOBENCH_EMBEDDING_UPSTREAM_URL"] == (
        "${DITTOBENCH_EMBEDDING_UPSTREAM_URL:-"
        "https://dittobench.ai/api/v1/inference/embeddings}"
    )

    api = compose["services"]["dittobench-api"]
    assert "DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES" not in api["environment"]
    # The worker fails closed and claims nothing at all when the scorer
    # advertises fewer full-run slots than the worker configured, so these two
    # must ride the same variable with the same default. A host that sets
    # nothing must land on the production value, not on the old behaviour.
    # That default is the protocol maximum on purpose: the platform's cap can
    # narrow an advertised eight to anything in [1, 8] within seconds and with
    # no release, but it can never widen past what the validator advertises, so
    # advertising less makes the fleet-wide dial work in one direction only.
    assert api["environment"]["DITTOBENCH_MAX_CONCURRENT_RUNS"] == (
        "${VALIDATOR_BENCHMARK_CAPACITY:-8}"
    )
    worker_env = compose["services"]["ditto-subnet"]["environment"]
    assert worker_env["VALIDATOR_BENCHMARK_CAPACITY"] == (
        "${VALIDATOR_BENCHMARK_CAPACITY:-8}"
    )
    assert _compose_default(
        api["environment"]["DITTOBENCH_MAX_CONCURRENT_RUNS"]
    ) == _compose_default(worker_env["VALIDATOR_BENCHMARK_CAPACITY"])
    assert "RELAY_API_KEY" not in env
    assert "model-relay" not in service["depends_on"]

    entrypoint = (
        COMPOSE_PATH.parent / "scripts/sandbox-docker-entrypoint.sh"
    ).read_text()
    assert "su-exec rootless" not in entrypoint
    assert "DITTO-SANDBOX-EGRESS" in entrypoint
    assert "--dport 11436 -j ACCEPT" in entrypoint
    assert '--dport "$openrouter_shim_port" -j ACCEPT' in entrypoint
    assert "--dport 11434 -j ACCEPT" not in entrypoint
    assert "--dport 11435 -j ACCEPT" not in entrypoint
    assert "ditto-sandbox-deny" in entrypoint
    # Denied egress must fail fast: a silent DROP costs a full SYN timeout per
    # connect (~53 s of dead time per benchmark check), which burns the whole
    # ticket and starves the scoring slot. The allowlist is unchanged.
    assert "-j DROP" not in entrypoint
    assert "-p tcp -j REJECT --reject-with tcp-reset" in entrypoint
    assert "-j REJECT --reject-with icmp-port-unreachable" in entrypoint
    assert "/var/run/docker.sock" not in COMPOSE_PATH.read_text()


def test_hardcoded_openrouter_https_is_shimmed_into_the_bound_broker() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    services = compose["services"]
    scorer = services["dittobench-api"]
    sandbox = services["sandbox-docker"]
    entrypoint = (
        COMPOSE_PATH.parent / "scripts/sandbox-docker-entrypoint.sh"
    ).read_text()

    ca_path = "/var/lib/dittobench-openrouter-shim/ca-bundle.pem"
    assert scorer["environment"]["DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH"] == ca_path
    assert (
        sandbox["environment"]["DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH"] == ca_path
    )
    assert scorer["environment"]["DITTOBENCH_OPENROUTER_SHIM_PORT"] == "11437"
    assert sandbox["environment"]["DITTOBENCH_OPENROUTER_SHIM_PORT"] == "11437"
    shared_mount = "openrouter-shim-ca:/var/lib/dittobench-openrouter-shim"
    assert shared_mount in scorer["volumes"]
    assert shared_mount in sandbox["volumes"]
    assert "chown 65532:65532" in entrypoint

    # Only host-local port 443 is redirected. Public HTTPS is still rejected;
    # Docker's per-run /etc/hosts entry makes openrouter.ai the one local name.
    assert "-m addrtype --dst-type LOCAL -p tcp --dport 443" in entrypoint
    assert '-j REDIRECT --to-ports "$openrouter_shim_port"' in entrypoint
    assert "-t nat -I PREROUTING 1 -i 'dtj+'" in entrypoint
    assert "-t nat -I PREROUTING 1 -i ditto-sandbox0" in entrypoint


def test_screened_image_capability_is_enabled_without_a_global_requirement() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    scorer = compose["services"]["dittobench-api"]["environment"]
    validator = compose["services"]["ditto-subnet"]["environment"]

    assert scorer["DITTOBENCH_ALLOW_SCREENED_IMAGES"] == "1"
    assert "DITTOBENCH_REQUIRE_SCREENED_IMAGE" not in scorer
    assert validator["VALIDATOR_SCREENED_IMAGES"] == "1"
    assert "VALIDATOR_REQUIRE_SCREENED_IMAGE" not in validator
    assert validator["NETUID"] == "118"
    assert validator["VALIDATOR_STACK_MODE"] == "source"
    assert validator["VALIDATOR_EXECUTOR_ISOLATION"] == "privileged_dind"


def test_dittobench_context_is_the_local_monorepo_service() -> None:
    raw_compose = COMPOSE_PATH.read_text()
    compose = yaml.safe_load(raw_compose)
    assert compose["x-dittobench-build-context"] == "${DITTOBENCH_BUILD_CONTEXT:-.}"
    assert compose["x-dittobench-revision"] == (
        "${DITTOBENCH_SOURCE_REVISION:-source-build}"
    )
    assert (
        compose["services"]["dittobench-api"]["build"]["context"]
        == (compose["x-dittobench-build-context"])
    )
    assert compose["services"]["dittobench-api"]["build"]["dockerfile"] == (
        "services/dittobench-api/Dockerfile"
    )
    validator_environment = compose["services"]["ditto-subnet"]["environment"]
    assert validator_environment["VALIDATOR_STACK_COMPONENT_DITTOBENCH_API"] == (
        "${DITTOBENCH_SOURCE_IDENTITY:-source:source-build}"
    )
    assert "VALIDATOR_STACK_COMPONENT_MODEL_RELAY" not in validator_environment
    scorer_environment = compose["services"]["dittobench-api"]["environment"]
    assert (
        scorer_environment["DITTOBENCH_SOURCE_SHA"]
        == (compose["x-dittobench-revision"])
    )
    assert "DITTOBENCH_SOURCE_SHA: *dittobench-revision" in raw_compose
    wrapper = COMPOSE_WRAPPER_PATH.read_text()
    assert 'dittobench_context="$ROOT_DIR"' in wrapper
    assert "research/dittobench-datagen" in wrapper
    assert 'export DITTOBENCH_SOURCE_REVISION="$checksum"' in wrapper
    assert 'export DITTOBENCH_SOURCE_IDENTITY="source:$checksum"' in wrapper
    assert 'export DITTOBENCH_SOFTWARE_VERSION="$software_version"' in wrapper
    assert '"$ROOT_DIR/pyproject.toml"' in wrapper
    assert "DittoBench source has local changes" in wrapper
    assert "materialize_context=false" in wrapper


def test_scorer_identity_is_stamped_into_the_image_it_is_built_from() -> None:
    """The revision the scorer reports must be a property of the build.

    v0.29.3 shipped ``DITTOBENCH_SOURCE_SHA`` as an environment value only, so
    `docker compose up -d` recreated the scorer with the new pin's environment
    while reusing the cached image: the scorer asserted a revision it had never
    been compiled from and the validator's identity check happily passed.
    """
    raw_compose = COMPOSE_PATH.read_text()
    compose = yaml.safe_load(raw_compose)
    checksum = compose["x-dittobench-revision"]
    software_version = compose["x-dittobench-software-version"]

    for service in ("dittobench-api",):
        build_args = compose["services"][service]["build"]["args"]
        assert build_args["DITTOBENCH_SOURCE_SHA"] == checksum
        assert build_args["DITTOBENCH_SOFTWARE_VERSION"] == software_version

    # The environment fallback must come from the same anchor as the build
    # argument, so the two can never name different commits.
    scorer_environment = compose["services"]["dittobench-api"]["environment"]
    assert scorer_environment["DITTOBENCH_SOURCE_SHA"] == checksum
    assert scorer_environment["DITTOBENCH_SOFTWARE_VERSION"] == software_version

    # Published release images must be stamped too. A managed stack pulls them
    # by digest and never builds, so if the release build skipped the arguments
    # the whole managed fleet would report environment-asserted provenance.
    workflow = RELEASE_WORKFLOW_PATH.read_text()
    assert (
        workflow.count(
            "DITTOBENCH_SOURCE_SHA=${{ steps.dittobench-source.outputs.revision }}"
        )
        == 1
    )
    assert (
        workflow.count(
            "DITTOBENCH_SOFTWARE_VERSION=${{ needs.release.outputs.version }}"
        )
        == 1
    )


def test_stack_update_rebuilds_the_scorer_only_when_the_pin_moves() -> None:
    wrapper = COMPOSE_WRAPPER_PATH.read_text()

    # Keyed on the pin: a changed pin forces a real build + a container
    # replacement, and an unchanged pin costs nothing.
    assert 'built_revision_file="$STATE_DIR/dittobench-built-revision"' in wrapper
    assert 'if [ "$built_revision" != "$checksum" ]; then' in wrapper
    assert "build --pull dittobench-api" in wrapper
    assert "up -d --no-deps --no-build dittobench-api" in wrapper
    assert 'printf \'%s\\n\' "$checksum" > "$built_revision_file"' in wrapper


def test_validator_requires_binary_provenance_from_the_pinned_scorer() -> None:
    """The requirement is committed beside the pin and moves with it.

    The pinned dittobench-api revision links its identity into the binary, so
    the validator can insist on compiled-in evidence instead of trusting an
    environment variable that a recreated container makes say anything.
    """
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    environment = compose["services"]["ditto-subnet"]["environment"]

    assert environment["VALIDATOR_SCORER_REQUIRE_BINARY_PROVENANCE"] == "1"


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


def test_validator_service_pins_build_and_runtime_contract() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    services = compose["services"]
    # The legacy single-service updater's scoping label went away with it.
    for service in services.values():
        assert "io.heyditto.validator.auto-update-target" not in service.get(
            "labels", {}
        )

    validator = services["ditto-subnet"]
    assert validator["image"].endswith("ditto-subnet-validator:local}")
    assert validator["pull_policy"] == "build"
    assert validator["build"]["context"] == "."
    assert validator["build"]["args"]["DITTO_REVISION"] == (
        "${DITTO_SOURCE_REVISION:-local}"
    )
    assert validator["build"]["args"]["VALIDATOR_COMPATIBILITY_EPOCH"] == "2"
    assert int(validator["build"]["args"]["VALIDATOR_HEARTBEAT_PROTOCOL"]) == (
        HEARTBEAT_PROTOCOL_VERSION
    )
    assert validator["environment"]["VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH"] == "2"
    assert validator["stop_grace_period"] == "80m"
    assert validator["environment"]["VALIDATOR_SWEEP_SECONDS"] == "30"
    assert validator["environment"]["VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS"] == "4500"
    wrapper = COMPOSE_WRAPPER_PATH.read_text()
    assert 'git -C "$ROOT_DIR" rev-parse HEAD' in wrapper
    assert 'export DITTO_SOURCE_REVISION="$source_revision"' in wrapper
    assert 'export DITTO_SOURCE_IDENTITY="local-source:$source_revision"' in wrapper


def test_source_stack_builds_pylon_with_the_reviewed_turbobt_fix() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    pylon = compose["services"]["pylon"]

    assert pylon["image"] == "${PYLON_IMAGE:-ditto-subnet-pylon:local}"
    assert pylon["pull_policy"] == "build"
    assert pylon["build"]["dockerfile"] == "Dockerfile.pylon"
    context = pylon["build"]["additional_contexts"]["turbobt"]
    assert context.startswith(
        "https://github.com/ditto-assistant/turbobt.git?"
        "ref=refs/heads/ditto/subscription-id-str&checksum="
    )
    revision = context.rsplit("checksum=", 1)[1]
    assert len(revision) == 40
    assert pylon["build"]["args"]["PYLON_BASE_IMAGE"].startswith(
        "docker.io/backenddevelopersltd/bittensor-pylon@sha256:"
    )
    assert (
        compose["services"]["ditto-subnet"]["environment"][
            "VALIDATOR_STACK_COMPONENT_PYLON"
        ]
        == f"source:{revision}"
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
    # QEMU emulation is required for the arm64 legs of the release, and it must
    # stay pinned to an immutable commit rather than a floating tag. The exact
    # commit is Dependabot's to move, so assert the pinning discipline only.
    assert re.search(r"docker/setup-qemu-action@[0-9a-f]{40}\b", workflow)
    # Validator, sandbox daemon, scorer, patched Pylon, the frozen-updater relay
    # shim, and the final signed stack descriptor are independently built and
    # published from the exact release.
    build_action_count = sum(
        workflow.count(action)
        for action in (
            "docker/build-push-action@",
            "useblacksmith/build-push-action@",
        )
    )
    assert build_action_count == 6
    for repository in (
        "ghcr.io/ditto-assistant/ditto-subnet-validator",
        "ghcr.io/ditto-assistant/ditto-subnet-sandbox-docker",
        "ghcr.io/ditto-assistant/ditto-subnet-pylon",
        "ghcr.io/ditto-assistant/dittobench-api-sandbox",
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


def test_hosted_v7_parallelism_remains_tunable() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    env = compose["services"]["dittobench-api"]["environment"]

    assert "DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES" not in env

    # The hosted knobs are exposed, under distinct anchors, and default to
    # empty so the scorer image's own shipped defaults govern. An empty value
    # is read as unset by envIntDefault, so this can never pin them to zero.
    assert env["DITTOBENCH_V7_CASE_CONCURRENCY"] == "${VALIDATOR_V7_CASE_CONCURRENCY:-}"
    assert env["DITTOBENCH_V7_EMBEDDING_CONCURRENCY"] == (
        "${VALIDATOR_V7_EMBEDDING_CONCURRENCY:-}"
    )

    assert (
        env["DITTOBENCH_V7_CASE_CONCURRENCY"]
        != env["DITTOBENCH_V7_EMBEDDING_CONCURRENCY"]
    )
