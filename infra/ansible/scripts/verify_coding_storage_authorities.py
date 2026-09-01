#!/usr/bin/env python3
"""Verify coding storage control-plane state without reading object secrets.

This tool is intentionally inert: every provider call is a ``gcloud`` describe,
list, or IAM-policy read. It never accesses a Secret Manager payload, signs an
S3 request, or reads/writes/lists/deletes an object. The resulting receipt
contains only expected resource identities, booleans, counts, and a canonical
SHA-256 digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ENVIRONMENTS = {"dev", "prod"}
_PHASES = {"authorities", "platform-access"}
_STORAGE_SERVICE = "storage.googleapis.com"
_SECURE_TRANSPORT = "constraints/storage.secureHttpTransport"


class VerificationError(Exception):
    """The applied control plane disagrees with the reviewed contract."""


Query = Callable[[tuple[str, ...]], Any]


@dataclass(frozen=True)
class VerificationConfig:
    project: str
    environment: str
    phase: str
    source_sha: str
    auditors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _PROJECT.fullmatch(self.project) is None:
            raise VerificationError("project id is outside GCP bounds")
        if self.environment not in _ENVIRONMENTS:
            raise VerificationError("environment must be dev or prod")
        if self.phase not in _PHASES:
            raise VerificationError("phase must be authorities or platform-access")
        if _COMMIT_SHA.fullmatch(self.source_sha) is None:
            raise VerificationError("source SHA must be 40 lowercase hex characters")
        if len(set(self.auditors)) != len(self.auditors) or any(
            not _valid_member(member) for member in self.auditors
        ):
            raise VerificationError("auditors must be unique explicit IAM members")


def _valid_member(member: str) -> bool:
    return bool(
        re.fullmatch(r"(?:group|serviceAccount|user):[^\s]+", member)
        and member not in {"allUsers", "allAuthenticatedUsers"}
    )


def _gcloud_json(command: tuple[str, ...]) -> Any:
    try:
        completed = subprocess.run(
            ("gcloud", *command),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerificationError("gcloud query could not complete") from error
    if completed.returncode != 0:
        # Never include provider stderr: IAM errors can carry principal names,
        # URLs, or other details that do not belong in a durable receipt.
        raise VerificationError(f"gcloud query failed for {command[0]} {command[1]}")
    try:
        return json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise VerificationError("gcloud returned malformed JSON") from error


def _query(query: Query, *command: str) -> Any:
    return query((*command, "--format=json"))


def _bucket_description(
    query: Query,
    *,
    project: str,
    bucket: str,
    retention_seconds: int,
) -> dict[str, object]:
    value = _query(
        query,
        "storage",
        "buckets",
        "describe",
        f"gs://{bucket}",
        f"--project={project}",
    )
    if not isinstance(value, dict) or value.get("name") != bucket:
        raise VerificationError(f"bucket identity mismatch: {bucket}")
    iam = value.get("iamConfiguration")
    retention = value.get("retentionPolicy")
    versioning = value.get("versioning")
    if not isinstance(iam, dict) or not isinstance(retention, dict):
        raise VerificationError(f"bucket protection metadata is absent: {bucket}")
    uniform = iam.get("uniformBucketLevelAccess")
    if (
        str(value.get("location", "")).upper() != "EU"
        or value.get("storageClass") != "STANDARD"
        or not isinstance(uniform, dict)
        or uniform.get("enabled") is not True
        or str(iam.get("publicAccessPrevention", "")).lower() != "enforced"
        or not isinstance(versioning, dict)
        or versioning.get("enabled") is not True
        or int(retention.get("retentionPeriod", -1)) != retention_seconds
        or retention.get("isLocked", False) is not False
    ):
        raise VerificationError(f"bucket controls disagree: {bucket}")
    return {
        "name": bucket,
        "location": "EU",
        "storage_class": "STANDARD",
        "retention_seconds": retention_seconds,
        "retention_locked": False,
        "uniform_access": True,
        "public_access_prevention": True,
        "versioning": True,
    }


def _bindings(policy: Any, *, label: str) -> dict[str, set[str]]:
    if not isinstance(policy, dict):
        raise VerificationError(f"IAM policy is malformed: {label}")
    result: dict[str, set[str]] = {}
    raw_bindings = policy.get("bindings", [])
    if not isinstance(raw_bindings, list):
        raise VerificationError(f"IAM bindings are malformed: {label}")
    for raw in raw_bindings:
        if not isinstance(raw, dict):
            raise VerificationError(f"IAM binding is malformed: {label}")
        role = raw.get("role")
        members = raw.get("members", [])
        if not isinstance(role, str) or not isinstance(members, list):
            raise VerificationError(f"IAM binding fields are malformed: {label}")
        result.setdefault(role, set()).update(str(member) for member in members)
    if any(
        member in {"allUsers", "allAuthenticatedUsers"}
        for members in result.values()
        for member in members
    ):
        raise VerificationError(f"public IAM member detected: {label}")
    return result


def _bucket_policy(
    query: Query,
    *,
    project: str,
    bucket: str,
    expected: dict[str, set[str]],
    platform_member: str,
) -> dict[str, list[str]]:
    policy = _query(
        query,
        "storage",
        "buckets",
        "get-iam-policy",
        f"gs://{bucket}",
        f"--project={project}",
    )
    actual = _bindings(policy, label=bucket)
    if actual != expected:
        raise VerificationError(f"bucket IAM disagrees: {bucket}")
    if any(platform_member in members for members in actual.values()):
        raise VerificationError(f"Platform has direct bucket IAM: {bucket}")
    forbidden = {
        "roles/storage.admin",
        "roles/storage.objectAdmin",
        "roles/storage.objectUser",
        "roles/storage.legacyBucketOwner",
        "roles/storage.legacyObjectOwner",
    }
    if forbidden.intersection(actual):
        raise VerificationError(f"broad storage role detected: {bucket}")
    return {role: sorted(members) for role, members in sorted(actual.items())}


def _service_account(query: Query, *, project: str, email: str) -> dict[str, object]:
    value = _query(
        query,
        "iam",
        "service-accounts",
        "describe",
        email,
        f"--project={project}",
    )
    if (
        not isinstance(value, dict)
        or value.get("email") != email
        or value.get("disabled", False) is not False
    ):
        raise VerificationError(f"service account identity mismatch: {email}")
    return {"email": email, "disabled": False}


def _hmac_count(query: Query, *, project: str, email: str) -> int:
    value = _query(
        query,
        "storage",
        "hmac",
        "list",
        f"--project={project}",
        f"--filter=serviceAccountEmail={email}",
    )
    if not isinstance(value, list):
        raise VerificationError(f"HMAC metadata is malformed: {email}")
    active = [
        item
        for item in value
        if isinstance(item, dict)
        and item.get("serviceAccountEmail") == email
        and str(item.get("state", "")).upper() == "ACTIVE"
    ]
    if len(active) != 1:
        raise VerificationError(f"expected one active HMAC key: {email}")
    return 1


def _secret_access(
    query: Query,
    *,
    project: str,
    secret: str,
    platform_member: str,
    expected: bool,
) -> bool:
    policy = _query(
        query,
        "secrets",
        "get-iam-policy",
        secret,
        f"--project={project}",
    )
    bindings = _bindings(policy, label=secret)
    accessors = bindings.get("roles/secretmanager.secretAccessor", set())
    expected_accessors = {platform_member} if expected else set()
    if accessors != expected_accessors:
        raise VerificationError(f"Platform secret access disagrees: {secret}")
    return expected


def _secure_transport(query: Query, *, project: str) -> bool:
    value = _query(
        query,
        "org-policies",
        "describe",
        _SECURE_TRANSPORT,
        f"--project={project}",
        "--effective",
    )
    if not isinstance(value, dict):
        raise VerificationError("secure transport policy is malformed")
    spec = value.get("spec")
    rules = spec.get("rules", []) if isinstance(spec, dict) else []
    if not isinstance(rules, list) or not any(
        isinstance(rule, dict) and rule.get("enforce") is True for rule in rules
    ):
        raise VerificationError("secure HTTP transport is not enforced")
    return True


def _storage_audit(query: Query, *, project: str) -> list[str]:
    value = _query(query, "projects", "get-iam-policy", project)
    if not isinstance(value, dict):
        raise VerificationError("project IAM policy is malformed")
    configs = value.get("auditConfigs", [])
    if not isinstance(configs, list):
        raise VerificationError("project audit configuration is malformed")
    observed: set[str] = set()
    for config in configs:
        if not isinstance(config, dict) or config.get("service") != _STORAGE_SERVICE:
            continue
        logs = config.get("auditLogConfigs", [])
        if not isinstance(logs, list):
            raise VerificationError("storage audit configuration is malformed")
        observed.update(
            str(item.get("logType")) for item in logs if isinstance(item, dict)
        )
    required = {"DATA_READ", "DATA_WRITE"}
    if not required.issubset(observed):
        raise VerificationError("Cloud Storage Data Access audit is incomplete")
    return sorted(required)


def verify(
    config: VerificationConfig, *, query: Query = _gcloud_json
) -> dict[str, Any]:
    project = config.project
    environment = config.environment
    platform_member = (
        f"serviceAccount:ditto-platform-api@{project}.iam.gserviceaccount.com"
    )
    curator_email = (
        f"ditto-coding-curator-{environment}@{project}.iam.gserviceaccount.com"
    )
    reader_email = f"ditto-coding-input-{environment}@{project}.iam.gserviceaccount.com"
    finalizer_email = (
        f"ditto-coding-evidence-{environment}@{project}.iam.gserviceaccount.com"
    )
    curator_member = f"serviceAccount:{curator_email}"
    reader_member = f"serviceAccount:{reader_email}"
    finalizer_member = f"serviceAccount:{finalizer_email}"
    private_bucket = f"{project}-coding-private-inputs-{environment}"
    evidence_bucket = f"{project}-coding-sealed-evidence-{environment}"
    reader_role = f"projects/{project}/roles/dittoCodingPrivateInputReader"
    finalizer_role = f"projects/{project}/roles/dittoCodingEvidenceFinalizer"
    auditors = set(config.auditors)

    buckets = {
        "private_inputs": _bucket_description(
            query,
            project=project,
            bucket=private_bucket,
            retention_seconds=2_592_000,
        ),
        "sealed_evidence": _bucket_description(
            query,
            project=project,
            bucket=evidence_bucket,
            retention_seconds=7_776_000,
        ),
    }
    bucket_iam = {
        "private_inputs": _bucket_policy(
            query,
            project=project,
            bucket=private_bucket,
            expected={
                "roles/storage.objectCreator": {curator_member},
                reader_role: {reader_member, *auditors},
            },
            platform_member=platform_member,
        ),
        "sealed_evidence": _bucket_policy(
            query,
            project=project,
            bucket=evidence_bucket,
            expected={
                finalizer_role: {finalizer_member},
                **({reader_role: auditors} if auditors else {}),
            },
            platform_member=platform_member,
        ),
    }
    identities = {}
    hmac_active = {}
    for label, email in (
        ("curator", curator_email),
        ("private_input_reader", reader_email),
        ("evidence_finalizer", finalizer_email),
    ):
        identities[label] = _service_account(query, project=project, email=email)
        hmac_active[label] = _hmac_count(query, project=project, email=email)

    expect_platform = config.phase == "platform-access"
    secret_access = {
        "curator": _secret_access(
            query,
            project=project,
            secret=f"coding-input-curator-{environment}-hmac-secret",
            platform_member=platform_member,
            expected=False,
        ),
        "private_input_reader": _secret_access(
            query,
            project=project,
            secret=f"coding-input-reader-{environment}-hmac-secret",
            platform_member=platform_member,
            expected=expect_platform,
        ),
        "evidence_finalizer": _secret_access(
            query,
            project=project,
            secret=f"coding-evidence-finalizer-{environment}-hmac-secret",
            platform_member=platform_member,
            expected=expect_platform,
        ),
    }

    receipt: dict[str, Any] = {
        "schema": "dittobench-coding-storage-control-verification-v1",
        "project": project,
        "environment": environment,
        "phase": config.phase,
        "source_sha": config.source_sha,
        "verified_at": datetime.now(UTC).isoformat(),
        "buckets": buckets,
        "bucket_iam": bucket_iam,
        "identities": identities,
        "active_hmac_key_counts": hmac_active,
        "platform_secret_access": secret_access,
        "secure_http_transport": _secure_transport(query, project=project),
        "storage_audit_log_types": _storage_audit(query, project=project),
        "object_operations_performed": False,
        "secret_payloads_read": False,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    receipt["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--environment", required=True, choices=sorted(_ENVIRONMENTS))
    parser.add_argument("--phase", required=True, choices=sorted(_PHASES))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--auditor", action="append", default=[])
    parser.add_argument("--output", type=Path)
    return parser


def _write_receipt(receipt: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    except OSError as error:
        raise VerificationError("receipt output must be a new writable file") from error
    print(f"receipt_sha256={receipt['receipt_sha256']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = VerificationConfig(
            project=args.project,
            environment=args.environment,
            phase=args.phase,
            source_sha=args.source_sha,
            auditors=tuple(args.auditor),
        )
        _write_receipt(verify(config), args.output)
    except VerificationError as error:
        print(f"coding storage verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
