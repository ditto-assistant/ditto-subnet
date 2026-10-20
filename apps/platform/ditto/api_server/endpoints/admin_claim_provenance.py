"""Operator-only per-case v13 claim provenance for one exact accepted run (#1852).

``GET /api/v1/admin/agents/{agent_id}/claim-provenance`` answers "which cases
behind this run's shadow claim-provenance aggregate were flagged, and on what
evidence" before an exact-artifact ruling. Every key is exact: the agent, its
artifact SHA-256 (so a ruling cannot be read against a different artifact),
and the accepted run id; ``case_id`` narrows to one case.

Read-only, admin-only, ``Cache-Control: no-store``. It projects the per-case
record already persisted in ``scores.details`` and never returns the answer
key, prompts, user records, tool results or completion text.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.claim_provenance_cases import (
    CLAIM_PROVENANCE_CASE_LIMIT_MAX,
    AdminClaimProvenanceCases,
    ClaimProvenanceCase,
)
from ditto.api_models.gate_evidence import GATE_NOTE_VOCABULARY
from ditto.api_models.validator import CaseScore
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.gate_evidence import (
    carries_gate_evidence,
    case_gate_notes,
    is_flagged_case,
    operator_case_provenance,
    stored_gate_evidence,
)
from ditto.db.models import Agent, Score

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@router.get(
    "/agents/{agent_id}/claim-provenance",
    response_model=AdminClaimProvenanceCases,
)
async def get_claim_provenance_cases(
    agent_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    artifact_sha256: Annotated[
        str,
        Query(
            min_length=64,
            max_length=64,
            description="The agent's exact artifact SHA-256 (lowercase hex).",
        ),
    ],
    run_id: Annotated[
        str,
        Query(min_length=1, max_length=200, description="The accepted run id."),
    ],
    case_id: Annotated[
        str | None,
        Query(min_length=1, max_length=200, description="One exact case id."),
    ] = None,
    finding: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=64,
            description="Only cases carrying this closed-vocabulary finding.",
        ),
    ] = None,
    include_unflagged: Annotated[
        bool,
        Query(
            description=(
                "Also return cases no gate would zero or discount. Ignored when "
                "case_id or finding selects the cases."
            )
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=CLAIM_PROVENANCE_CASE_LIMIT_MAX)] = 50,
) -> AdminClaimProvenanceCases:
    response.headers["Cache-Control"] = "no-store"
    if not _SHA256.fullmatch(artifact_sha256):
        raise HTTPException(
            status_code=422, detail="artifact_sha256 must be 64 lowercase hex"
        )
    if finding is not None and finding not in GATE_NOTE_VOCABULARY:
        raise HTTPException(
            status_code=422,
            detail=f"finding must be one of: {', '.join(sorted(GATE_NOTE_VOCABULARY))}",
        )

    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent.sha256 != artifact_sha256:
        # The exact-artifact binding is the point of the key: never answer for
        # a different artifact than the one the operator is ruling on.
        raise HTTPException(
            status_code=409,
            detail="artifact_sha256 does not match this agent's artifact",
        )

    rows = list(
        (
            await session.scalars(
                select(Score).where(Score.agent_id == agent_id, Score.run_id == run_id)
            )
        ).all()
    )
    if not rows:
        raise HTTPException(
            status_code=404, detail="no accepted score with this run_id for the agent"
        )
    if len(rows) > 1:
        raise HTTPException(
            status_code=409,
            detail="run_id is not unique for this agent; refusing to pick one",
        )
    score = rows[0]
    if not carries_gate_evidence(score.bench_version):
        raise HTTPException(
            status_code=409,
            detail=(
                f"run is bench v{score.bench_version}; claim provenance is a "
                "bench v13+ record"
            ),
        )

    details = score.details if isinstance(score.details, dict) else {}
    stored_per_case = details.get("per_case")
    per_case_available = isinstance(stored_per_case, list)
    raw_cases: list[Any] = stored_per_case if isinstance(stored_per_case, list) else []
    stored = stored_gate_evidence(score.gate_evidence)

    total = 0
    malformed = 0
    matched: list[ClaimProvenanceCase] = []
    matched_count = 0
    for index, raw in enumerate(raw_cases):
        total += 1
        try:
            case = CaseScore.model_validate(raw)
        except ValidationError:
            malformed += 1
            continue
        if case_id is not None:
            selected = case.case_id == case_id
        elif finding is not None:
            selected = finding in case_gate_notes(case)
        else:
            selected = include_unflagged or is_flagged_case(case)
        if not selected:
            continue
        matched_count += 1
        if len(matched) < limit:
            matched.append(
                operator_case_provenance(
                    case,
                    case_index=index,
                    agent_id=agent_id,
                    bench_version=score.bench_version,
                    validator_hotkey=score.validator_hotkey,
                    run_id=score.run_id,
                )
            )
    if malformed:
        logger.warning(
            "claim-provenance read skipped %d malformed stored case(s) agent=%s run=%s",
            malformed,
            agent_id,
            run_id,
        )
    if case_id is not None and per_case_available and matched_count == 0:
        raise HTTPException(status_code=404, detail="case_id not found in this run")

    return AdminClaimProvenanceCases(
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        agent_status=agent.status.value,
        validator_hotkey=score.validator_hotkey,
        run_id=score.run_id,
        bench_version=score.bench_version,
        composite=score.composite,
        generated_at=score.generated_at,
        posture=stored.posture if stored is not None else None,
        claim_provenance=stored.claim_provenance if stored is not None else None,
        case_id=case_id,
        finding=finding,
        include_unflagged=include_unflagged,
        per_case_available=per_case_available,
        total_cases=total,
        matched_cases=matched_count,
        malformed_cases=malformed,
        limit=limit,
        truncated=matched_count > len(matched),
        cases=matched,
    )
