"""Conservative runtime derivation of signed heartbeat v7 stack identity."""

from __future__ import annotations

import os
import re
from typing import cast

from ditto import __version__
from ditto.api_models.validator_capabilities import (
    ExecutorIsolation,
    ValidatorCapabilities,
    ValidatorComponentIdentity,
    ValidatorStackComponents,
    ValidatorStackIdentity,
)

_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_REFERENCE = re.compile(
    r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@(?P<digest>sha256:[0-9a-f]{64})$"
)
_DESCRIPTOR_REFERENCE = re.compile(
    r"^ghcr\.io/ditto-assistant/ditto-subnet-stack@(?P<digest>sha256:[0-9a-f]{64})$"
)
_MANAGED_COMPONENT_KEYS = {
    "ditto_subnet": "VALIDATOR_IMAGE",
    "dittobench_api": "DITTOBENCH_API_IMAGE",
    "sandbox_docker": "SANDBOX_DOCKER_IMAGE",
    "model_relay": "MODEL_RELAY_IMAGE",
    "pylon": "PYLON_IMAGE",
    "ollama": "OLLAMA_IMAGE",
}
_TRUTHY = {"1", "true", "yes"}


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _managed_identity() -> ValidatorStackIdentity | None:
    mode = os.environ.get("VALIDATOR_STACK_MODE", "source")
    if mode == "source":
        return None
    if mode != "managed":
        raise ValueError("validator stack mode must be source or managed")
    descriptor_ref = os.environ["VALIDATOR_STACK_DESCRIPTOR_REF"]
    descriptor_match = _DESCRIPTOR_REFERENCE.fullmatch(descriptor_ref)
    if descriptor_match is None:
        raise ValueError("managed stack descriptor reference is not immutable")
    compose_schema = int(os.environ["VALIDATOR_STACK_COMPOSE_SCHEMA"])
    version = os.environ["VALIDATOR_STACK_VERSION"]
    stack_revision = os.environ["VALIDATOR_STACK_REVISION"]
    dbench_revision = os.environ["VALIDATOR_STACK_DITTOBENCH_REVISION"]
    if not _FULL_REVISION.fullmatch(stack_revision) or not _FULL_REVISION.fullmatch(
        dbench_revision
    ):
        raise ValueError("managed stack revisions must be full Git SHAs")
    if int(os.environ["VALIDATOR_STACK_UPDATE_PROTOCOL"]) < 1:
        raise ValueError("managed stack update protocol must be positive")
    components: dict[str, ValidatorComponentIdentity] = {}
    for name in _MANAGED_COMPONENT_KEYS:
        env_name = f"VALIDATOR_STACK_COMPONENT_{name.upper()}"
        image_match = _IMAGE_REFERENCE.fullmatch(os.environ[env_name])
        if image_match is None:
            raise ValueError(f"managed component {name} is not immutable")
        components[name] = ValidatorComponentIdentity(
            image_digest=image_match.group("digest"),
            source_revision=(
                dbench_revision
                if name in {"dittobench_api", "model_relay"}
                else stack_revision
                if name in {"ditto_subnet", "sandbox_docker"}
                else None
            ),
            version=version if name in {"ditto_subnet", "dittobench_api"} else None,
            provenance="signed_descriptor",
        )
    return ValidatorStackIdentity(
        mode="managed",
        compose_schema=compose_schema,
        release_descriptor_digest=descriptor_match.group("digest"),
        components=ValidatorStackComponents(**components),
    )


def _source_component(name: str) -> ValidatorComponentIdentity:
    value = os.environ.get(f"VALIDATOR_STACK_COMPONENT_{name.upper()}", "")
    image_match = _IMAGE_REFERENCE.fullmatch(value)
    source_revision = None
    local_source = value.startswith("local-source:")
    if local_source:
        source_revision = value.removeprefix("local-source:")
    elif value.startswith("source:"):
        source_revision = value.removeprefix("source:")
    if source_revision is not None and not _FULL_REVISION.fullmatch(source_revision):
        source_revision = None
    version = __version__ if name == "ditto_subnet" else "unknown"
    if value.startswith("version:") and value.removeprefix("version:"):
        version = value.removeprefix("version:")
    pinned = bool(image_match or source_revision) and not local_source
    return ValidatorComponentIdentity(
        image_digest=image_match.group("digest") if image_match else None,
        source_revision=source_revision,
        version=version,
        provenance="committed_pin" if pinned else "local_unverified",
    )


def _source_identity() -> ValidatorStackIdentity:
    components = {name: _source_component(name) for name in _MANAGED_COMPONENT_KEYS}
    return ValidatorStackIdentity(
        mode="source",
        compose_schema=int(os.environ.get("VALIDATOR_STACK_COMPOSE_SCHEMA", "1")),
        release_descriptor_digest=None,
        components=ValidatorStackComponents(**components),
    )


def validator_capabilities_and_stack() -> tuple[
    ValidatorCapabilities, ValidatorStackIdentity
]:
    """Derive only claims supported by validated state; fail closed to source mode."""
    # Do not turn malformed managed claims into a signed managed heartbeat. The
    # exception is caught by best-effort heartbeat reporting, so scoring remains
    # available while platform acceptance (and updater promotion) fails closed.
    stack = _managed_identity() or _source_identity()

    screened_images = _truthy("VALIDATOR_SCREENED_IMAGES", default=False)
    capabilities = ValidatorCapabilities(
        screened_images=screened_images,
        # These legacy global flags remain on the v8 wire for old platforms.
        # Requirements are now benchmark-version contracts: v2 can fall back,
        # while v3 always requires a screened image.
        require_screened_image=False,
        source_build_fallback=True,
        full_stack_managed=stack.mode == "managed",
        stack_updater=stack.mode == "managed" and _truthy("VALIDATOR_STACK_UPDATER"),
        sandbox_egress_restricted=_truthy(
            "VALIDATOR_SANDBOX_EGRESS_RESTRICTED", default=False
        ),
        executor_isolation=cast(
            ExecutorIsolation,
            os.environ.get("VALIDATOR_EXECUTOR_ISOLATION", "unknown"),
        ),
    )
    return capabilities, stack
