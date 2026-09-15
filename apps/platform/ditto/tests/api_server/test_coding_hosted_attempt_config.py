"""Fixed-host attempt config materializer against real Platform authority.

Synthetic public files and credentials only; no unit, provider or host is touched.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text, update

from ditto.api_server import coding_hosted_attempt_config as attempt
from ditto.api_server import coding_hosted_control as control
from ditto.api_server import coding_hosted_runtime as runtime
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_probe import (
    PROBE_RECEIPT_MAX_AGE_SECONDS,
    HippiusProbeCheck,
    HippiusProbeCheckStatus,
    hippius_private_input_authority_sha256,
    load_hippius_probe_receipt,
    write_hippius_probe_receipt,
)
from ditto.api_server.coding_hosted_attempt_config import HostedAttemptConfigError
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_launch import inspect_launch
from ditto.api_server.coding_hosted_runtime_config import load_runtime_config
from ditto.api_server.coding_hosted_runtime_io import write_private
from ditto.db.models import Agent, CodingHostedAssignment, CodingPrivateV2Release
from ditto.db.queries.coding_hosted_admission import start_hosted_attempt
from ditto.db.queries.coding_private_v2_releases import append_private_v2_release_event
from ditto.tests.api_server.test_coding_hippius_evidence import _config, _probe
from ditto.tests.api_server.test_coding_hosted_budget import budget_profile
from ditto.tests.api_server.test_coding_hosted_inference import native_policy
from ditto.tests.api_server.test_coding_hosted_inputs import fixture
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request
from ditto_screening_protocol import SCREENING_POLICY_VERSION

IMAGE = "sha256:" + "d" * 64
REPOSITORY = "coding-runtime.invalid/python/runtime"
REVISION = "5" * 40
PROVIDER_KEY = b"synthetic-provider-key"
HIPPIUS = {
    "DITTO_CODING_HIPPIUS_" + key: value
    for key, value in {
        "ENDPOINT_URL": "https://s3.hippius.com",
        "PRIVATE_INPUT_BUCKET": "coding-private-inputs",
        "PRIVATE_INPUT_CURATOR_ACCESS_KEY": "hip_curator",
        "PRIVATE_INPUT_READER_ACCESS_KEY": "hip_reader",
        "PRIVATE_INPUT_READER_SECRET_KEY": "synthetic-reader-secret",
        "SEALED_EVIDENCE_BUCKET": "coding-sealed-evidence",
        "EVIDENCE_MEDIATOR_ACCESS_KEY": "hip_evidence_mediator",
        "EVIDENCE_MEDIATOR_SECRET_KEY": "evidence-mediator-secret",
        "REGION": "decentralized",
    }.items()
}
# The release and probe bind the reader authority the Hippius environment derives.
READER = hippius_private_input_authority_sha256(
    endpoint_url="https://s3.hippius.com",
    region="decentralized",
    bucket="coding-private-inputs",
    curator_access_key="hip_curator",
    reader_access_key="hip_reader",
)
HOST_RECORD = {
    "schema": "dittobench-coding-hosted-host-prerequisites-v2",
    "shadow_only": True,
    "weight_eligible": False,
    "router_listen": "10.33.0.2:18080",
    "egress_network": "ditto-coding-restricted",
    "egress_proxy": "http://10.33.0.2:18090",
    "candidate_uid": 10001,
    "candidate_gid": 10001,
}


@cache
def evidence_key() -> bytes:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=3072)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def execution_profile(max_patch_bytes: int = 1024) -> bytes:
    return canonical(
        {
            "schema": "dittobench-coding-hosted-authoring-profile-v2",
            "image_digest": IMAGE,
            "resource_policy": {
                "candidate_limits": {"max_patch_bytes": max_patch_bytes}
            },
            "budgets": {"wall_time_seconds": 300},
        }
    )


GRADING = canonical(
    {
        "schema": "dittobench-coding-hosted-grading-profile-v2",
        "image_digest": IMAGE,
        "execution_timeout": 20_000_000_000,
    }
)


def probe_receipt(
    scratch: Path,
    *,
    age: float = 0,
    checked: float | None = None,
    reader: str = READER,
    evidence: dict | None = None,
    padding: int = 0,
) -> bytes:
    scratch.mkdir(mode=0o700)
    base, _ = load_hippius_probe_receipt(_probe(scratch, _config(**(evidence or {}))))
    if padding:
        base = replace(
            base,
            checks=(
                *base.checks,
                HippiusProbeCheck(
                    name="synthetic_padding",
                    status=HippiusProbeCheckStatus.PASS,
                    detail="x" * padding,
                ),
            ),
        )
    output = scratch / "fresh.json"
    write_hippius_probe_receipt(
        receipt=replace(
            base,
            checked_at=datetime.fromtimestamp(
                time.time() - age if checked is None else checked, UTC
            ).isoformat(),
            private_input_authority_sha256=reader,
        ),
        output=output,
    )
    return output.read_bytes()


def stage_input(f, kind: str, body: bytes) -> str:
    """As the wrapper stages it: named by digest, not writable by anyone."""
    digest = sha(body)
    path = f.layout.input(kind, digest)
    write_private(path, body)
    path.chmod(0o440)
    return digest


@asynccontextmanager
async def build(
    tmp_path, session_maker, *, admit=True, budget_seconds=3600, max_patch_bytes=1024
):
    tmp_path.chmod(0o700)
    source = tmp_path / "release-source"
    source.mkdir(mode=0o700)
    execution = execution_profile(max_patch_bytes)
    now = int(time.time())
    budget = budget_profile(
        valid_from_unix=now - 1, valid_until_unix=now + budget_seconds
    )
    policy = native_policy(runtime_profile_sha256=budget.digest())
    authority, *_ = await fixture(
        source,
        session_maker,
        {"sha256": sha(execution)},
        policy_sha256=policy.digest(),
        grading_profile_factory=lambda _artifacts: sha(GRADING),
        reader_authority_sha256=READER,
        start=False,
    )
    async with session_maker() as session, session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == authority.agent_id)
            .values(
                screened_image_ref=f"ditto-screen/{authority.agent_id}:latest",
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
    if admit:
        await _admit(session_maker, _request(authority))

    home = tmp_path / "home"
    inputs = tmp_path / "inputs"
    release = home / "release" / authority.registration_sha256
    tools = tmp_path / "tools"
    # No attempts directory: the materializer creates it as the worker.
    for directory in (
        home,
        inputs,
        home / "private",
        home / "authority",
        home / "release",
        release,
        home / "custody",
        home / "bin",
        tools,
    ):
        directory.mkdir(mode=0o700)
    worker = home / "bin" / "dittobench-coding-hosted-worker"
    worker.write_text("#!/bin/sh\nexit 1\n")
    worker.chmod(0o700)
    unwrap = home / "custody" / "unwrap"
    unwrap.write_text("#!/bin/sh\nexit 2\n")
    unwrap.chmod(0o500)
    docker = tools / "docker"
    docker.write_text("#!/bin/sh\nexit 3\n")
    docker.chmod(0o700)
    record = tools / "host-prerequisites.json"
    record.write_bytes(json.dumps(HOST_RECORD, sort_keys=True).encode())
    record.chmod(0o444)
    run_root = Path(tempfile.mkdtemp(prefix="attempt-docker-"))
    run_root.chmod(0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(run_root / "docker.sock"))
    (run_root / "docker.sock").chmod(0o600)
    layout = attempt.HostLayout(
        home=home,
        inputs=inputs,
        runtime_revision=REVISION,
        worker_executable=worker,
        python_executable=Path(shutil.which("python3", path="/usr/bin:/bin")),
        prerequisites_file=record,
        docker_executable=docker,
        docker_socket=run_root / "docker.sock",
        custody_socket=tmp_path / "custody-run" / "custody.sock",
        trusted_uid=os.geteuid(),
    )
    postgres = [
        f"{key}={os.environ[key]}"
        for key in (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
        )
    ]
    for name, body in {
        "postgres-environment.json": json.dumps(postgres).encode(),
        "hippius-environment.json": canonical(HIPPIUS),
        "image-storage.json": canonical(
            {
                "endpoint_url": "https://storage.invalid",
                "bucket": "screened-images",
                "access_key": "synthetic-image-reader",
                "secret_key": "synthetic-image-secret",
                "region": "test",
            }
        ),
        "provider-key": PROVIDER_KEY,
    }.items():
        write_private(layout.private / name, body)
    for name, origin in {
        "transport-manifest.json": source / "transport/manifest.json",
        "payload-authority.json": source / "payload/payload-authority.json",
        "publication-receipt.json": source / "receipt.json",
        "curator-public.pem": source / "curator.pem",
    }.items():
        write_private(release / name, origin.read_bytes())
    write_private(layout.evidence_public_key, evidence_key())
    f = SimpleNamespace(
        layout=layout,
        authority=authority,
        execution=execution,
        policy=policy,
        budget=budget,
        tmp=tmp_path,
        units=lambda: None,
        repositories=[],
    )
    for kind, body in {
        "execution-profile": execution,
        "grading-profile": GRADING,
        "inference-policy": canonical(policy.model_dump(mode="json", by_alias=True)),
        "budget-profile": budget.canonical_bytes(),
    }.items():
        stage_input(f, kind, body)

    def repository(_layout, digests):
        f.repositories.append(digests)
        return attempt.unique_repository(
            digests, {f"{REPOSITORY}@{digest}" for digest in digests}
        )

    f.repository = repository
    probe = stage_input(f, "probe-receipt", probe_receipt(tmp_path / "probe"))
    f.request = attempt.AttemptRequest.parse(
        evaluation_id=str(authority.evaluation_id),
        runtime_revision=REVISION,
        assignment_sha256=authority.digest(),
        execution_profile_sha256=authority.execution_profile_sha256,
        grading_profile_sha256=authority.grading_profile_sha256,
        inference_policy_sha256=authority.policy_sha256,
        budget_profile_sha256=budget.digest(),
        probe_receipt_sha256=probe,
        evidence_wrapping_key_sha256=RsaOaepHippiusEvidenceKeyWrapper(
            layout.evidence_public_key
        ).wrapping_key_sha256,
    )
    try:
        yield f
    finally:
        listener.close()
        shutil.rmtree(run_root, ignore_errors=True)


@pytest.fixture
async def attempt_fixture(tmp_path, session_maker):
    async with build(tmp_path, session_maker) as value:
        yield value


async def run(f, **changes):
    request = replace(f.request, **changes)
    return await attempt.materialize(
        f.layout, request, units=f.units, repository=f.repository
    )


async def refused(f, stage: str, **changes) -> None:
    with pytest.raises(HostedAttemptConfigError, match=stage):
        await run(f, **changes)
    attempts = f.layout.attempts
    assert not attempts.exists() or list(attempts.iterdir()) == []


async def runtime_refuses(f, session_maker) -> None:
    """The runtime's locked launch inspection refuses the same state."""
    with pytest.raises(ValueError):
        await inspect_launch(
            session_maker,
            evaluation_id=f.authority.evaluation_id,
            attempt_id=f.authority.attempt_id,
            assignment_sha256=f.authority.digest(),
            policy_sha256=f.authority.policy_sha256,
            execution_profile_sha256=f.authority.execution_profile_sha256,
            grading_profile_sha256=f.authority.grading_profile_sha256,
        )


