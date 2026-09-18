"""Hosted-v2 profile approval document: deterministic builder and curator verifier.

Run only on an owner-controlled machine. ``build`` binds one helper-produced
execution/grading profile pair to its profile request, its registered private-v2
release, the native release set, the private compatibility plan and the native
compatibility controls. File digests are recomputed from input bytes; the other
bound values are copied from pinned or cross-checked inputs, as listed in
``docs/coding-hosted-profile-approval-v2.md``. Launch checks are attested by the
helper receipt, not re-run here. The output is an unsigned draft
(``approved=false``) and carries no approval field that could be flipped.
Approval exists only as a detached 64-byte Ed25519 curator signature over the
exact canonical document bytes, the format the private-v2 publication signing
message already uses. ``verify`` checks that signature against a pinned curator
key. No private key is read, accepted or produced here, and nothing is activated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOCUMENT_SCHEMA = "dittobench-coding-hosted-profile-approval-v1"
REQUEST_SCHEMA = "dittobench-coding-hosted-profile-approval-request-v1"
MAX_DOCUMENT_BYTES = 16 << 10
MAX_PLAN_BYTES = 8 << 20
MAX_PROFILE_REQUEST_BYTES = 1 << 20
SIGNATURE_BYTES = 64
# Kept equal to PROFILES in coding_runtime/qualification/{native,run}.py and
# infra/scripts/build-coding-native-release.py by a parity test.
LANGUAGE_PROFILES = {
    "python": "python-call-ast-v2",
    "node": "node-call-ast-v2",
    "go": "go-call-ast-v1",
    "rust": "rust-call-ast-v1",
}
PROFILE_FILES = ("execution-profile.json", "grading-profile.json", "receipt.json")
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "catalog_index",
        "language",
        "source_revision",
        "registration_sha256",
        "release_manifest_sha256",
        "native_controls_approval_sha256",
        "native_controls_provenance_sha256",
        "native_controls_summary_sha256",
        "grader_contract_sha256",
        "curator_signing_key_sha256",
        "shadow_only",
        "weight_eligible",
    }
)
_REQUEST_PINS = (
    "registration_sha256",
    "release_manifest_sha256",
    "native_controls_approval_sha256",
    "native_controls_provenance_sha256",
    "native_controls_summary_sha256",
    "grader_contract_sha256",
    "curator_signing_key_sha256",
)
# Exactly the dittobench-coding-hosted-profile-receipt-v1 fields that
# cmd/dittobench-coding-hosted-profiles writes.
RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "catalog_index",
        "task_version_id",
        "task_commitment_sha256",
        "corpus_release_id",
        "private_release_sha256",
        "payload_sha256",
        "payload_authority_file_sha256",
        "request_sha256",
        "image_digest",
        "execution_profile_sha256",
        "grading_profile_sha256",
        "max_patch_bytes",
        "grader_bundle_sha256",
        "grader_contract_sha256",
        "launch_checks_passed",
        "approved",
        "shadow_only",
        "weight_eligible",
    }
)
# The helper's dittobench-coding-hosted-profile-request-v1 JSON fields.
PROFILE_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "catalog_index",
        "image_digest",
        "candidate_limits",
        "protected_limits",
        "max_combined_disk_bytes",
        "budgets",
        "build",
        "test_groups",
        "execution_timeout_milliseconds",
        "shadow_only",
        "weight_eligible",
    }
)
_PROFILE_REQUEST_COMMAND = frozenset({"id", "argv", "timeout_milliseconds"})
_PROFILE_REQUEST_BUILD = frozenset({"required", "command"})
_PROFILE_REQUEST_GROUP = frozenset({"group", "command", "expected_total"})
EXECUTION_PROFILE_KEYS = frozenset(
    {"schema", "image_digest", "resource_policy", "budgets"}
)
# Hosted v2 grading profiles carry no test manifest. Kept equal to
# ditto.api_server.coding_hosted_grading.GRADING_PROFILE_KEYS by a test; defined
# here so this module does not import the grading control plane.
GRADING_PROFILE_KEYS = frozenset(
    {
        "schema",
        "image_digest",
        "grader_contract_sha256",
        "grader_bundle_sha256",
        "resource_policy",
        "build",
        "test_groups",
        "execution_timeout",
    }
)
# Go codinggrader.ResourcePolicy and codingrunner.Limits field names.
_RESOURCE_POLICY_FIELDS = frozenset(
    {
        "CandidateLimits",
        "ProtectedLimits",
        "MaxCombinedDiskBytes",
        "MemoryLimitBytes",
        "ScratchLimitBytes",
        "PidsLimit",
        "CPUQuotaMillis",
    }
)
_LIMIT_FIELDS = frozenset(
    {
        "MaxBundleBytes",
        "MaxWorkspaceBytes",
        "MaxFileBytes",
        "MaxPatchBytes",
        "MaxEntries",
        "MaxToolCalls",
        "MaxReadBytes",
        "MaxResponseBytes",
        "MaxSearchResults",
        "MaxReplayCacheBytes",
        "MaxTranscriptBytes",
    }
)
_BUDGET_FIELDS = frozenset(
    {
        "model_input_tokens",
        "model_output_tokens",
        "workspace_tool_calls",
        "wall_time_seconds",
    }
)
_COMMAND_FIELDS = frozenset({"ID", "Argv", "Timeout"})
_BUILD_FIELDS = frozenset({"Required", "Command"})
_TEST_GROUP_FIELDS = frozenset({"Group", "Command", "ExpectedTotal"})
# Hosted grading test_groups order (codinggrader hostedEvidenceGroups).
_TEST_GROUPS = ("hidden", "visible")
_NANOS_PER_MILLISECOND = 1_000_000
_MAX_COMMAND_NANOS = 600 * 1_000_000_000
_MAX_EXECUTION_NANOS = 3600 * 1_000_000_000
_INT64_MAX = (1 << 63) - 1
# coding_runtime/qualification/prepare.py plan and run.py role/phase coverage.
PLAN_FIELDS = frozenset(
    {
        "schema",
        "source_sha",
        "replicates",
        "cases",
        "production_api_approval",
        "node_count_inventory_sha256",
        "preparer_sha256",
    }
)
_PLAN_CONTROLS = frozenset(
    {
        ("base", "visible"),
        ("base", "hidden"),
        ("reference", "visible"),
        ("reference", "hidden"),
    }
)
# coding_runtime/qualification/native.py policy() and Binding.provenance().
NATIVE_APPROVAL_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "source_revision",
        "release_manifest_sha256",
        "plan_sha256",
        "helper_sha256",
        "runner_sha256",
        "binding_sha256",
        "machine_id_sha256",
        "boot_id",
        "issued_at_unix",
        "expires_at_unix",
        "controls",
        "max_jobs",
        "images",
        "evidence_sha256",
        "shadow_only",
        "weight_eligible",
    }
)
_NATIVE_IMAGE_FIELDS = (
    "image_ref",
    "config_digest",
    "approval_sha256",
    "driver_profile",
)
_NATIVE_AUTHORITY_FIELDS = frozenset(
    {
        "approval_sha256",
        "release_manifest_sha256",
        "machine_id_sha256",
        "boot_id",
        "evidence_sha256",
        "daemon_identity_sha256",
    }
)
# coding_runtime/qualification/run.py native-bound provenance.json and summary.json.
PROVENANCE_FIELDS = frozenset(
    {
        "source_sha",
        "plan_sha256",
        "image_references_sha256",
        "helper_sha256",
        "runner_sha256",
        "images",
        "kernel",
        "runtime_qualification",
        "production_api_approval",
        "image_binding_kind",
        "native_control_authority",
    }
)
SUMMARY_FIELDS = frozenset(
    {
        "schema",
        "source_sha",
        "controls",
        "cases",
        "replicates",
        "languages",
        "groups_by_language",
        "repeat_results_equal",
        "failed_controls",
        "private_controls_passed",
        "runtime_qualification",
        "production_api_approval",
        "native_host_ready",
        "canary_completed",
        "weight_eligible",
        "native_control_authority",
        "native_controls_passed",
        "image_binding_kind",
    }
)
DOCUMENT_SHA256_FIELDS = (
    "task_commitment_sha256",
    "registration_sha256",
    "private_release_sha256",
    "payload_sha256",
    "payload_authority_file_sha256",
    "profile_receipt_sha256",
    "execution_profile_sha256",
    "grading_profile_sha256",
    "grader_contract_sha256",
    "grader_bundle_sha256",
    "release_manifest_sha256",
    "image_approval_sha256",
    "native_controls_approval_sha256",
    "native_controls_plan_sha256",
    "native_controls_provenance_sha256",
    "native_controls_summary_sha256",
    "curator_signing_key_sha256",
)
# Non-secret identities that ``verify`` prints before the digests.
DOCUMENT_IDENTITY_FIELDS = (
    "catalog_index",
    "language",
    "source_revision",
    "task_version_id",
    "corpus_release_id",
    "image_digest",
)
DOCUMENT_FIELDS = frozenset(
    {
        *DOCUMENT_SHA256_FIELDS,
        *DOCUMENT_IDENTITY_FIELDS,
        "schema",
        "coding_contract_version",
        "shadow_only",
        "weight_eligible",
    }
)
_NATIVE_BINDING = "approved_native_oci_manifest"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class ProfileApprovalError(ValueError):
    """Safe rejection with a fixed reason and no input content."""


@dataclass(frozen=True)
class ApprovalInputs:
    request: bytes
    profile_request: bytes
    execution_profile: bytes
    grading_profile: bytes
    profile_receipt: bytes
    payload_authority: bytes
    registration: bytes
    release_index: bytes
    native_approval: bytes
    native_plan: bytes
    native_summary: bytes
    native_provenance: bytes


@dataclass(frozen=True)
class VerifiedApproval:
    document: dict[str, Any]
    document_sha256: str
    signature_sha256: str


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProfileApprovalError(reason)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SHA256.fullmatch(value) is not None
        and value != "0" * 64
    )


def _image_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _digest(value.removeprefix("sha256:"))
    )


def _revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and _REVISION.fullmatch(value) is not None
        and value != "0" * 40
    )


def _identifier(value: object, maximum: int = 256) -> bool:
    from ditto.api_models.coding_evaluation import _bounded_identifier

    if not isinstance(value, str) or not value:
        return False
    try:
        _bounded_identifier(value, maximum)
    except ValueError:
        return False
    return True


def _index(value: object) -> bool:
    return type(value) is int and 0 <= value <= 999_999


def _language(value: object) -> bool:
    return isinstance(value, str) and value in LANGUAGE_PROFILES


def _positive(value: object, maximum: int = _INT64_MAX) -> bool:
    return type(value) is int and 0 < value <= maximum


def _same(left: object, right: object) -> bool:
    """Type-strict JSON equality: ``true`` never equals ``1``, nor ``1.0`` ``1``."""

    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def _json(body: bytes, maximum: int, label: str) -> dict[str, Any]:
    from ditto.api_models.coding_inference import _decode_json_document

    try:
        value = _decode_json_document(body, maximum_bytes=maximum)
    except ValueError:
        raise ProfileApprovalError(f"{label} is not strict bounded JSON") from None
    _require(isinstance(value, dict), f"{label} is not a JSON object")
    return value


def _canonical(value: dict[str, Any], maximum: int, label: str) -> bytes:
    from ditto.api_models.coding_canonical import coding_canonical_json_bytes

    try:
        return coding_canonical_json_bytes(value, maximum_bytes=maximum, label=label)
    except (TypeError, ValueError):
        raise ProfileApprovalError(f"{label} exceeds its canonical bound") from None


def _canonical_json(body: bytes, maximum: int, label: str) -> dict[str, Any]:
    value = _json(body, maximum, label)
    _require(_canonical(value, maximum, label) == body, f"{label} is not canonical")
    return value


def _compact_json(
    body: bytes, maximum: int, label: str, *, newline: bool
) -> dict[str, Any]:
    """Exact bytes of the release/qualification writers' sorted compact JSON."""

    value = _json(body, maximum, label)
    expected = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    _require(
        body == expected + (b"\n" if newline else b""),
        f"{label} is not its writer's exact encoding",
    )
    return value


