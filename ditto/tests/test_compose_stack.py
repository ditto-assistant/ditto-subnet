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


def _compose_default(expression: str) -> str:
    """Return the ``:-`` fallback baked into a ``${VAR:-default}`` expression.

    The fleet runs the compose default, so a knob whose default is empty ships
    as a no-op. Tests assert on this value, never on a `.env.example` line.
    """
    _, _, remainder = expression.partition(":-")
    return remainder.rstrip("}")


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

    assert sandbox["volumes"] == ["sandbox-docker-rootful-data:/var/lib/docker"]
    # `docker system prune` also removes unused networks, which would delete the
    # idle ditto-sandbox bridge between benchmarks and break every harness run.
    # Reclaim containers, images, and build cache explicitly; never system-prune.
    assert "docker system prune" not in entrypoint
    assert "docker container prune --force" in entrypoint
    assert "docker image prune --all --force --filter 'until=24h'" in entrypoint
    assert "docker builder prune --all --force" in entrypoint
    assert "docker volume prune --all --force" in entrypoint
    assert "sleep 21600" in entrypoint


def test_untrusted_runtime_fails_closed_and_uses_restricted_network() -> None:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    service = compose["services"]["dittobench-api"]
    env = service["environment"]
    assert env["DITTOBENCH_ALLOW_SCREENED_IMAGES"] == "1"
    assert "DITTOBENCH_REQUIRE_SCREENED_IMAGE" not in env
    assert env["DITTOBENCH_SANDBOX_HARDEN"] == "1"
    assert env["DITTOBENCH_SANDBOX_EGRESS_NETWORK"] == "ditto-sandbox"
    # The scorer reaches the v6 relay / embedding forwarders on loopback, so
    # host.docker.internal must resolve there. The scorer joins sandbox-docker's
    # netns and SHARES its /etc/hosts, so the mapping lives on sandbox-docker (an
    # extra_hosts on the joining dittobench-api service would be ignored).
    assert "extra_hosts" not in service
    assert (
        "host.docker.internal:127.0.0.1"
        in compose["services"]["sandbox-docker"]["extra_hosts"]
    )
    assert env["HARNESS_GATEWAY_URL"] == "http://host.docker.internal:11435"
    assert env["DITTOBENCH_REQUIRE_TICKET_INFERENCE"] == (
        "${DITTOBENCH_REQUIRE_TICKET_INFERENCE:-false}"
    )
    relay = compose["services"]["model-relay"]
    assert relay["environment"]["RELAY_PROVIDER"] == "${RELAY_PROVIDER:-chutes}"
    assert relay["environment"]["RELAY_ENABLE_DEPRECATED_CHUTES"] == (
        "${RELAY_ENABLE_DEPRECATED_CHUTES:-1}"
    )

    api = compose["services"]["dittobench-api"]
    assert api["environment"]["DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES"] == (
        "${VALIDATOR_BENCHMARK_MEMORY_CAPACITY:-1}"
    )
    # The worker fails closed and claims nothing at all when the scorer
    # advertises fewer full-run slots than the worker configured, so these two
    # must ride the same variable with the same default. A host that sets
    # nothing must land on the production value, not on the old behaviour.
    assert api["environment"]["DITTOBENCH_MAX_CONCURRENT_RUNS"] == (
        "${VALIDATOR_BENCHMARK_CAPACITY:-4}"
    )
    worker_env = compose["services"]["ditto-subnet"]["environment"]
    assert worker_env["VALIDATOR_BENCHMARK_CAPACITY"] == (
        "${VALIDATOR_BENCHMARK_CAPACITY:-4}"
    )
    assert _compose_default(
        api["environment"]["DITTOBENCH_MAX_CONCURRENT_RUNS"]
    ) == _compose_default(worker_env["VALIDATOR_BENCHMARK_CAPACITY"])
    assert "RELAY_API_KEY" in relay["environment"]
    assert "model-relay" in service["depends_on"]

    entrypoint = (
        COMPOSE_PATH.parent / "scripts/sandbox-docker-entrypoint.sh"
    ).read_text()
    assert "com.docker.network.bridge.name=ditto-sandbox0" in entrypoint
    assert "network_driver" in entrypoint
    assert "[ \"$network_driver\" != 'bridge' ]" in entrypoint
    assert "bridge_name" in entrypoint
    assert "[ \"$bridge_name\" != 'ditto-sandbox0' ]" in entrypoint
    assert "unsafe ditto-sandbox network" in entrypoint
    # The network + firewall are provisioned through one idempotent function that
    # also runs inside the maintenance loop, so a missing bridge self-heals.
    assert "ensure_sandbox_network" in entrypoint
    assert entrypoint.count("ensure_sandbox_network") >= 3
    assert "DITTO-SANDBOX-EGRESS" in entrypoint
    assert "--dst-type LOCAL -p tcp --dport 11434 -j ACCEPT" in entrypoint
    assert "--dst-type LOCAL -p tcp --dport 11435 -j ACCEPT" in entrypoint
    assert "--dst-type LOCAL -p tcp --dport 11436 -j ACCEPT" in entrypoint
    assert "-i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS" in entrypoint
    assert "com.docker.network.bridge.enable_icc=false" in entrypoint
    assert "-i 'dtj+' -j DITTO-SANDBOX-EGRESS" in entrypoint
    assert "ditto-sandbox-deny" in entrypoint
    # Denied egress must fail fast: a silent DROP costs a full SYN timeout per
    # connect (~53 s of dead time per benchmark check), which burns the whole
    # ticket and starves the scoring slot. The allowlist is unchanged.
    assert "-j DROP" not in entrypoint
    assert "-p tcp -j REJECT --reject-with tcp-reset" in entrypoint
    assert "-j REJECT --reject-with icmp-port-unreachable" in entrypoint
    assert "/var/run/docker.sock" not in COMPOSE_PATH.read_text()


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
    # The checksum must remain the exact current main commit. A moving branch
    # paired with a stale checksum makes the documented local Compose build
    # fail before the release materializer can replace the source context.
    assert checksum == "3de0c62ff90a4f6288b19db5a310ecc6e6f78540"

    compose = yaml.safe_load(raw_compose)
    expected = compose["x-dittobench-build-context"]
    assert compose["services"]["model-relay"]["build"]["context"] == expected
    assert compose["services"]["dittobench-api"]["build"]["context"] == expected
    validator_environment = compose["services"]["ditto-subnet"]["environment"]
    assert validator_environment["VALIDATOR_STACK_COMPONENT_DITTOBENCH_API"] == (
        f"source:{checksum}"
    )
    assert validator_environment["VALIDATOR_STACK_COMPONENT_MODEL_RELAY"] == (
        f"source:{checksum}"
    )
    scorer_environment = compose["services"]["dittobench-api"]["environment"]
    assert scorer_environment["DITTOBENCH_SOURCE_SHA"] == checksum
    # One anchor definition plus the build-context default. Everything else that
    # names the revision aliases the anchor or carries the `source:` prefix, so
    # no copy of the revision can drift away from the build context.
    assert raw_compose.count(checksum) == 4
    assert f"x-dittobench-revision: &dittobench-revision {checksum}" in raw_compose
    assert "DITTOBENCH_SOURCE_SHA: *dittobench-revision" in raw_compose
    wrapper = COMPOSE_WRAPPER_PATH.read_text()
    assert 'git -C "$checkout" fetch' in wrapper
    assert 'context_override="${DITTOBENCH_BUILD_CONTEXT:-}"' in wrapper
    assert 'merge-base --is-ancestor "$checksum" FETCH_HEAD' in wrapper
    assert 'verified_marker="$checkout.ref-verified"' in wrapper
    assert "DITTOBENCH_ALLOW_UNMERGED_SMOKE:-false" in wrapper
    assert "SUBTENSOR_NETWORK:-finney" in wrapper
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

    for service in ("dittobench-api", "model-relay"):
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
        == 2
    )
    assert (
        workflow.count(
            "DITTOBENCH_SOFTWARE_VERSION=${{ needs.release.outputs.version }}"
        )
        == 2
    )


