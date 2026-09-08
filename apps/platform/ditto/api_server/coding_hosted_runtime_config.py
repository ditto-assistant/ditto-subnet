"""Protected, explicit configuration for the one-attempt Platform process."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ditto.api_models.coding_hosted_inference import HostedInferencePolicy
from ditto.api_models.coding_hosted_runtime import (
    HostedPlatformRuntimeInput,
    HostedRuntimeImageStorage,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceConfig,
    parse_hippius_sealed_evidence_config,
)
from ditto.api_server.coding_hippius_retrieval import (
    HippiusPrivateInputRetrievalConfig,
    parse_hippius_private_input_retrieval_config,
)
from ditto.api_server.coding_hosted_authoring_evidence import canonical
from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator
from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    private_directory,
    protected_helper,
    read_json,
    read_private,
)
from ditto.api_server.storage.models import StorageConfig
from ditto.db.config import PostgresConfig

POSTGRES_KEYS = frozenset(
    {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_COMMAND_TIMEOUT",
        "POSTGRES_POOL_MIN_SIZE",
        "POSTGRES_POOL_MAX_SIZE",
    }
)
HIPPIUS_KEYS = frozenset(
    "DITTO_CODING_HIPPIUS_" + suffix
    for suffix in (
        "ENDPOINT_URL",
        "PRIVATE_INPUT_BUCKET",
        "PRIVATE_INPUT_CURATOR_ACCESS_KEY",
        "PRIVATE_INPUT_READER_ACCESS_KEY",
        "PRIVATE_INPUT_READER_SECRET_KEY",
        "SEALED_EVIDENCE_BUCKET",
        "EVIDENCE_MEDIATOR_ACCESS_KEY",
        "EVIDENCE_MEDIATOR_SECRET_KEY",
        "REGION",
        "TIMEOUT_SECONDS",
    )
)


@dataclass(frozen=True, repr=False)
class HostedRuntimeConfig:
    wire: HostedPlatformRuntimeInput
    postgres: PostgresConfig
    postgres_entries: tuple[str, ...]
    reader: HippiusPrivateInputRetrievalConfig
    evidence: HippiusSealedEvidenceConfig
    image_storage: StorageConfig
    provider_key: str
    execution: bytes
    grading: bytes
    budget: bytes
    policy: HostedInferencePolicy


def postgres_config(entries: object) -> tuple[PostgresConfig, tuple[str, ...]]:
    if not isinstance(entries, list) or not 5 <= len(entries) <= len(POSTGRES_KEYS):
        raise HostedRuntimeError("runtime database configuration invalid")
    values: dict[str, str] = {}
    for entry in entries:
        if type(entry) is not str or not 1 <= len(entry) <= 16384 or "\x00" in entry:
            raise HostedRuntimeError("runtime database configuration invalid")
        name, _, value = entry.partition("=")
        if name not in POSTGRES_KEYS or name in values or not value:
            raise HostedRuntimeError("runtime database configuration invalid")
        values[name] = value
    required = {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    }
    if not required <= values.keys():
        raise HostedRuntimeError("runtime database configuration incomplete")
    result = PostgresConfig(
        host=values["POSTGRES_HOST"],
        port=int(values["POSTGRES_PORT"]),
        user=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        database=values["POSTGRES_DB"],
        command_timeout=float(values.get("POSTGRES_COMMAND_TIMEOUT", "30")),
        pool_min_size=int(values.get("POSTGRES_POOL_MIN_SIZE", "2")),
        pool_max_size=int(values.get("POSTGRES_POOL_MAX_SIZE", "10")),
    )
    if (
        not 1 <= result.port <= 65535
        or not 1 <= result.pool_min_size <= result.pool_max_size <= 32
        or not math.isfinite(result.command_timeout)
        or not 1 <= result.command_timeout <= 60
    ):
        raise HostedRuntimeError("runtime database bounds invalid")
    return result, tuple(entries)


def load_runtime_config(
    path: Path, *, expected_sha256: str | None = None
) -> HostedRuntimeConfig:
    if expected_sha256 is None:
        document = read_json(path)
    else:
        from ditto.api_models.coding_inference import _decode_json_document

        body = read_private(path, 65536)
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            raise HostedRuntimeError("runtime configuration commitment differs")
        document = _decode_json_document(body, maximum_bytes=65536)
    wire = HostedPlatformRuntimeInput.model_validate(document)
    private_directory(Path(wire.runtime_root))
    private_directory(Path(wire.unwrap_work_root))
    protected_helper(Path(wire.worker_executable))
    protected_helper(Path(wire.unwrap_executable))
    if Path(wire.worker_executable).samefile(wire.unwrap_executable):
        raise HostedRuntimeError("runtime worker and custody executable must differ")
    postgres, entries = postgres_config(
        read_json(Path(wire.postgres_environment_file), 128 << 10)
    )
    hippius = read_json(Path(wire.hippius_environment_file))
    if (
        not isinstance(hippius, dict)
        or set(hippius) - HIPPIUS_KEYS
        or any(type(v) is not str or not v for v in hippius.values())
    ):
        raise HostedRuntimeError("runtime storage settings invalid")
    reader = parse_hippius_private_input_retrieval_config(hippius)
    evidence = parse_hippius_sealed_evidence_config(hippius)
    if (
        reader.bucket == evidence.bucket
        or reader.reader.access_key == evidence.mediator.access_key
        or reader.curator_access_key_id == evidence.mediator.access_key
    ):
        raise HostedRuntimeError("runtime storage identities must be distinct")
    image = HostedRuntimeImageStorage.model_validate(
        read_json(Path(wire.image_storage_file))
    )
    endpoint = urlparse(image.endpoint_url)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.port not in {None, 443}
    ):
        raise HostedRuntimeError("runtime image storage endpoint invalid")
    image_storage = StorageConfig(
        endpoint_url=image.endpoint_url,
        bucket=image.bucket,
        access_key=image.access_key,
        secret_key=image.secret_key,
        region=image.region,
        use_tls=True,
    )
    provider = read_private(Path(wire.provider_key_file), 4096).decode("ascii")
    if not provider or any(not 33 <= ord(c) <= 126 for c in provider):
        raise HostedRuntimeError("runtime provider credential invalid")
    execution = read_private(Path(wire.execution_profile_file), 16384)
    grading = read_private(Path(wire.grading_profile_file), 65536)
    budget = read_private(Path(wire.budget_profile_file), 65536)
    for body, maximum in ((execution, 16384), (grading, 65536)):
        from ditto.api_models.coding_inference import _decode_json_document

        if (
            canonical(_decode_json_document(body, maximum_bytes=maximum), maximum)
            != body
        ):
            raise HostedRuntimeError("runtime profile is not canonical")
    policy = HostedInferencePolicy.model_validate(
        read_json(Path(wire.policy_file), 16384)
    )
    ProfiledBudgetEstimator(profile_bytes=budget, policy=policy)
    for filename in (
        wire.transport_manifest_file,
        wire.payload_authority_file,
        wire.publication_receipt_file,
    ):
        read_private(Path(filename), 16 << 20)
    for filename in (
        wire.curator_public_key_file,
        wire.evidence_public_key_file,
        wire.probe_receipt_file,
    ):
        read_private(Path(filename), 65536)
    return HostedRuntimeConfig(
        wire,
        postgres,
        entries,
        reader,
        evidence,
        image_storage,
        provider,
        execution,
        grading,
        budget,
        policy,
    )