def _registration(body: bytes) -> dict[str, Any]:
    from pydantic import ValidationError

    from ditto.api_models.coding_private_v2_registry import (
        CodingPrivateV2RegistrationAuthority,
    )

    value = _canonical_json(body, 64 << 10, "registration")
    try:
        authority = CodingPrivateV2RegistrationAuthority.model_validate(value)
    except ValidationError:
        raise ProfileApprovalError("registration authority is invalid") from None
    _require(
        set(value) == set(authority.model_dump(mode="json", by_alias=True))
        and value["shadow_only"] is True
        and value["weight_eligible"] is False
        and type(value["coding_contract_version"]) is int,
        "registration authority is invalid",
    )
    return value


def _request(body: bytes) -> dict[str, Any]:
    request = _json(body, 1 << 16, "approval request")
    _require(
        set(request) == REQUEST_FIELDS
        and request["schema"] == REQUEST_SCHEMA
        and request["shadow_only"] is True
        and request["weight_eligible"] is False
        and _index(request["catalog_index"])
        and _language(request["language"])
        and _revision(request["source_revision"])
        and all(_digest(request[name]) for name in _REQUEST_PINS),
        "approval request is invalid",
    )
    return request


def _resource_policy(value: object) -> bool:
    """Shape of Go codinggrader.ResourcePolicy; bounds are the helper's checks."""

    return (
        isinstance(value, dict)
        and set(value) == _RESOURCE_POLICY_FIELDS
        and all(
            isinstance(value[name], dict)
            and set(value[name]) == _LIMIT_FIELDS
            and all(_positive(limit) for limit in value[name].values())
            for name in ("CandidateLimits", "ProtectedLimits")
        )
        and all(
            _positive(value[name])
            for name in _RESOURCE_POLICY_FIELDS - {"CandidateLimits", "ProtectedLimits"}
        )
    )