@contextmanager
def swapped(path: Path, body: bytes | None):
    aside = path.with_name(path.name + ".aside")
    path.rename(aside)
    try:
        if body is not None:
            write_private(path, body)
        yield
    finally:
        path.unlink(missing_ok=True)
        aside.rename(path)


async def test_materializes_a_config_the_runtime_loader_and_launch_check_accept(
    attempt_fixture, session_maker, monkeypatch
):
    f = attempt_fixture
    secrets = {
        str(f.layout.private / name) for name in ("image-storage.json", "provider-key")
    }
    opened: list[str] = []
    helpers: list[Path] = []
    real_os_open, real_open = os.open, builtins.open
    real_helper = attempt.protected_helper

    def track_os_open(path, *args, **kwargs):
        opened.append(os.fsdecode(path))
        return real_os_open(path, *args, **kwargs)

    def track_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)):
            opened.append(os.fsdecode(file))
        return real_open(file, *args, **kwargs)

    def track_helper(path):
        helpers.append(path)
        return real_helper(path)

    monkeypatch.setattr(os, "open", track_os_open)
    monkeypatch.setattr(builtins, "open", track_open)
    monkeypatch.setattr(io, "open", track_open)
    monkeypatch.setattr(attempt, "protected_helper", track_helper)
    receipt = await run(f)
    monkeypatch.undo()
    # Credential files are referenced, never opened. The database and Hippius
    # environments are parsed in memory exactly as the runtime parses them.
    assert not secrets & set(opened)
    assert str(f.layout.private / "postgres-environment.json") in opened
    assert str(f.layout.private / "hippius-environment.json") in opened
    # The installed worker is admitted, and its binary hashed, once.
    assert helpers.count(f.layout.worker_executable) == 1
    assert not hasattr(attempt, "require_installed_worker")

    info = f.layout.attempts.lstat()
    assert (info.st_mode & 0o777, info.st_uid) == (0o700, os.geteuid())
    root = f.layout.attempts / str(f.authority.attempt_id)
    config_path = root / "runtime.json"
    assert receipt["config_file"] == str(config_path)
    info = config_path.lstat()
    assert (info.st_mode & 0o777, info.st_nlink, info.st_uid) == (
        0o600,
        1,
        os.geteuid(),
    )
    assert not (root / "runtime.json.partial").exists()
    body = config_path.read_bytes()
    assert sha(body) == receipt["config_sha256"]
    for secret in (
        PROVIDER_KEY,
        os.environ["POSTGRES_PASSWORD"].encode(),
        b"synthetic-reader-secret",
        b"evidence-mediator-secret",
        b"synthetic-image-secret",
    ):
        assert secret not in body
        assert secret not in json.dumps(receipt).encode()
    assert receipt | {"worker_id": None, "config_file": None, "runtime_root": None} == {
        "schema": "dittobench-coding-hosted-attempt-config-receipt-v2",
        "evaluation_id": str(f.authority.evaluation_id),
        "attempt_id": str(f.authority.attempt_id),
        "worker_id": None,
        "assignment_sha256": f.authority.digest(),
        "deadline_unix": f.authority.deadline_unix,
        "runtime_revision": REVISION,
        "registration_sha256": f.authority.registration_sha256,
        "execution_profile_sha256": f.authority.execution_profile_sha256,
        "grading_profile_sha256": f.authority.grading_profile_sha256,
        "policy_sha256": f.authority.policy_sha256,
        "budget_profile_sha256": f.budget.digest(),
        "probe_receipt_sha256": f.request.probe_receipt_sha256,
        "evidence_wrapping_key_sha256": f.request.evidence_wrapping_key_sha256,
        "config_file": None,
        "config_sha256": sha(body),
        "runtime_root": None,
        "admitted": True,
        "services_started": False,
        "shadow_only": True,
        "weight_eligible": False,
    }
    assert f.repositories == [{IMAGE}]

    config = load_runtime_config(config_path, expected_sha256=receipt["config_sha256"])
    wire = config.wire
    assert (wire.evaluation_id, wire.attempt_id, wire.assignment_sha256) == (
        f.authority.evaluation_id,
        f.authority.attempt_id,
        f.authority.digest(),
    )
    assert wire.worker_id == UUID(receipt["worker_id"])
    assert wire.host.model_dump() == {
        "router_listen": "10.33.0.2:18080",
        "egress_network": "ditto-coding-restricted",
        "egress_proxy": "http://10.33.0.2:18090",
        "candidate_uid": 10001,
        "candidate_gid": 10001,
        "docker_executable": str(f.layout.docker_executable),
        "docker_socket": str(f.layout.docker_socket),
        "executor_repository": REPOSITORY,
        "seccomp_profile": "",
        "apparmor_profile": "",
    }
    # The storage authorities the runtime derives are the ones the probe binds.
    probe, _ = load_hippius_probe_receipt(Path(wire.probe_receipt_file))
    assert probe.private_input_authority_sha256 == config.reader.authority_sha256
    assert probe.sealed_evidence_authority_sha256 == config.evidence.authority_sha256
    assert wire.provider_key_file == str(f.layout.private / "provider-key")
    assert wire.unwrap_executable == str(f.layout.unwrap_executable)
    assert wire.transport_manifest_file.startswith(
        str(f.layout.home / "release" / f.authority.registration_sha256)
    )
    assert Path(wire.execution_profile_file).read_bytes() == f.execution
    assert Path(wire.execution_profile_file).parent == root / "authority"
    assert config.policy.digest() == f.authority.policy_sha256
    assert Path(wire.runtime_root) == root / "runtime"
    assert list(Path(wire.runtime_root).iterdir()) == []
    assert list(Path(wire.unwrap_work_root).iterdir()) == []

    # The runtime's own locked pre-start projection accepts the result unchanged.
    projection = await runtime._projection(config, session_maker)
    assert projection.authority.digest() == f.authority.digest()
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, f.authority.evaluation_id)
        assert row.started_at is None and row.worker_id is None

    with pytest.raises(HostedAttemptConfigError, match="already materialized"):
        await run(f)
    assert config_path.read_bytes() == body


