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
    "dittobench-api": "DITTOBENCH_API_IMAGE",
    "model-relay": "MODEL_RELAY_IMAGE",
    "pylon": "PYLON_IMAGE",
    "ollama": "OLLAMA_IMAGE",
}
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


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
            "VALIDATOR_STACK_COMPONENT_PYLON": images["PYLON_IMAGE"],
            "VALIDATOR_STACK_COMPONENT_OLLAMA": images["OLLAMA_IMAGE"],
        }
    )

    remaining_builds = sorted(
        name for name, service in services.items() if "build" in service
    )
    if remaining_builds:
        raise ValueError(f"managed compose still contains builds: {remaining_builds}")

    compose.pop("x-dittobench-build-context", None)
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
