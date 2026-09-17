"""Durable, identity-scoped provenance for the pinned Pylon weight adapter.

Legacy weight submission is unchanged. Evidence is written before transmission;
uncertain attempts never retry silently and never become disclosure permission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pylon_service.db.database import Base, session_factory
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

SCHEMA_VERSION = 1
MAX_UNACKNOWLEDGED = 4096
MAX_CIPHERTEXT_BYTES = 1024 * 1024
_dispatch_lock = asyncio.Lock()
_receipt_task: ContextVar[int | None] = ContextVar("ditto_receipt_task", default=None)

receipts = Table(
    "ditto_weight_receipts",
    Base.metadata,
    Column("request_id", String(36), primary_key=True),
    Column(
        "task_id", Integer, ForeignKey("weight_tasks.id"), nullable=False, unique=True
    ),
    Column("identity_name", String, nullable=False, index=True),
    Column("netuid", Integer, nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("body", JSON(none_as_null=True), nullable=True),
    Column("attempt", JSON(none_as_null=True), nullable=True),
    Column("acknowledged", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("acknowledged_at", DateTime(timezone=True), nullable=True),
)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_request(data: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize known fields only; reject malformed proof inputs."""
    if (
        type(data.get("schema_version")) is not int
        or data.get("schema_version") != 1
        or type(data.get("mechanism_id")) is not int
        or data["mechanism_id"] != 0
    ):
        raise ValueError("unsupported receipt schema or mechanism")
    raw_weights = data.get("weights")
    if not isinstance(raw_weights, dict) or not 0 < len(raw_weights) <= 4096:
        raise ValueError("weights must contain 1..4096 hotkeys")
    weights = {}
    for hotkey, value in raw_weights.items():
        if (
            not isinstance(hotkey, str)
            or not 0 < len(hotkey) <= 128
            or type(value) not in (int, float)
        ):
            raise ValueError("invalid weight entry")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("invalid weight value")
        weights[hotkey] = value
    if not any(weights.values()):
        raise ValueError("weights must include positive allocation")
    raw = data.get("provenance")
    if not isinstance(raw, dict):
        raise ValueError("missing provenance")
    provenance: dict[str, Any] = {}
    for name in ("ledger_snapshot_id", "champion_agent_id"):
        provenance[name] = str(UUID(raw[name]))
    for name in ("ledger_digest", "champion_artifact_sha256", "vector_digest"):
        value = raw[name]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("invalid provenance digest")
        provenance[name] = value
    for name in ("epoch_index", "bench_version"):
        value = raw[name]
        if type(value) is not int or value < (1 if name == "bench_version" else 0):
            raise ValueError("invalid provenance integer")
        provenance[name] = value
    if provenance["vector_digest"] != canonical_digest(weights):
        raise ValueError("weight vector digest does not match")
    body = {
        "schema_version": 1,
        "mechanism_id": 0,
        "weights": weights,
        "provenance": provenance,
    }
    if len(json.dumps(body)) > MAX_CIPHERTEXT_BYTES:
        raise ValueError("receipt request is too large")
    return body