@pytest.mark.parametrize(
    "marker", ["runtime/platform-consumed", "runtime/worker/consumed"]
)
async def test_refuses_existing_or_consumed_attempt_state(attempt_fixture, marker):
    f = attempt_fixture
    f.layout.attempts.mkdir(mode=0o700)
    root = f.layout.attempts / str(f.authority.attempt_id)
    (root / marker).parent.mkdir(parents=True)
    (root / marker).write_bytes(b"consumed\n")
    with pytest.raises(HostedAttemptConfigError, match="already consumed"):
        await run(f)
    shutil.rmtree(root)
    root.mkdir(mode=0o700)
    with pytest.raises(HostedAttemptConfigError, match="already materialized"):
        await run(f)
    assert list(root.iterdir()) == []


async def test_refuses_mismatched_or_unlaunchable_assignment(
    tmp_path, session_maker, monkeypatch
):
    async with build(tmp_path, session_maker, admit=False) as f:
        await refused(f, "is not launchable")
        await runtime_refuses(f, session_maker)
        await _admit(session_maker, _request(f.authority))
        await refused(f, "authority differs", assignment_sha256="0" * 64)
        await refused(f, "assignment unavailable", evaluation_id=uuid4())
        with monkeypatch.context() as patch:
            patch.setattr(attempt, "MINIMUM_REMAINING_SECONDS", 700)
            await refused(f, "is not launchable")
        async with session_maker() as session, session.begin():
            await start_hosted_attempt(
                session,
                evaluation_id=f.authority.evaluation_id,
                expected_attempt_id=f.authority.attempt_id,
                worker_id=uuid4(),
            )
        await refused(f, "is not launchable")
        await runtime_refuses(f, session_maker)


