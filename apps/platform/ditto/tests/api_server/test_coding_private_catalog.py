"""Tests for the bounded private coding-catalog object transport."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_server.coding_private_catalog import (
    CodingPrivateCatalogConfig,
    CodingPrivateCatalogConfigurationError,
    S3CodingPrivateCatalogSource,
    coding_private_catalog_record_key,
    parse_coding_private_catalog_config_from_env,
)
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectDownloadTooLargeError,
)
from ditto.coding_selection import (
    CodingSelectionCatalogIntegrityError,
    CodingSelectionCatalogUnavailableError,
)

_VECTOR_PATH = (
    Path(__file__).parents[5]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_selection_v1.json"
)
_EXECUTION_PATH = (
    Path(__file__).parents[5]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_execution_plan_v1.json"
)
_RECURSIVE_JSON = b'{"future":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}"


class _Reader:
    def __init__(
        self,
        body: bytes = b"",
        *,
        error: Exception | None = None,
        wait: asyncio.Event | None = None,
    ) -> None:
        self.body = body
        self.error = error
        self.wait = wait
        self.calls: list[tuple[str, int]] = []

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        self.calls.append((key, max_bytes))
        if self.wait is not None:
            await self.wait.wait()
        if self.error is not None:
            raise self.error
        return self.body


def _vector() -> dict[str, Any]:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def _execution() -> dict[str, Any]:
    return json.loads(_EXECUTION_PATH.read_text(encoding="utf-8"))


def _record(vector: dict[str, Any]) -> dict[str, Any]:
    execution = _execution()
    return {
        "schema": "dittobench-coding-private-catalog-record-v1",
        "catalog_commitment_sha256": vector["commitment"]["commitment_sha256"],
        "task_version": deepcopy(vector["task_version"]),
        "membership_proof": deepcopy(vector["membership_proof"]),
        "issue": deepcopy(vector["issue"]),
        "runtime_policy": deepcopy(vector["runtime_policy"]),
        "budgets": deepcopy(vector["budgets"]),
        "runner_plan": execution["runner_plan"],
        "grader_plan": execution["grader_plan"],
        "grader_resource_profile": execution["grader_resource_profile"],
    }


def _body(record: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _config(**overrides: object) -> CodingPrivateCatalogConfig:
    values: dict[str, object] = {
        "endpoint_url": "https://storage.example.com",
        "bucket": "ditto-coding-private",
        "access_key": "catalog-access",
        "secret_key": "catalog-secret",
        "region": "eu-west-1",
        "use_tls": True,
        "max_record_bytes": 2 << 20,
        "timeout_seconds": 1.0,
    }
    values.update(overrides)
    return CodingPrivateCatalogConfig(**values)  # type: ignore[arg-type]


def _source(
    body: bytes,
    *,
    reader: _Reader | None = None,
    config: CodingPrivateCatalogConfig | None = None,
) -> tuple[S3CodingPrivateCatalogSource, _Reader]:
    resolved_reader = reader or _Reader(body)
    return (
        S3CodingPrivateCatalogSource(
            config or _config(), object_reader=resolved_reader
        ),
        resolved_reader,
    )


def _commitment(vector: dict[str, Any]) -> CodingCatalogCommitment:
    return CodingCatalogCommitment.model_validate(vector["commitment"])


def test_private_catalog_configuration_is_disabled_by_default() -> None:
    assert parse_coding_private_catalog_config_from_env({}) is None


def test_private_catalog_default_record_bound_covers_contract_maximum() -> None:
    config = parse_coding_private_catalog_config_from_env(
        {
            "DITTO_CODING_CATALOG_STORAGE_ENDPOINT_URL": (
                "https://storage.example.com"
            ),
            "DITTO_CODING_CATALOG_STORAGE_BUCKET": "private-catalog",
            "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY": "catalog-access",
            "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY": "catalog-secret",
        }
    )
    assert config is not None
    assert config.max_record_bytes == 2 << 20


def test_private_catalog_configuration_is_separate_and_redacted() -> None:
    config = parse_coding_private_catalog_config_from_env(
        {
            "DITTO_CODING_CATALOG_STORAGE_ENDPOINT_URL": (
                "https://storage.example.com"
            ),
            "DITTO_CODING_CATALOG_STORAGE_BUCKET": "private-catalog",
            "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY": "catalog-access",
            "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY": "catalog-secret",
            "DITTO_CODING_CATALOG_STORAGE_REGION": "eu-central-1",
            "DITTO_CODING_CATALOG_STORAGE_USE_TLS": "true",
            "DITTO_CODING_CATALOG_MAX_RECORD_BYTES": "32768",
            "DITTO_CODING_CATALOG_TIMEOUT_SECONDS": "2.5",
        }
    )
    assert config is not None
    assert config.bucket == "private-catalog"
    assert config.max_record_bytes == 32768
    assert config.timeout_seconds == 2.5
    rendered = repr(config)
    assert "configured=True" in rendered
    assert "storage.example.com" not in rendered
    assert "private-catalog" not in rendered
    assert "catalog-access" not in rendered
    assert "catalog-secret" not in rendered


@pytest.mark.parametrize(
    "values,match",
    [
        (
            {"DITTO_CODING_CATALOG_STORAGE_BUCKET": "private"},
            "incomplete",
        ),
        (
            {
                "DITTO_CODING_CATALOG_STORAGE_ENDPOINT_URL": (
                    "http://storage.example.com"
                ),
                "DITTO_CODING_CATALOG_STORAGE_BUCKET": "private",
                "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY": "access",
                "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY": "secret",
                "DITTO_CODING_CATALOG_STORAGE_USE_TLS": "false",
            },
            "loopback",
        ),
        (
            {
                "DITTO_CODING_CATALOG_STORAGE_ENDPOINT_URL": (
                    "https://user:password@storage.example.com/path?query=1"
                ),
                "DITTO_CODING_CATALOG_STORAGE_BUCKET": "private",
                "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY": "access",
                "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY": "secret",
            },
            "origin URL",
        ),
        (
            {
                "DITTO_CODING_CATALOG_STORAGE_ENDPOINT_URL": (
                    "https://storage.example.com:99999"
                ),
                "DITTO_CODING_CATALOG_STORAGE_BUCKET": "private",
                "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY": "access",
                "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY": "secret",
            },
            "origin URL",
        ),
    ],
)
def test_private_catalog_configuration_fails_closed(
    values: dict[str, str], match: str
) -> None:
    with pytest.raises(CodingPrivateCatalogConfigurationError, match=match):
        parse_coding_private_catalog_config_from_env(values)


def test_record_key_is_fixed_and_content_addressed() -> None:
    commitment = "ab" * 32
    assert (
        coding_private_catalog_record_key(
            catalog_commitment_sha256=commitment,
            catalog_index=42,
        )
        == f"coding-catalog/v1/{commitment}/records/000042.json"
    )

    for invalid in ("../" + commitment, commitment.upper(), "ab"):
        with pytest.raises(ValueError, match="lowercase SHA-256"):
            coding_private_catalog_record_key(
                catalog_commitment_sha256=invalid,
                catalog_index=0,
            )
    for invalid_index in (-1, 1_000_000, True):
        with pytest.raises(ValueError, match="catalog index"):
            coding_private_catalog_record_key(
                catalog_commitment_sha256=commitment,
                catalog_index=invalid_index,
            )


async def test_source_loads_exactly_one_bounded_record() -> None:
    vector = _vector()
    commitment = _commitment(vector)
    record = _record(vector)
    record["future_field"] = {"ignored": True}
    record["issue"]["future_field"] = "ignored"
    source, reader = _source(_body(record))

    material = await source.get_task_material(
        commitment=commitment,
        catalog_index=4,
    )
    task = material.task_version
    proof = material.membership_proof

    assert (
        task.task_commitment_sha256 == vector["task_version"]["task_commitment_sha256"]
    )
    assert material.issue.model_dump(mode="json") == vector["issue"]
    assert material.runtime_policy.model_dump(mode="json") == vector["runtime_policy"]
    assert material.budgets.model_dump(mode="json") == vector["budgets"]
    assert (
        material.task_version.payload.runner_plan_sha256
        == _execution()["expected"]["runner_plan_sha256"]
    )
    assert (
        proof.catalog_membership_proof_sha256
        == vector["membership_proof"]["catalog_membership_proof_sha256"]
    )
    assert reader.calls == [
        (
            coding_private_catalog_record_key(
                catalog_commitment_sha256=commitment.commitment_sha256,
                catalog_index=4,
            ),
            2 << 20,
        )
    ]


async def test_task_version_adapter_preserves_selector_contract() -> None:
    vector = _vector()
    source, reader = _source(_body(_record(vector)))

    task, proof = await source.get_task_version(
        commitment=_commitment(vector),
        catalog_index=4,
    )

    assert (
        task.task_commitment_sha256 == vector["task_version"]["task_commitment_sha256"]
    )
    assert (
        proof.catalog_membership_proof_sha256
        == vector["membership_proof"]["catalog_membership_proof_sha256"]
    )
    assert len(reader.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"\xff",
        b'{"schema":"one","schema":"two"}',
        b'{"schema":NaN}',
        _RECURSIVE_JSON,
    ],
)
async def test_source_rejects_malformed_or_ambiguous_json(body: bytes) -> None:
    vector = _vector()
    source, _reader = _source(body)
    with pytest.raises(CodingSelectionCatalogIntegrityError, match="malformed"):
        await source.get_task_version(
            commitment=_commitment(vector),
            catalog_index=4,
        )


async def test_source_rejects_excessive_unknown_field_depth() -> None:
    vector = _vector()
    record = _record(vector)
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(40):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child
    record["future"] = nested
    source, _reader = _source(_body(record))
    with pytest.raises(CodingSelectionCatalogIntegrityError, match="malformed"):
        await source.get_task_version(
            commitment=_commitment(vector),
            catalog_index=4,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "commitment",
        "release",
        "index",
        "root",
        "count",
        "task_digest",
        "proof_digest",
        "issue",
        "runtime_policy",
        "budgets",
        "runner_plan",
        "runner_digest",
        "grader_plan",
        "resource_profile",
        "missing_plan",
    ],
)
async def test_source_rejects_record_or_membership_drift(mutation: str) -> None:
    vector = _vector()
    record = _record(vector)
    if mutation == "commitment":
        record["catalog_commitment_sha256"] = "ff" * 32
    elif mutation == "release":
        record["task_version"]["payload"]["corpus_release_id"] = "other-release"
    elif mutation == "index":
        record["task_version"]["payload"]["catalog_index"] = 3
    elif mutation == "root":
        proof = record["membership_proof"]
        proof["catalog_merkle_root"] = "ff" * 32
        proof["catalog_membership_proof_sha256"] = _proof_digest(proof)
    elif mutation == "count":
        proof = record["membership_proof"]
        proof["task_version_count"] = 8
        proof["catalog_membership_proof_sha256"] = _proof_digest(proof)
    elif mutation == "task_digest":
        record["task_version"]["task_commitment_sha256"] = "ff" * 32
    elif mutation == "proof_digest":
        record["membership_proof"]["catalog_membership_proof_sha256"] = "ff" * 32
    elif mutation == "issue":
        record["issue"]["description"] = "Changed after catalog commitment."
    elif mutation == "runtime_policy":
        record["runtime_policy"]["editable_paths"] = ["src/other.py"]
    elif mutation == "budgets":
        record["budgets"]["workspace_tool_calls"] += 1
    elif mutation == "runner_plan":
        record["runner_plan"]["test_commands"][0]["timeout_milliseconds"] += 1
    elif mutation == "runner_digest":
        record["task_version"]["payload"]["runner_plan_sha256"] = "ff" * 32
    elif mutation == "grader_plan":
        record["grader_plan"]["test_groups"][0]["expected_total"] += 1
    elif mutation == "resource_profile":
        record["grader_resource_profile"]["memory_limit_bytes"] += 1
    elif mutation == "missing_plan":
        record.pop("grader_plan")
    source, _reader = _source(_body(record))
    with pytest.raises(CodingSelectionCatalogIntegrityError):
        await source.get_task_version(
            commitment=_commitment(vector),
            catalog_index=4,
        )


async def test_source_classifies_bounds_and_transport_without_leaking_keys() -> None:
    vector = _vector()
    commitment = _commitment(vector)
    oversized_source, _reader = _source(
        b"x" * 4097,
        config=_config(max_record_bytes=4096),
    )
    with pytest.raises(CodingSelectionCatalogIntegrityError, match="violated"):
        await oversized_source.get_task_version(
            commitment=commitment,
            catalog_index=4,
        )

    too_large_source, _reader = _source(
        b"",
        reader=_Reader(error=ObjectDownloadTooLargeError("secret/key/000004.json")),
    )
    with pytest.raises(CodingSelectionCatalogIntegrityError) as too_large:
        await too_large_source.get_task_version(
            commitment=commitment,
            catalog_index=4,
        )
    assert "secret/key" not in str(too_large.value)

    missing_source, _reader = _source(
        b"",
        reader=_Reader(error=ObjectDownloadFailedError("secret/key/000004.json")),
    )
    with pytest.raises(CodingSelectionCatalogUnavailableError) as missing:
        await missing_source.get_task_version(
            commitment=commitment,
            catalog_index=4,
        )
    assert "secret/key" not in str(missing.value)


async def test_source_rejects_a_reader_returning_a_non_byte_body() -> None:
    vector = _vector()
    reader = _Reader()
    reader.body = "not-bytes"  # type: ignore[assignment]
    source, _reader = _source(b"", reader=reader)
    with pytest.raises(CodingSelectionCatalogIntegrityError, match="byte bound"):
        await source.get_task_version(
            commitment=_commitment(vector),
            catalog_index=4,
        )


async def test_source_times_out_and_preserves_cancellation() -> None:
    vector = _vector()
    commitment = _commitment(vector)
    blocked = asyncio.Event()
    timed_source, _reader = _source(
        b"",
        reader=_Reader(wait=blocked),
        config=_config(timeout_seconds=0.1),
    )
    with pytest.raises(CodingSelectionCatalogUnavailableError, match="timed out"):
        await timed_source.get_task_version(
            commitment=commitment,
            catalog_index=4,
        )

    cancelled_source, _reader = _source(b"", reader=_Reader(wait=blocked))
    task = asyncio.create_task(
        cancelled_source.get_task_version(
            commitment=commitment,
            catalog_index=4,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _proof_digest(proof: dict[str, Any]) -> str:
    projection = deepcopy(proof)
    projection.pop("catalog_membership_proof_sha256", None)
    return coding_canonical_sha256(
        projection,
        maximum_bytes=4 << 20,
        label="coding catalog membership proof",
    )
