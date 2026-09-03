from __future__ import annotations

import importlib.util
import json
import stat
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_evidence import CodingSealedEvidenceKind
from ditto.api_server import coding_hippius_canary_unwrap as unwrap
from ditto.api_server.coding_hippius_canary import HippiusShadowCanaryPlan
from ditto.api_server.coding_hippius_encryption import (
    load_hippius_private_input_transport_manifest,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceSourceAuthority,
)
from ditto.api_server.coding_hippius_publication import (
    load_hippius_private_input_publication_receipt,
)
from ditto.tests.api_server.test_coding_hippius_retrieval import (
    _published_release,
    _ticket,
)

ROOT = Path(__file__).parents[5]
_RELEASE = "hippius-synthetic-canary-v1"


def _script() -> ModuleType:
    path = ROOT / "apps/platform/scripts/prepare_hippius_canary_unwrap_authority.py"
    spec = importlib.util.spec_from_file_location(
        "prepare_hippius_canary_unwrap_authority",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _inputs(tmp_path: Path) -> tuple[Any, Any, Any, str]:
    release = await _published_release(tmp_path)
    manifest = load_hippius_private_input_transport_manifest(release.manifest_path)
    receipt, receipt_payload_sha256 = load_hippius_private_input_publication_receipt(
        release.receipt_path
    )
    commitment_raw = release.commitment.model_dump(mode="json", by_alias=True)
    commitment_raw["corpus_release_id"] = _RELEASE
    commitment_raw.pop("commitment_sha256")
    commitment_raw["commitment_sha256"] = coding_canonical_sha256(
        commitment_raw,
        maximum_bytes=1 << 20,
        label="synthetic canary commitment",
    )
    commitment = CodingCatalogCommitment.model_validate(commitment_raw)
    manifest = replace(
        manifest,
        corpus_release_id=_RELEASE,
        catalog_commitment_sha256=commitment.commitment_sha256,
    )
    receipt = replace(
        receipt,
        catalog_commitment_sha256=commitment.commitment_sha256,
    )
    private = replace(
        _ticket(release),
        commitment=commitment,
        transport_manifest_sha256=manifest.transport_manifest_sha256,
        publication_receipt_payload_sha256=receipt_payload_sha256,
    )
    plan = HippiusShadowCanaryPlan(
        canary_id=UUID("33333333-3333-4333-8333-333333333333"),
        source_sha="a" * 40,
        synthetic_corpus_release_id=_RELEASE,
        synthetic_record_sha256="b" * 64,
        private_input=private,
        sealed_evidence=HippiusSealedEvidenceSourceAuthority(
            ticket_id=private.ticket_id,
            claim_generation=1,
            validator_hotkey=private.validator_hotkey,
            instance_id="hippius-canary-validator-001",
            ticket_deadline=private.ticket_deadline,
            evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            weight_eligible=False,
        ),
        synthetic_only=True,
        single_validator=True,
        weight_eligible=False,
    )
    return plan, manifest, receipt, receipt_payload_sha256


async def test_authority_builder_derives_only_two_exact_phase_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, manifest, receipt, _payload_sha256 = await _inputs(tmp_path)
    monkeypatch.setattr(unwrap, "load_hippius_shadow_canary_plan", lambda _path: plan)
    monkeypatch.setattr(
        unwrap,
        "load_hippius_private_input_transport_manifest",
        lambda _path: manifest,
    )
    monkeypatch.setattr(
        unwrap,
        "load_hippius_private_input_publication_receipt",
        lambda _path: (receipt, plan.private_input.publication_receipt_payload_sha256),
    )

    authority = unwrap.prepare_hippius_canary_unwrap_authority(
        plan_path=tmp_path / "plan.json",
        manifest_path=tmp_path / "manifest.json",
        publication_receipt_path=tmp_path / "publication.json",
        confirmation=unwrap.HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION,
    )

    assert authority["synthetic_only"] is True
    assert authority["single_validator"] is True
    assert authority["weight_eligible"] is False
    assert authority["source_sha"] == plan.source_sha
    assert authority["ticket_id"] == str(plan.private_input.ticket_id)
    allowed = authority["allowed_requests"]
    assert isinstance(allowed, list)
    assert [item["delivery_phase"] for item in allowed] == ["authoring", "grading"]
    for field in ("aad_sha256", "ciphertext_sha256", "wrapped_data_key_sha256"):
        assert allowed[0][field] == allowed[1][field]
    assert allowed[0]["request_sha256"] != allowed[1]["request_sha256"]
    serialized = json.dumps(authority)
    assert "wrapped_data_key_b64" not in serialized
    assert '"data_key_b64"' not in serialized

    output = (tmp_path / "unwrap-authority.json").resolve()
    authority_sha256 = unwrap.write_hippius_canary_unwrap_authority(
        authority=authority,
        output=output,
    )
    assert authority_sha256 == authority["authority_sha256"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(unwrap.HippiusCanaryUnwrapAuthorityError, match="unsafe"):
        unwrap.write_hippius_canary_unwrap_authority(
            authority=authority,
            output=output,
        )


async def test_authority_builder_rejects_confirmation_or_registration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, manifest, receipt, payload_sha256 = await _inputs(tmp_path)
    monkeypatch.setattr(unwrap, "load_hippius_shadow_canary_plan", lambda _path: plan)
    monkeypatch.setattr(
        unwrap,
        "load_hippius_private_input_transport_manifest",
        lambda _path: manifest,
    )
    monkeypatch.setattr(
        unwrap,
        "load_hippius_private_input_publication_receipt",
        lambda _path: (receipt, payload_sha256),
    )
    with pytest.raises(unwrap.HippiusCanaryUnwrapAuthorityError, match="confirmed"):
        unwrap.prepare_hippius_canary_unwrap_authority(
            plan_path=tmp_path / "plan",
            manifest_path=tmp_path / "manifest",
            publication_receipt_path=tmp_path / "receipt",
            confirmation="PREPARE",
        )
    monkeypatch.setattr(
        unwrap,
        "load_hippius_private_input_publication_receipt",
        lambda _path: (receipt, "f" * 64),
    )
    with pytest.raises(unwrap.HippiusCanaryUnwrapAuthorityError, match="inputs"):
        unwrap.prepare_hippius_canary_unwrap_authority(
            plan_path=tmp_path / "plan",
            manifest_path=tmp_path / "manifest",
            publication_receipt_path=tmp_path / "receipt",
            confirmation=unwrap.HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION,
        )


def test_authority_cli_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script()
    arguments = [
        "--plan",
        "/protected/plan",
        "--manifest",
        "/protected/manifest",
        "--publication-receipt",
        "/protected/receipt",
        "--output",
        "/protected/output",
    ]
    with pytest.raises(SystemExit) as error:
        script.main([*arguments, "--confirm", "PREPARE"])
    assert error.value.code == 2

    monkeypatch.setattr(
        script,
        "prepare_hippius_canary_unwrap_authority",
        lambda **_kwargs: {"authority_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        script,
        "write_hippius_canary_unwrap_authority",
        lambda **_kwargs: "a" * 64,
    )
    assert (
        script.main(
            [
                *arguments,
                "--confirm",
                unwrap.HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION,
            ]
        )
        == 0
    )
