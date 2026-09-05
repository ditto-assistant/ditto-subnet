from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_catalog_v2_compile import (
    compile_private_catalog_v2,
)
from ditto.api_server.coding_private_v2_shadow_plan import (
    PrivateV2ShadowPlanError,
    build_private_v2_shadow_canary,
)
from ditto.tests.api_server.test_coding_private_catalog_v2_compile import _fixture


def test_shadow_canary_rejects_extra_registration_fields(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _fixture(protected)
    catalog = compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    projection = {
        "catalog_merkle_root": catalog["catalog_merkle_root"],
        "catalog_sha256": catalog["catalog_sha256"],
        "coding_contract_version": 2,
        "corpus_release_id": catalog["corpus_release_id"],
        "payload_sha256": "a" * 64,
        "previous_registration_sha256": None,
        "private_release_sha256": catalog["private_release_sha256"],
        "publication_receipt_sha256": "b" * 64,
        "schema": "dittobench-coding-private-v2-registration-v1",
        "shadow_only": True,
        "transport_sha256": "c" * 64,
        "weight_eligible": False,
        "wrapping_key_sha256": "d" * 64,
        "activate": True,
    }
    registration = {
        **projection,
        "registration_sha256": hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=64 << 10,
                label="private v2 authority",
            )
        ).hexdigest(),
    }
    path = protected / "registration.json"
    path.write_bytes(
        coding_canonical_json_bytes(
            registration,
            maximum_bytes=64 << 10,
            label="private v2 registration authority",
        )
    )
    path.chmod(0o600)
    with pytest.raises(PrivateV2ShadowPlanError, match="registration authority"):
        build_private_v2_shadow_canary(
            registration_authority=path,
            catalog_directory=protected / "catalog",
            catalog_index=0,
            output=protected / "canary.json",
        )


def test_shadow_canary_rejects_catalog_identity_drift(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    release, groups = _fixture(protected)
    catalog = compile_private_catalog_v2(
        release_authority=release,
        groups_root=groups,
        output=protected / "catalog",
    )
    projection = {
        "catalog_merkle_root": "f" * 64,
        "catalog_sha256": catalog["catalog_sha256"],
        "coding_contract_version": 2,
        "corpus_release_id": catalog["corpus_release_id"],
        "payload_sha256": "a" * 64,
        "previous_registration_sha256": None,
        "private_release_sha256": catalog["private_release_sha256"],
        "publication_receipt_sha256": "b" * 64,
        "schema": "dittobench-coding-private-v2-registration-v1",
        "shadow_only": True,
        "transport_sha256": "c" * 64,
        "weight_eligible": False,
        "wrapping_key_sha256": "d" * 64,
    }
    registration = {
        **projection,
        "registration_sha256": hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=64 << 10,
                label="private v2 authority",
            )
        ).hexdigest(),
    }
    path = protected / "registration.json"
    path.write_bytes(
        coding_canonical_json_bytes(
            registration,
            maximum_bytes=64 << 10,
            label="private v2 registration authority",
        )
    )
    path.chmod(0o600)
    with pytest.raises(PrivateV2ShadowPlanError, match="registration authority"):
        build_private_v2_shadow_canary(
            registration_authority=path,
            catalog_directory=protected / "catalog",
            catalog_index=0,
            output=protected / "canary.json",
        )
