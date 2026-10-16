"""Hosted-v2 profile approval builder and curator verifier tests.

Synthetic inputs only; the curator key is a throwaway Ed25519 key generated in
memory per test. No private key file, provider, database or live artifact.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ditto.coding_hosted_profile_approval as approval_module
from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_control import SourceBinding
from ditto.api_models.coding_private_catalog_v2 import CodingMemoryConditionV2
from ditto.api_server.coding_hippius_publication import (
    load_curator_signing_public_key,
)
from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceError
from ditto.api_server.coding_hosted_grading import (
    GRADING_PROFILE_KEYS as PLATFORM_GRADING_PROFILE_KEYS,
)
from ditto.api_server.coding_hosted_grading import expected_grading
from ditto.api_server.coding_private_v2_publication import (
    private_v2_publication_signing_message,
)
from ditto.coding_hosted_profile_approval import (
    DOCUMENT_FIELDS,
    DOCUMENT_IDENTITY_FIELDS,
    DOCUMENT_SCHEMA,
    DOCUMENT_SHA256_FIELDS,
    GRADING_PROFILE_KEYS,
    LANGUAGE_PROFILES,
    REQUEST_SCHEMA,
    ApprovalInputs,
    ProfileApprovalError,
    build_profile_approval,
    main,
    parse_profile_approval_document,
    verify_profile_approval,
)
from ditto.tests.api_server import test_coding_hosted_profiles as profiles_helper

REPO = Path(__file__).resolve().parents[4]
REVISION = "c0ffee" + "1" * 34
IMAGE_DIGEST = "sha256:" + "9" * 64
LANGUAGE = "python"
INDEX = 3
CONDITIONS = tuple(condition.value for condition in CodingMemoryConditionV2)
GROUP = "private-group-000"
NATIVE = "approved_native_oci_manifest"
# Task-bound sandbox values the helper takes from the private resource profile.
PRIVATE_RESOURCES = {
    "MemoryLimitBytes": 1024 << 20,
    "ScratchLimitBytes": 512 << 20,
    "PidsLimit": 256,
    "CPUQuotaMillis": 2000,
}


def _h(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


CONTRACT = _h("hosted-grader-contract")


def _canonical(value: dict[str, Any]) -> bytes:
    return coding_canonical_json_bytes(value, maximum_bytes=8 << 20, label="test")


def _compact(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _compact_line(value: dict[str, Any]) -> bytes:
    return _compact(value) + b"\n"


ENCODERS: dict[str, Callable[[dict[str, Any]], bytes]] = {
    "profile_request": lambda value: json.dumps(value).encode(),
    "execution_profile": _canonical,
    "grading_profile": _canonical,
    "profile_receipt": _canonical,
    "payload_authority": _canonical,
    "registration": _canonical,
    "release_index": _compact,
    "native_approval": _compact_line,
    "native_plan": _compact_line,
    "native_provenance": _compact_line,
    "native_summary": _compact_line,
    "request": lambda value: json.dumps(value, indent=2).encode(),
}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _image_ref(language: str) -> str:
    digest = IMAGE_DIGEST if language == LANGUAGE else "sha256:" + _h(language)
    return f"coding-runtime.invalid/{language}/runtime@{digest}"


def _command(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "ID": value["id"],
        "Argv": value["argv"],
        "Timeout": value["timeout_milliseconds"] * 1_000_000,
    }


def _profiles(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The helper's projection of a profile request into both profiles."""

    request = copy.deepcopy(request)
    policy = {
        "CandidateLimits": request["candidate_limits"],
        "ProtectedLimits": request["protected_limits"],
        "MaxCombinedDiskBytes": request["max_combined_disk_bytes"],
        **PRIVATE_RESOURCES,
    }
    execution = {
        "schema": "dittobench-coding-hosted-authoring-profile-v2",
        "image_digest": request["image_digest"],
        "resource_policy": copy.deepcopy(policy),
        "budgets": dict(request["budgets"]),
    }
    grading = {
        "schema": "dittobench-coding-hosted-grading-profile-v2",
        "image_digest": request["image_digest"],
        "grader_contract_sha256": CONTRACT,
        "grader_bundle_sha256": _h("grader"),
        "resource_policy": copy.deepcopy(policy),
        "build": {
            "Required": request["build"]["required"],
            "Command": _command(request["build"]["command"]),
        },
        "test_groups": [
            {
                "Group": group["group"],
                "Command": _command(group["command"]),
                "ExpectedTotal": group["expected_total"],
            }
            for group in request["test_groups"]
        ],
        "execution_timeout": request["execution_timeout_milliseconds"] * 1_000_000,
    }
    return execution, grading