async def test_refuses_quarantined_release_and_unscreened_artifact(
    attempt_fixture, session_maker
):
    f = attempt_fixture
    async with session_maker() as session, session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == f.authority.agent_id)
            .values(screening_policy_version=SCREENING_POLICY_VERSION - 1)
        )
    await refused(f, "artifact unavailable")
    await runtime_refuses(f, session_maker)
    async with session_maker() as session, session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == f.authority.agent_id)
            .values(screening_policy_version=SCREENING_POLICY_VERSION)
        )
        release = await session.scalar(
            select(CodingPrivateV2Release).where(
                CodingPrivateV2Release.release_row_id == f.authority.release_row_id
            )
        )
        await append_private_v2_release_event(
            session,
            corpus_release_id=release.corpus_release_id,
            expected_registration_sha256=f.authority.registration_sha256,
            action="quarantined",
            actor="test",
            reason="synthetic quarantine",
        )
    await refused(f, "release unavailable")
    await runtime_refuses(f, session_maker)


async def test_refuses_closed_task(attempt_fixture, session_maker):
    f = attempt_fixture
    async with session_maker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE coding_hosted_private_tasks SET closed_at = clock_timestamp(), "
                "close_reason = 'aborted' WHERE evaluation_id = :evaluation"
            ),
            {"evaluation": f.authority.evaluation_id},
        )
    await refused(f, "task unavailable")
    await runtime_refuses(f, session_maker)