def _budgets(value: object, policy: dict[str, Any]) -> bool:
    """Execution budgets as hosted inference and ProfileDigest accept them."""

    return (
        isinstance(value, dict)
        and set(value) == _BUDGET_FIELDS
        and all(_positive(budget) for budget in value.values())
        and value["wall_time_seconds"] <= 3600
        and value["workspace_tool_calls"] == policy["CandidateLimits"]["MaxToolCalls"]
    )


def _command(value: object) -> bool:
    """Go codingrunner.CommandSpec as the grading profile encodes it."""

    return (
        isinstance(value, dict)
        and set(value) == _COMMAND_FIELDS
        and _identifier(value["ID"], 80)
        and isinstance(value["Argv"], list)
        and 0 < len(value["Argv"]) <= 64
        and all(isinstance(argument, str) for argument in value["Argv"])
        and _positive(value["Timeout"], _MAX_COMMAND_NANOS)
        and value["Timeout"] % _NANOS_PER_MILLISECOND == 0
    )


def _grading_structure(grading: dict[str, Any]) -> bool:
    """Build, hidden-then-visible driver groups and timeout of a grading profile.

    Mirrors the shape ``coding_hosted_grading.expected_grading`` reads and the
    Go hosted ``ValidateExecutionProfile`` group rules.
    """

    build = grading["build"]
    groups = grading["test_groups"]
    timeout = grading["execution_timeout"]
    return (
        isinstance(build, dict)
        and set(build) == _BUILD_FIELDS
        and type(build["Required"]) is bool
        and _command(build["Command"])
        and isinstance(groups, list)
        and len(groups) == len(_TEST_GROUPS)
        and all(
            isinstance(group, dict)
            and set(group) == _TEST_GROUP_FIELDS
            and group["Group"] == name
            and _positive(group["ExpectedTotal"], 1_000_000)
            and _command(group["Command"])
            and group["Command"]["Argv"][0] == "dittobench-test-driver"
            for group, name in zip(groups, _TEST_GROUPS, strict=True)
        )
        and len({build["Command"]["ID"], *(g["Command"]["ID"] for g in groups)})
        == 1 + len(groups)
        and _positive(timeout, _MAX_EXECUTION_NANOS)
        and timeout % _NANOS_PER_MILLISECOND == 0
    )


