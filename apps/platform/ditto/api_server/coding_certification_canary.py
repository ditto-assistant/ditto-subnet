"""Pinned public coding-certification canary identities.

The committed ``certification/v1`` manifest is the only public canary a
qualified lease may bind. This module never loads a private catalog record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[4]
    / "research"
    / "dittobench-coding-datagen"
    / "certification"
    / "v1"
    / "manifest.json"
)
_MAX_OBJECT_BYTES = 1 << 20


class CodingCertificationCanaryUnavailableError(RuntimeError):
    """The committed public canary identity cannot be loaded."""


@dataclass(frozen=True)
class PublicCertificationCanary:
    canary_manifest_sha256: str
    runner_plan_sha256: str
    grader_plan_sha256: str
    resource_profile_sha256: str
    inference_policy_sha256: str


@cache
def public_certification_canary() -> PublicCertificationCanary:
    """Return the content-addressed public canary bound into new leases."""

    try:
        body = _MANIFEST_PATH.read_bytes()
        manifest = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodingCertificationCanaryUnavailableError(
            "public certification canary is unavailable"
        ) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "dittobench-coding-public-certification-canary-v1"
        or manifest.get("coding_contract_version") != 1
        or manifest.get("weight_eligible") is not False
        or manifest.get("corpus_scope") != "public_certification"
    ):
        raise CodingCertificationCanaryUnavailableError(
            "public certification canary identity is invalid"
        )
    inference = manifest.get("inference_policy")
    if not isinstance(inference, dict) or not isinstance(inference.get("sha256"), str):
        raise CodingCertificationCanaryUnavailableError(
            "public certification canary inference policy is invalid"
        )
    try:
        canonical = coding_canonical_json_bytes(
            manifest,
            maximum_bytes=_MAX_OBJECT_BYTES,
            label="public certification canary",
        )
        if body != canonical:
            raise ValueError("public certification canary is not canonical")
        return PublicCertificationCanary(
            canary_manifest_sha256=hashlib.sha256(body).hexdigest(),
            runner_plan_sha256=_object_sha256(manifest, "runner_plan"),
            grader_plan_sha256=_object_sha256(manifest, "grader_plan"),
            resource_profile_sha256=_object_sha256(manifest, "resource_profile"),
            inference_policy_sha256=inference["sha256"],
        )
    except (TypeError, ValueError) as error:
        raise CodingCertificationCanaryUnavailableError(
            "public certification canary identity is invalid"
        ) from error


def _object_sha256(manifest: dict[str, object], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"public certification canary {field} is invalid")
    return coding_canonical_sha256(
        value,
        maximum_bytes=_MAX_OBJECT_BYTES,
        label=f"public certification canary {field}",
    )