async def test_rechecks_authority_immediately_before_the_write(
    attempt_fixture, session_maker, monkeypatch
):
    f = attempt_fixture
    snapshots = []
    real = attempt.read_launchable

    async def racing(sessions, request):
        snapshots.append(request)
        if len(snapshots) == 2:
            async with session_maker() as session, session.begin():
                await start_hosted_attempt(
                    session,
                    evaluation_id=f.authority.evaluation_id,
                    expected_attempt_id=f.authority.attempt_id,
                    worker_id=uuid4(),
                )
        return await real(sessions, request)

    monkeypatch.setattr(attempt, "read_launchable", racing)
    await refused(f, "is not launchable")
    assert len(snapshots) == 2


async def test_refuses_mismatched_public_authorities(attempt_fixture):
    f = attempt_fixture
    execution = f.layout.input(
        "execution-profile", f.authority.execution_profile_sha256
    )
    with swapped(execution, execution_profile(max_patch_bytes=2048)):
        await refused(f, "execution profile refused")
    with swapped(execution, f.execution.rstrip(b"\n") + b" \n"):
        await refused(f, "execution profile refused")
    grading = f.layout.input("grading-profile", f.authority.grading_profile_sha256)
    with swapped(grading, None):
        await refused(f, "grading profile refused")
    policy = f.layout.input("inference-policy", f.authority.policy_sha256)
    other = native_policy(runtime_profile_sha256=f.budget.digest(), max_requests=3)
    with swapped(policy, canonical(other.model_dump(mode="json", by_alias=True))):
        await refused(f, "inference policy refused")
    with swapped(f.layout.evidence_public_key, None):
        write_private(
            f.layout.evidence_public_key,
            rsa.generate_private_key(public_exponent=65537, key_size=3072)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
        )
        await refused(f, "evidence key refused")
    await refused(f, "evidence key refused", evidence_wrapping_key_sha256="e" * 64)
    release = f.layout.release(f.authority.registration_sha256)
    receipt = release / "publication-receipt.json"
    with swapped(
        receipt, receipt.read_bytes().replace(b'"ready":true', b'"ready":false')
    ):
        await refused(f, "release authorities refused")


async def test_refuses_reviewed_pins_that_differ_from_the_assignment(attempt_fixture):
    f = attempt_fixture
    # A pin that names a staged, well-formed input is still not the assignment's.
    other_execution = stage_input(
        f, "execution-profile", execution_profile(max_patch_bytes=2048)
    )
    for changes in (
        {"execution_profile_sha256": other_execution},
        {"grading_profile_sha256": "0" * 64},
        {"inference_policy_sha256": "0" * 64},
    ):
        await refused(f, "assignment authority differs", **changes)
    other_budget = budget_profile(
        valid_from_unix=int(time.time()) - 1,
        valid_until_unix=int(time.time()) + 7200,
    )
    await refused(
        f,
        "budget profile refused",
        budget_profile_sha256=stage_input(
            f, "budget-profile", other_budget.canonical_bytes()
        ),
    )
    await run(f)


async def test_refuses_a_runtime_revision_other_than_the_running_one(attempt_fixture):
    f = attempt_fixture
    units = []
    f.units = lambda: units.append(True)
    await refused(f, "runtime revision differs", runtime_revision="6" * 40)
    # Refused before any host, unit or database check.
    assert units == []


