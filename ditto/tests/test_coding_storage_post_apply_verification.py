from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra" / "ansible" / "scripts" / "verify_coding_storage_authorities.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coding_storage_verify", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(*, platform_access: bool = True) -> dict[tuple[str, ...], Any]:
    project = "ditto-app-dev"
    environment = "prod"
    platform = f"serviceAccount:ditto-platform-api@{project}.iam.gserviceaccount.com"
    curator_email = (
        f"ditto-coding-curator-{environment}@{project}.iam.gserviceaccount.com"
    )
    reader_email = f"ditto-coding-input-{environment}@{project}.iam.gserviceaccount.com"
    finalizer_email = (
        f"ditto-coding-evidence-{environment}@{project}.iam.gserviceaccount.com"
    )
    private_bucket = f"{project}-coding-private-inputs-{environment}"
    evidence_bucket = f"{project}-coding-sealed-evidence-{environment}"
    reader_role = f"projects/{project}/roles/dittoCodingPrivateInputReader"
    finalizer_role = f"projects/{project}/roles/dittoCodingEvidenceFinalizer"

    def command(*parts: str) -> tuple[str, ...]:
        return (*parts, "--format=json")

    fixture: dict[tuple[str, ...], Any] = {
        command(
            "storage",
            "buckets",
            "describe",
            f"gs://{private_bucket}",
            f"--project={project}",
        ): _bucket(private_bucket, 2_592_000),
        command(
            "storage",
            "buckets",
            "describe",
            f"gs://{evidence_bucket}",
            f"--project={project}",
        ): _bucket(evidence_bucket, 7_776_000),
        command(
            "storage",
            "buckets",
            "get-iam-policy",
            f"gs://{private_bucket}",
            f"--project={project}",
        ): {
            "bindings": [
                {
                    "role": "roles/storage.objectCreator",
                    "members": [f"serviceAccount:{curator_email}"],
                },
                {
                    "role": reader_role,
                    "members": [f"serviceAccount:{reader_email}"],
                },
            ]
        },
        command(
            "storage",
            "buckets",
            "get-iam-policy",
            f"gs://{evidence_bucket}",
            f"--project={project}",
        ): {
            "bindings": [
                {
                    "role": finalizer_role,
                    "members": [f"serviceAccount:{finalizer_email}"],
                }
            ]
        },
        command(
            "org-policies",
            "describe",
            "constraints/storage.secureHttpTransport",
            f"--project={project}",
            "--effective",
        ): {"spec": {"rules": [{"enforce": True}]}},
        command("projects", "get-iam-policy", project): {
            "auditConfigs": [
                {
                    "service": "storage.googleapis.com",
                    "auditLogConfigs": [
                        {"logType": "DATA_READ"},
                        {"logType": "DATA_WRITE"},
                    ],
                }
            ]
        },
    }
    for email in (curator_email, reader_email, finalizer_email):
        fixture[
            command(
                "iam",
                "service-accounts",
                "describe",
                email,
                f"--project={project}",
            )
        ] = {"email": email, "disabled": False}
        fixture[
            command(
                "storage",
                "hmac",
                "list",
                f"--project={project}",
                f"--filter=serviceAccountEmail={email}",
            )
        ] = [
            {
                "accessId": "redacted-from-receipt",
                "serviceAccountEmail": email,
                "state": "ACTIVE",
            }
        ]

    for secret, allowed in (
        (f"coding-input-curator-{environment}-hmac-secret", False),
        (f"coding-input-reader-{environment}-hmac-secret", platform_access),
        (f"coding-evidence-finalizer-{environment}-hmac-secret", platform_access),
    ):
        fixture[
            command(
                "secrets",
                "get-iam-policy",
                secret,
                f"--project={project}",
            )
        ] = {
            "bindings": (
                [
                    {
                        "role": "roles/secretmanager.secretAccessor",
                        "members": [platform],
                    }
                ]
                if allowed
                else []
            )
        }
    return fixture


def _bucket(name: str, retention: int) -> dict[str, Any]:
    return {
        "name": name,
        "location": "EU",
        "storageClass": "STANDARD",
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "versioning": {"enabled": True},
        "retentionPolicy": {
            "retentionPeriod": str(retention),
            "isLocked": False,
        },
    }


def test_platform_access_receipt_is_redacted_and_digest_bound() -> None:
    module = _load()
    fixture = _fixture()

    receipt = module.verify(
        module.VerificationConfig(
            project="ditto-app-dev",
            environment="prod",
            phase="platform-access",
            source_sha="ab" * 20,
        ),
        query=lambda command: fixture[command],
    )

    rendered = json.dumps(receipt, sort_keys=True)
    assert receipt["platform_secret_access"] == {
        "curator": False,
        "private_input_reader": True,
        "evidence_finalizer": True,
    }
    assert receipt["secret_payloads_read"] is False
    assert receipt["object_operations_performed"] is False
    assert len(receipt["receipt_sha256"]) == 64
    assert "redacted-from-receipt" not in rendered
    assert "accessId" not in rendered


def test_authority_phase_requires_no_platform_secret_access() -> None:
    module = _load()
    fixture = _fixture(platform_access=False)
    receipt = module.verify(
        module.VerificationConfig(
            project="ditto-app-dev",
            environment="prod",
            phase="authorities",
            source_sha="cd" * 20,
        ),
        query=lambda command: fixture[command],
    )
    assert not any(receipt["platform_secret_access"].values())


def test_verifier_fails_closed_on_curator_access_or_bucket_drift() -> None:
    module = _load()
    fixture = _fixture()
    project = "ditto-app-dev"
    platform = f"serviceAccount:ditto-platform-api@{project}.iam.gserviceaccount.com"
    curator_policy = (
        "secrets",
        "get-iam-policy",
        "coding-input-curator-prod-hmac-secret",
        f"--project={project}",
        "--format=json",
    )
    fixture[curator_policy] = {
        "bindings": [
            {
                "role": "roles/secretmanager.secretAccessor",
                "members": [platform],
            }
        ]
    }
    config = module.VerificationConfig(
        project=project,
        environment="prod",
        phase="platform-access",
        source_sha="ef" * 20,
    )
    with pytest.raises(module.VerificationError, match="secret access"):
        module.verify(config, query=lambda command: fixture[command])

    fixture = _fixture()
    bucket_command = (
        "storage",
        "buckets",
        "describe",
        "gs://ditto-app-dev-coding-private-inputs-prod",
        "--project=ditto-app-dev",
        "--format=json",
    )
    fixture[bucket_command]["retentionPolicy"]["retentionPeriod"] = "60"
    with pytest.raises(module.VerificationError, match="bucket controls"):
        module.verify(config, query=lambda command: fixture[command])


def test_receipt_output_is_exclusive_and_mode_0600(tmp_path: Path) -> None:
    module = _load()
    output = tmp_path / "receipt.json"
    receipt = {"receipt_sha256": "ab" * 32, "secret_payloads_read": False}

    module._write_receipt(receipt, output)

    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == receipt
    with pytest.raises(module.VerificationError, match="new writable file"):
        module._write_receipt(receipt, output)
