"""Wire models for operator diagnostic targets."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ISOLATE_CONFIRMATION = "ISOLATE OPERATOR DIAGNOSTIC AGENT"


class OperatorDiagnosticIsolate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    actor: Annotated[str, Field(min_length=1, max_length=120)]
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str


class OperatorDiagnosticWithdrawal(BaseModel):
    bench_version: int
    created_at: datetime
    in_force: bool


class OperatorDiagnosticView(BaseModel):
    """Read-back of one agent's diagnostic-target eligibility."""

    agent_id: UUID
    miner_hotkey: str
    operator_owned: bool
    shadow: bool
    status: str
    withdrawals: list[OperatorDiagnosticWithdrawal]
    ordinary_lease_count: int
    diagnostic_lease_count: int
    score_count: int
    isolation_refusal: str | None
    diagnostic_refusal_by_version: dict[int, str | None]
