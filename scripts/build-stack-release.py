#!/usr/bin/env python3
"""Render one immutable, image-only validator stack release bundle."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

IMAGE_KEYS = {
    "ditto-subnet": "VALIDATOR_IMAGE",
    "sandbox-docker": "SANDBOX_DOCKER_IMAGE",
    "model-relay": "MODEL_RELAY_IMAGE",
    "ollama": "OLLAMA_IMAGE",
    "dittobench-api": "DITTOBENCH_API_IMAGE",
    "pylon": "PYLON_IMAGE",
}
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

# Incident-scoped bootstrap target. This public validator hotkey is rendered as
# signed descriptor policy; the host supplies its configured hotkey separately,
# and the scorer bootstrap mutates the checkout only when they match exactly.
FROZEN_UPDATER_BOOTSTRAP_TARGET_HOTKEY = (
    "5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118"
)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--dittobench-revision", required=True)
    parser.add_argument("--compatibility-epoch", default="2")
    parser.add_argument("--update-protocol", default="1")
    parser.add_argument("--heartbeat-protocol", default="6")
    parser.add_argument("--compose-schema", default="1")
    for key in IMAGE_KEYS.values():
        parser.add_argument(f"--{key.lower().replace('_', '-')}", required=True)
    return parser


def _positive_integer(name: str, value: str) -> str:
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def main() -> None:
    args = _argument_parser().parse_args()
    if not VERSION_RE.fullmatch(args.version):
        raise ValueError("version must be an unprefixed semantic version")
    if not REVISION_RE.fullmatch(args.revision):
        raise ValueError("revision must be a full lowercase Git SHA")
    if not REVISION_RE.fullmatch(args.dittobench_revision):
        raise ValueError("dittobench revision must be a full lowercase Git SHA")

    images = {key: getattr(args, key.lower()) for key in IMAGE_KEYS.values()}
    for key, image in images.items():
        if not IMAGE_RE.fullmatch(image):
            raise ValueError(f"{key} must be an immutable sha256 image reference")

    compose = yaml.safe_load(args.compose.read_text())
    services = compose.get("services", {})

    # compat-2 is also the discovery channel used by the original managed-stack
    # updater. That frozen client validates an exact six-service/14-field
    # descriptor before it drains the validator. Keep the retired inference
    # images as isolated compatibility shims while the signed scorer image
    # atomically refreshes that host-side updater during candidate startup.
    # Nothing depends on these services, and the dummy relay credential cannot
    # authorize an upstream request.
    services.setdefault(
        "model-relay",
        {
            "restart": "unless-stopped",
            "environment": {
                "RELAY_PROVIDER": "openrouter",
                "RELAY_API_KEY": "retired-compatibility-shim",
                "PORT": "11435",
            },
            "read_only": True,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
        },
    )
    services.setdefault(
        "ollama",
        {
            "restart": "unless-stopped",
            "read_only": True,
            # Ollama initializes this directory even when it is retained only
            # as an inert compatibility service for frozen updaters.
            "tmpfs": ["/root/.ollama:rw,noexec,nosuid,nodev,mode=0700"],
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
        },
    )
    missing = sorted(set(IMAGE_KEYS) - set(services))
    if missing:
        raise ValueError(f"compose file is missing managed services: {missing}")

    # A managed release never builds on the validator host. Every first-party
    # and third-party runtime is selected by the signed descriptor digest.
    for service_name, manifest_key in IMAGE_KEYS.items():
        service = services[service_name]
        service.pop("build", None)
        service["image"] = images[manifest_key]
        service["pull_policy"] = "never"

    validator_environment = services["ditto-subnet"].get("environment")
    if not isinstance(validator_environment, dict):
        raise ValueError("ditto-subnet environment must be a mapping")
    # All managed-release identity except the descriptor digest is rendered as
    # a literal from the validated build inputs. The digest is known only after
    # publishing the descriptor image and is supplied by the host wrapper after
    # it validates the extracted descriptor state. No Docker socket is exposed
    # to the validator for discovery.
    validator_environment.update(
        {
            "VALIDATOR_STACK_MODE": "managed",
            "VALIDATOR_STACK_DESCRIPTOR_REF": (
                "${VALIDATOR_STACK_DESCRIPTOR_REF:?validated descriptor ref required}"
            ),
            "VALIDATOR_STACK_VERSION": args.version,
            "VALIDATOR_STACK_REVISION": args.revision,
            "VALIDATOR_STACK_DITTOBENCH_REVISION": args.dittobench_revision,
            "VALIDATOR_STACK_COMPOSE_SCHEMA": _positive_integer(
                "compose schema", args.compose_schema
            ),
            "VALIDATOR_STACK_UPDATE_PROTOCOL": _positive_integer(
                "update protocol", args.update_protocol
            ),
            "VALIDATOR_STACK_COMPONENT_DITTO_SUBNET": images["VALIDATOR_IMAGE"],
            "VALIDATOR_STACK_COMPONENT_DITTOBENCH_API": images["DITTOBENCH_API_IMAGE"],
            "VALIDATOR_STACK_COMPONENT_SANDBOX_DOCKER": images["SANDBOX_DOCKER_IMAGE"],
            "VALIDATOR_STACK_COMPONENT_MODEL_RELAY": images["MODEL_RELAY_IMAGE"],
            "VALIDATOR_STACK_COMPONENT_OLLAMA": images["OLLAMA_IMAGE"],
            "VALIDATOR_STACK_COMPONENT_PYLON": images["PYLON_IMAGE"],
        }
    )

    scorer_environment = services["dittobench-api"].get("environment")
    if not isinstance(scorer_environment, dict):
        raise ValueError("dittobench-api environment must be a mapping")
    # These values are authenticated indirectly by the descriptor: the same
    # signed manifest binds the scorer image digest, source revision, and stack
    # version. Render literals so an operator .env cannot make an old scorer
    # claim a newer identity. Capability discovery needs no shared secret: the
    # validator verifies these literals against the same signed descriptor.
    scorer_environment.update(
        {
            "DITTOBENCH_SOFTWARE_VERSION": args.version,
            "DITTOBENCH_SOURCE_SHA": args.dittobench_revision,
            # Literal signed-descriptor policy, not an operator substitution.
            # The image contains the updater from this reviewed source revision.
            "DITTOBENCH_BOOTSTRAP_VALIDATOR_STACK_UPDATER": "true",
            "DITTOBENCH_BOOTSTRAP_VALIDATOR_STACK_UPDATER_TARGET_HOTKEY": (
                FROZEN_UPDATER_BOOTSTRAP_TARGET_HOTKEY
            ),
            # The target above is authenticated by the signed descriptor. This
            # value is the host's public validator identity and must match it
            # byte-for-byte before the helper can touch the checkout.
            "DITTOBENCH_BOOTSTRAP_VALIDATOR_HOTKEY": (
                "${VALIDATOR_HOTKEY:?validator hotkey required}"
            ),
        }
    )
    scorer = services["dittobench-api"]
    # One known frozen host updater cannot update its own retry/rollback logic.
    # The authenticated scorer image starts briefly as root and atomically
    # replaces only that target validator's updater entrypoint. It then drops
    # permanently to uid/gid 65532, proves the mounted scripts tree is no longer
    # writable, and execs the ordinary scorer. The nested miner daemon never
    # receives this mount.
    scorer["user"] = "0:0"
    scorer["entrypoint"] = ["/updater-bootstrap"]
    scorer["cap_add"] = ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"]
    scorer.setdefault("volumes", []).append(
        {
            "type": "bind",
            # validator-stack-compose.sh pins the Compose project directory to
            # the checkout root, so this is the updater's own scripts directory.
            "source": "./scripts",
            "target": "/opt/ditto/host-validator-scripts",
            "read_only": False,
            "bind": {"create_host_path": False},
        }
    )

    remaining_builds = sorted(
        name for name, service in services.items() if "build" in service
    )
    if remaining_builds:
        raise ValueError(f"managed compose still contains builds: {remaining_builds}")

    # Build-time extensions describe how a source stack is built. A managed
    # release only ever pulls signed digests, so drop them rather than ship a
    # pin that nothing in the rendered model consults.
    compose.pop("x-dittobench-build-context", None)
    compose.pop("x-dittobench-revision", None)
    compose.pop("x-dittobench-source-identity", None)
    compose.pop("x-dittobench-software-version", None)
    args.output.mkdir(parents=True, exist_ok=True)
    rendered_compose = yaml.safe_dump(compose, sort_keys=False, width=1000)
    for key, image in images.items():
        if rendered_compose.count(image) != 2:
            raise ValueError(
                f"{key} must appear once as its service image and once in "
                "validator release identity"
            )
    (args.output / "compose.yml").write_text(rendered_compose)

    manifest = {
        "STACK_FORMAT_VERSION": "1",
        "STACK_VERSION": args.version,
        "STACK_REVISION": args.revision,
        "DITTOBENCH_REVISION": args.dittobench_revision,
        "COMPATIBILITY_EPOCH": _positive_integer(
            "compatibility epoch", args.compatibility_epoch
        ),
        "UPDATE_PROTOCOL": _positive_integer("update protocol", args.update_protocol),
        "HEARTBEAT_PROTOCOL": _positive_integer(
            "heartbeat protocol", args.heartbeat_protocol
        ),
        "COMPOSE_SCHEMA": _positive_integer("compose schema", args.compose_schema),
        **images,
    }
    (args.output / "manifest.env").write_text(
        "".join(f"{key}={value}\n" for key, value in manifest.items())
    )


if __name__ == "__main__":
    main()