def test_stack_update_rebuilds_the_scorer_only_when_the_pin_moves() -> None:
    wrapper = COMPOSE_WRAPPER_PATH.read_text()

    # Keyed on the pin: a changed pin forces a real build + a container
    # replacement, and an unchanged pin costs nothing.
    assert 'built_revision_file="$STATE_DIR/dittobench-built-revision"' in wrapper
    assert 'if [ "$built_revision" != "$checksum" ]; then' in wrapper
    assert "build --pull dittobench-api model-relay" in wrapper
    assert "up -d --no-deps --no-build dittobench-api model-relay" in wrapper
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


def test_hosted_v7_parallelism_is_tunable_without_widening_the_local_ollama_lane() -> (
    None
):
    """The two lanes must stay independently controllable, and only one may widen.

    Bench versions 2-6 embed against the single Ollama container this compose
    file starts; v7 bypasses it for the hosted route (dittobench-api #93). The
    hazard this pins is an operator who wants more v7 parallelism reaching for
    the memory-phase knob, which would serialise nothing and hammer Ollama.
    """
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    env = compose["services"]["dittobench-api"]["environment"]

    # The local lane keeps its own anchor and its default of one.
    assert env["DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES"] == (
        "${VALIDATOR_BENCHMARK_MEMORY_CAPACITY:-1}"
    )

    # The hosted knobs are exposed, under distinct anchors, and default to
    # empty so the scorer image's own shipped defaults govern. An empty value
    # is read as unset by envIntDefault, so this can never pin them to zero.
    assert env["DITTOBENCH_V7_CASE_CONCURRENCY"] == "${VALIDATOR_V7_CASE_CONCURRENCY:-}"
    assert env["DITTOBENCH_V7_EMBEDDING_CONCURRENCY"] == (
        "${VALIDATOR_V7_EMBEDDING_CONCURRENCY:-}"
    )

    anchors = {
        env["DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES"],
        env["DITTOBENCH_V7_CASE_CONCURRENCY"],
        env["DITTOBENCH_V7_EMBEDDING_CONCURRENCY"],
    }
    assert len(anchors) == 3, "hosted and local concurrency share an anchor"
