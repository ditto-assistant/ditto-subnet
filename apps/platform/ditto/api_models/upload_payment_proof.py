"""Admin contract for what Platform recorded about one upload-payment proof.

Operator-only. A miner who reports 3201, 3207 or 3208 quotes one finalized
transfer; this answers whether Platform consumed it, holds it as a reusable
credit, or never recorded it. Platform records only: nothing here re-reads the
chain.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

UploadPaymentProofState = Literal[
    "unrecorded", "funded_agent", "reusable_credit", "funded_generator"
]


class UploadPaymentProofAgent(BaseModel):
    """The agent upload a proof funded, or the identical upload behind a credit."""

    agent_id: UUID
    agent_name: str
    agent_version: int | None = None
    agent_status: str
    artifact_sha256: str
    submitted_at: datetime


class UploadPaymentProofView(BaseModel):
    """Platform's record of one ``(block_hash, extrinsic_index)`` payment proof."""

    block_hash: str
    extrinsic_index: int
    #: ``unrecorded``: no upload consumed the proof and no credit holds it.
    #: ``funded_agent``: ``agent`` is the upload it paid for. ``reusable_credit``:
    #: a byte-identical upload left it unspent, ``agent`` is that earlier upload,
    #: and the same hotkey may spend it until ``recovery_expires_at``.
    #: ``funded_generator``: it paid for ``generator_submission_id``.
    state: UploadPaymentProofState
    miner_hotkey: str | None = None
    #: Payment-time owner of ``miner_hotkey`` and signer of the transfer.
    miner_coldkey: str | None = None
    amount_rao: int | None = None
    dest_address: str | None = None
    block_timestamp: datetime | None = None
    #: When Platform verified the proof and wrote its row.
    recorded_at: datetime | None = None
    agent: UploadPaymentProofAgent | None = None
    generator_submission_id: UUID | None = None
    #: Credits only: block time plus the 24-hour recovery window. At or after
    #: it, an upload that spends the credit is refused with 3208.
    recovery_expires_at: datetime | None = None
    recovery_expired: bool | None = None
    generated_at: datetime