async def create_request(
    service: Any, netuid: int, request_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    from litestar.exceptions import HTTPException
    from pylon_service.api._unstable.tasks import ApplyWeights
    from pylon_service.db.models import TaskStatus, WeightTask

    try:
        request_id = str(UUID(request_id))
        body = validate_request(data)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise HTTPException(
            status_code=400, detail="invalid weight receipt request"
        ) from exc
    identity = str(service.identity.identity_name)
    digest = canonical_digest(body)
    try:
        async with session_factory() as session, session.begin():
            existing = (
                (
                    await session.execute(
                        select(receipts).where(receipts.c.request_id == request_id)
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if (
                    existing["identity_name"] != identity
                    or existing["netuid"] != netuid
                    or existing["request_digest"] != digest
                ):
                    raise HTTPException(
                        status_code=409, detail="receipt request identity conflict"
                    )
                task_id = existing["task_id"]
            else:
                # Never discard unacknowledged evidence to make room.
                outstanding = await session.scalar(
                    select(func.count())
                    .select_from(receipts)
                    .where(
                        receipts.c.identity_name == identity,
                        receipts.c.acknowledged.is_(False),
                    )
                )
                if outstanding is not None and outstanding >= MAX_UNACKNOWLEDGED:
                    raise HTTPException(
                        status_code=503,
                        detail="unacknowledged receipt capacity reached",
                    )
                await session.execute(
                    update(receipts)
                    .where(
                        receipts.c.acknowledged.is_(True),
                        receipts.c.acknowledged_at
                        < datetime.now(UTC) - timedelta(days=90),
                    )
                    .values(body=None, attempt=None)
                )
                await session.execute(
                    update(WeightTask)
                    .where(
                        WeightTask.identity_name == identity,
                        WeightTask.netuid == netuid,
                        WeightTask.mechanism_id == 0,
                        WeightTask.status == TaskStatus.RUNNING,
                    )
                    .values(status=TaskStatus.CANCELLED)
                )
                task = WeightTask(
                    identity_name=identity,
                    weights=body["weights"],
                    netuid=netuid,
                    mechanism_id=0,
                    hotkey=service.contact_router.hotkey,
                    start_block_number=None,
                )
                session.add(task)
                await session.flush()
                task_id = task.id
                await session.execute(
                    insert(receipts).values(
                        request_id=request_id,
                        task_id=task_id,
                        identity_name=identity,
                        netuid=netuid,
                        request_digest=digest,
                        body=body,
                        acknowledged=False,
                        created_at=datetime.now(UTC),
                    )
                )
    except IntegrityError:
        # A concurrent identical retry can win the unique request key. Its
        # transaction owns scheduling; do not cancel it or create another task.
        existing = await get_request(service, netuid, request_id)
        if existing["request_digest"] != digest:
            raise HTTPException(
                status_code=409, detail="receipt request identity conflict"
            ) from None
        task_id = existing["task_id"]
    # Reconcile a crash/cancellation after durable creation but before dispatch.
    # The per-process lock prevents duplicate scheduling; prepared-commit CAS
    # remains the cross-process boundary against duplicate transmission.
    async with _dispatch_lock, session_factory() as session:
        task = await session.get(WeightTask, task_id)
        current = (
            (
                await session.execute(
                    select(receipts).where(receipts.c.task_id == task_id)
                )
            )
            .mappings()
            .one()
        )
        if task is None:
            raise RuntimeError("durable receipt task disappeared")
        active = any(
            getattr(running, "_task_id", None) == task_id
            for running in ApplyWeights.tasks_running
        )
        if (
            task.status == TaskStatus.RUNNING
            and current["attempt"] is None
            and not active
        ):
            await ApplyWeights.from_persisted_task(
                service.identity, service.contact_router, task
            ).schedule()
    return await get_request(service, netuid, request_id)


async def _envelope(row: Any) -> dict[str, Any]:
    from pylon_service.db.models import WeightTask

    async with session_factory() as session:
        task = await session.get(WeightTask, row["task_id"])
        if task is None:
            raise RuntimeError("receipt task missing")
        attempt = row["attempt"]
        if row["body"] is None:
            return {
                "schema_version": 1,
                "request_id": row["request_id"],
                "request_digest": row["request_digest"],
                "task_id": task.id,
                "netuid": row["netuid"],
                "acknowledged": True,
                "status": "retained",
                "attempts": [],
            }
        return {
            **row["body"],
            "request_id": row["request_id"],
            "request_digest": row["request_digest"],
            "task_id": task.id,
            "netuid": row["netuid"],
            "validator_hotkey": task.hotkey,
            "status": "finalized"
            if attempt and attempt["status"] == "finalized"
            else "uncertain"
            if attempt
            else task.status.value,
            "acknowledged": row["acknowledged"],
            "attempts": [attempt] if attempt else [],
        }


async def get_request(service: Any, netuid: int, request_id: str) -> dict[str, Any]:
    from litestar.exceptions import HTTPException

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    select(receipts).where(
                        receipts.c.request_id == request_id,
                        receipts.c.identity_name == str(service.identity.identity_name),
                        receipts.c.netuid == netuid,
                    )
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status_code=404, detail="weight receipt not found")
    return await _envelope(row)


async def list_requests(
    service: Any, netuid: int, after_task_id: int, limit: int
) -> dict[str, Any]:
    from litestar.exceptions import HTTPException

    if after_task_id < 0 or not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="invalid receipt page")
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(receipts)
                    .where(
                        receipts.c.identity_name == str(service.identity.identity_name),
                        receipts.c.netuid == netuid,
                        receipts.c.acknowledged.is_(False),
                        receipts.c.task_id > after_task_id,
                    )
                    .order_by(receipts.c.task_id)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
    return {
        "receipts": [await _envelope(row) for row in rows[:limit]],
        "next_after_task_id": rows[limit - 1]["task_id"] if len(rows) > limit else None,
    }


def immutable_receipt(envelope: dict[str, Any]) -> dict[str, Any]:
    """Match the shared finalized claim; mutable queue fields are not signed."""
    claim = {
        name: envelope[name]
        for name in (
            "schema_version",
            "request_id",
            "request_digest",
            "task_id",
            "netuid",
            "mechanism_id",
            "validator_hotkey",
            "weights",
            "provenance",
        )
    }
    claim["attempt"] = {
        key: value for key, value in envelope["attempts"][0].items() if key != "status"
    }
    return claim


async def acknowledge_request(
    service: Any, netuid: int, request_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    from litestar.exceptions import HTTPException

    envelope = await get_request(service, netuid, request_id)
    attempt = envelope["attempts"][0] if envelope["attempts"] else None
    if (
        not attempt
        or attempt["status"] != "finalized"
        or data.get("request_digest") != envelope["request_digest"]
        or data.get("attempt_id") != attempt["attempt_id"]
        or data.get("receipt_digest") != canonical_digest(immutable_receipt(envelope))
    ):
        raise HTTPException(
            status_code=409,
            detail="receipt acknowledgement does not match finalized evidence",
        )
    async with session_factory() as session, session.begin():
        await session.execute(
            update(receipts)
            .where(receipts.c.request_id == request_id)
            .values(acknowledged=True, acknowledged_at=datetime.now(UTC))
        )
    return {"acknowledged": True, "request_id": request_id}


@asynccontextmanager
async def record_task(task_id: int | None):
    from pylon_service.api._unstable.tasks import StopRetrying

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    select(receipts).where(receipts.c.task_id == task_id)
                )
            )
            .mappings()
            .first()
        )
    if row is not None and row["attempt"] is not None:
        if row["attempt"]["status"] == "finalized":
            yield False
            return
        raise StopRetrying(
            "weight receipt transmission uncertain; new epoch may submit a new request"
        )
    token = _receipt_task.set(task_id if row is not None else None)
    try:
        yield True
    finally:
        _receipt_task.reset(token)


