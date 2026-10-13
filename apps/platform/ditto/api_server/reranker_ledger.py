"""Build the Season 2 reranker shadow ledger served to validators (SN-209).

The ledger is a read of Platform state: the current shadow crown
(``reranker_champions``) and the most recently scored submissions with the
comparison the trusted scorer recorded. It is shadow only. Every entry and the
champion are clamped to ``weight_eligible=False`` / ``combined_score=0`` here,
whatever the stored rows say, so a served ledger folds to zero emission.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dittobench_reranker.contract import CONTRACT, CONTRACT_ID
from pydantic import ValidationError

from ditto.api_models.reranker_ledger import (
    RerankerLedgerChampion,
    RerankerLedgerEntry,
    RerankerLedgerResponse,
)
from ditto.db.queries.reranker import (
    get_current_reranker_champion,
    get_reranker_submission,
    list_recent_scored_reranker_submissions,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.db.models import RerankerSubmission

logger = logging.getLogger(__name__)

RERANKER_LEDGER_ENTRIES = 50


def _entry(row: RerankerSubmission) -> RerankerLedgerEntry | None:
    """One ledger entry from a stored score, or ``None`` for a foreign shape."""
    score: dict[str, Any] = row.score or {}
    if row.scored_at is None:
        return None
    try:
        comparison = score["comparison"]
        return RerankerLedgerEntry(
            submission_id=row.id,
            miner_hotkey=row.miner_hotkey,
            artifact_sha256=row.artifact_sha256,
            status=row.status,  # type: ignore[arg-type]
            shadow_score=score["challenger"]["score"],
            champion_score=score["champion"]["score"],
            champion_kind=score["champion"]["kind"],
            mean_diff=comparison["mean_diff"],
            threshold=comparison["threshold"],
            dethrones=bool(comparison["dethrones"]),
            latency_passed=bool(score["latency"]["passed"]),
            transfer_gate=str(score["transfer_gate"]["status"]),
            crowned=bool(score.get("crowned", False)),
            pools_sha256=score["pools_sha256"],
            scored_at=row.scored_at,
        )
    except (KeyError, TypeError, ValidationError):
        logger.warning(
            "reranker ledger skipped submission %s: unexpected score shape", row.id
        )
        return None


async def build_reranker_ledger(
    session: AsyncSession, *, limit: int = RERANKER_LEDGER_ENTRIES
) -> RerankerLedgerResponse:
    crown = await get_current_reranker_champion(session)
    champion = RerankerLedgerChampion(kind="incumbent")
    if crown is not None:
        holder = await get_reranker_submission(
            session, submission_id=crown.submission_id
        )
        if holder is not None:
            champion = RerankerLedgerChampion(
                kind="submission",
                submission_id=holder.id,
                miner_hotkey=holder.miner_hotkey,
                artifact_sha256=holder.artifact_sha256,
                crowned_at=crown.crowned_at,
            )
    rows = await list_recent_scored_reranker_submissions(session, limit=limit)
    entries = [entry for row in rows if (entry := _entry(row)) is not None]
    return clamp_reranker_ledger_to_shadow(
        RerankerLedgerResponse(
            contract_id=CONTRACT_ID,
            contract_sha256=CONTRACT.digest(),
            champion=champion,
            entries=entries,
            generated_at=datetime.now(UTC),
            count=len(entries),
        )
    )


def clamp_reranker_ledger_to_shadow(
    ledger: RerankerLedgerResponse,
) -> RerankerLedgerResponse:
    """Force the shadow invariant on everything a validator could fold.

    The relay is the last place before a validator reads these numbers, so it
    never vouches for a weight-bearing reranker entry, whatever was stored.
    """
    shadow = {"weight_eligible": False, "combined_score": 0.0}
    champion = (
        ledger.champion.model_copy(update=shadow)
        if ledger.champion is not None
        else None
    )
    entries = [entry.model_copy(update=shadow) for entry in ledger.entries]
    return ledger.model_copy(update={"champion": champion, "entries": entries})