async def test_refuses_wrong_stale_or_mismatched_probe_receipt(attempt_fixture):
    f = attempt_fixture
    await refused(f, "probe receipt refused", probe_receipt_sha256="f" * 64)
    pinned = f.layout.input("probe-receipt", f.request.probe_receipt_sha256)
    with swapped(pinned, probe_receipt(f.tmp / "other")):
        await refused(f, "probe receipt refused")
    pinned.chmod(0o660)
    await refused(f, "probe receipt refused")
    pinned.chmod(0o440)
    for index, age in enumerate((23 * 3600, 25 * 3600, -120)):
        digest = stage_input(
            f, "probe-receipt", probe_receipt(f.tmp / f"stale-{index}", age=age)
        )
        await refused(f, "probe receipt is stale", probe_receipt_sha256=digest)
    digest = stage_input(
        f, "probe-receipt", probe_receipt(f.tmp / "reader", reader="7" * 64)
    )
    await refused(f, "probe receipt authority differs", probe_receipt_sha256=digest)


def test_probe_freshness_covers_the_runtime_evidence_publication_bound():
    assert attempt.EVIDENCE_PUBLICATION_SECONDS == (
        runtime.WORKER_FINALIZATION_SECONDS
        + runtime.WORKER_SHUTDOWN_GRACE_SECONDS
        + 2 * control.SHUTDOWN_OPERATION_SECONDS
    )
    deadline = 2_000_000_000
    now = deadline - 600
    edge = deadline + attempt.EVIDENCE_PUBLICATION_SECONDS
    edge -= PROBE_RECEIPT_MAX_AGE_SECONDS
    assert not attempt.probe_fresh(edge, now, deadline)
    assert attempt.probe_fresh(edge + 1, now, deadline)
    # A receipt that only outlives the Go timeout, without grace and drain.
    assert not attempt.probe_fresh(
        deadline
        + runtime.WORKER_FINALIZATION_SECONDS
        + 1
        - PROBE_RECEIPT_MAX_AGE_SECONDS,
        now,
        deadline,
    )
    assert not attempt.probe_fresh(now + 1, now, deadline)
    assert not attempt.probe_fresh(now - PROBE_RECEIPT_MAX_AGE_SECONDS, now, deadline)


async def test_probe_receipt_must_stay_fresh_through_the_last_publication(
    attempt_fixture,
):
    f = attempt_fixture
    edge = (
        f.authority.deadline_unix
        + attempt.EVIDENCE_PUBLICATION_SECONDS
        - PROBE_RECEIPT_MAX_AGE_SECONDS
    )
    digest = stage_input(
        f, "probe-receipt", probe_receipt(f.tmp / "edge", checked=edge)
    )
    await refused(f, "probe receipt is stale", probe_receipt_sha256=digest)
    digest = stage_input(
        f, "probe-receipt", probe_receipt(f.tmp / "inside", checked=edge + 1)
    )
    receipt = await run(f, probe_receipt_sha256=digest)
    assert receipt["probe_receipt_sha256"] == digest


async def test_refuses_a_probe_receipt_over_the_runtime_read_bound(attempt_fixture):
    f = attempt_fixture
    # Valid for the probe loader's own bound, but not for load_runtime_config's.
    body = probe_receipt(f.tmp / "large", padding=70000)
    assert 65536 < len(body) < 1 << 20
    load_hippius_probe_receipt(f.tmp / "large" / "fresh.json")
    digest = stage_input(f, "probe-receipt", body)
    await refused(f, "probe receipt refused", probe_receipt_sha256=digest)


async def test_refuses_storage_authorities_the_hippius_environment_does_not_derive(
    attempt_fixture,
):
    f = attempt_fixture
    digest = stage_input(
        f,
        "probe-receipt",
        probe_receipt(f.tmp / "sealed", evidence={"bucket": "other-sealed-evidence"}),
    )
    await refused(f, "sealed evidence authority differs", probe_receipt_sha256=digest)
    hippius = f.layout.private / "hippius-environment.json"
    reader = HIPPIUS | {"DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": "hip_x"}
    with swapped(hippius, canonical(reader)):
        await refused(f, "probe receipt authority differs")
    mediator = HIPPIUS | {"DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": "hip_y"}
    with swapped(hippius, canonical(mediator)):
        await refused(f, "sealed evidence authority differs")
    shared = HIPPIUS | {
        "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": "coding-private-inputs"
    }
    with swapped(hippius, canonical(shared)):
        await refused(f, "storage environment refused")
    with swapped(hippius, canonical(HIPPIUS | {"UNEXPECTED": "value"})):
        await refused(f, "storage environment refused")
    await run(f)


@pytest.mark.parametrize(
    "changes,stage",
    [
        ({"budget_seconds": 400}, "budget profile refused"),
        ({"max_patch_bytes": 2048}, "execution profile refused"),
    ],
    ids=["budget-expires-before-deadline", "profile-patch-bound-differs"],
)
async def test_refuses_assignment_bound_authorities_unfit_for_the_attempt(
    tmp_path, session_maker, changes, stage
):
    async with build(tmp_path, session_maker, **changes) as f:
        await refused(f, stage)


