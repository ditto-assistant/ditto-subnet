"""Explicit one-attempt Platform composition; no public API or scheduler wiring."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt
from ditto.api_server.coding_hippius_retrieval import AiobotoHippiusPrivateInputReader
from ditto.api_server.coding_hosted_authoring_evidence import (
    HostedAuthoringEvidencePublisher,
    canonical,
    sha,
)
from ditto.api_server.coding_hosted_control import HostedAuthoringControl
from ditto.api_server.coding_hosted_control_server import HostedControlServer
from ditto.api_server.coding_hosted_evidence import HostedInferenceEvidencePublisher
from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceSpool
from ditto.api_server.coding_hosted_grading import HostedGradingControl
from ditto.api_server.coding_hosted_inputs import HostedAuthoringInputAssembler
from ditto.api_server.coding_hosted_launch import (
    HostedLaunchProjection,
    inspect_launch,
    prepare_launch,
)
from ditto.api_server.coding_hosted_private_grants import HostedPrivateGrantStore
from ditto.api_server.coding_hosted_runtime_config import (
    HostedRuntimeConfig,
    load_runtime_config,
)
from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    run_private_process,
    write_private,
)
from ditto.api_server.coding_private_v2_retrieval import PrivateV2InputRetriever
from ditto.api_server.coding_private_v2_unwrap import ProcessPrivateV2Unwrapper
from ditto.api_server.storage import S3StorageClient
from ditto.db.factory import create_db_engine, create_session_maker
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)


@dataclass(repr=False)
class HostedRuntimeServices:
    reader: AiobotoHippiusPrivateInputReader
    retriever: PrivateV2InputRetriever
    control: HostedAuthoringControl
    server: HostedControlServer
    spools: list[HostedEvidenceSpool]
    socket: Path
    token: bytes
    drained: bool = False

    async def shutdown(self) -> None:
        await self.server.shutdown()
        try:
            await self.control.shutdown()
        except Exception:
            # A failed revoke may leave a provider's shielded retention task
            # active. Keep its reader/spools/database alive, never dispose them
            # behind it and call that cleanup. The dedicated process fails.
            raise HostedRuntimeError("runtime control drain is unconfirmed") from None
        self.drained = True
        failed = False
        for spool in self.spools:
            try:
                spool.close()
            except Exception:
                failed = True
        try:
            await self.reader.__aexit__(None, None, None)
        except Exception:
            failed = True
        if failed:
            raise HostedRuntimeError("runtime service shutdown is unconfirmed")


async def build_runtime_services(
    config: HostedRuntimeConfig,
    sessions: async_sessionmaker[AsyncSession],
    projection: HostedLaunchProjection,
) -> HostedRuntimeServices:
    wire = config.wire
    root = Path(wire.runtime_root)
    probe, _ = load_hippius_probe_receipt(Path(wire.probe_receipt_file))
    checked = datetime.fromisoformat(probe.checked_at.replace("Z", "+00:00"))
    if (
        not 0 <= (datetime.now(UTC) - checked).total_seconds() < 86400
        or probe.private_input_authority_sha256 != config.reader.authority_sha256
        or probe.sealed_evidence_authority_sha256 != config.evidence.authority_sha256
    ):
        raise HostedRuntimeError("runtime storage probe is stale or mismatched")
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(Path(wire.evidence_public_key_file))
    if wrapper.wrapping_key_sha256 != wire.evidence_wrapping_key_sha256:
        raise HostedRuntimeError("runtime wrapping identity differs")
    reader = AiobotoHippiusPrivateInputReader(config.reader, object_namespace="v2")
    spools: list[HostedEvidenceSpool] = []
    try:
        unwrap = ProcessPrivateV2Unwrapper(
            executable=Path(wire.unwrap_executable),
            work_root=Path(wire.unwrap_work_root),
        )
        retrievers = []
        for audience in ("platform-authoring", "platform-grading"):
            retrievers.append(
                PrivateV2InputRetriever(
                    registration=projection.registration,
                    transport_manifest=Path(wire.transport_manifest_file),
                    payload_authority=Path(wire.payload_authority_file),
                    publication_receipt=Path(wire.publication_receipt_file),
                    trusted_curator_public_key_path=Path(wire.curator_public_key_file),
                    reader_authority_sha256=config.reader.authority_sha256,
                    audience=audience,
                    grants=HostedPrivateGrantStore(
                        sessions=sessions, worker_id=wire.worker_id, audience=audience
                    ),
                    reader=reader,
                    unwrapper=unwrap,
                )
            )
        for name in ("inference-spool", "authoring-spool", "terminal-spool"):
            directory = root / name
            directory.mkdir(mode=0o700)
            spools.append(
                HostedEvidenceSpool(
                    directory,
                    max_bytes=wire.spool_max_bytes,
                    max_objects=wire.spool_max_objects,
                )
            )
        inference = HostedInferenceEvidencePublisher(
            sessions=sessions,
            worker_id=wire.worker_id,
            spool=spools[0],
            wrapper=wrapper,
            runtime_profile=config.budget,
            config=config.evidence,
            probe_receipt_path=Path(wire.probe_receipt_file),
        )
        authoring = HostedAuthoringEvidencePublisher(
            sessions=sessions,
            worker_id=wire.worker_id,
            spool=spools[1],
            wrapper=wrapper,
            config=config.evidence,
            probe_receipt_path=Path(wire.probe_receipt_file),
        )
        terminal = HostedAuthoringEvidencePublisher(
            sessions=sessions,
            worker_id=wire.worker_id,
            spool=spools[2],
            wrapper=wrapper,
            config=config.evidence,
            probe_receipt_path=Path(wire.probe_receipt_file),
        )
        grading = HostedGradingControl(
            sessions=sessions,
            worker_id=wire.worker_id,
            retriever=retrievers[1],
            authoring_evidence=authoring,
            cipher_store=terminal,
            profile=config.grading,
        )
        bridge_root = root / "bridges"
        bridge_root.mkdir(mode=0o700)
        control = HostedAuthoringControl(
            sessions=sessions,
            worker_id=wire.worker_id,
            evaluation_id=wire.evaluation_id,
            assembler=HostedAuthoringInputAssembler(
                sessions=sessions, worker_id=wire.worker_id, retriever=retrievers[0]
            ),
            policy=config.policy,
            execution_profile=config.execution,
            budget_profile=config.budget,
            api_key=config.provider_key,
            inference_evidence=inference,
            authoring_evidence=authoring,
            bridge_root=bridge_root,
            grading=grading,
        )
        token = secrets.token_bytes(32)
        server = HostedControlServer(control, token)
        socket = root / "control.sock"
        await server.start(socket)
        return HostedRuntimeServices(
            reader, retrievers[0], control, server, spools, socket, token
        )
    except BaseException:
        for spool in spools:
            spool.close()
        await reader.__aexit__(None, None, None)
        raise


async def _projection(
    config: HostedRuntimeConfig, sessions: async_sessionmaker[AsyncSession]
) -> HostedLaunchProjection:
    wire = config.wire
    return await inspect_launch(
        sessions,
        evaluation_id=wire.evaluation_id,
        attempt_id=wire.attempt_id,
        assignment_sha256=wire.assignment_sha256,
        policy_sha256=config.policy.digest(),
        execution_profile_sha256=sha(config.execution),
        grading_profile_sha256=sha(config.grading),
    )


def write_worker_config(
    config: HostedRuntimeConfig,
    services: HostedRuntimeServices,
    expected: dict,
    harness: dict,
) -> Path:
    wire = config.wire
    root = Path(wire.runtime_root)
    for name, body in (
        ("control-token", services.token),
        ("execution.json", config.execution),
        ("grading.json", config.grading),
        (
            "postgres.json",
            coding_canonical_json_bytes(
                list(config.postgres_entries),
                maximum_bytes=128 << 10,
                label="private start configuration",
            ),
        ),
    ):
        write_private(root / name, body)
    worker_root = root / "worker"
    worker_root.mkdir(mode=0o700)
    value = {
        **wire.host.model_dump(),
        "schema": "dittobench-coding-hosted-runtime-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "expected": expected,
        "harness": harness,
        "authoring_profile_file": str(root / "execution.json"),
        "grading_profile_file": str(root / "grading.json"),
        "grading_profile_sha256": sha(config.grading),
        "control_socket": str(services.socket),
        "control_token_file": str(root / "control-token"),
        "python_executable": wire.python_executable,
        "postgres_environment_file": str(root / "postgres.json"),
        "state_root": str(worker_root),
    }
    output = root / "worker.json"
    write_private(output, canonical(value, 65536))
    return output


async def verify_runtime_result(
    body: bytes, config: HostedRuntimeConfig, sessions: async_sessionmaker[AsyncSession]
) -> str:
    result = _decode_json_document(body, maximum_bytes=4096)
    if (
        not isinstance(result, dict)
        or result.get("schema") != "dittobench-coding-hosted-runtime-result-v2"
        or result.get("shadow_only") is not True
        or result.get("weight_eligible") is not False
    ):
        raise HostedRuntimeError("runtime child receipt invalid")
    wire = config.wire
    async with asyncio.timeout(20), sessions() as session:
        final = await session.get(CodingHostedTerminalFinalization, wire.evaluation_id)
        row = await session.get(CodingHostedTerminalReservation, wire.evaluation_id)
        assignment = await session.get(CodingHostedAssignment, wire.evaluation_id)
        if final is None or row is None or assignment is None:
            raise HostedRuntimeError("runtime terminal evidence not finalized")
        identity = HostedTerminalIdentity.model_validate_json(canonical(row.identity))
        if (
            identity.digest() != row.identity_sha256
            or result.get("terminal_evidence_sha256") != row.identity_sha256
            or identity.source.evaluation_id != wire.evaluation_id
            or identity.source.attempt_id != wire.attempt_id
            or identity.source.worker_id != wire.worker_id
            or identity.source.assignment_sha256 != wire.assignment_sha256
            or identity.grading_profile_sha256 != sha(config.grading)
            or assignment.assignment_sha256 != wire.assignment_sha256
            or assignment.worker_id != wire.worker_id
            or identity.source.artifact_sha256 != assignment.artifact_sha256
            or identity.source.deadline_unix != int(assignment.expires_at.timestamp())
            or assignment.authority["policy_sha256"] != config.policy.digest()
            or assignment.authority["execution_profile_sha256"] != sha(config.execution)
            or assignment.authority["grading_profile_sha256"] != sha(config.grading)
        ):
            raise HostedRuntimeError("runtime terminal evidence differs")
        return row.identity_sha256


async def run_runtime(path: Path) -> str:
    return await run_loaded_runtime(load_runtime_config(path))


async def run_loaded_runtime(config: HostedRuntimeConfig) -> str:
    """Execute one already-loaded configuration; never reload mutable policy files."""
    wire = config.wire
    engine = create_db_engine(config.postgres)
    sessions = create_session_maker(engine)
    services: HostedRuntimeServices | None = None
    try:
        projection = await _projection(config, sessions)
        root = Path(wire.runtime_root)
        write_private(
            root / "platform-consumed",
            b"dittobench-coding-hosted-platform-consumed-v2\n",
        )
        services = await build_runtime_services(config, sessions, projection)
        async with S3StorageClient(config.image_storage) as storage:
            expected, harness = await prepare_launch(
                sessions,
                projection=projection,
                worker_id=wire.worker_id,
                retriever=services.retriever,
                storage=storage,
            )
        worker_config = write_worker_config(config, services, expected, harness)
        valid = await run_private_process(
            (wire.worker_executable, "--validate-only", "--config", str(worker_config)),
            root=root,
            timeout=30,
        )
        if (
            valid != b"hosted worker configuration valid\n"
            or await _projection(config, sessions) != projection
        ):
            raise HostedRuntimeError("runtime launch validation failed")
        remaining = projection.authority.deadline_unix - time.time()
        if remaining <= 0:
            raise HostedRuntimeError("runtime launch expired")
        body = await run_private_process(
            (
                wire.worker_executable,
                "--private-shadow-once",
                "--config",
                str(worker_config),
            ),
            root=root,
            timeout=remaining + 3600,
            shutdown_grace=1800,
        )
        return await verify_runtime_result(body, config, sessions)
    finally:
        try:
            if services is not None:
                await services.shutdown()
        finally:
            if services is None or services.drained:
                await engine.dispose()