def _plan_cases(
    group_id: str, language: str, test_groups: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """prepare.py cases: each phase runs for the base and reference workspace."""

    return [
        {
            "schema": "dittobench-private-compatibility-case-v1",
            "language": language,
            "group_id": group_id,
            "role": role,
            "phase": group["Group"],
            "expected_total": group["ExpectedTotal"],
            "argv": list(group["Command"]["Argv"]),
            "workspace": [],
            "grader": [],
            "image_sha256": "0" * 64,
        }
        for group in reversed(test_groups)
        for role in ("base", "reference")
    ]


def _objects(curator_sha256: str) -> dict[str, dict[str, Any]]:
    evidence = {
        name: _h(name)
        for name in (
            "host_preflight",
            "network_enforcement",
            "resource_enforcement",
            "preexec_confinement",
            "cleanup_recovery",
            "private_input_custody",
        )
    }
    profile_request = {**profiles_helper.request(), "catalog_index": INDEX}
    execution, grading = _profiles(profile_request)
    cases = _plan_cases(GROUP, LANGUAGE, grading["test_groups"])
    for offset, language in enumerate(("node", "go", "rust"), start=1):
        other = copy.deepcopy(grading["test_groups"])
        for group in other:
            group["Command"]["Argv"] = [
                "dittobench-test-driver",
                "--group",
                group["Group"],
                "--suite",
                f"{language}-suite",
            ]
        cases += _plan_cases(f"private-group-{900 + offset:03d}", language, other)
    return {
        "profile_request": profile_request,
        "execution_profile": execution,
        "grading_profile": grading,
        "payload_authority": {
            "schema": "dittobench-coding-private-payload-v2",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "catalog_sha256": _h("catalog"),
            "catalog_merkle_root": _h("merkle"),
            "task_version_count": 5,
            "objects": [],
            "task_assets": [
                {
                    "catalog_index": index,
                    "task_version_id": f"{GROUP}-{CONDITIONS[index]}",
                    "task_commitment_sha256": _h(f"commitment-{index}"),
                    "artifacts": {
                        "grader_bundle": _h("grader" if index == INDEX else str(index))
                    },
                }
                for index in range(5)
            ],
        },
        "registration": {
            "schema": "dittobench-coding-private-v2-registration-v1",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "shadow_only": True,
            "corpus_release_id": "coding-private-v2-test",
            "private_release_sha256": _h("private-release"),
            "transport_sha256": _h("transport"),
            "wrapping_key_sha256": _h("wrapping-key"),
            "publication_receipt_sha256": _h("publication"),
            "previous_registration_sha256": None,
        },
        "profile_receipt": {
            "schema": "dittobench-coding-hosted-profile-receipt-v1",
            "catalog_index": INDEX,
            "task_version_id": f"{GROUP}-{CONDITIONS[INDEX]}",
            "task_commitment_sha256": _h(f"commitment-{INDEX}"),
            "corpus_release_id": "coding-private-v2-test",
            "private_release_sha256": _h("private-release"),
            "image_digest": IMAGE_DIGEST,
            "max_patch_bytes": profile_request["candidate_limits"]["MaxPatchBytes"],
            "grader_bundle_sha256": _h("grader"),
            "grader_contract_sha256": CONTRACT,
            "launch_checks_passed": True,
            "approved": False,
            "shadow_only": True,
            "weight_eligible": False,
        },
        "release_index": {
            "schema": "dittobench-coding-native-release-set-v2",
            "source_revision": REVISION,
            "images": {
                language: {
                    "archive": f"{language}/runtime.oci.tar",
                    "approval": f"{language}/approval.json",
                    "approval_sha256": _h(f"image-approval-{language}"),
                    "archive_sha256": _h(f"archive-{language}"),
                    "image_ref": _image_ref(language),
                    "config_digest": "sha256:" + _h(f"config-{language}"),
                    "driver_profile": profile,
                }
                for language, profile in LANGUAGE_PROFILES.items()
            },
            "runtime": {"archive_sha256": _h("runtime")},
            "independent_approval_required": True,
            "native_imported": False,
            "runtime_qualification": False,
            "canary_completed": False,
            "shadow_only": True,
            "weight_eligible": False,
        },
        "native_approval": {
            "schema": "dittobench-coding-native-controls-approval-v2",
            "purpose": "private-compatibility-once",
            "source_revision": REVISION,
            "helper_sha256": _h("helper"),
            "runner_sha256": _h("runner"),
            "binding_sha256": _h("binding"),
            "machine_id_sha256": _h("machine"),
            "boot_id": "01234567-89ab-cdef-0123-456789abcdef",
            "issued_at_unix": 1_900_000_000,
            "expires_at_unix": 1_900_003_600,
            "controls": 2 * len(cases),
            "max_jobs": 2,
            "evidence_sha256": evidence,
            "shadow_only": True,
            "weight_eligible": False,
        },
        "native_plan": {
            "schema": "dittobench-private-compatibility-plan-v1",
            "source_sha": REVISION,
            "replicates": 2,
            "cases": cases,
            "production_api_approval": False,
            "node_count_inventory_sha256": _h("node-counts"),
            "preparer_sha256": _h("preparer"),
        },
        "native_provenance": {
            "source_sha": REVISION,
            "image_references_sha256": _h("image-references"),
            "helper_sha256": _h("helper"),
            "runner_sha256": _h("runner"),
            "kernel": "6.8.0-test",
            "runtime_qualification": False,
            "production_api_approval": False,
            "image_binding_kind": NATIVE,
        },
        "native_summary": {
            "schema": "dittobench-private-compatibility-summary-v1",
            "source_sha": REVISION,
            "controls": 2 * len(cases),
            "cases": len(cases),
            "replicates": 2,
            "languages": sorted(LANGUAGE_PROFILES),
            "groups_by_language": dict.fromkeys(LANGUAGE_PROFILES, 1),
            "repeat_results_equal": True,
            "failed_controls": 0,
            "private_controls_passed": True,
            "runtime_qualification": False,
            "production_api_approval": False,
            "native_host_ready": False,
            "canary_completed": False,
            "weight_eligible": False,
            "native_controls_passed": True,
            "image_binding_kind": NATIVE,
        },
        "request": {
            "schema": REQUEST_SCHEMA,
            "catalog_index": INDEX,
            "language": LANGUAGE,
            "source_revision": REVISION,
            "grader_contract_sha256": CONTRACT,
            "curator_signing_key_sha256": curator_sha256,
            "shadow_only": True,
            "weight_eligible": False,
        },
    }


Mutation = Callable[[dict[str, Any]], object]
BytesMutation = Callable[[dict[str, bytes]], None]


def _inputs(
    curator_sha256: str,
    *,
    before: Mutation | None = None,
    transform: dict[str, Callable[[bytes], bytes]] | None = None,
    tamper: BytesMutation | None = None,
) -> ApprovalInputs:
    """Encode consistent inputs; dependents link to each input's final bytes.

    ``before`` edits objects before linking, ``transform`` rewrites one input's
    bytes before its dependents are linked, and ``tamper`` edits bytes after all
    linking so no dependent digest or pin follows.
    """

    objects = _objects(curator_sha256)
    if before is not None:
        before(objects)
    transforms = transform or {}
    out: dict[str, bytes] = {}

    def emit(name: str) -> dict[str, Any]:
        body = ENCODERS[name](objects[name])
        out[name] = transforms.get(name, lambda value: value)(body)
        return json.loads(out[name])

    emit("profile_request")
    emit("execution_profile")
    emit("grading_profile")
    payload = objects["payload_authority"]
    payload["payload_sha256"] = _sha(
        _canonical({k: v for k, v in payload.items() if k != "payload_sha256"})
    )
    payload = emit("payload_authority")
    registration = objects["registration"]
    registration.update(
        payload_sha256=payload["payload_sha256"],
        catalog_sha256=payload["catalog_sha256"],
        catalog_merkle_root=payload["catalog_merkle_root"],
    )
    registration["registration_sha256"] = _sha(
        _canonical(
            {k: v for k, v in registration.items() if k != "registration_sha256"}
        )
    )
    registration = emit("registration")
    objects["profile_receipt"].update(
        request_sha256=_sha(out["profile_request"]),
        execution_profile_sha256=_sha(out["execution_profile"]),
        grading_profile_sha256=_sha(out["grading_profile"]),
        payload_authority_file_sha256=_sha(out["payload_authority"]),
        payload_sha256=payload["payload_sha256"],
    )
    emit("profile_receipt")
    release = emit("release_index")
    emit("native_plan")
    approval = objects["native_approval"]
    approval["plan_sha256"] = _sha(out["native_plan"])
    approval["release_manifest_sha256"] = _sha(out["release_index"])
    approval["images"] = {
        language: {
            field: release["images"][language][field]
            for field in (
                "image_ref",
                "config_digest",
                "approval_sha256",
                "driver_profile",
            )
        }
        for language in release["images"]
    }
    approval = emit("native_approval")
    authority = {
        "approval_sha256": _sha(out["native_approval"]),
        "release_manifest_sha256": _sha(out["release_index"]),
        "machine_id_sha256": approval["machine_id_sha256"],
        "boot_id": approval["boot_id"],
        "evidence_sha256": approval["evidence_sha256"],
        "daemon_identity_sha256": _h("daemon"),
    }
    objects["native_provenance"].setdefault(
        "images",
        {
            language: {
                "id": "sha256:" + _h(f"id-{language}"),
                "descriptor": None,
                "repo_digests": [release["images"][language]["image_ref"]],
            }
            for language in release["images"]
        },
    )
    objects["native_provenance"]["plan_sha256"] = _sha(out["native_plan"])
    objects["native_provenance"]["native_control_authority"] = authority
    objects["native_summary"]["native_control_authority"] = authority
    emit("native_provenance")
    emit("native_summary")
    objects["request"].update(
        registration_sha256=registration["registration_sha256"],
        release_manifest_sha256=_sha(out["release_index"]),
        native_controls_approval_sha256=_sha(out["native_approval"]),
        native_controls_provenance_sha256=_sha(out["native_provenance"]),
        native_controls_summary_sha256=_sha(out["native_summary"]),
    )
    emit("request")
    if tamper is not None:
        tamper(out)
    return ApprovalInputs(**out)


def _patch(name: str, mutate: Mutation) -> BytesMutation:
    def apply(out: dict[str, bytes]) -> None:
        value = json.loads(out[name])
        mutate(value)
        out[name] = ENCODERS[name](value)

    return apply


def _relinked(name: str, mutate: Mutation) -> dict[str, Any]:
    """Edit one emitted input so every dependent digest and pin still follows."""

    def apply(body: bytes) -> bytes:
        value = json.loads(body)
        mutate(value)
        return ENCODERS[name](value)

    return {"transform": {name: apply}}


def _replace(body: bytes) -> Callable[[bytes], bytes]:
    return lambda _old: body


def _pretty(body: bytes) -> bytes:
    return json.dumps(json.loads(body), indent=2, sort_keys=True).encode() + b"\n"


@pytest.fixture
def curator(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path, str]:
    private = Ed25519PrivateKey.generate()
    return (private, *_public_key_file(tmp_path, private, "curator"))


def _public_key_file(
    root: Path, private: Ed25519PrivateKey, name: str
) -> tuple[Path, str]:
    path = root / f"{name}-public.pem"
    path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return path, load_curator_signing_public_key(path)[1]


def test_build_is_deterministic_and_binds_every_recomputed_digest(curator) -> None:
    _private, _path, key_sha256 = curator
    inputs = _inputs(key_sha256)
    body = build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    assert body == build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    assert body.endswith(b"\n") and body.count(b"\n") == 1
    document = parse_profile_approval_document(body)
    assert set(document) == DOCUMENT_FIELDS
    assert "approved" not in document
    registration = json.loads(inputs.registration)
    payload = json.loads(inputs.payload_authority)
    release = json.loads(inputs.release_index)
    assert json.loads(inputs.native_approval)["plan_sha256"] == _sha(inputs.native_plan)
    assert document == {
        "schema": DOCUMENT_SCHEMA,
        "coding_contract_version": 2,
        "catalog_index": INDEX,
        "task_version_id": f"{GROUP}-v3_stale_conflict",
        "task_commitment_sha256": _h(f"commitment-{INDEX}"),
        "corpus_release_id": "coding-private-v2-test",
        "registration_sha256": registration["registration_sha256"],
        "private_release_sha256": _h("private-release"),
        "payload_sha256": payload["payload_sha256"],
        "payload_authority_file_sha256": _sha(inputs.payload_authority),
        "profile_receipt_sha256": _sha(inputs.profile_receipt),
        "execution_profile_sha256": _sha(inputs.execution_profile),
        "grading_profile_sha256": _sha(inputs.grading_profile),
        "grader_contract_sha256": CONTRACT,
        "grader_bundle_sha256": _h("grader"),
        "image_digest": IMAGE_DIGEST,
        "language": LANGUAGE,
        "source_revision": REVISION,
        "release_manifest_sha256": _sha(inputs.release_index),
        "image_approval_sha256": release["images"][LANGUAGE]["approval_sha256"],
        "native_controls_approval_sha256": _sha(inputs.native_approval),
        "native_controls_plan_sha256": _sha(inputs.native_plan),
        "native_controls_provenance_sha256": _sha(inputs.native_provenance),
        "native_controls_summary_sha256": _sha(inputs.native_summary),
        "curator_signing_key_sha256": key_sha256,
        "shadow_only": True,
        "weight_eligible": False,
    }


def _receipt(mutate: Mutation) -> dict[str, Any]:
    return {"tamper": _patch("profile_receipt", mutate)}


def _summary(**changes: Any) -> dict[str, Any]:
    return _relinked("native_summary", lambda value: value.update(changes))


def _provenance(**changes: Any) -> dict[str, Any]:
    return _relinked("native_provenance", lambda value: value.update(changes))


def _request(**changes: Any) -> dict[str, Any]:
    return {"tamper": _patch("request", lambda value: value.update(changes))}


def _before(name: str, mutate: Mutation) -> dict[str, Any]:
    return {"before": lambda objects: mutate(objects[name])}


def _both_profiles(mutate: Mutation) -> dict[str, Any]:
    def apply(objects: dict[str, Any]) -> None:
        mutate(objects["execution_profile"])
        mutate(objects["grading_profile"])

    return {"before": apply}


def _edit_json(**changes: Any) -> Callable[[bytes], bytes]:
    def apply(body: bytes) -> bytes:
        value = json.loads(body)
        value.update(changes)
        return _canonical(value)

    return apply


def _group_cases(mutate: Callable[[list[dict[str, Any]]], object]) -> dict[str, Any]:
    def apply(plan: dict[str, Any]) -> None:
        mutate([case for case in plan["cases"] if case["group_id"] == GROUP])

    return _before("native_plan", apply)


def _set_path(path: list[Any], value: Any) -> Mutation:
    def mutate(body: dict[str, Any]) -> None:
        target: Any = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _without(path: list[Any]) -> Mutation:
    def mutate(body: dict[str, Any]) -> None:
        target: Any = body
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


REJECTIONS = [
    # Profile set and receipt.
    pytest.param(
        {
            "tamper": _patch(
                "execution_profile",
                lambda value: value["budgets"].update(wall_time_seconds=601),
            )
        },
        "execution profile bytes differ from the receipt",
        id="tampered-execution-profile",
    ),
    pytest.param(
        _receipt(lambda value: value.update(grading_profile_sha256=_h("other"))),
        "grading profile bytes differ from the receipt",
        id="receipt-grading-digest-mismatch",
    ),
    pytest.param(
        _receipt(lambda value: value.update(approved=True)),
        "profile receipt claims approval",
        id="receipt-approved-true",
    ),
    pytest.param(
        _receipt(lambda value: value.update(launch_checks_passed=False)),
        "profile launch checks failed",
        id="launch-checks-false",
    ),
    pytest.param(
        _receipt(lambda value: value.update(weight_eligible=True)),
        "profile receipt is not shadow-only",
        id="receipt-weight-eligible",
    ),
    pytest.param(
        _receipt(lambda value: value.update(test_manifest_sha256=_h("tests"))),
        "profile receipt schema is invalid",
        id="receipt-unknown-field",
    ),
    pytest.param(
        {
            "tamper": lambda out: out.update(
                profile_receipt=_pretty(out["profile_receipt"])
            )
        },
        "profile receipt is not canonical",
        id="receipt-noncanonical",
    ),
    pytest.param(
        {"transform": {"execution_profile": _pretty}},
        "execution profile is not canonical",
        id="execution-profile-noncanonical-relinked",
    ),
    pytest.param(
        _before(
            "grading_profile",
            lambda value: value.update(test_manifest_sha256=_h("tests")),
        ),
        "profile schema is invalid",
        id="grading-profile-test-manifest",
    ),
    pytest.param(
        _before("execution_profile", lambda value: value.update(extra=1)),
        "profile schema is invalid",
        id="execution-profile-unknown-key",
    ),
    pytest.param(
        _before(
            "grading_profile",
            lambda value: value.update(image_digest="sha256:" + _h("other")),
        ),
        "profile image digests disagree",
        id="grading-image-drift",
    ),
    pytest.param(
        _request(grader_contract_sha256=_h("other-contract")),
        "grader contract differs from the reviewed pin",
        id="grader-contract-pin",
    ),
    pytest.param(
        _before(
            "grading_profile",
            lambda value: value.update(grader_bundle_sha256=_h("other")),
        ),
        "grader bundle differs from the receipt",
        id="grader-bundle-drift",
    ),
    pytest.param(
        _before("grading_profile", _set_path(["resource_policy", "PidsLimit"], 128)),
        "grading resource policy differs from the execution profile",
        id="grading-resource-policy-drift",
    ),
    pytest.param(
        _before(
            "grading_profile",
            _set_path(["resource_policy", "CandidateLimits", "MaxPatchBytes"], 2),
        ),
        "grading resource policy differs from the execution profile",
        id="grading-patch-limit-only",
    ),
    pytest.param(
        _both_profiles(
            _set_path(["resource_policy", "CandidateLimits", "MaxPatchBytes"], 2)
        ),
        "patch limit differs from the receipt",
        id="patch-limit-drift",
    ),
    # Profile structure.
    pytest.param(
        _before("execution_profile", _set_path(["budgets", "model_output_tokens"], 0)),
        "execution profile structure is invalid",
        id="execution-budget-zero",
    ),
    pytest.param(
        _before("execution_profile", _set_path(["budgets", "wall_time_seconds"], 3601)),
        "execution profile structure is invalid",
        id="execution-wall-over-hour",
    ),
    pytest.param(
        _before(
            "execution_profile", _set_path(["budgets", "workspace_tool_calls"], 127)
        ),
        "execution profile structure is invalid",
        id="execution-tool-calls-drift",
    ),
    pytest.param(
        _both_profiles(_without(["resource_policy", "PidsLimit"])),
        "execution profile structure is invalid",
        id="resource-policy-missing-field",
    ),
    pytest.param(
        _both_profiles(
            _set_path(["resource_policy", "ProtectedLimits", "MaxEntries"], True)
        ),
        "execution profile structure is invalid",
        id="resource-policy-boolean-limit",
    ),
    pytest.param(
        _before("grading_profile", lambda value: value["test_groups"].reverse()),
        "grading profile structure is invalid",
        id="grading-group-order",
    ),
    pytest.param(
        _before("grading_profile", _set_path(["test_groups", 0, "ExpectedTotal"], 0)),
        "grading profile structure is invalid",
        id="grading-zero-count",
    ),
    pytest.param(
        _before(
            "grading_profile",
            _set_path(["test_groups", 1, "Command", "Argv", 0], "python"),
        ),
        "grading profile structure is invalid",
        id="grading-untrusted-driver",
    ),
    pytest.param(
        _before(
            "grading_profile",
            _set_path(["test_groups", 1, "Command", "ID"], "hidden-tests"),
        ),
        "grading profile structure is invalid",
        id="grading-duplicate-command-id",
    ),
    pytest.param(
        _before("grading_profile", _set_path(["execution_timeout"], 600_000_000_001)),
        "grading profile structure is invalid",
        id="grading-timeout-not-milliseconds",
    ),
    pytest.param(
        _before(
            "grading_profile",
            _set_path(["build", "Command", "Timeout"], 601 * 1_000_000_000),
        ),
        "grading profile structure is invalid",
        id="grading-command-timeout-over-bound",
    ),
    # Profile request.
    pytest.param(
        {"tamper": _patch("profile_request", lambda value: value.update(extra=1))},
        "profile request bytes differ from the receipt",
        id="profile-request-bytes-drift",
    ),
    pytest.param(
        _before("profile_request", lambda value: value.update(extra=1)),
        "profile request schema is invalid",
        id="profile-request-unknown-field",
    ),
    pytest.param(
        _before("profile_request", lambda value: value.update(weight_eligible=True)),
        "profile request schema is invalid",
        id="profile-request-weight-eligible",
    ),
    pytest.param(
        _before(
            "profile_request",
            lambda value: value.update(image_digest="sha256:" + _h("other")),
        ),
        "profiles differ from the profile request",
        id="profile-request-image-drift",
    ),
    pytest.param(
        _before(
            "profile_request",
            _set_path(["candidate_limits", "MaxPatchBytes"], 2),
        ),
        "profiles differ from the profile request",
        id="profile-request-limit-drift",
    ),
    pytest.param(
        _before("profile_request", _set_path(["budgets", "model_input_tokens"], 1)),
        "profiles differ from the profile request",
        id="profile-request-budget-drift",
    ),
    pytest.param(
        _before(
            "profile_request",
            _set_path(["test_groups", 0, "command", "argv", 4], "other_test.py"),
        ),
        "profiles differ from the profile request",
        id="profile-request-driver-argv-drift",
    ),
    pytest.param(
        _before(
            "profile_request",
            _set_path(["build", "command", "timeout_milliseconds"], 30001),
        ),
        "profiles differ from the profile request",
        id="profile-request-build-timeout-drift",
    ),
    pytest.param(
        _before(
            "profile_request",
            lambda value: value.update(execution_timeout_milliseconds=600001),
        ),
        "profiles differ from the profile request",
        id="profile-request-execution-timeout-drift",
    ),
    pytest.param(
        _before(
            "profile_request", _set_path(["test_groups", 1, "expected_total"], True)
        ),
        "profiles differ from the profile request",
        id="profile-request-boolean-count",
    ),
    # Catalog index and private-v2 identity.
    pytest.param(
        _request(catalog_index=INDEX + 1),
        "profile catalog index differs from the reviewed index",
        id="catalog-index-mismatch",
    ),
    pytest.param(
        {
            "before": lambda objects: (
                objects["request"].update(catalog_index=99),
                objects["profile_receipt"].update(catalog_index=99),
                objects["profile_request"].update(catalog_index=99),
            )
        },
        "reviewed catalog index is not exactly one payload task",
        id="catalog-index-absent-from-payload",
    ),
    pytest.param(
        {
            "tamper": _patch(
                "payload_authority",
                lambda value: value.update(catalog_sha256=_h("other")),
            )
        },
        "payload authority bytes differ from the receipt",
        id="tampered-payload-authority",
    ),
    pytest.param(
        {"transform": {"payload_authority": _edit_json(task_version_count=6)}},
        "payload authority digest is invalid",
        id="payload-self-digest-stale",
    ),
    pytest.param(
        _before(
            "payload_authority",
            lambda value: value["task_assets"][INDEX].update(
                task_commitment_sha256=_h("other")
            ),
        ),
        "payload task differs from the profile receipt",
        id="payload-task-commitment-drift",
    ),
    pytest.param(
        _request(registration_sha256=_h("live-registration")),
        "registration differs from the reviewed pin",
        id="registration-pin",
    ),
    pytest.param(
        _before(
            "registration",
            lambda value: value.update(private_release_sha256=_h("other-release")),
        ),
        "registered release differs from the profile task",
        id="registration-private-release-drift",
    ),
    pytest.param(
        {"transform": {"registration": _edit_json(corpus_release_id="other")}},
        "registration authority is invalid",
        id="registration-self-digest-stale",
    ),
    # Release set.
    pytest.param(
        _request(release_manifest_sha256=_h("other-release-index")),
        "release index differs from the reviewed pin",
        id="release-index-pin",
    ),
    pytest.param(
        _before("release_index", lambda value: value.update(source_revision="d" * 40)),
        "release revision differs from the reviewed revision",
        id="release-revision-mismatch",
    ),
    pytest.param(
        _request(source_revision="d" * 40),
        "release revision differs from the reviewed revision",
        id="request-revision-mismatch",
    ),
    pytest.param(
        _before("release_index", lambda value: value.update(native_imported=True)),
        "release index is invalid",
        id="release-readiness-edited",
    ),
    pytest.param(
        {"transform": {"release_index": _pretty}},
        "release index is not its writer's exact encoding",
        id="release-index-noncanonical-relinked",
    ),
    pytest.param(
        _before(
            "release_index",
            _set_path(["images", "go", "driver_profile"], "go-call-ast-v0"),
        ),
        "release images are invalid",
        id="release-image-driver-profile",
    ),
    pytest.param(
        _before("release_index", _without(["images", "rust"])),
        "release images are invalid",
        id="release-image-missing-language",
    ),
    pytest.param(
        _before(
            "release_index", _set_path(["images", "node", "approval_sha256"], "0" * 64)
        ),
        "release images are invalid",
        id="release-image-approval-digest",
    ),
    pytest.param(
        _request(language="node"),
        "profile image is not the reviewed language's release image",
        id="language-image-mismatch",
    ),
    # Native compatibility controls.
    pytest.param(
        _request(native_controls_approval_sha256=_h("other-approval")),
        "native approval differs from the reviewed pin",
        id="native-approval-pin",
    ),
    pytest.param(
        _before(
            "native_approval", lambda value: value.update(source_revision="d" * 40)
        ),
        "native approval revision differs from the reviewed revision",
        id="native-approval-revision-mismatch",
    ),
    pytest.param(
        _relinked(
            "native_approval",
            lambda value: value.update(release_manifest_sha256=_h("other")),
        ),
        "native approval names another release set",
        id="native-approval-other-release",
    ),
    pytest.param(
        _before("native_approval", lambda value: value.update(extra=True)),
        "native approval is invalid",
        id="native-approval-unknown-field",
    ),
    pytest.param(
        _relinked(
            "native_approval",
            _set_path(["images", "python", "config_digest"], "sha256:" + _h("other")),
        ),
        "native approval images differ from the release set",
        id="native-approval-image-config-drift",
    ),
    pytest.param(
        _relinked("native_approval", _without(["images", "go"])),
        "native approval images differ from the release set",
        id="native-approval-image-missing",
    ),
    # Private compatibility plan.
    pytest.param(
        {"tamper": _patch("native_plan", lambda value: value.update(extra=1))},
        "native plan differs from the native approval",
        id="native-plan-bytes-drift",
    ),
    pytest.param(
        {"transform": {"native_plan": _pretty}},
        "native plan is not its writer's exact encoding",
        id="native-plan-noncanonical-relinked",
    ),
    pytest.param(
        _before("native_plan", lambda value: value.update(source_sha="d" * 40)),
        "native plan is invalid",
        id="native-plan-revision-mismatch",
    ),
    pytest.param(
        _before(
            "native_plan", lambda value: value.update(production_api_approval=True)
        ),
        "native plan is invalid",
        id="native-plan-production-approval",
    ),
    pytest.param(
        _before("native_plan", lambda value: value.update(cases=[])),
        "native plan is invalid",
        id="native-plan-empty",
    ),
    pytest.param(
        {
            "before": lambda objects: [
                target.update(task_version_id=f"{GROUP}-v9_unknown")
                for target in (
                    objects["payload_authority"]["task_assets"][INDEX],
                    objects["profile_receipt"],
                )
            ]
        },
        "profile task is not a private group condition",
        id="task-version-not-a-condition",
    ),
    pytest.param(
        _before(
            "native_plan",
            lambda value: value.update(
                cases=[case for case in value["cases"] if case["group_id"] != GROUP]
            ),
        ),
        "native plan does not cover the profile task group",
        id="native-plan-group-absent",
    ),
    pytest.param(
        _group_cases(lambda cases: cases[0].update(role="reference")),
        "native plan does not cover the profile task group",
        id="native-plan-group-role-missing",
    ),
    pytest.param(
        _group_cases(lambda cases: cases[0].update(role=["base"])),
        "native plan does not cover the profile task group",
        id="native-plan-group-unhashable-role",
    ),
    pytest.param(
        _group_cases(lambda cases: [case.update(language="node") for case in cases]),
        "native plan language differs from the reviewed language",
        id="native-plan-group-language",
    ),
    pytest.param(
        _group_cases(lambda cases: cases[3]["argv"].append("--extra")),
        "grading test groups differ from the native plan",
        id="native-plan-reference-argv-drift",
    ),
    pytest.param(
        _group_cases(lambda cases: cases[0].update(expected_total=2)),
        "grading test groups differ from the native plan",
        id="native-plan-base-count-drift",
    ),
    pytest.param(
        _group_cases(lambda cases: cases[2].update(expected_total=True)),
        "grading test groups differ from the native plan",
        id="native-plan-boolean-count",
    ),
    # Native control outputs.
    pytest.param(
        {"tamper": _patch("native_provenance", lambda value: value.update(kernel="x"))},
        "native provenance differs from the reviewed pin",
        id="native-provenance-pin",
    ),
    pytest.param(
        _request(native_controls_summary_sha256=_h("other-summary")),
        "native summary differs from the reviewed pin",
        id="native-summary-pin",
    ),
    pytest.param(
        {"tamper": _patch("native_summary", lambda value: value.update(controls=64))},
        "native summary differs from the reviewed pin",
        id="native-summary-bytes-drift",
    ),
    pytest.param(
        _provenance(source_sha="d" * 40),
        "native control revision differs from the reviewed revision",
        id="provenance-revision-mismatch",
    ),
    pytest.param(
        _summary(source_sha="d" * 40),
        "native control revision differs from the reviewed revision",
        id="summary-revision-mismatch",
    ),
    pytest.param(
        _relinked(
            "native_summary",
            lambda value: value["native_control_authority"].update(
                daemon_identity_sha256=_h("other-daemon")
            ),
        ),
        "native control authority differs from the approval",
        id="native-authority-drift",
    ),
    pytest.param(
        _provenance(plan_sha256=_h("other-plan")),
        "native control inputs differ from the approval",
        id="native-plan-drift",
    ),
    pytest.param(
        _relinked(
            "native_summary",
            lambda value: [
                value.pop(name)
                for name in (
                    "native_control_authority",
                    "native_controls_passed",
                    "image_binding_kind",
                )
            ],
        ),
        "native control outputs are not native-bound",
        id="local-matrix-summary",
    ),
    pytest.param(
        _provenance(image_binding_kind="local_config_id_not_native_import_approval"),
        "native provenance is not an approved native binding",
        id="local-image-binding",
    ),
    pytest.param(
        _provenance(runtime_qualification=True),
        "native provenance is not an approved native binding",
        id="provenance-runtime-qualification",
    ),
    pytest.param(
        _provenance(production_api_approval=True),
        "native provenance is not an approved native binding",
        id="provenance-production-api-approval",
    ),
    pytest.param(
        _relinked(
            "native_provenance",
            _set_path(["images", LANGUAGE, "repo_digests"], []),
        ),
        "native controls did not run the profile image",
        id="native-image-not-run",
    ),
    pytest.param(
        _summary(native_controls_passed=False),
        "native compatibility controls did not pass",
        id="native-controls-failed",
    ),
    pytest.param(
        _summary(private_controls_passed=False),
        "native compatibility controls did not pass",
        id="private-controls-failed",
    ),
    pytest.param(
        _summary(failed_controls=1),
        "native compatibility controls did not pass",
        id="native-failed-control-count",
    ),
    pytest.param(
        _summary(repeat_results_equal=False),
        "native compatibility controls did not pass",
        id="native-repeats-unequal",
    ),
    pytest.param(
        _summary(controls=30),
        "native compatibility controls did not pass",
        id="native-controls-incomplete",
    ),
    pytest.param(
        _before("native_approval", lambda value: value.update(controls=34)),
        "native compatibility controls did not pass",
        id="summary-controls-differ-from-approval",
    ),
    pytest.param(
        _summary(cases=15),
        "native compatibility controls did not pass",
        id="summary-cases-differ-from-plan",
    ),
    pytest.param(
        _summary(replicates=3),
        "native compatibility controls did not pass",
        id="summary-replicates-invalid",
    ),
    pytest.param(
        _summary(languages=["go", "node", "python"]),
        "native compatibility controls did not pass",
        id="summary-languages-incomplete",
    ),
    pytest.param(
        _summary(native_host_ready=True),
        "native compatibility controls did not pass",
        id="native-summary-readiness-edited",
    ),
    # Reviewed request.
    pytest.param(
        _request(approved=True),
        "approval request is invalid",
        id="request-unknown-field",
    ),
    pytest.param(
        _request(weight_eligible=True),
        "approval request is invalid",
        id="request-weight-eligible",
    ),
    pytest.param(
        _request(catalog_index=True),
        "approval request is invalid",
        id="request-boolean-index",
    ),
    pytest.param(
        _request(language=["python"]),
        "approval request is invalid",
        id="request-unhashable-language",
    ),
    pytest.param(
        _request(language={"python": True}),
        "approval request is invalid",
        id="request-object-language",
    ),
]


@pytest.mark.parametrize(("case", "reason"), REJECTIONS)
def test_builder_rejects_drift(curator, case: dict[str, Any], reason: str) -> None:
    _private, _path, key_sha256 = curator
    inputs = _inputs(key_sha256, **case)
    with pytest.raises(ProfileApprovalError) as error:
        build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    assert str(error.value) == reason


def test_builder_rejects_a_curator_key_other_than_the_reviewed_pin(curator) -> None:
    _private, _path, key_sha256 = curator
    with pytest.raises(ProfileApprovalError, match="differs from the reviewed pin"):
        build_profile_approval(
            _inputs(key_sha256), curator_signing_key_sha256=_h("other-key")
        )


def test_rejections_never_name_private_plan_or_request_content(curator) -> None:
    _private, _path, key_sha256 = curator
    secret = "private-suite-name-" + _h("secret")[:16]

    def mutate(objects: dict[str, Any]) -> None:
        for case in objects["native_plan"]["cases"]:
            if case["group_id"] == GROUP and case["role"] == "reference":
                case["argv"] = [*case["argv"], secret]

    with pytest.raises(ProfileApprovalError) as error:
        build_profile_approval(
            _inputs(key_sha256, before=mutate), curator_signing_key_sha256=key_sha256
        )
    assert secret not in str(error.value)
    assert "argv" not in str(error.value)


def test_local_grading_keys_equal_the_platform_grading_control() -> None:
    assert GRADING_PROFILE_KEYS == PLATFORM_GRADING_PROFILE_KEYS


def test_grading_structure_matches_the_platform_expected_grading(curator) -> None:
    _private, _path, key_sha256 = curator
    grading = json.loads(_inputs(key_sha256).grading_profile)
    source = SourceBinding(
        evaluation_id=uuid4(),
        attempt_id=uuid4(),
        worker_id=uuid4(),
        assignment_sha256="1" * 64,
        artifact_sha256="2" * 64,
        harness_instance_id="profile-approval-test",
        profile_capability_id="hosted-profile-approval-test",
        deadline_unix=2_000_000_000,
    )
    submission = {
        "visible_bundle_sha256": "3" * 64,
        "base_tree_sha256": "4" * 64,
        "final_tree_sha256": "5" * 64,
    }
    plan = expected_grading(grading, source, submission)
    assert [group["group"] for group in plan["groups"]] == ["hidden", "visible"]
    for mutate in (
        lambda value: value["test_groups"].reverse(),
        _set_path(["test_groups", 1, "ExpectedTotal"], 0),
    ):
        drifted = copy.deepcopy(grading)
        mutate(drifted)
        assert not approval_module._grading_structure(drifted)
        with pytest.raises(HostedEvidenceError):
            expected_grading(drifted, source, submission)


def _profiles_mapping(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and [getattr(target, "id", None) for target in node.targets] == ["PROFILES"]
    ]
    assert len(values) == 1, path
    return values[0]


@pytest.mark.parametrize(
    "relative",
    [
        "services/dittobench-api/coding_runtime/qualification/native.py",
        "services/dittobench-api/coding_runtime/qualification/run.py",
        "infra/scripts/build-coding-native-release.py",
    ],
)
def test_language_profiles_equal_the_native_release_tooling(relative: str) -> None:
    assert _profiles_mapping(REPO / relative) == LANGUAGE_PROFILES


def _signed(curator) -> tuple[bytes, bytes, Path, str]:
    private, path, key_sha256 = curator
    document = build_profile_approval(
        _inputs(key_sha256), curator_signing_key_sha256=key_sha256
    )
    return document, private.sign(document), path, key_sha256


def test_verifier_accepts_the_pinned_curator_signature(curator) -> None:
    document, signature, path, key_sha256 = _signed(curator)
    verified = verify_profile_approval(
        document=document,
        signature=signature,
        curator_public_key_path=path,
        curator_signing_key_sha256=key_sha256,
    )
    assert verified.document_sha256 == _sha(document)
    assert verified.signature_sha256 == _sha(signature)
    assert verified.document["curator_signing_key_sha256"] == key_sha256


def test_verifier_rejects_wrong_keys_pins_and_modified_bytes(curator, tmp_path) -> None:
    document, signature, path, key_sha256 = _signed(curator)
    other = Ed25519PrivateKey.generate()
    other_path, other_sha256 = _public_key_file(tmp_path, other, "other")

    def rejects(reason: str, **overrides: Any) -> None:
        arguments: dict[str, Any] = {
            "document": document,
            "signature": signature,
            "curator_public_key_path": path,
            "curator_signing_key_sha256": key_sha256,
            **overrides,
        }
        with pytest.raises(ProfileApprovalError) as error:
            verify_profile_approval(**arguments)
        assert str(error.value) == reason

    rejects("curator signature does not verify", signature=other.sign(document))
    rejects(
        "approval document names another curator key",
        signature=other.sign(document),
        curator_public_key_path=other_path,
        curator_signing_key_sha256=other_sha256,
    )
    rejects(
        "curator public key differs from the pinned identity",
        curator_signing_key_sha256=other_sha256,
    )
    rejects(
        "curator public key differs from the pinned identity",
        curator_public_key_path=other_path,
    )
    rejects(
        "input path must be absolute",
        curator_public_key_path=Path(os.path.relpath(path)),
    )
    position = document.index(b'"execution_profile_sha256":"') + 28
    flipped = bytearray(document)
    flipped[position] = ord("0") if flipped[position] != ord("0") else ord("1")
    rejects("curator signature does not verify", document=bytes(flipped))
    modified = bytearray(signature)
    modified[0] ^= 1
    rejects("curator signature does not verify", signature=bytes(modified))
    rejects("pinned curator key digest is invalid", curator_signing_key_sha256="aa")


def test_verifier_never_accepts_an_unsigned_document(curator) -> None:
    document, _signature, path, key_sha256 = _signed(curator)
    for signature in (b"", b"\x00" * 64, document[:64], document):
        with pytest.raises(ProfileApprovalError):
            verify_profile_approval(
                document=document,
                signature=signature,
                curator_public_key_path=path,
                curator_signing_key_sha256=key_sha256,
            )


def test_verifier_rejects_validly_signed_documents_outside_the_schema(
    curator,
) -> None:
    private, path, key_sha256 = curator
    document = parse_profile_approval_document(
        build_profile_approval(
            _inputs(key_sha256), curator_signing_key_sha256=key_sha256
        )
    )
    publication = private_v2_publication_signing_message(
        manifest={
            "schema": "dittobench-coding-private-v2-transport-v1",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "transport_sha256": _h("transport"),
            "payload_sha256": _h("payload"),
            "catalog_sha256": _h("catalog"),
            "catalog_merkle_root": _h("merkle"),
            "wrapping_key_sha256": _h("wrapping-key"),
            "objects": [{}],
        },
        source_sha=REVISION,
        probe_receipt_payload_sha256=_h("probe"),
        private_input_authority_sha256=_h("authority"),
        curator_signing_key_sha256=key_sha256,
    )
    candidates = {
        "publication signing message": publication,
        "noncanonical": json.dumps(document, indent=2).encode(),
        "approved field": _canonical({**document, "approved": True}),
        "weight eligible": _canonical({**document, "weight_eligible": True}),
        "shadow only false": _canonical({**document, "shadow_only": False}),
        "missing field": _canonical(
            {k: v for k, v in document.items() if k != "grader_contract_sha256"}
        ),
        "other schema": _canonical({**document, "schema": "other"}),
        "unhashable language": _canonical({**document, "language": ["python"]}),
        "object language": _canonical({**document, "language": {"python": 1}}),
    }
    for body in candidates.values():
        with pytest.raises(ProfileApprovalError):
            verify_profile_approval(
                document=body,
                signature=private.sign(body),
                curator_public_key_path=path,
                curator_signing_key_sha256=key_sha256,
            )
    with pytest.raises(ProfileApprovalError) as error:
        parse_profile_approval_document(candidates["unhashable language"])
    assert str(error.value) == "approval document values are invalid"


def _write_inputs(root: Path, inputs: ApprovalInputs) -> dict[str, Path]:
    root.mkdir(mode=0o700)
    profiles = root / "profiles"
    profiles.mkdir(mode=0o700)
    (profiles / "execution-profile.json").write_bytes(inputs.execution_profile)
    (profiles / "grading-profile.json").write_bytes(inputs.grading_profile)
    (profiles / "receipt.json").write_bytes(inputs.profile_receipt)
    paths = {"profiles": profiles}
    for name in (
        "request",
        "profile_request",
        "payload_authority",
        "registration",
        "release_index",
        "native_approval",
        "native_plan",
        "native_summary",
        "native_provenance",
    ):
        paths[name] = root / f"{name}.json"
        paths[name].write_bytes(getattr(inputs, name))
    return paths


def _build_argv(paths: dict[str, Path], key: Path | str, output: Path) -> list[str]:
    argv = ["build"]
    for name, path in paths.items():
        argv += [f"--{name.replace('_', '-')}", str(path)]
    return [*argv, "--curator-public-key", str(key), "--output", str(output)]


def test_cli_builds_a_draft_and_verifies_only_a_signed_document(
    curator, tmp_path, capsys
) -> None:
    private, key_path, key_sha256 = curator
    paths = _write_inputs(tmp_path / "inputs", _inputs(key_sha256))
    output = tmp_path / "inputs" / "approval-document.json"
    assert main(_build_argv(paths, key_path, output)) == 0
    document = output.read_bytes()
    assert capsys.readouterr().out == (
        f"approved=false\ndocument_sha256={_sha(document)}\n"
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert main(_build_argv(paths, key_path, output)) == 70
    assert "output must be new" in capsys.readouterr().err

    verify = [
        "verify",
        "--document",
        str(output),
        "--curator-public-key",
        str(key_path),
        "--curator-signing-key-sha256",
        key_sha256,
    ]
    assert main(verify) == 70
    assert capsys.readouterr().err == (
        "hosted profile approval rejected: arguments are invalid\n"
    )
    signature = tmp_path / "inputs" / "approval-signature.bin"
    signature.write_bytes(b"")
    assert main([*verify, "--signature", str(signature)]) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curator signature is missing or malformed" in captured.err

    signature.write_bytes(private.sign(document))
    assert main([*verify, "--signature", str(signature)]) == 0
    lines = capsys.readouterr().out.splitlines()
    printed = dict(line.split("=", 1) for line in lines)
    assert list(printed) == [
        "document_sha256",
        "signature_sha256",
        *DOCUMENT_IDENTITY_FIELDS,
        *DOCUMENT_SHA256_FIELDS,
    ]
    assert printed["document_sha256"] == _sha(document)
    assert printed["signature_sha256"] == _sha(signature.read_bytes())
    assert printed["curator_signing_key_sha256"] == key_sha256
    assert printed["catalog_index"] == str(INDEX)
    assert printed["language"] == LANGUAGE
    assert printed["source_revision"] == REVISION
    assert printed["task_version_id"] == f"{GROUP}-v3_stale_conflict"
    assert printed["corpus_release_id"] == "coding-private-v2-test"
    assert printed["image_digest"] == IMAGE_DIGEST


def test_cli_requires_an_absolute_curator_public_key(
    curator, tmp_path, capsys, monkeypatch
) -> None:
    private, key_path, key_sha256 = curator
    paths = _write_inputs(tmp_path / "inputs", _inputs(key_sha256))
    output = tmp_path / "inputs" / "approval-document.json"
    monkeypatch.chdir(key_path.parent)
    assert key_path.name and (Path.cwd() / key_path.name).is_file()
    assert main(_build_argv(paths, key_path.name, output)) == 70
    assert capsys.readouterr().err == (
        "hosted profile approval rejected: input path must be absolute\n"
    )
    assert not output.exists()

    assert main(_build_argv(paths, key_path, output)) == 0
    capsys.readouterr()
    signature = tmp_path / "inputs" / "approval-signature.bin"
    signature.write_bytes(private.sign(output.read_bytes()))
    assert (
        main(
            [
                "verify",
                "--document",
                str(output),
                "--signature",
                str(signature),
                "--curator-public-key",
                key_path.name,
                "--curator-signing-key-sha256",
                key_sha256,
            ]
        )
        == 70
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "hosted profile approval rejected: input path must be absolute\n"
    )


def test_cli_fsyncs_the_output_directory_and_writes_nothing_on_failure(
    curator, tmp_path, capsys, monkeypatch
) -> None:
    _private, key_path, key_sha256 = curator
    paths = _write_inputs(tmp_path / "inputs", _inputs(key_sha256))
    output = tmp_path / "inputs" / "approval-document.json"
    synced: list[bool] = []
    real_fsync = os.fsync

    def recording(descriptor: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(approval_module.os, "fsync", recording)
    assert main(_build_argv(paths, key_path, output)) == 0
    assert synced == [False, True]
    capsys.readouterr()

    def failing_directory(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        real_fsync(descriptor)

    retry = tmp_path / "inputs" / "approval-document-retry.json"
    monkeypatch.setattr(approval_module.os, "fsync", failing_directory)
    assert main(_build_argv(paths, key_path, retry)) == 70
    assert capsys.readouterr().out == ""
    assert not retry.exists()


def test_cli_rejects_a_profile_directory_that_is_not_exact_helper_output(
    curator, tmp_path, capsys
) -> None:
    _private, key_path, key_sha256 = curator
    paths = _write_inputs(tmp_path / "inputs", _inputs(key_sha256))
    (paths["profiles"] / "request.json").write_bytes(b"{}")
    output = tmp_path / "inputs" / "approval-document.json"
    assert main(_build_argv(paths, key_path, output)) == 70
    assert "profile directory is not an exact helper output" in capsys.readouterr().err
    assert not output.exists()


def test_real_profile_helper_output_builds_and_verifies(curator, tmp_path) -> None:
    private, key_path, key_sha256 = curator
    root = tmp_path / "helper"
    root.mkdir(mode=0o700)
    for name in ("binary", "payload"):
        (root / name).mkdir(mode=0o700)
    binary = profiles_helper.build_profiles_binary(root / "binary")
    payload_dir, _authority = profiles_helper.build_payload(root / "payload")
    result, output = profiles_helper.run(
        binary, root, payload_dir, profiles_helper.request()
    )
    assert result.returncode == 0, result.stderr
    real = {
        "profile_request": (root / "out-request.json").read_bytes(),
        "execution_profile": (output / "execution-profile.json").read_bytes(),
        "grading_profile": (output / "grading-profile.json").read_bytes(),
        "profile_receipt": (output / "receipt.json").read_bytes(),
        "payload_authority": (payload_dir / "payload-authority.json").read_bytes(),
    }
    # Re-running the helper with the same inputs reproduces every output byte,
    # which is how an approver checks the attested launch checks.
    again, repeated = profiles_helper.run(
        binary, root, payload_dir, profiles_helper.request(), name="again"
    )
    assert again.returncode == 0, again.stderr
    for name in ("execution-profile.json", "grading-profile.json", "receipt.json"):
        assert (repeated / name).read_bytes() == (output / name).read_bytes()
    receipt = json.loads(real["profile_receipt"])
    grading = json.loads(real["grading_profile"])
    # The synthetic projection of a request is exactly what the helper writes.
    synthetic_execution, synthetic_grading = _profiles(profiles_helper.request())
    synthetic_grading["grader_contract_sha256"] = grading["grader_contract_sha256"]
    synthetic_grading["grader_bundle_sha256"] = grading["grader_bundle_sha256"]
    assert _canonical(synthetic_execution) == real["execution_profile"]
    assert _canonical(synthetic_grading) == real["grading_profile"]
    group = receipt["task_version_id"].rsplit("-", 1)[0]

    def before(objects: dict[str, Any]) -> None:
        objects["request"].update(
            catalog_index=receipt["catalog_index"],
            grader_contract_sha256=grading["grader_contract_sha256"],
        )
        objects["registration"].update(
            corpus_release_id=receipt["corpus_release_id"],
            private_release_sha256=receipt["private_release_sha256"],
        )
        plan = objects["native_plan"]
        plan["cases"] = [
            *_plan_cases(group, LANGUAGE, grading["test_groups"]),
            *(case for case in plan["cases"] if case["language"] != LANGUAGE),
        ]

    inputs = _inputs(
        key_sha256,
        before=before,
        transform={name: _replace(body) for name, body in real.items()},
    )
    document = build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    value = parse_profile_approval_document(document)
    assert value["task_version_id"] == receipt["task_version_id"]
    assert value["execution_profile_sha256"] == receipt["execution_profile_sha256"]
    assert value["grading_profile_sha256"] == receipt["grading_profile_sha256"]
    assert value["grader_contract_sha256"] == receipt["grader_contract_sha256"]
    assert value["task_commitment_sha256"] == receipt["task_commitment_sha256"]
    verified = verify_profile_approval(
        document=document,
        signature=private.sign(document),
        curator_public_key_path=key_path,
        curator_signing_key_sha256=key_sha256,
    )
    assert verified.document_sha256 == _sha(document)
    unapproved = copy.deepcopy(receipt)
    unapproved["approved"] = True
    with pytest.raises(ProfileApprovalError, match="claims approval"):
        build_profile_approval(
            _inputs(
                key_sha256,
                before=before,
                transform={
                    **{name: _replace(body) for name, body in real.items()},
                    "profile_receipt": _replace(_canonical(unapproved)),
                },
            ),
            curator_signing_key_sha256=key_sha256,
        )