async def test_refuses_unsafe_secret_file_metadata(attempt_fixture):
    f = attempt_fixture
    provider = f.layout.private / "provider-key"
    provider.chmod(0o640)
    await refused(f, "secret file metadata refused")
    provider.chmod(0o600)
    linked = f.layout.private / "provider-key-link"
    os.link(provider, linked)
    await refused(f, "secret file metadata refused")
    linked.unlink()
    with swapped(provider, None):
        await refused(f, "secret file metadata refused")
        os.symlink(f.layout.private / "provider-key.aside", provider)
        await refused(f, "secret file metadata refused")
    hippius = f.layout.private / "hippius-environment.json"
    f.layout.private.chmod(0o750)
    await refused(f, "secret file metadata refused")
    f.layout.private.chmod(0o700)
    with swapped(hippius, None):
        await refused(f, "secret file metadata refused")
    await run(f)


async def test_refuses_live_units_custody_socket_and_free_form_host_values(
    attempt_fixture, monkeypatch
):
    f = attempt_fixture

    def live():
        raise HostedAttemptConfigError("hosted worker or custody unit is live")

    f.units = live
    await refused(f, "unit is live")
    f.units = lambda: None
    f.layout.custody_socket.parent.mkdir(mode=0o700)
    f.layout.custody_socket.write_bytes(b"")
    await refused(f, "unit is live")
    f.layout.custody_socket.unlink()
    record = f.layout.prerequisites_file
    for change in (
        {"router_listen": "8.8.8.8:18080"},
        {"egress_proxy": "http://10.33.0.3:18090"},
        {"egress_network": "ditto-job-1"},
        {"candidate_uid": 0},
        {"extra": True},
        {"weight_eligible": True},
    ):
        record.chmod(0o600)
        record.write_bytes(json.dumps({**HOST_RECORD, **change}).encode())
        record.chmod(0o444)
        await refused(f, "host refused")
    record.chmod(0o600)
    record.write_bytes(json.dumps(HOST_RECORD).encode())
    record.chmod(0o666)
    await refused(f, "host refused")
    record.chmod(0o444)
    f.layout.attempts.mkdir(mode=0o700)
    for path, mode in (
        (f.layout.unwrap_executable, 0o755),
        (f.layout.docker_socket, 0o644),
        (f.layout.attempts, 0o755),
        (f.layout.docker_executable, 0o777),
        (f.layout.docker_executable, 0o644),
        (f.layout.inputs, 0o770),
    ):
        original = path.stat().st_mode & 0o777
        path.chmod(mode)
        with pytest.raises(HostedAttemptConfigError):
            await run(f)
        path.chmod(original)
    f.layout.attempts.rmdir()
    f.layout.attempts.symlink_to(f.tmp)
    with pytest.raises(HostedAttemptConfigError, match="host refused"):
        await run(f)
    f.layout.attempts.unlink()
    # Execute access is the worker's own, not a root owner's mode bits.
    real_access = os.access
    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "access",
            lambda path, mode: (
                Path(path) != f.layout.docker_executable and real_access(path, mode)
            ),
        )
        await refused(f, "host refused")
    await run(f)


def test_unit_listing_accepts_only_stopped_or_failed_units():
    assert attempt.idle_units(b"")
    assert attempt.idle_units(b"\n  \n")
    assert attempt.idle_units(
        b"ditto-coding-hosted-worker.service loaded inactive dead Attempt\n"
        b"ditto-coding-custody@1.service not-found failed failed Custody\n"
    )
    for state in (
        b"active running",
        b"activating start",
        b"deactivating stop",
        b"reloading reload",
    ):
        assert not attempt.idle_units(
            b"ditto-coding-custody@1.service loaded " + state + b" Custody\n"
        )
    # Short or unparseable lines are refused, never read as stopped.
    for line in (
        b"ditto-coding-hosted-worker.service\n",
        b"ditto-coding-hosted-worker.service loaded\n",
        b"ditto-coding-hosted-worker.service loaded inactive\n",
    ):
        assert not attempt.idle_units(line)
    with pytest.raises(UnicodeDecodeError):
        attempt.idle_units(b"\xff loaded inactive dead\n")


def test_executor_repository_must_be_the_unique_local_runtime_holding_every_image():
    other = "sha256:" + "e" * 64
    held = {
        "coding-runtime.invalid/go/runtime@" + IMAGE,
        "coding-runtime.invalid/go/runtime@" + other,
        "coding-runtime.invalid/rust/runtime@" + IMAGE,
    }
    assert (
        attempt.unique_repository({IMAGE, other}, held)
        == "coding-runtime.invalid/go/runtime"
    )
    with pytest.raises(HostedAttemptConfigError, match="image unavailable"):
        attempt.unique_repository({IMAGE}, held)
    with pytest.raises(HostedAttemptConfigError, match="image unavailable"):
        attempt.unique_repository({"sha256:" + "0" * 64}, held)


