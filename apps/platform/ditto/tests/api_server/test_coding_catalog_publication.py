"""Tests for curator-only private Coding catalog publication plans."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import stat
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingCatalogTaskPayload,
    coding_catalog_task_commitment_digest,
)
from ditto.api_server.coding_catalog_publication import (
    _MAX_PLAN_BYTES,
    CodingCatalogPublicationError,
    plan_private_catalog_publication,
    read_private_catalog_publication_object,
    write_private_catalog_publication_plan,
)
from ditto.api_server.coding_hippius_encryption import (
    HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION,
    HippiusPrivateInputEncryptionError,
    hippius_private_input_aad_bytes,
    load_hippius_private_input_transport,
    prepare_hippius_private_input_transport,
)
from ditto.coding_selection import coding_catalog_leaf_hash

ROOT = Path(__file__).parents[5]
SELECTION = (
    ROOT / "packages/dittobench-coding-contract/testdata/coding_selection_v1.json"
)
EXECUTION = (
    ROOT / "packages/dittobench-coding-contract/testdata/coding_execution_plan_v1.json"
)


def _load_encryption_script() -> ModuleType:
    path = ROOT / "apps/platform/scripts/prepare_hippius_private_inputs.py"
    spec = importlib.util.spec_from_file_location(
        "prepare_hippius_private_inputs", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(value: dict[str, Any], *, label: str) -> bytes:
    return coding_canonical_json_bytes(value, maximum_bytes=2 << 20, label=label)


def _fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    execution = json.loads(EXECUTION.read_text(encoding="utf-8"))
    payload = deepcopy(selection["task_version"]["payload"])
    payload["catalog_index"] = 0
    payload["task_version_id"] = "curator-private-task-v000"
    task_payload = CodingCatalogTaskPayload.model_validate(payload)
    task_commitment = coding_catalog_task_commitment_digest(task_payload)
    root = coding_catalog_leaf_hash(
        catalog_index=0,
        task_commitment_sha256=task_commitment,
    )
    commitment = deepcopy(selection["commitment"])
    commitment["catalog_merkle_root"] = root
    commitment["task_version_count"] = 1
    commitment.pop("commitment_sha256")
    commitment["commitment_sha256"] = coding_canonical_sha256(
        commitment,
        maximum_bytes=1 << 20,
        label="catalog commitment",
    )
    membership = {
        "schema": "dittobench-coding-catalog-membership-proof-v1",
        "coding_contract_version": 1,
        "corpus_release_id": commitment["corpus_release_id"],
        "catalog_merkle_root": root,
        "task_version_count": 1,
        "catalog_index": 0,
        "task_commitment_sha256": task_commitment,
        "sibling_sha256": [],
    }
    membership["catalog_membership_proof_sha256"] = coding_canonical_sha256(
        membership,
        maximum_bytes=1 << 20,
        label="catalog membership proof",
    )
    record = {
        "schema": "dittobench-coding-private-catalog-record-v1",
        "catalog_commitment_sha256": commitment["commitment_sha256"],
        "task_version": {
            "payload": payload,
            "task_commitment_sha256": task_commitment,
        },
        "membership_proof": membership,
        "issue": selection["issue"],
        "runtime_policy": selection["runtime_policy"],
        "budgets": selection["budgets"],
        "runner_plan": execution["runner_plan"],
        "grader_plan": execution["grader_plan"],
        "grader_resource_profile": execution["grader_resource_profile"],
    }
    CodingCatalogCommitment.model_validate(commitment)
    return commitment, record


def _write_fixture(root: Path) -> tuple[Path, Path]:
    commitment, record = _fixture()
    commitment_path = root / "commitment.json"
    records_dir = root / "records"
    records_dir.mkdir(parents=True)
    commitment_path.write_bytes(_canonical(commitment, label="catalog commitment"))
    (records_dir / "000000.json").write_bytes(
        _canonical(record, label="private catalog record")
    )
    return commitment_path, records_dir


def test_curator_publication_plan_is_canonical_and_content_addressed(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    plan = plan_private_catalog_publication(
        commitment_path=commitment_path,
        records_dir=records_dir,
    )

    assert len(plan.objects) == 1
    item = plan.objects[0]
    assert item.catalog_index == 0
    assert item.object_key == (
        f"coding-catalog/v1/{plan.commitment.commitment_sha256}/records/000000.json"
    )
    output = write_private_catalog_publication_plan(
        plan=plan,
        output=tmp_path / "publication.json",
    )
    body = json.loads(output.read_bytes())
    assert body["publication_sha256"] == plan.publication_sha256
    assert body["objects"] == [item.as_json()]
    assert "problem_statement" not in output.read_text(encoding="utf-8")
    with pytest.raises(CodingCatalogPublicationError, match="digest"):
        write_private_catalog_publication_plan(
            plan=replace(plan, publication_sha256="f" * 64),
            output=tmp_path / "forged-publication.json",
        )


def _write_wrapping_public_key(
    root: Path, *, key_size: int = 3072
) -> tuple[rsa.RSAPrivateKey, Path]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=key_size)
    path = root / "wrapping-public-key.pem"
    path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, path


def test_hippius_private_input_transport_encrypts_and_binds_exact_record(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    private_key, public_key_path = _write_wrapping_public_key(tmp_path)
    output_dir = (tmp_path / "encrypted-transport").resolve()

    manifest = prepare_hippius_private_input_transport(
        commitment_path=commitment_path,
        records_dir=records_dir,
        wrapping_public_key_path=public_key_path,
        output_dir=output_dir,
    )

    assert len(manifest.objects) == 1
    assert load_hippius_private_input_transport(output_dir) == manifest
    item = manifest.objects[0]
    plan = plan_private_catalog_publication(
        commitment_path=commitment_path,
        records_dir=records_dir,
    )
    aad = hippius_private_input_aad_bytes(
        plan=plan,
        item=plan.objects[0],
        wrapping_key_sha256=manifest.wrapping_key_sha256,
    )
    assert hashlib.sha256(aad).hexdigest() == item.aad_sha256
    data_key = private_key.decrypt(
        base64.b64decode(item.wrapped_data_key_b64, validate=True),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=bytes.fromhex(item.aad_sha256),
        ),
    )
    ciphertext = (output_dir / item.ciphertext_relative_path).read_bytes()
    plaintext = AESGCM(data_key).decrypt(
        base64.b64decode(item.nonce_b64, validate=True),
        ciphertext,
        aad,
    )
    expected = (records_dir / "000000.json").read_bytes()
    assert plaintext == expected
    assert item.plaintext_sha256 == hashlib.sha256(expected).hexdigest()
    assert item.ciphertext_sha256 == hashlib.sha256(ciphertext).hexdigest()
    assert item.ciphertext_size_bytes == item.plaintext_size_bytes + 16
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((output_dir / "objects").stat().st_mode) == 0o700
    assert stat.S_IMODE((output_dir / "manifest.json").stat().st_mode) == 0o600
    assert (
        stat.S_IMODE((output_dir / item.ciphertext_relative_path).stat().st_mode)
        == 0o600
    )
    manifest_text = (output_dir / "manifest.json").read_text()
    manifest_document = json.loads(manifest_text)
    manifest_projection = {
        key: value
        for key, value in manifest_document.items()
        if key != "transport_manifest_sha256"
    }
    assert (
        manifest.transport_manifest_sha256
        == hashlib.sha256(
            coding_canonical_json_bytes(
                manifest_projection,
                maximum_bytes=512 << 20,
                label="test Hippius private-input transport manifest",
            )
        ).hexdigest()
    )
    assert "problem_statement" not in manifest_text
    assert b"problem_statement" not in ciphertext
    assert manifest.transport_manifest_sha256 in manifest_text

    with pytest.raises(HippiusPrivateInputEncryptionError, match="must not exist"):
        prepare_hippius_private_input_transport(
            commitment_path=commitment_path,
            records_dir=records_dir,
            wrapping_public_key_path=public_key_path,
            output_dir=output_dir,
        )


def test_publication_object_read_rejects_post_plan_drift(tmp_path: Path) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    plan = plan_private_catalog_publication(
        commitment_path=commitment_path,
        records_dir=records_dir,
    )
    record_path = records_dir / "000000.json"
    record_path.write_bytes(record_path.read_bytes() + b" ")

    with pytest.raises(CodingCatalogPublicationError, match="changed after planning"):
        read_private_catalog_publication_object(
            records_dir=records_dir,
            item=plan.objects[0],
        )


def test_hippius_private_input_transport_rejects_weak_or_private_wrapping_key(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    weak_private, weak_public_path = _write_wrapping_public_key(tmp_path, key_size=2048)
    with pytest.raises(HippiusPrivateInputEncryptionError, match="RSA-3072"):
        prepare_hippius_private_input_transport(
            commitment_path=commitment_path,
            records_dir=records_dir,
            wrapping_public_key_path=weak_public_path,
            output_dir=(tmp_path / "weak-output").resolve(),
        )

    private_path = tmp_path / "private-key.pem"
    private_path.write_bytes(
        weak_private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(HippiusPrivateInputEncryptionError, match="invalid"):
        prepare_hippius_private_input_transport(
            commitment_path=commitment_path,
            records_dir=records_dir,
            wrapping_public_key_path=private_path,
            output_dir=(tmp_path / "private-output").resolve(),
        )


def test_hippius_private_input_transport_keeps_incomplete_output_unpublishable(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    _private_key, public_key_path = _write_wrapping_public_key(tmp_path)
    output_dir = (tmp_path / "incomplete-output").resolve()

    with pytest.raises(HippiusPrivateInputEncryptionError, match="entropy"):
        prepare_hippius_private_input_transport(
            commitment_path=commitment_path,
            records_dir=records_dir,
            wrapping_public_key_path=public_key_path,
            output_dir=output_dir,
            random_bytes=lambda size: b"x" * (size - 1),
        )

    assert output_dir.is_dir()
    assert not (output_dir / "manifest.json").exists()
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700


def test_hippius_private_input_transport_loader_rejects_ciphertext_drift(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    _private_key, public_key_path = _write_wrapping_public_key(tmp_path)
    output_dir = (tmp_path / "encrypted").resolve()
    manifest = prepare_hippius_private_input_transport(
        commitment_path=commitment_path,
        records_dir=records_dir,
        wrapping_public_key_path=public_key_path,
        output_dir=output_dir,
    )
    ciphertext_path = output_dir / manifest.objects[0].ciphertext_relative_path
    ciphertext_path.write_bytes(ciphertext_path.read_bytes() + b"x")

    with pytest.raises(HippiusPrivateInputEncryptionError, match="manifest"):
        load_hippius_private_input_transport(output_dir)


def test_hippius_private_input_cli_requires_confirmation_and_prepares_transport(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_encryption_script()
    with pytest.raises(SystemExit) as caught:
        script.main(
            [
                "--commitment",
                "missing",
                "--records-dir",
                "missing",
                "--wrapping-public-key",
                "missing",
                "--output-dir",
                str((tmp_path / "not-created").resolve()),
                "--confirm",
                "ENCRYPT CODING INPUTS",
            ]
        )
    assert caught.value.code == 2
    assert HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION in capsys.readouterr().err

    commitment_path, records_dir = _write_fixture(tmp_path / "valid")
    _private_key, public_key_path = _write_wrapping_public_key(tmp_path / "valid")
    output_dir = (tmp_path / "valid/encrypted").resolve()
    result = script.main(
        [
            "--commitment",
            str(commitment_path),
            "--records-dir",
            str(records_dir),
            "--wrapping-public-key",
            str(public_key_path),
            "--output-dir",
            str(output_dir),
            "--confirm",
            HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION,
        ]
    )
    assert result == 0
    assert (output_dir / "manifest.json").is_file()
    stdout = capsys.readouterr().out
    assert "encrypted 1 private catalog records" in stdout
    assert "transport_manifest_sha256=" in stdout


def test_curator_publication_rejects_noncanonical_or_incomplete_records(
    tmp_path: Path,
) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    record = records_dir / "000000.json"
    raw = json.loads(record.read_bytes())
    raw["future_private_hint"] = "must not become publication authority"
    record.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CodingCatalogPublicationError, match="not canonical"):
        plan_private_catalog_publication(
            commitment_path=commitment_path,
            records_dir=records_dir,
        )

    commitment_path, records_dir = _write_fixture(tmp_path / "incomplete")
    (records_dir / "000001.json").write_bytes(
        (records_dir / "000000.json").read_bytes()
    )
    with pytest.raises(
        CodingCatalogPublicationError, match="does not match the commitment"
    ):
        plan_private_catalog_publication(
            commitment_path=commitment_path,
            records_dir=records_dir,
        )

    commitment_path, records_dir = _write_fixture(tmp_path / "oversized")
    commitment_path.write_bytes(b"{" + (b"x" * ((1 << 20) + 1)) + b"}")
    with pytest.raises(CodingCatalogPublicationError, match="exceeds bounds"):
        plan_private_catalog_publication(
            commitment_path=commitment_path,
            records_dir=records_dir,
        )


def test_publication_plan_bound_covers_maximum_committed_catalog() -> None:
    assert _MAX_PLAN_BYTES >= (1_000_000 * 1024) + (1 << 20)