async def prepare_commit(
    netuid: int,
    mechanism_id: int,
    weights: dict[int, int],
    ciphertext: bytes,
    reveal_round: int,
    version_key: int,
) -> None:
    task_id = _receipt_task.get()
    if task_id is None:
        return
    from pylon_service.api._unstable.tasks import StopRetrying

    if mechanism_id != 0 or not 0 < len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
        raise StopRetrying("unsupported receipt commit")
    attempt = {
        "attempt_id": str(uuid4()),
        "status": "prepared",
        "normalized_weights": [
            [int(uid), int(value)] for uid, value in sorted(weights.items())
        ],
        "ciphertext_hex": ciphertext.hex(),
        "ciphertext_hash": hashlib.blake2b(ciphertext, digest_size=32).hexdigest(),
        "reveal_round": int(reveal_round),
        "version_key": int(version_key),
    }
    async with session_factory() as session, session.begin():
        row = (
            (
                await session.execute(
                    select(receipts).where(receipts.c.task_id == task_id)
                )
            )
            .mappings()
            .one()
        )
        if row["netuid"] != netuid or row["attempt"] is not None:
            raise StopRetrying("receipt already prepared or subnet mismatch")
        result = await session.execute(
            update(receipts)
            .where(receipts.c.task_id == task_id, receipts.c.attempt.is_(None))
            .values(attempt=attempt)
        )
        if result.rowcount != 1:
            raise StopRetrying("receipt preparation race")


async def finalize_commit(
    block_hash: str,
    block: dict[str, Any],
    extrinsic_hash: str,
    extrinsic_index: int,
    events: list[dict[str, Any]],
) -> None:
    task_id = _receipt_task.get()
    if task_id is None:
        return
    from pylon_service.db.models import WeightTask

    async with session_factory() as session, session.begin():
        row = (
            (
                await session.execute(
                    select(receipts).where(receipts.c.task_id == task_id)
                )
            )
            .mappings()
            .one()
        )
        task = await session.get(WeightTask, task_id)
        attempt = row["attempt"]
        if task is None or not attempt or attempt["status"] != "prepared":
            raise ValueError("commit receipt was not durably prepared")
        matching = [
            e
            for e in events
            if e.get("module_id") == "SubtensorModule"
            and e.get("event_id") == "TimelockedWeightsCommitted"
        ]
        success = [
            e
            for e in events
            if e.get("module_id") == "System"
            and e.get("event_id") == "ExtrinsicSuccess"
        ]
        if len(matching) != 1 or len(success) != 1:
            raise ValueError("commit finalized without unique successful commit event")
        attrs = matching[0].get("event", {}).get("attributes")
        if not isinstance(attrs, (list, tuple)) or len(attrs) != 4:
            raise ValueError("unsupported commit event schema")
        hotkey, netuid, commit_hash, reveal_round = attrs
        if (
            hotkey != task.hotkey
            or netuid != row["netuid"]
            or str(commit_hash).removeprefix("0x") != attempt["ciphertext_hash"]
            or reveal_round != attempt["reveal_round"]
        ):
            raise ValueError(
                "finalized commit event does not match prepared provenance"
            )
        raw_number = block["block"]["header"]["number"]
        number = int(raw_number, 16) if isinstance(raw_number, str) else int(raw_number)
        finalized = {
            **attempt,
            "status": "finalized",
            "commit_block": number,
            "commit_block_hash": block_hash,
            "extrinsic_hash": extrinsic_hash,
            "extrinsic_index": extrinsic_index,
        }
        await session.execute(
            update(receipts)
            .where(receipts.c.task_id == task_id)
            .values(attempt=finalized)
        )