@pytest.mark.parametrize(
    "listed,expected",
    [
        ([f'["{REPOSITORY}@{IMAGE}","{REPOSITORY}@OTHER"]', "null"], REPOSITORY),
        ([f'["{REPOSITORY}@{IMAGE}"]'], None),
        ([f'["{REPOSITORY}@{IMAGE}"]', "{}"], None),
        ([], None),
    ],
    ids=["one-repository-holds-both", "partial", "malformed", "none"],
)
def test_docker_repository_inspects_every_candidate_in_one_call(
    tmp_path, listed, expected
):
    other = "sha256:" + "e" * 64
    log = tmp_path / "argv"
    docker = tmp_path / "docker"
    lines = "".join(
        f"printf '%s\\n' '{line.replace('OTHER', other)}'\n" for line in listed
    )
    # A missing reference fails the command; present images are still listed.
    docker.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{log}'\n{lines}exit 1\n")
    docker.chmod(0o700)
    layout = SimpleNamespace(
        docker_executable=docker, docker_socket=tmp_path / "sock", home=tmp_path
    )
    if expected is None:
        with pytest.raises(HostedAttemptConfigError, match="image unavailable"):
            attempt.docker_repository(layout, {IMAGE, other})
    else:
        assert attempt.docker_repository(layout, {IMAGE, other}) == expected
    argv = log.read_text().splitlines()
    assert argv[:4] == ["image", "inspect", "--format", "{{json .RepoDigests}}"]
    assert argv[4:] == [
        f"{repository}@{digest}"
        for repository in attempt.EXECUTOR_REPOSITORIES
        for digest in sorted({IMAGE, other})
    ]


async def test_authority_snapshot_is_read_only(session_maker):
    for statement in (
        "UPDATE coding_hosted_assignments SET reason = reason",
        "SELECT evaluation_id FROM coding_hosted_assignments FOR UPDATE",
    ):
        with pytest.raises(Exception, match="read-only transaction"):
            async with attempt.read_only_snapshot(session_maker) as session:
                await session.execute(text(statement))


PINS = {
    "assignment_sha256": "a" * 64,
    "execution_profile_sha256": "b" * 64,
    "grading_profile_sha256": "c" * 64,
    "inference_policy_sha256": "d" * 64,
    "budget_profile_sha256": "e" * 64,
    "probe_receipt_sha256": "f" * 64,
    "evidence_wrapping_key_sha256": "0" * 64,
}


def test_request_pins_are_closed():
    good = {"evaluation_id": str(uuid4()), "runtime_revision": REVISION, **PINS}
    attempt.AttemptRequest.parse(**good)
    for change in (
        {"evaluation_id": "00000000-0000-0000-0000-000000000000"},
        {"evaluation_id": good["evaluation_id"].upper()},
        {"runtime_revision": "5" * 39},
        {"runtime_revision": "5" * 64},
        {"assignment_sha256": "A" * 64},
        {"budget_profile_sha256": "e" * 63},
        {"probe_receipt_sha256": "b" * 63},
        {"evidence_wrapping_key_sha256": "../" + "c" * 61},
        {"extra_sha256": "1" * 64},
    ):
        with pytest.raises(HostedAttemptConfigError, match="request invalid"):
            attempt.AttemptRequest.parse(**{**good, **change})
    missing = dict(good)
    del missing["grading_profile_sha256"]
    with pytest.raises(HostedAttemptConfigError, match="request invalid"):
        attempt.AttemptRequest.parse(**missing)


def test_cli_takes_only_closed_pins_and_refuses_off_host(tmp_path):
    command = [sys.executable, "-I", "-m", "ditto.coding_hosted_attempt_config"]
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(tmp_path)}
    pins = [
        "--materialize-attempt-config",
        "--evaluation-id",
        str(uuid4()),
        "--runtime-revision",
        REVISION,
    ]
    for name, value in PINS.items():
        pins += [f"--{name.replace('_', '-')}", value]
    for argv in (
        ["--materialize-attempt-config", "--config", "/tmp/x.json"],
        pins[:-2],
    ):
        usage = subprocess.run(
            [*command, *argv],
            cwd=Path(attempt.__file__).parents[2],
            env=environment,
            capture_output=True,
            timeout=60,
        )
        assert usage.returncode == 2 and usage.stdout == b""
    result = subprocess.run(
        [*command, *pins],
        cwd=Path(attempt.__file__).parents[2],
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 1 and result.stdout == b""
    assert (
        result.stderr == b"hosted attempt config refused: hosted attempt host refused\n"
    )
