"""Durable admin review surface for ``ath_pending_review`` holds."""

import asyncio
import hashlib
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import String, exists, false, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Load, undefer_group
from sqlalchemy.sql.elements import ColumnElement

from ditto.api_models.admin_copy_review import (
    AdminCopyReviewAction,
    AdminCopyReviewAudit,
    AdminCopyReviewComparisonUnavailable,
    AdminCopyReviewCurrentComparison,
    AdminCopyReviewEvidence,
    AdminCopyReviewItem,
    AdminCopyReviewList,
    AdminCopyReviewOpenRequest,
    AdminCopyReviewOpenResponse,
    AdminCopyReviewPrecedent,
    AdminCopyReviewPrecedentList,
    AdminCopyReviewResolveRequest,
    AdminCopyReviewResolveResponse,
    AdminDeferredReviewEvidence,
    AdminSourceDiffFileDetail,
    AdminSourceDiffManifest,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.anti_copy_comparison import compare_anti_copy_pair
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.source_diff import (
    build_source_diff_manifest,
    unified_diff_for_file,
)
from ditto.api_server.source_inspect import (
    MAX_TARBALL_BYTES,
    SourceInspectError,
    TarSourceInspector,
)
from ditto.api_server.storage import ObjectDownloadFailedError, S3StorageClient
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    AthReviewAction,
    Score,
    ValidatorTicket,
)
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_ADMIN_COPY_REVIEW_DIFF,
    ENDPOINT_ADMIN_COPY_REVIEW_DIFF_FILE,
    record_artifact_fetch,
)
from ditto.db.queries.benchmark_rollout import (
    active_bench_version,
    open_rollout,
    preserve_desired_authority,
)
from ditto.db.queries.lease_liveness import (
    ACTION_FORCE_EXPIRED,
    expire_issued_tickets,
)
from ditto.db.queries.payments import (
    get_miner_coldkey_for_agent,
    get_miner_coldkeys_for_agents,
)
from ditto.db.queries.scores import (
    MIN_ELIGIBLE_CASES,
    LedgerRow,
    list_scores_for_agent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
AdminDep = Annotated[None, Depends(require_admin)]


def _fingerprint_versions(evidence: dict) -> dict[str, int | str | None]:
    return {
        "lexical": evidence.get("content_fingerprint_version"),
        "structural": evidence.get("structural_fingerprint_version"),
        "prompt": evidence.get("prompt_fingerprint_version"),
    }


async def _review_coldkeys(
    session: AsyncSession, agent: Agent, matched: Agent | None
) -> tuple[str | None, str | None]:
    """Payment-time coldkeys for a held agent and the agent it was matched to.

    Both are single-row lookups on ``evaluation_payments.agent_id`` (unique).
    Surfacing them lets a reviewer see at a glance whether a copy came from the
    same payer without a manual database query — the question that repeatedly
    stalled duplicate adjudication. It stays one signal: see
    :mod:`ditto.db.queries.ownership`.
    """
    coldkeys = await get_miner_coldkeys_for_agents(
        session,
        agent_ids={agent.agent_id}
        | ({matched.agent_id} if matched is not None else set()),
    )
    return (
        coldkeys.get(agent.agent_id),
        coldkeys.get(matched.agent_id) if matched is not None else None,
    )


def _item(
    review: AthReview,
    agent: Agent,
    matched: Agent | None = None,
    comparison: (
        AdminCopyReviewCurrentComparison | AdminCopyReviewComparisonUnavailable | None
    ) = None,
    *,
    miner_coldkey: str | None = None,
    duplicate_of_coldkey: str | None = None,
) -> AdminCopyReviewItem:
    provenance = review.algorithm_provenance
    review_kind = provenance.get("review_kind")
    if review_kind not in _KNOWN_REVIEW_KINDS:
        review_kind = "copy"
    deferred_raw = review.original_evidence.get("deferred_review")
    deferred_review = (
        AdminDeferredReviewEvidence.model_validate(deferred_raw)
        if isinstance(deferred_raw, dict)
        else None
    )
    return AdminCopyReviewItem(
        review_id=review.review_id,
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        miner_coldkey=miner_coldkey,
        agent_name=agent.name,
        agent_version=agent.version,
        submitted_at=agent.created_at,
        status=cast(Literal["pending", "resolved"], review.status),
        agent_status=agent.status.value,
        opened_at=review.reopened_at or review.opened_at,
        resolved_at=review.resolved_at,
        resolved_by=review.resolved_by,
        resolution=cast(Literal["clear", "reject"] | None, review.resolution),
        resolution_reason=review.resolution_reason,
        original=AdminCopyReviewEvidence(
            review_kind=cast(
                Literal[
                    "copy",
                    "benchmark_overfit",
                    "deferred_source_review",
                    "anomalous_score",
                ],
                review_kind,
            ),
            duplicate_of=review.original_duplicate_of,
            reason=review.original_reason,
            policy_version=review.original_policy_version,
            fingerprint_versions=_fingerprint_versions(review.original_evidence),
            reference_provenance=str(
                provenance.get("reference_corpus_id")
                or provenance.get("reference_provenance", "unknown")
            ),
            backfilled=bool(provenance.get("backfilled", False)),
            duplicate_of_name=matched.name if matched else None,
            duplicate_of_version=matched.version if matched else None,
            duplicate_of_hotkey=matched.miner_hotkey if matched else None,
            duplicate_of_coldkey=duplicate_of_coldkey if matched else None,
            duplicate_of_submitted_at=matched.created_at if matched else None,
            deferred_review=deferred_review,
        ),
        current_comparison=comparison,
    )


def _audit(
    review: AthReview,
    agent: Agent,
    matched: Agent | None = None,
    actions: list[AthReviewAction] | None = None,
    *,
    miner_coldkey: str | None = None,
    duplicate_of_coldkey: str | None = None,
) -> AdminCopyReviewAudit:
    evidence = review.original_evidence
    provenance = review.algorithm_provenance
    held_artifact_sha256 = evidence.get("sha256")
    if not isinstance(held_artifact_sha256, str):
        held_artifact_sha256 = None
    held_score_count = evidence.get("score_count")
    if (
        not isinstance(held_score_count, int)
        or isinstance(held_score_count, bool)
        or held_score_count < 0
    ):
        held_score_count = None
    previous_status = evidence.get("previous_status")
    if not isinstance(previous_status, str):
        previous_status = None
    opened_by = provenance.get("opened_by")
    if not isinstance(opened_by, str):
        opened_by = None
    return AdminCopyReviewAudit(
        review=_item(
            review,
            agent,
            matched,
            miner_coldkey=miner_coldkey,
            duplicate_of_coldkey=duplicate_of_coldkey,
        ),
        agent_status=agent.status.value,
        held_artifact_sha256=held_artifact_sha256,
        held_score_count=held_score_count,
        previous_status=previous_status,
        opened_by=opened_by,
        action_history=[
            AdminCopyReviewAction(
                action=cast(Literal["reopen", "clear", "reject"], action.action),
                reason=action.reason,
                actor=action.actor,
                created_at=action.created_at,
                previous_status=action.evidence.get("previous_status"),
                artifact_sha256=action.evidence.get("sha256"),
                score_count=action.evidence.get("score_count"),
            )
            for action in actions or []
        ],
    )


async def _review_actions(
    session: AsyncSession, review_id: UUID
) -> list[AthReviewAction]:
    return list(
        await session.scalars(
            select(AthReviewAction)
            .where(AthReviewAction.review_id == review_id)
            .order_by(AthReviewAction.created_at, AthReviewAction.action_id)
        )
    )


async def _matched_agents(
    session: AsyncSession, reviews: list[AthReview], *, with_anticopy: bool = False
) -> dict[UUID, Agent]:
    """Batch-load the originally matched agents for a page of reviews.

    ``with_anticopy=True`` also loads the deferred sketch columns — needed
    only when the matched agents serve as comparison references.
    """
    ids = {r.original_duplicate_of for r in reviews if r.original_duplicate_of}
    if not ids:
        return {}
    stmt = select(Agent).where(Agent.agent_id.in_(ids))
    if with_anticopy:
        stmt = stmt.options(undefer_group("anticopy"))
    rows = (await session.execute(stmt)).scalars().all()
    return {row.agent_id: row for row in rows}


_UNAVAILABLE = "current comparison unavailable"


async def _batch_comparisons(
    session: AsyncSession,
    rows: list[tuple[AthReview, Agent]],
    matched: dict[UUID, Agent],
) -> dict[
    UUID, AdminCopyReviewCurrentComparison | AdminCopyReviewComparisonUnavailable
]:
    """Recompute the pair comparison for a whole page of reviews.

    Consumers previously fanned out one ``/current-comparison`` request per
    row (~4 queries each). This loads every involved agent's scores with ONE
    ``IN`` query and runs the pure per-pair compares in a worker thread so
    the event loop stays responsive. Rows the dedicated endpoint would 409
    embed the same fail-closed unavailable state instead.
    """
    involved: set[UUID] = set()
    for review, agent in rows:
        if (
            review.status == "pending"
            and agent.status == AgentStatus.ATH_PENDING_REVIEW
            and review.original_duplicate_of is not None
            and review.original_duplicate_of in matched
        ):
            involved.add(agent.agent_id)
            involved.add(review.original_duplicate_of)
    scores_by_agent: defaultdict[UUID, list[Score]] = defaultdict(list)
    coldkeys_by_agent = await get_miner_coldkeys_for_agents(session, agent_ids=involved)
    if involved:
        score_rows = (
            (await session.execute(select(Score).where(Score.agent_id.in_(involved))))
            .scalars()
            .all()
        )
        for score in score_rows:
            scores_by_agent[score.agent_id].append(score)

    pairs: list[tuple[UUID, LedgerRow, LedgerRow]] = []
    out: dict[
        UUID, AdminCopyReviewCurrentComparison | AdminCopyReviewComparisonUnavailable
    ] = {}
    for review, agent in rows:
        reference = (
            matched.get(review.original_duplicate_of)
            if review.original_duplicate_of
            else None
        )
        if (
            review.status != "pending"
            or agent.status != AgentStatus.ATH_PENDING_REVIEW
            or reference is None
        ):
            out[review.review_id] = AdminCopyReviewComparisonUnavailable(
                reason=_UNAVAILABLE
            )
            continue
        candidate = _canonical_ledger_row(
            agent,
            scores_by_agent.get(agent.agent_id, []),
            miner_coldkey=coldkeys_by_agent.get(agent.agent_id),
        )
        reference_row = _canonical_ledger_row(
            reference,
            scores_by_agent.get(reference.agent_id, []),
            miner_coldkey=coldkeys_by_agent.get(reference.agent_id),
        )
        if candidate is None or reference_row is None:
            out[review.review_id] = AdminCopyReviewComparisonUnavailable(
                reason=_UNAVAILABLE
            )
            continue
        pairs.append((review.review_id, candidate, reference_row))

    def _compute() -> dict[UUID, dict[str, object]]:
        return {
            review_id: compare_anti_copy_pair(
                candidate=candidate, reference=reference_row
            ).to_wire()
            for review_id, candidate, reference_row in pairs
        }

    for review_id, wire in (await asyncio.to_thread(_compute)).items():
        out[review_id] = AdminCopyReviewCurrentComparison.model_validate(wire)
    return out


async def _get_review(
    session: AsyncSession,
    agent_id: UUID,
    *,
    lock: bool = False,
    with_anticopy: bool = False,
) -> tuple[AthReview, Agent] | None:
    """``with_anticopy=True`` also loads the agent's deferred sketch columns —
    needed only when the row feeds a pair comparison."""
    stmt = (
        select(AthReview, Agent)
        .join(Agent, Agent.agent_id == AthReview.agent_id)
        .where(AthReview.agent_id == agent_id)
    )
    if with_anticopy:
        stmt = stmt.options(Load(Agent).undefer_group("anticopy"))
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    return None if row is None else (row[0], row[1])


def _canonical_ledger_row(
    agent: Agent,
    scores: list[Score],
    *,
    miner_coldkey: str | None = None,
) -> LedgerRow | None:
    """Build the same median-score value object used by the anti-copy gate."""
    if not scores:
        return None
    ordered = sorted(
        scores, key=lambda score: (score.composite, score.validator_hotkey)
    )
    canonical = ordered[(len(ordered) - 1) // 2]
    return LedgerRow(
        miner_hotkey=agent.miner_hotkey,
        agent_id=agent.agent_id,
        composite=canonical.composite,
        tool_mean=canonical.tool_mean,
        memory_mean=canonical.memory_mean,
        first_seen=agent.created_at,
        sha256=agent.sha256,
        size_bytes=agent.size_bytes,
        run_id=canonical.run_id,
        seed=canonical.seed,
        validator_hotkey=canonical.validator_hotkey,
        signature=canonical.signature,
        status=agent.status,
        miner_coldkey=miner_coldkey,
        content_fingerprint=agent.content_fingerprint,
        structural_fingerprint=agent.structural_fingerprint,
        normalized_source_hash=agent.normalized_source_hash,
        prompt_fingerprint=agent.prompt_fingerprint,
        code_embedding=agent.code_embedding,
        code_embed_model=agent.code_embed_model,
        median_ms=canonical.median_ms,
        n=canonical.n,
        eligible=canonical.n >= MIN_ELIGIBLE_CASES and canonical.composite > 0.0,
        details=canonical.details,
    )


_KNOWN_REVIEW_KINDS = (
    "copy",
    "benchmark_overfit",
    "deferred_source_review",
    "anomalous_score",
)


def _review_kind_filter(review_kind: str) -> ColumnElement[bool]:
    """SQL for ``review_kind``, matching :func:`_item`'s fallback exactly.

    ``review_kind`` lives in ``algorithm_provenance`` and predates the column
    it describes, so older rows carry no key at all and :func:`_item` renders
    them as ``copy``. The filter has to agree: if it matched only rows that
    literally say ``copy``, a ``review_kind=copy`` query would silently drop
    every hold opened before the key existed while the rows it *did* return
    all claimed the kind the caller asked for -- an omission with nothing on
    its face to reveal it.
    """
    stored = AthReview.algorithm_provenance["review_kind"].as_string()
    if review_kind != "copy":
        return stored == review_kind
    return or_(stored.is_(None), stored.not_in(_KNOWN_REVIEW_KINDS), stored == "copy")


def _ilike_contains(column: Any, query: str) -> Any:
    """Case-insensitive substring match that treats ``%`` and ``_`` as literals."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def _precedent_item(review: AthReview, agent: Agent) -> AdminCopyReviewPrecedent:
    provenance = review.algorithm_provenance
    review_kind = provenance.get("review_kind")
    if review_kind not in _KNOWN_REVIEW_KINDS:
        review_kind = "copy"
    return AdminCopyReviewPrecedent(
        review_id=review.review_id,
        agent_id=agent.agent_id,
        agent_name=agent.name,
        agent_version=agent.version,
        miner_hotkey=agent.miner_hotkey,
        status=cast(Literal["pending", "resolved"], review.status),
        resolution=cast(Literal["clear", "reject"] | None, review.resolution),
        resolution_reason=review.resolution_reason,
        original_reason=review.original_reason,
        review_kind=cast(
            Literal["copy", "benchmark_overfit", "deferred_source_review"],
            review_kind,
        ),
        opened_at=review.reopened_at or review.opened_at,
        resolved_at=review.resolved_at,
        resolved_by=review.resolved_by,
    )


@router.get("/copy-reviews", response_model=AdminCopyReviewList)
async def list_copy_reviews(
    _admin: AdminDep,
    session: SessionDep,
    status: Literal["pending", "resolved", "all"] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include: Literal["current_comparison"] | None = None,
    generation: Literal["active", "rollout", "history", "all"] = "active",
    review_kind: Literal[
        "copy", "benchmark_overfit", "deferred_source_review", "anomalous_score"
    ]
    | None = None,
) -> AdminCopyReviewList:
    active_version = await active_bench_version(session)
    rollout = await open_rollout(session)
    rollout_version = (
        int(rollout.desired_version)
        if rollout is not None and rollout.desired_version != active_version
        else None
    )
    has_active_score = exists(
        select(Score.agent_id).where(
            Score.agent_id == AthReview.agent_id,
            Score.bench_version == active_version,
        )
    )
    where: list[ColumnElement[bool]] = (
        [] if status == "all" else [AthReview.status == status]
    )
    if review_kind is not None:
        where.append(_review_kind_filter(review_kind))
    if generation == "active":
        where.append(has_active_score)
    elif generation == "rollout":
        if rollout_version is None:
            where.append(false())
        else:
            where.append(
                exists(
                    select(Score.agent_id).where(
                        Score.agent_id == AthReview.agent_id,
                        Score.bench_version == rollout_version,
                    )
                )
            )
    elif generation == "history":
        where.append(~has_active_score)
        if rollout_version is not None:
            where.append(
                ~exists(
                    select(Score.agent_id).where(
                        Score.agent_id == AthReview.agent_id,
                        Score.bench_version == rollout_version,
                    )
                )
            )
    count = await session.scalar(
        select(func.count()).select_from(AthReview).where(*where)
    )
    with_comparisons = include == "current_comparison"
    stmt = (
        select(AthReview, Agent)
        .join(Agent, Agent.agent_id == AthReview.agent_id)
        .where(*where)
        .order_by(
            func.coalesce(AthReview.reopened_at, AthReview.opened_at).asc(),
            AthReview.review_id.asc(),
        )
        .limit(limit)
        .offset(offset)
    )
    if with_comparisons:
        stmt = stmt.options(Load(Agent).undefer_group("anticopy"))
    rows = (await session.execute(stmt)).all()
    row_pairs = [(review, agent) for review, agent in rows]
    matched = await _matched_agents(
        session,
        [review for review, _ in row_pairs],
        with_anticopy=with_comparisons,
    )
    comparisons: dict[
        UUID, AdminCopyReviewCurrentComparison | AdminCopyReviewComparisonUnavailable
    ] = {}
    if with_comparisons:
        comparisons = await _batch_comparisons(session, row_pairs, matched)
    # One batched lookup for the whole page: the held agents and every agent
    # they were matched against.
    coldkeys = await get_miner_coldkeys_for_agents(
        session,
        agent_ids={agent.agent_id for _review, agent in row_pairs} | set(matched),
    )
    return AdminCopyReviewList(
        items=[
            _item(
                review,
                agent,
                matched.get(review.original_duplicate_of),
                comparison=comparisons.get(review.review_id),
                miner_coldkey=coldkeys.get(agent.agent_id),
                duplicate_of_coldkey=(
                    coldkeys.get(review.original_duplicate_of)
                    if review.original_duplicate_of is not None
                    else None
                ),
            )
            for review, agent in row_pairs
        ],
        count=count or 0,
        limit=limit,
        offset=offset,
        review_kind=review_kind,
        generation=generation,
        active_bench_version=active_version,
        rollout_bench_version=rollout_version,
    )


@router.get("/copy-reviews/precedents", response_model=AdminCopyReviewPrecedentList)
async def search_copy_review_precedents(
    _admin: AdminDep,
    session: SessionDep,
    q: Annotated[str | None, Query(min_length=2, max_length=200)] = None,
    resolution: Literal["clear", "reject", "all"] = "all",
    status: Literal["resolved", "all"] = "resolved",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    review_kind: Literal["copy", "benchmark_overfit", "deferred_source_review"]
    | None = None,
) -> AdminCopyReviewPrecedentList:
    """Search decided ATH reviews as case law.

    Courts cite holdings. The operator queue lists *open* work; this lists
    *resolved* reasons so a later review can match a phrase table, family
    compiler, or zero-token bypass to a prior clear or reject. Declared
    before ``/copy-reviews/{agent_id}`` so ``precedents`` is never parsed
    as a UUID.
    """
    where: list[ColumnElement[bool]] = []
    if status != "all":
        where.append(AthReview.status == status)
    if resolution != "all":
        where.append(AthReview.resolution == resolution)
    if review_kind is not None:
        where.append(_review_kind_filter(review_kind))
    if q is not None:
        needle = q.strip()
        if needle:
            where.append(
                or_(
                    _ilike_contains(AthReview.original_reason, needle),
                    _ilike_contains(AthReview.resolution_reason, needle),
                    _ilike_contains(Agent.name, needle),
                    _ilike_contains(Agent.miner_hotkey, needle),
                    _ilike_contains(sa_cast(Agent.version, String()), needle),
                )
            )
    count = await session.scalar(
        select(func.count())
        .select_from(AthReview)
        .join(Agent, Agent.agent_id == AthReview.agent_id)
        .where(*where)
    )
    rows = (
        await session.execute(
            select(AthReview, Agent)
            .join(Agent, Agent.agent_id == AthReview.agent_id)
            .where(*where)
            .order_by(
                AthReview.resolved_at.desc().nulls_last(),
                func.coalesce(AthReview.reopened_at, AthReview.opened_at).desc(),
                AthReview.review_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return AdminCopyReviewPrecedentList(
        items=[_precedent_item(review, agent) for review, agent in rows],
        count=count or 0,
        limit=limit,
        offset=offset,
        q=q,
        resolution=resolution,
        review_kind=review_kind,
        status=status,
    )


@router.get("/copy-reviews/{agent_id}", response_model=AdminCopyReviewItem)
async def get_copy_review(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminCopyReviewItem:
    row = await _get_review(session, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="copy review not found")
    review, agent = row
    matched = (
        await session.get(Agent, review.original_duplicate_of)
        if review.original_duplicate_of
        else None
    )
    candidate_coldkey, reference_coldkey = await _review_coldkeys(
        session, agent, matched
    )
    return _item(
        review,
        agent,
        matched,
        miner_coldkey=candidate_coldkey,
        duplicate_of_coldkey=reference_coldkey,
    )


@router.get("/copy-reviews/{agent_id}/audit", response_model=AdminCopyReviewAudit)
async def get_copy_review_audit(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> AdminCopyReviewAudit:
    """Return the durable reason and attribution needed to explain an ATH hold."""
    row = await _get_review(session, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="copy review not found")
    review, agent = row
    matched = (
        await session.get(Agent, review.original_duplicate_of)
        if review.original_duplicate_of
        else None
    )
    actions = await _review_actions(session, review.review_id)
    candidate_coldkey, reference_coldkey = await _review_coldkeys(
        session, agent, matched
    )
    return _audit(
        review,
        agent,
        matched,
        actions,
        miner_coldkey=candidate_coldkey,
        duplicate_of_coldkey=reference_coldkey,
    )


@router.get(
    "/copy-reviews/{agent_id}/current-comparison",
    response_model=AdminCopyReviewCurrentComparison,
)
async def get_copy_review_current_comparison(
    agent_id: UUID, _admin: AdminDep, session: SessionDep
) -> dict[str, object]:
    row = await _get_review(session, agent_id, with_anticopy=True)
    if row is None:
        raise HTTPException(status_code=404, detail="copy review not found")
    review, candidate_agent = row
    if (
        review.status != "pending"
        or candidate_agent.status != AgentStatus.ATH_PENDING_REVIEW
        or review.original_duplicate_of is None
    ):
        raise HTTPException(status_code=409, detail="current comparison unavailable")
    reference_agent = await session.get(
        Agent, review.original_duplicate_of, options=[undefer_group("anticopy")]
    )
    if reference_agent is None:
        raise HTTPException(status_code=409, detail="current comparison unavailable")
    candidate_scores = await list_scores_for_agent(
        session, agent_id=candidate_agent.agent_id
    )
    reference_scores = await list_scores_for_agent(
        session, agent_id=reference_agent.agent_id
    )
    candidate_coldkey = await get_miner_coldkey_for_agent(
        session, agent_id=candidate_agent.agent_id
    )
    reference_coldkey = await get_miner_coldkey_for_agent(
        session, agent_id=reference_agent.agent_id
    )
    candidate = _canonical_ledger_row(
        candidate_agent, candidate_scores, miner_coldkey=candidate_coldkey
    )
    reference = _canonical_ledger_row(
        reference_agent, reference_scores, miner_coldkey=reference_coldkey
    )
    if candidate is None or reference is None:
        raise HTTPException(status_code=409, detail="current comparison unavailable")
    comparison = compare_anti_copy_pair(candidate=candidate, reference=reference)
    return comparison.to_wire()


@router.post(
    "/copy-reviews/{agent_id}/open",
    response_model=AdminCopyReviewOpenResponse,
)
async def open_copy_review(
    agent_id: UUID,
    payload: AdminCopyReviewOpenRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminCopyReviewOpenResponse:
    """Manually hold one exact scored artifact for benchmark-overfit review.

    The identity guards keep a stale Backroom tab from holding a replacement
    artifact or a submission whose score set changed after the operator's
    review. Scores remain durable; changing the agent status removes it from
    the emission-eligible ledger until an operator resolves the review.
    """
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")

    async with session.begin():
        agent = await session.scalar(
            select(Agent)
            .options(undefer_group("anticopy"))
            .where(Agent.agent_id == agent_id)
            .with_for_update()
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        score_count = await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        score_count = int(score_count or 0)
        if agent.sha256 != payload.expected_sha256:
            raise HTTPException(status_code=409, detail="artifact sha256 changed")
        if score_count != payload.expected_score_count:
            raise HTTPException(status_code=409, detail="score count changed")

        existing = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id).with_for_update()
        )
        if existing is not None:
            evidence = existing.original_evidence
            provenance = existing.algorithm_provenance
            latest_reopen = await session.scalar(
                select(AthReviewAction)
                .where(
                    AthReviewAction.review_id == existing.review_id,
                    AthReviewAction.action == "reopen",
                )
                .order_by(
                    AthReviewAction.created_at.desc(),
                    AthReviewAction.action_id.desc(),
                )
                .limit(1)
            )
            reopened_hold = (
                latest_reopen is not None
                and latest_reopen.reason == payload.reason
                and latest_reopen.evidence.get("sha256") == payload.expected_sha256
                and latest_reopen.evidence.get("score_count")
                == payload.expected_score_count
            )
            same_hold = (
                existing.status == "pending"
                and agent.status == AgentStatus.ATH_PENDING_REVIEW
                and (
                    (
                        existing.original_reason == payload.reason
                        and evidence.get("sha256") == payload.expected_sha256
                        and evidence.get("score_count") == payload.expected_score_count
                        and provenance.get("review_kind") == "benchmark_overfit"
                    )
                    or reopened_hold
                )
            )
            if same_hold:
                return AdminCopyReviewOpenResponse(
                    review=_item(existing, agent),
                    agent_status=agent.status.value,
                    idempotent=True,
                    reopened=latest_reopen is not None,
                )
            if existing.status != "resolved":
                raise HTTPException(status_code=409, detail="ATH review already exists")
            reconsidering_rejection = (
                existing.resolution == "reject" and agent.status == AgentStatus.BANNED
            )
            if agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE) and not (
                reconsidering_rejection
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"agent is {agent.status.value}, not scored or live",
                )

            reopened_at = datetime.now(UTC)
            await preserve_desired_authority(session, now=reopened_at)
            previous_status = agent.status.value
            if reconsidering_rejection:
                # A rejected ATH review is the operation that put this exact
                # submission in BANNED. Reopening that same guarded review must
                # retain the status that a later clear should restore, rather
                # than recording BANNED as though it predated the review.
                previous_status = (
                    latest_reopen.evidence.get("previous_status")
                    if latest_reopen is not None
                    else existing.original_evidence.get("previous_status")
                )
                if previous_status not in {
                    AgentStatus.SCORED.value,
                    AgentStatus.LIVE.value,
                }:
                    raise HTTPException(
                        status_code=409,
                        detail="review has no restorable previous status",
                    )
            existing.status = "pending"
            existing.reopened_at = reopened_at
            existing.resolved_at = None
            existing.resolved_by = None
            existing.resolution = None
            existing.resolution_reason = None
            agent.status = AgentStatus.ATH_PENDING_REVIEW
            agent.duplicate_of = existing.original_duplicate_of
            agent.review_reason = existing.original_reason
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=existing.review_id,
                    action="reopen",
                    reason=payload.reason,
                    actor=actor,
                    evidence={
                        "sha256": payload.expected_sha256,
                        "score_count": payload.expected_score_count,
                        "previous_status": previous_status,
                    },
                    created_at=reopened_at,
                )
            )
            await session.flush()
            return AdminCopyReviewOpenResponse(
                review=_item(existing, agent),
                agent_status=agent.status.value,
                idempotent=False,
                reopened=True,
            )

        if agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE):
            raise HTTPException(
                status_code=409,
                detail=f"agent is {agent.status.value}, not scored or live",
            )

        opened_at = datetime.now(UTC)
        await preserve_desired_authority(session, now=opened_at)
        review = AthReview(
            review_id=uuid4(),
            agent_id=agent.agent_id,
            status="pending",
            opened_at=opened_at,
            original_duplicate_of=None,
            original_reason=payload.reason,
            original_policy_version=agent.screening_policy_version,
            original_evidence={
                "sha256": agent.sha256,
                "score_count": score_count,
                "previous_status": agent.status.value,
                "content_fingerprint_version": (agent.content_fingerprint or {}).get(
                    "v"
                ),
                "structural_fingerprint_version": (
                    agent.structural_fingerprint or {}
                ).get("v"),
                "prompt_fingerprint_version": (agent.prompt_fingerprint or {}).get("v"),
            },
            algorithm_provenance={
                "snapshot": "manual-admin-hold",
                "review_kind": "benchmark_overfit",
                "algorithm_version": "manual-ath-review-v1",
                "opened_by": actor,
                "backfilled": False,
                "opened_at_source": "admin-request",
            },
        )
        agent.status = AgentStatus.ATH_PENDING_REVIEW
        agent.duplicate_of = None
        agent.review_reason = payload.reason
        session.add(review)
        await session.flush()

    candidate_coldkey, _reference_coldkey = await _review_coldkeys(session, agent, None)
    return AdminCopyReviewOpenResponse(
        review=_item(review, agent, miner_coldkey=candidate_coldkey),
        agent_status=agent.status.value,
        idempotent=False,
        reopened=False,
    )


@router.post(
    "/copy-reviews/{agent_id}/resolve",
    response_model=AdminCopyReviewResolveResponse,
)
async def resolve_copy_review(
    agent_id: UUID,
    payload: AdminCopyReviewResolveRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminCopyReviewResolveResponse:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    canonical = {"release": "clear", "ban": "reject"}.get(
        payload.resolution, payload.resolution
    )
    async with session.begin():
        row = await _get_review(session, agent_id, lock=True)
        if row is None:
            raise HTTPException(status_code=404, detail="copy review not found")
        review, agent = row
        matched = (
            await session.get(Agent, review.original_duplicate_of)
            if review.original_duplicate_of
            else None
        )
        if review.status == "resolved":
            if review.resolution != canonical:
                raise HTTPException(status_code=409, detail="review already resolved")
            candidate_coldkey, reference_coldkey = await _review_coldkeys(
                session, agent, matched
            )
            return AdminCopyReviewResolveResponse(
                review=_item(
                    review,
                    agent,
                    matched,
                    miner_coldkey=candidate_coldkey,
                    duplicate_of_coldkey=reference_coldkey,
                ),
                agent_status=agent.status.value,
                idempotent=True,
            )
        if agent.status != AgentStatus.ATH_PENDING_REVIEW:
            raise HTTPException(status_code=409, detail="agent is no longer held")
        if agent.duplicate_of != review.original_duplicate_of:
            raise HTTPException(
                status_code=409, detail="agent hold evidence no longer matches review"
            )
        if agent.review_reason != review.original_reason:
            raise HTTPException(
                status_code=409, detail="agent hold reason no longer matches review"
            )
        latest_reopen = await session.scalar(
            select(AthReviewAction)
            .where(
                AthReviewAction.review_id == review.review_id,
                AthReviewAction.action == "reopen",
            )
            .order_by(
                AthReviewAction.created_at.desc(), AthReviewAction.action_id.desc()
            )
            .limit(1)
        )
        previous_status = (
            latest_reopen.evidence.get("previous_status")
            if latest_reopen is not None
            else review.original_evidence.get("previous_status")
        )
        if canonical != "clear":
            await preserve_desired_authority(session, now=datetime.now(UTC))
        now = datetime.now(UTC)
        agent.status = (
            AgentStatus.LIVE
            if canonical == "clear" and previous_status == AgentStatus.LIVE.value
            else AgentStatus.SCORED
            if canonical == "clear"
            else AgentStatus.BANNED
        )
        if canonical == "reject":
            live_tickets = list(
                (
                    await session.scalars(
                        select(ValidatorTicket)
                        .where(
                            ValidatorTicket.agent_id == agent.agent_id,
                            ValidatorTicket.status == TicketStatus.ISSUED,
                            ValidatorTicket.deadline > now,
                        )
                        .with_for_update()
                    )
                ).all()
            )
            await expire_issued_tickets(
                session,
                tickets=live_tickets,
                now=now,
                context="ath_reject",
                action=ACTION_FORCE_EXPIRED,
                actor=actor,
                reason=payload.reason,
                request_id=review.review_id,
                compensate=False,
            )
        review.status = "resolved"
        review.resolved_at = now
        review.resolved_by = actor
        review.resolution = canonical
        review.resolution_reason = payload.reason
        session.add(
            AthReviewAction(
                action_id=uuid4(),
                review_id=review.review_id,
                action=canonical,
                reason=payload.reason,
                actor=actor,
                evidence={"previous_status": previous_status},
                created_at=review.resolved_at,
            )
        )
        await session.flush()
    candidate_coldkey, reference_coldkey = await _review_coldkeys(
        session, agent, matched
    )
    return AdminCopyReviewResolveResponse(
        review=_item(
            review,
            agent,
            matched,
            miner_coldkey=candidate_coldkey,
            duplicate_of_coldkey=reference_coldkey,
        ),
        agent_status=agent.status.value,
        idempotent=False,
    )


async def _open_inspector(agent: Agent, storage: S3StorageClient) -> TarSourceInspector:
    """Fetch one agent's stored tarball, verify its digest, open a bounded reader."""
    try:
        tar_bytes = await storage.get_object(
            key=f"{agent.agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
        )
    except ObjectDownloadFailedError as error:
        raise HTTPException(
            status_code=502, detail="artifact is unavailable in storage"
        ) from error

    def _verify_and_open() -> TarSourceInspector:
        if hashlib.sha256(tar_bytes).hexdigest() != agent.sha256:
            raise HTTPException(
                status_code=502, detail="stored artifact does not match its digest"
            )
        return TarSourceInspector(tar_bytes)

    try:
        return await asyncio.to_thread(_verify_and_open)
    except SourceInspectError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _diff_pair(
    agent_id: UUID, session: AsyncSession, storage: S3StorageClient
) -> tuple[Agent, Agent, dict[str, str], dict[str, str]]:
    """Load the held agent, its matched reference, and both text-file maps.

    Both tarballs are fetched, digest-verified, and read in one pass each; the
    per-file text maps feed either the manifest or a single-file unified diff.
    """
    row = await _get_review(session, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="copy review not found")
    review, candidate_agent = row
    if review.original_duplicate_of is None:
        raise HTTPException(
            status_code=409, detail="review has no matched reference to diff against"
        )
    reference_agent = await session.get(Agent, review.original_duplicate_of)
    if reference_agent is None:
        raise HTTPException(
            status_code=409, detail="matched reference agent no longer exists"
        )
    candidate_inspector = await _open_inspector(candidate_agent, storage)
    reference_inspector = await _open_inspector(reference_agent, storage)
    candidate_text, reference_text = await asyncio.gather(
        asyncio.to_thread(candidate_inspector.read_all_text),
        asyncio.to_thread(reference_inspector.read_all_text),
    )
    return candidate_agent, reference_agent, candidate_text, reference_text


async def _audit_diff_pair(
    session: AsyncSession,
    *,
    request: Request,
    actor: str,
    endpoint: str,
    candidate: Agent,
    reference: Agent,
    path: str | None = None,
) -> None:
    """Record one audit row per agent whose source this diff exposed.

    Both sides of a copy-review diff are real miner source. Writing a row for
    each -- tagged with the role it played -- is what lets the agent-scoped
    index answer "who read this submission's source" for the *reference* agent,
    who never asked to be part of anyone else's review.
    """
    for role, agent, counterpart in (
        ("candidate", candidate, reference),
        ("reference", reference, candidate),
    ):
        await record_artifact_fetch(
            session,
            agent_id=agent.agent_id,
            endpoint=endpoint,
            requester_kind="admin",
            requester_id=actor,
            artifact_sha256=agent.sha256,
            source_ip=client_ip(request),
            detail=request_detail(
                request,
                role=role,
                counterpart_agent_id=str(counterpart.agent_id),
                path=path,
            ),
        )


@router.get(
    "/copy-reviews/{agent_id}/source-diff",
    response_model=AdminSourceDiffManifest,
)
async def get_copy_review_source_diff(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminSourceDiffManifest:
    """Per-file diff manifest between a held agent and the agent it copied.

    Classifies every path as added / removed / modified / identical / renamed
    with change stats so an operator can see at a glance which files were copied
    verbatim, which were altered, and which were only moved. Unified-diff
    bodies come from the per-file endpoint.
    """
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    candidate, reference, candidate_text, reference_text = await _diff_pair(
        agent_id, session, storage
    )
    manifest = await asyncio.to_thread(
        build_source_diff_manifest, candidate_text, reference_text
    )
    logger.info(
        "admin_actor=%s viewed copy-review source diff agent_id=%s reference_id=%s",
        x_admin_actor,
        agent_id,
        reference.agent_id,
    )
    # This route reads BOTH artifacts, so it writes one row per agent. A later
    # "who read this agent's source" query must find the fetch whether the agent
    # was the held candidate or the reference it was diffed against.
    await _audit_diff_pair(
        session,
        request=request,
        actor=x_admin_actor,
        endpoint=ENDPOINT_ADMIN_COPY_REVIEW_DIFF,
        candidate=candidate,
        reference=reference,
    )
    return AdminSourceDiffManifest(
        agent_id=agent_id,
        reference_agent_id=reference.agent_id,
        candidate_sha256=candidate.sha256,
        reference_sha256=reference.sha256,
        **manifest,  # type: ignore[arg-type]
    )


@router.get(
    "/copy-reviews/{agent_id}/source-diff/file",
    response_model=AdminSourceDiffFileDetail,
)
async def get_copy_review_source_diff_file(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
    path: Annotated[str, Query(min_length=1, max_length=240)],
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminSourceDiffFileDetail:
    """Bounded unified diff (reference -> candidate) for one file in the pair."""
    if x_admin_actor is None or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    normalized = path.removeprefix("./")
    candidate, reference, candidate_text, reference_text = await _diff_pair(
        agent_id, session, storage
    )
    try:
        detail = await asyncio.to_thread(
            unified_diff_for_file, normalized, candidate_text, reference_text
        )
    except KeyError as error:
        raise HTTPException(
            status_code=404, detail=f"no file at {normalized!r} in either artifact"
        ) from error
    logger.info(
        "admin_actor=%s viewed copy-review file diff agent_id=%s path=%s",
        x_admin_actor,
        agent_id,
        normalized,
    )
    await _audit_diff_pair(
        session,
        request=request,
        actor=x_admin_actor,
        endpoint=ENDPOINT_ADMIN_COPY_REVIEW_DIFF_FILE,
        candidate=candidate,
        reference=reference,
        path=normalized,
    )
    return AdminSourceDiffFileDetail(
        agent_id=agent_id,
        reference_agent_id=reference.agent_id,
        **detail,  # type: ignore[arg-type]
    )
