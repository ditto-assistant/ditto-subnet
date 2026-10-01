"""Synthetic archived submissions, not independently labeled paid calibration."""

import hashlib
import io
import tarfile
from datetime import datetime
from uuid import uuid4

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, EvaluationPayment


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        for path, data in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(data)
            packed.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def source(label: str) -> bytes:
    return "\n".join(
        f"def {label}_{i}(input_{label}):\n"
        f'    result_{label} = input_{label}.get("{label}_{i}", {i})\n'
        f"    return result_{label} * {i + 1}\n"
        for i in range(40)
    ).encode()


def payment(agent: Agent, coldkey: str) -> EvaluationPayment:
    return EvaluationPayment(
        agent_id=agent.agent_id,
        block_hash=uuid4().hex * 2,
        extrinsic_index=0,
        miner_hotkey=agent.miner_hotkey,
        miner_coldkey=coldkey,
        amount_rao=1,
        dest_address="test-destination",
        timestamp=agent.created_at,
        created_at=agent.created_at,
    )


def paid_submission(
    session,
    data: bytes,
    *,
    coldkey: str,
    created_at: datetime,
    hotkey: str | None = None,
    paid: bool = True,
):
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=hotkey or uuid4().hex,
        name="Synthetic observation",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        status=AgentStatus.UPLOADED,
        created_at=created_at,
    )
    session.add(agent)
    if paid:
        session.add(payment(agent, coldkey))
    return agent