def _nanos(value: object) -> int | None:
    return value * _NANOS_PER_MILLISECOND if type(value) is int else None


def _requested_command(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != _PROFILE_REQUEST_COMMAND:
        return None
    return {
        "ID": value["id"],
        "Argv": value["argv"],
        "Timeout": _nanos(value["timeout_milliseconds"]),
    }


def _requested_build(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != _PROFILE_REQUEST_BUILD:
        return None
    return {
        "Required": value["required"],
        "Command": _requested_command(value["command"]),
    }


def _requested_groups(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not all(
        isinstance(group, dict) and set(group) == _PROFILE_REQUEST_GROUP
        for group in value
    ):
        return None
    return [
        {
            "Group": group["group"],
            "Command": _requested_command(group["command"]),
            "ExpectedTotal": group["expected_total"],
        }
        for group in value
    ]


def _task_group(task_version_id: object) -> str | None:
    """Private group of a catalog task: ``task_version_id`` is ``{group}-{condition}``.

    ``coding_private_catalog_v2_compile._compile_group`` derives it that way for
    each of the five memory conditions of one group.
    """

    from ditto.api_models.coding_private_catalog_v2 import CodingMemoryConditionV2

    if not isinstance(task_version_id, str):
        return None
    groups = [
        task_version_id.removesuffix(f"-{condition.value}")
        for condition in CodingMemoryConditionV2
        if task_version_id.endswith(f"-{condition.value}")
    ]
    return groups[0] if len(groups) == 1 and groups[0] else None


def build_profile_approval(
    inputs: ApprovalInputs, *, curator_signing_key_sha256: str
) -> bytes:
    """Return the exact canonical unsigned approval document bytes."""

    request = _request(inputs.request)
    _require(
        curator_signing_key_sha256 == request["curator_signing_key_sha256"],
        "curator public key differs from the reviewed pin",
    )
    index = request["catalog_index"]
    language = request["language"]
    revision = request["source_revision"]

    # Profile set. File digests are recomputed here; receipt claims are only
    # compared with them. Launch checks are the helper's attestation.
    receipt = _canonical_json(inputs.profile_receipt, 1 << 16, "profile receipt")
    _require(
        set(receipt) == RECEIPT_FIELDS
        and receipt["schema"] == "dittobench-coding-hosted-profile-receipt-v1",
        "profile receipt schema is invalid",
    )
    _require(receipt["launch_checks_passed"] is True, "profile launch checks failed")
    _require(receipt["approved"] is False, "profile receipt claims approval")
    _require(
        receipt["shadow_only"] is True and receipt["weight_eligible"] is False,
        "profile receipt is not shadow-only",
    )
    _require(
        type(receipt["catalog_index"]) is int and receipt["catalog_index"] == index,
        "profile catalog index differs from the reviewed index",
    )
    execution = _canonical_json(inputs.execution_profile, 16384, "execution profile")
    _require(
        _sha(inputs.execution_profile) == receipt["execution_profile_sha256"],
        "execution profile bytes differ from the receipt",
    )
    grading = _canonical_json(inputs.grading_profile, 65536, "grading profile")
    _require(
        _sha(inputs.grading_profile) == receipt["grading_profile_sha256"],
        "grading profile bytes differ from the receipt",
    )
    _require(
        set(execution) == EXECUTION_PROFILE_KEYS
        and execution["schema"] == "dittobench-coding-hosted-authoring-profile-v2"
        and set(grading) == GRADING_PROFILE_KEYS
        and grading["schema"] == "dittobench-coding-hosted-grading-profile-v2",
        "profile schema is invalid",
    )
    policy = execution["resource_policy"]
    _require(
        _resource_policy(policy) and _budgets(execution["budgets"], policy),
        "execution profile structure is invalid",
    )
    _require(_grading_structure(grading), "grading profile structure is invalid")
    image_digest = execution["image_digest"]
    _require(
        _image_digest(image_digest)
        and grading["image_digest"] == image_digest
        and receipt["image_digest"] == image_digest,
        "profile image digests disagree",
    )
    _require(
        grading["grader_contract_sha256"] == receipt["grader_contract_sha256"]
        and grading["grader_contract_sha256"] == request["grader_contract_sha256"],
        "grader contract differs from the reviewed pin",
    )
    _require(
        _digest(grading["grader_bundle_sha256"])
        and grading["grader_bundle_sha256"] == receipt["grader_bundle_sha256"],
        "grader bundle differs from the receipt",
    )
    # The helper derives both profiles from one ResourcePolicy value.
    _require(
        _same(grading["resource_policy"], policy),
        "grading resource policy differs from the execution profile",
    )
    _require(
        _same(policy["CandidateLimits"]["MaxPatchBytes"], receipt["max_patch_bytes"]),
        "patch limit differs from the receipt",
    )

    # Profile request: the exact helper input the receipt names, and every value
    # the helper copies from it into the profiles.
    _require(
        _sha(inputs.profile_request) == receipt["request_sha256"],
        "profile request bytes differ from the receipt",
    )
    profile_request = _json(
        inputs.profile_request, MAX_PROFILE_REQUEST_BYTES, "profile request"
    )
    _require(
        set(profile_request) == PROFILE_REQUEST_FIELDS
        and profile_request["schema"] == "dittobench-coding-hosted-profile-request-v1"
        and profile_request["shadow_only"] is True
        and profile_request["weight_eligible"] is False,
        "profile request schema is invalid",
    )
    _require(
        _same(profile_request["catalog_index"], index)
        and _same(profile_request["image_digest"], image_digest)
        and _same(profile_request["candidate_limits"], policy["CandidateLimits"])
        and _same(profile_request["protected_limits"], policy["ProtectedLimits"])
        and _same(
            profile_request["max_combined_disk_bytes"], policy["MaxCombinedDiskBytes"]
        )
        and _same(profile_request["budgets"], execution["budgets"])
        and _same(_requested_build(profile_request["build"]), grading["build"])
        and _same(
            _requested_groups(profile_request["test_groups"]), grading["test_groups"]
        )
        and _same(
            _nanos(profile_request["execution_timeout_milliseconds"]),
            grading["execution_timeout"],
        ),
        "profiles differ from the profile request",
    )

    # Private-v2 identity: payload authority task and the registered release.
    _require(
        _sha(inputs.payload_authority) == receipt["payload_authority_file_sha256"],
        "payload authority bytes differ from the receipt",
    )
    payload = _canonical_json(inputs.payload_authority, 8 << 20, "payload authority")
    projection = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    _require(
        payload.get("schema") == "dittobench-coding-private-payload-v2"
        and type(payload.get("coding_contract_version")) is int
        and payload["coding_contract_version"] == 2
        and payload.get("weight_eligible") is False
        and _digest(payload.get("payload_sha256"))
        and _sha(_canonical(projection, 8 << 20, "payload authority"))
        == payload["payload_sha256"]
        and payload["payload_sha256"] == receipt["payload_sha256"],
        "payload authority digest is invalid",
    )
    task_assets = payload.get("task_assets")
    tasks = [
        task
        for task in (task_assets if isinstance(task_assets, list) else [])
        if isinstance(task, dict)
        and type(task.get("catalog_index")) is int
        and task["catalog_index"] == index
    ]
    _require(len(tasks) == 1, "reviewed catalog index is not exactly one payload task")
    artifacts = tasks[0].get("artifacts")
    _require(
        tasks[0].get("task_version_id") == receipt["task_version_id"]
        and tasks[0].get("task_commitment_sha256") == receipt["task_commitment_sha256"]
        and isinstance(artifacts, dict)
        and artifacts.get("grader_bundle") == grading["grader_bundle_sha256"],
        "payload task differs from the profile receipt",
    )
    registration = _registration(inputs.registration)
    _require(
        registration["registration_sha256"] == request["registration_sha256"],
        "registration differs from the reviewed pin",
    )
    _require(
        registration["corpus_release_id"] == receipt["corpus_release_id"]
        and registration["private_release_sha256"] == receipt["private_release_sha256"]
        and registration["payload_sha256"] == payload["payload_sha256"]
        and registration["catalog_sha256"] == payload.get("catalog_sha256"),
        "registered release differs from the profile task",
    )

    # Native release set: the pinned index names the profile image for the language.
    _require(
        _sha(inputs.release_index) == request["release_manifest_sha256"],
        "release index differs from the reviewed pin",
    )
    release = _compact_json(
        inputs.release_index, 64 << 10, "release index", newline=False
    )
    _require(
        release.get("schema") == "dittobench-coding-native-release-set-v2"
        and release.get("independent_approval_required") is True
        and release.get("shadow_only") is True
        and all(
            release.get(field) is False
            for field in (
                "native_imported",
                "runtime_qualification",
                "canary_completed",
                "weight_eligible",
            )
        ),
        "release index is invalid",
    )
    _require(
        release.get("source_revision") == revision,
        "release revision differs from the reviewed revision",
    )
    raw_images = release.get("images")
    images: dict[str, Any] = raw_images if isinstance(raw_images, dict) else {}
    _require(
        set(images) == set(LANGUAGE_PROFILES)
        and all(
            isinstance(images[name], dict)
            and images[name].get("driver_profile") == profile
            and _digest(images[name].get("approval_sha256"))
            for name, profile in LANGUAGE_PROFILES.items()
        ),
        "release images are invalid",
    )
    image = images[language]
    _require(
        image.get("image_ref")
        == f"coding-runtime.invalid/{language}/runtime@{image_digest}",
        "profile image is not the reviewed language's release image",
    )

    # Native compatibility controls: consumed approval and the plan it ran.
    _require(
        _sha(inputs.native_approval) == request["native_controls_approval_sha256"],
        "native approval differs from the reviewed pin",
    )
    approval = _json(inputs.native_approval, 65536, "native approval")
    _require(
        set(approval) == NATIVE_APPROVAL_FIELDS
        and approval["schema"] == "dittobench-coding-native-controls-approval-v2"
        and approval["purpose"] == "private-compatibility-once"
        and approval["shadow_only"] is True
        and approval["weight_eligible"] is False
        and type(approval["controls"]) is int
        and all(
            _digest(approval[name])
            for name in ("plan_sha256", "helper_sha256", "runner_sha256")
        ),
        "native approval is invalid",
    )
    _require(
        approval["source_revision"] == revision,
        "native approval revision differs from the reviewed revision",
    )
    _require(
        approval["release_manifest_sha256"] == request["release_manifest_sha256"],
        "native approval names another release set",
    )
    approved_images = approval["images"]
    _require(
        isinstance(approved_images, dict)
        and set(approved_images) == set(LANGUAGE_PROFILES)
        and all(
            isinstance(approved_images[name], dict)
            and set(approved_images[name]) == set(_NATIVE_IMAGE_FIELDS)
            and all(
                approved_images[name][field] == images[name].get(field)
                for field in _NATIVE_IMAGE_FIELDS
            )
            for name in LANGUAGE_PROFILES
        ),
        "native approval images differ from the release set",
    )
    # The plan carries private driver arguments and corpus paths: only its digest
    # is bound, and no rejection reason names its content.
    _require(
        _sha(inputs.native_plan) == approval["plan_sha256"],
        "native plan differs from the native approval",
    )
    plan = _compact_json(
        inputs.native_plan, MAX_PLAN_BYTES, "native plan", newline=True
    )
    raw_cases = plan.get("cases")
    cases: list[Any] = raw_cases if isinstance(raw_cases, list) else []
    _require(
        set(plan) == PLAN_FIELDS
        and plan["schema"] == "dittobench-private-compatibility-plan-v1"
        and plan["source_sha"] == revision
        and type(plan["replicates"]) is int
        and plan["replicates"] == 2
        and plan["production_api_approval"] is False
        and 0 < len(cases) <= 512
        and all(isinstance(case, dict) for case in cases),
        "native plan is invalid",
    )
    group = _task_group(receipt["task_version_id"])
    _require(group is not None, "profile task is not a private group condition")
    group_cases = [case for case in cases if case.get("group_id") == group]
    _require(
        len(group_cases) == len(_PLAN_CONTROLS)
        and all(
            isinstance(case.get("role"), str) and isinstance(case.get("phase"), str)
            for case in group_cases
        )
        and {(case["role"], case["phase"]) for case in group_cases} == _PLAN_CONTROLS,
        "native plan does not cover the profile task group",
    )
    _require(
        all(case.get("language") == language for case in group_cases),
        "native plan language differs from the reviewed language",
    )
    # Both roles of a phase must run exactly the profile's driver argv and count.
    # Controls use a fixed command ID and timeout, so those are not compared.
    _require(
        all(
            _same(case.get("argv"), profile_group["Command"]["Argv"])
            and _same(case.get("expected_total"), profile_group["ExpectedTotal"])
            for profile_group in grading["test_groups"]
            for case in group_cases
            if case["phase"] == profile_group["Group"]
        ),
        "grading test groups differ from the native plan",
    )

    # Native control outputs, each pinned independently.
    _require(
        _sha(inputs.native_provenance) == request["native_controls_provenance_sha256"],
        "native provenance differs from the reviewed pin",
    )
    _require(
        _sha(inputs.native_summary) == request["native_controls_summary_sha256"],
        "native summary differs from the reviewed pin",
    )
    provenance = _compact_json(
        inputs.native_provenance, 1 << 20, "native provenance", newline=True
    )
    summary = _compact_json(
        inputs.native_summary, 1 << 20, "native summary", newline=True
    )
    _require(
        set(provenance) == PROVENANCE_FIELDS and set(summary) == SUMMARY_FIELDS,
        "native control outputs are not native-bound",
    )
    _require(
        provenance["source_sha"] == revision and summary["source_sha"] == revision,
        "native control revision differs from the reviewed revision",
    )
    authority = provenance["native_control_authority"]
    _require(
        isinstance(authority, dict)
        and set(authority) == _NATIVE_AUTHORITY_FIELDS
        and summary["native_control_authority"] == authority
        and authority["approval_sha256"] == request["native_controls_approval_sha256"]
        and authority["release_manifest_sha256"] == request["release_manifest_sha256"]
        and all(
            authority[name] == approval[name]
            for name in ("machine_id_sha256", "boot_id", "evidence_sha256")
        ),
        "native control authority differs from the approval",
    )
    _require(
        all(
            provenance[name] == approval[name]
            for name in ("plan_sha256", "helper_sha256", "runner_sha256")
        ),
        "native control inputs differ from the approval",
    )
    _require(
        provenance["image_binding_kind"] == _NATIVE_BINDING
        and summary["image_binding_kind"] == _NATIVE_BINDING
        and provenance["runtime_qualification"] is False
        and provenance["production_api_approval"] is False,
        "native provenance is not an approved native binding",
    )
    inspected = provenance["images"]
    inspected_image = inspected.get(language) if isinstance(inspected, dict) else None
    repo_digests = (
        inspected_image.get("repo_digests")
        if isinstance(inspected_image, dict)
        else None
    )
    _require(
        isinstance(repo_digests, list) and image["image_ref"] in repo_digests,
        "native controls did not run the profile image",
    )
    _require(
        summary["schema"] == "dittobench-private-compatibility-summary-v1"
        and summary["private_controls_passed"] is True
        and summary["native_controls_passed"] is True
        and summary["repeat_results_equal"] is True
        and type(summary["failed_controls"]) is int
        and summary["failed_controls"] == 0
        and type(summary["replicates"]) is int
        and summary["replicates"] == 2
        and type(summary["cases"]) is int
        and summary["cases"] == len(cases)
        and type(summary["controls"]) is int
        and summary["controls"] == 2 * len(cases)
        and summary["controls"] == approval["controls"]
        and summary["languages"] == sorted(LANGUAGE_PROFILES)
        and all(
            summary[field] is False
            for field in (
                "runtime_qualification",
                "production_api_approval",
                "native_host_ready",
                "canary_completed",
                "weight_eligible",
            )
        ),
        "native compatibility controls did not pass",
    )

    document = {
        "schema": DOCUMENT_SCHEMA,
        "coding_contract_version": 2,
        "catalog_index": index,
        "task_version_id": receipt["task_version_id"],
        "task_commitment_sha256": receipt["task_commitment_sha256"],
        "corpus_release_id": registration["corpus_release_id"],
        "registration_sha256": registration["registration_sha256"],
        "private_release_sha256": registration["private_release_sha256"],
        "payload_sha256": payload["payload_sha256"],
        "payload_authority_file_sha256": _sha(inputs.payload_authority),
        "profile_receipt_sha256": _sha(inputs.profile_receipt),
        "execution_profile_sha256": _sha(inputs.execution_profile),
        "grading_profile_sha256": _sha(inputs.grading_profile),
        "grader_contract_sha256": grading["grader_contract_sha256"],
        "grader_bundle_sha256": grading["grader_bundle_sha256"],
        "image_digest": image_digest,
        "language": language,
        "source_revision": revision,
        "release_manifest_sha256": _sha(inputs.release_index),
        "image_approval_sha256": image["approval_sha256"],
        "native_controls_approval_sha256": _sha(inputs.native_approval),
        "native_controls_plan_sha256": _sha(inputs.native_plan),
        "native_controls_provenance_sha256": _sha(inputs.native_provenance),
        "native_controls_summary_sha256": _sha(inputs.native_summary),
        "curator_signing_key_sha256": curator_signing_key_sha256,
        "shadow_only": True,
        "weight_eligible": False,
    }
    body = _canonical(document, MAX_DOCUMENT_BYTES, "approval document")
    parse_profile_approval_document(body)
    return body


def parse_profile_approval_document(body: bytes) -> dict[str, Any]:
    """Validate exact canonical bytes and the closed document schema."""

    value = _canonical_json(body, MAX_DOCUMENT_BYTES, "approval document")
    _require(set(value) == DOCUMENT_FIELDS, "approval document fields are invalid")
    _require(
        value["schema"] == DOCUMENT_SCHEMA
        and type(value["coding_contract_version"]) is int
        and value["coding_contract_version"] == 2,
        "approval document schema is invalid",
    )
    _require(
        value["shadow_only"] is True and value["weight_eligible"] is False,
        "approval document is not shadow-only",
    )
    _require(
        _index(value["catalog_index"])
        and _language(value["language"])
        and _revision(value["source_revision"])
        and _image_digest(value["image_digest"])
        and _identifier(value["corpus_release_id"])
        and _identifier(value["task_version_id"])
        and all(_digest(value[name]) for name in DOCUMENT_SHA256_FIELDS),
        "approval document values are invalid",
    )
    return value


def _curator_public_key(path: Path) -> tuple[Ed25519PublicKey, str]:
    from ditto.api_server.coding_hippius_publication import (
        HippiusPrivateInputPublicationError,
        load_curator_signing_public_key,
    )

    _require(
        isinstance(path, Path) and path.is_absolute(), "input path must be absolute"
    )
    try:
        return load_curator_signing_public_key(path)
    except HippiusPrivateInputPublicationError:
        raise ProfileApprovalError("curator public key is invalid") from None


def verify_profile_approval(
    *,
    document: bytes,
    signature: bytes,
    curator_public_key_path: Path,
    curator_signing_key_sha256: str,
) -> VerifiedApproval:
    """Accept only a canonical document with a valid pinned-curator signature."""

    from cryptography.exceptions import InvalidSignature

    _require(
        _digest(curator_signing_key_sha256), "pinned curator key digest is invalid"
    )
    value = parse_profile_approval_document(document)
    _require(
        isinstance(signature, bytes) and len(signature) == SIGNATURE_BYTES,
        "curator signature is missing or malformed",
    )
    public_key, key_sha256 = _curator_public_key(curator_public_key_path)
    _require(
        key_sha256 == curator_signing_key_sha256,
        "curator public key differs from the pinned identity",
    )
    _require(
        value["curator_signing_key_sha256"] == key_sha256,
        "approval document names another curator key",
    )
    try:
        public_key.verify(signature, document)
    except InvalidSignature:
        raise ProfileApprovalError("curator signature does not verify") from None
    return VerifiedApproval(
        document=value,
        document_sha256=_sha(document),
        signature_sha256=_sha(signature),
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProfileApprovalError("arguments are invalid")


def _read(path: Path, maximum: int) -> bytes:
    from ditto.api_server.coding_hippius_publication import (
        HippiusPrivateInputPublicationError,
        _read_bounded_regular_file,
    )

    _require(path.is_absolute(), "input path must be absolute")
    try:
        return _read_bounded_regular_file(
            path, maximum_bytes=maximum, label="input file"
        )
    except HippiusPrivateInputPublicationError:
        raise ProfileApprovalError(
            "input file is not a readable bounded regular file"
        ) from None


def _read_profiles(directory: Path) -> tuple[bytes, bytes, bytes]:
    _require(
        directory.is_absolute()
        and not directory.is_symlink()
        and directory.is_dir()
        and sorted(os.listdir(directory)) == sorted(PROFILE_FILES),
        "profile directory is not an exact helper output",
    )
    execution, grading, receipt = PROFILE_FILES
    return (
        _read(directory / execution, 16384),
        _read(directory / grading, 65536),
        _read(directory / receipt, 1 << 16),
    )


def _write_new(path: Path, body: bytes) -> None:
    parent = path.parent
    _require(
        path.is_absolute()
        and not os.path.lexists(path)
        and not parent.is_symlink()
        and parent.is_dir()
        and stat.S_IMODE(parent.stat().st_mode) & 0o077 == 0,
        "output must be new in a protected directory",
    )
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | cloexec, 0o600
        )
    except OSError:
        raise ProfileApprovalError("output cannot be created") from None
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Make the new directory entry durable, as the qualification writers do.
        directory = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _build(argv: list[str]) -> str:
    parser = _Parser(add_help=False)
    for name in (
        "request",
        "profile-request",
        "profiles",
        "payload-authority",
        "registration",
        "release-index",
        "native-approval",
        "native-plan",
        "native-summary",
        "native-provenance",
        "curator-public-key",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    execution, grading, receipt = _read_profiles(args.profiles)
    _public_key, key_sha256 = _curator_public_key(args.curator_public_key)
    body = build_profile_approval(
        ApprovalInputs(
            request=_read(args.request, 1 << 16),
            profile_request=_read(args.profile_request, MAX_PROFILE_REQUEST_BYTES),
            execution_profile=execution,
            grading_profile=grading,
            profile_receipt=receipt,
            payload_authority=_read(args.payload_authority, 8 << 20),
            registration=_read(args.registration, 64 << 10),
            release_index=_read(args.release_index, 64 << 10),
            native_approval=_read(args.native_approval, 65536),
            native_plan=_read(args.native_plan, MAX_PLAN_BYTES),
            native_summary=_read(args.native_summary, 1 << 20),
            native_provenance=_read(args.native_provenance, 1 << 20),
        ),
        curator_signing_key_sha256=key_sha256,
    )
    _write_new(args.output, body)
    return f"approved=false\ndocument_sha256={_sha(body)}\n"


def _verify(argv: list[str]) -> str:
    parser = _Parser(add_help=False)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--curator-public-key", type=Path, required=True)
    parser.add_argument("--curator-signing-key-sha256", required=True)
    args = parser.parse_args(argv)
    document = _read(args.document, MAX_DOCUMENT_BYTES)
    try:
        signature = _read(args.signature, SIGNATURE_BYTES)
    except ProfileApprovalError:
        raise ProfileApprovalError(
            "curator signature is missing or malformed"
        ) from None
    verified = verify_profile_approval(
        document=document,
        signature=signature,
        curator_public_key_path=args.curator_public_key,
        curator_signing_key_sha256=args.curator_signing_key_sha256,
    )
    printed = {
        "document_sha256": verified.document_sha256,
        "signature_sha256": verified.signature_sha256,
        **{name: verified.document[name] for name in DOCUMENT_IDENTITY_FIELDS},
        **{name: verified.document[name] for name in DOCUMENT_SHA256_FIELDS},
    }
    return "".join(f"{name}={value}\n" for name, value in printed.items())


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        _require(
            bool(arguments) and arguments[0] in {"build", "verify"},
            "arguments are invalid",
        )
        command = _build if arguments[0] == "build" else _verify
        output = command(arguments[1:])
    except ProfileApprovalError as error:
        print(f"hosted profile approval rejected: {error}", file=sys.stderr)
        return 70
    except Exception:
        print("hosted profile approval rejected: internal error", file=sys.stderr)
        return 70
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
