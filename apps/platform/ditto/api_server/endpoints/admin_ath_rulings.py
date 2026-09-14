"""Batched ATH rulings: presigned upload, dry-run preview, guarded execute.

A board review yields 5-20 open / clear / reject rulings at once, and the
one-at-a-time ``/copy-reviews/{agent_id}/open`` and ``/resolve`` routes made
that a manual ladder with a stale-crown trap on every rung. This module keeps
those two routes as the ONLY writers -- every ruling is applied by calling
them -- and adds the quarantine court's shape around them:

1. ``POST /admin/ath-rulings/upload-url`` -- a five-minute presigned PUT for a
   <= 1 MiB rulings JSON under an operator-scoped prefix of the private
   trace bucket.
2. ``POST /admin/ath-rulings/batch-preview`` -- validate the document, re-read
   live state per item (status, artifact SHA, score count) and the crown
   arithmetic (eligible ledger -> official fold -> KOTH projection), and
   answer a per-item disposition plus a signed, actor-bound preview token.
   Never mutates.
3. ``POST /admin/ath-rulings/batch-execute`` -- verify the token, re-download
   the exact document, RE-READ THE BOARD, and apply each ruling
   independently. An item whose guards or crown outcome moved since the
   preview is refused; the others still land, each separately audited.

The crown re-read on both legs is the point (memory:
crown-arithmetic-stales-across-shifts): a prepared batch's "will not take the
crown" premise expires while the operator reads source. The board is the one
the operator sees: the validator-equivalent fold (``_current_koth_entries``)
under the same fleet-gated tie-weighting and ceiling-band-clamp flags the
public leaderboard applies. The batch's own writes are modeled too -- item
``i`` is judged against the board after items ``< i`` (simulated in preview,
re-read from Postgres in execute), so rejecting the champion in item 0 flags
item 1's new champion instead of hiding it behind the pre-batch snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_ath_rulings import (
    ATH_RULINGS_CONFIRMATION,
    ATH_RULINGS_KEY_PREFIX,
    ATH_RULINGS_MAX_BYTES,
    ATH_RULINGS_UPLOAD_TTL_SECONDS,
    AdminAthRuling,
    AdminAthRulingExecuteItem,
    AdminAthRulingPreviewItem,
    AdminAthRulingsBoardProjection,
    AdminAthRulingsDocument,
    AdminAthRulingsExecuteRequest,
    AdminAthRulingsExecuteResponse,
    AdminAthRulingsPreviewRequest,
    AdminAthRulingsPreviewResponse,
    AdminAthRulingsUploadRequest,
    AdminAthRulingsUploadResponse,
)
from ditto.api_models.admin_copy_review import (
    AdminCopyReviewOpenRequest,
    AdminCopyReviewResolveRequest,
)
from ditto.api_server.continual_retest_settings import tie_weighting_is_active
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_copy_review import (
    open_copy_review,
    resolve_copy_review,
)
from ditto.api_server.endpoints.admin_quarantine import (
    BATCH_PREVIEW_TTL,
    require_admin,
)
from ditto.api_server.endpoints.public import _VALIDATOR_STALE_WINDOW
from ditto.api_server.endpoints.scoring import (
    _DETHRONE_BAND_CLAMP_PROTOCOL,
    _TIE_WEIGHTING_PROTOCOL,
)
from ditto.api_server.endpoints.validator import _current_koth_entries
from ditto.api_server.hippius import HippiusClient, normalize_object_key
from ditto.api_server.koth import (
    KothEntry,
    KothProjection,
    _ranked_entries,
    project_koth,
)
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
)
from ditto.db.models import Agent, AgentStatus, AthReview, AthReviewAction, Score
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.heartbeats import live_validator_fleet_supports_protocol
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ActorDep = Annotated[str | None, Header(alias="X-Admin-Actor", max_length=320)]

_TOKEN_VERSION = 1
_ACTOR_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_FINGERPRINT_TOP_N = 5


def _require_actor(x_admin_actor: str | None) -> str:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


def actor_slug(actor: str) -> str:
    """The operator-scoped path component of a rulings upload key."""
    slug = _ACTOR_SLUG_UNSAFE.sub("-", actor.strip().lower()).strip("-.")
    return (slug or "operator")[:64]


def _rulings_store(request: Request) -> HippiusClient:
    # Rulings share the PRIVATE inference-trace bucket (never the avatar
    # bucket, which may serve a public prefix). None leaves the upload path
    # answering 503 while inline previews keep working.
    client = getattr(request.app.state, "traces_hippius", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="rulings upload storage is not configured; preview inline",
        )
    return client


def _checked_upload_key(key: str, actor: str) -> str:
    normalized = normalize_object_key(key)
    scope = f"{ATH_RULINGS_KEY_PREFIX}{actor_slug(actor)}/"
    if not normalized.startswith(scope):
        raise HTTPException(
            status_code=403,
            detail=f"upload_key must be under this operator's prefix {scope}",
        )
    return normalized


def rulings_digest(rulings: list[AdminAthRuling]) -> str:
    """Canonical digest of a rulings list, identical for inline and uploads."""
    canonical = json.dumps(
        [ruling.model_dump(mode="json") for ruling in rulings],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def _load_uploaded_document(
    request: Request, key: str
) -> AdminAthRulingsDocument:
    client = _rulings_store(request)
    try:
        body = await client.get_object(key=key)
    except ObjectNotFoundError:
        raise HTTPException(
            status_code=404, detail="rulings upload not found; upload it first"
        ) from None
    except ObjectDownloadFailedError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if len(body) > ATH_RULINGS_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"rulings upload exceeds {ATH_RULINGS_MAX_BYTES} bytes",
        )
    try:
        raw = json.loads(body)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="rulings upload is not valid JSON"
        ) from None
    return _validate_document(raw)


def _validate_document(raw: object) -> AdminAthRulingsDocument:
    try:
        return AdminAthRulingsDocument.model_validate(raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()[:8]
        )
        raise HTTPException(
            status_code=422, detail=f"rulings document invalid: {problems}"
        ) from None


# --- crown arithmetic ------------------------------------------------------


@dataclass(frozen=True)
class BoardSnapshot:
    """One read (or one simulated step) of the folded board and its crown.

    ``entries`` are the validator-equivalent KOTH fold rows; ``distinct_hotkeys``
    and ``ceiling_band_clamp`` are the fleet-gated flags the public leaderboard
    projected them under. :meth:`without` and :meth:`with_estimate` re-project
    the same entries under the same flags so a batch can be walked item by
    item without touching Postgres.
    """

    bench_version: int
    read_at: datetime
    entries: tuple[KothEntry, ...]
    distinct_hotkeys: bool
    ceiling_band_clamp: bool
    projection: KothProjection | None
    ranked_agent_ids: tuple[UUID, ...]

    @classmethod
    def build(
        cls,
        *,
        bench_version: int,
        read_at: datetime,
        entries: Sequence[KothEntry],
        distinct_hotkeys: bool,
        ceiling_band_clamp: bool,
    ) -> BoardSnapshot:
        scored = tuple(entry for entry in entries if entry.composite > 0.0)
        return cls(
            bench_version=bench_version,
            read_at=read_at,
            entries=scored,
            distinct_hotkeys=distinct_hotkeys,
            ceiling_band_clamp=ceiling_band_clamp,
            projection=project_koth(
                scored,
                distinct_hotkeys=distinct_hotkeys,
                ceiling_band_clamp=ceiling_band_clamp,
            ),
            ranked_agent_ids=tuple(
                entry.agent_id for entry in (_ranked_entries(scored) if scored else [])
            ),
        )

    def _with_entries(self, entries: Sequence[KothEntry]) -> BoardSnapshot:
        return BoardSnapshot.build(
            bench_version=self.bench_version,
            read_at=self.read_at,
            entries=entries,
            distinct_hotkeys=self.distinct_hotkeys,
            ceiling_band_clamp=self.ceiling_band_clamp,
        )

    def without(self, agent_id: UUID) -> BoardSnapshot:
        """The board once ``agent_id`` leaves the eligible ledger (open/reject)."""
        return self._with_entries(
            [entry for entry in self.entries if entry.agent_id != agent_id]
        )

    def with_estimate(self, entry: KothEntry) -> BoardSnapshot:
        """The board once a cleared agent re-enters at ``entry`` (an estimate).

        The ledger keeps one representative per owner; a hotkey already on the
        board keeps its row unless the returning agent scores higher.
        """
        siblings = [e for e in self.entries if e.miner_hotkey == entry.miner_hotkey]
        if any(sibling.composite >= entry.composite for sibling in siblings):
            return self
        return self._with_entries(
            [*(e for e in self.entries if e.miner_hotkey != entry.miner_hotkey), entry]
        )

    @property
    def champion_agent_id(self) -> UUID | None:
        return None if self.projection is None else self.projection.champion.agent_id

    @property
    def raw_leader_agent_id(self) -> UUID | None:
        return None if self.projection is None else self.projection.raw_leader.agent_id

    def touches_crown(self, agent_id: UUID) -> bool:
        return agent_id in {self.champion_agent_id, self.raw_leader_agent_id}

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "champion": (
                    str(self.champion_agent_id) if self.champion_agent_id else None
                ),
                "raw_leader": (
                    str(self.raw_leader_agent_id) if self.raw_leader_agent_id else None
                ),
                "top": [str(x) for x in self.ranked_agent_ids[:_FINGERPRINT_TOP_N]],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def projection_model(self) -> AdminAthRulingsBoardProjection:
        champion = None if self.projection is None else self.projection.champion
        leader = None if self.projection is None else self.projection.raw_leader
        return AdminAthRulingsBoardProjection(
            bench_version=self.bench_version,
            read_at=self.read_at,
            ranked_count=len(self.ranked_agent_ids),
            champion_agent_id=champion.agent_id if champion else None,
            champion_hotkey=champion.miner_hotkey if champion else None,
            champion_score=champion.composite if champion else None,
            raw_leader_agent_id=leader.agent_id if leader else None,
            raw_leader_score=leader.composite if leader else None,
            fingerprint=self.fingerprint,
        )


async def read_board_snapshot(
    session: AsyncSession, request: Request, *, now: datetime | None = None
) -> BoardSnapshot:
    """Project the crown exactly as the public leaderboard and the ledger do.

    Entries come from :func:`_current_koth_entries` -- the eligible ledger with
    stderr, quorum, completed-wave confirmation and efficiency inputs, owner
    reduced -- and are folded under the two flags the public ``emissions``
    block derives from fleet readiness: ``distinct_hotkeys`` (tie weighting,
    protocol 20 plus the operator switch) and ``ceiling_band_clamp`` (protocol
    24, no switch). The clamp is what has flipped the live crown, so a bare
    ``project_koth(entries)`` here would disagree with the board the operator
    is reading. Held agents are outside the eligible ledger; a clear's effect
    is estimated in :func:`_crown_effect`.
    """
    read_at = now or datetime.now(UTC)
    active = await active_bench_version(session)
    state = request.app.state
    session_maker = getattr(state, "session_maker", None)
    continual_settings = await state.continual_retest_settings.resolve(session_maker)
    efficiency_config = await state.efficiency_settings.resolve(session_maker)
    tie_weighting_fleet_ready = await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=_TIE_WEIGHTING_PROTOCOL,
        bench_version=active,
        now=read_at,
        freshness=_VALIDATOR_STALE_WINDOW,
    )
    tie_weighting_active = tie_weighting_is_active(
        continual_settings, fleet_protocol_ready=tie_weighting_fleet_ready
    )
    ceiling_band_clamp = await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=_DETHRONE_BAND_CLAMP_PROTOCOL,
        bench_version=active,
        now=read_at,
        freshness=_VALIDATOR_STALE_WINDOW,
    )
    fold = await _current_koth_entries(
        session,
        canonical_version=active,
        wave_membership=continual_settings.wave_membership,
        efficiency_config=efficiency_config,
        now=read_at,
    )
    return BoardSnapshot.build(
        bench_version=active,
        read_at=read_at,
        entries=fold.folded_entries,
        distinct_hotkeys=tie_weighting_active,
        ceiling_band_clamp=ceiling_band_clamp,
    )


async def _crown_effect(
    session: AsyncSession,
    *,
    board: BoardSnapshot,
    agent: Agent,
    action: str,
) -> tuple[bool, BoardSnapshot]:
    """``(would_change_crown, board after this ruling)`` for one ready item.

    Holding or rejecting removes the agent from the fold; it moves the crown
    when the agent is the champion or the raw leader. Clearing is estimated:
    the held agent re-enters at its canonical quorum median (the median-score
    rule the anti-copy gate uses) anchored at its upload time, and moves the
    crown when that re-projected board makes it champion or raw leader.
    """
    if action in {"open", "reject"}:
        return board.touches_crown(agent.agent_id), board.without(agent.agent_id)
    composites = list(
        (
            await session.execute(
                select(Score.composite, Score.validator_hotkey).where(
                    Score.agent_id == agent.agent_id,
                    Score.bench_version == board.bench_version,
                    Score.n >= MIN_ELIGIBLE_CASES,
                    Score.composite > 0.0,
                )
            )
        ).all()
    )
    if not composites:
        return False, board
    ordered = sorted(composites, key=lambda row: (row[0], row[1]))
    canonical = float(ordered[(len(ordered) - 1) // 2][0])
    after = board.with_estimate(
        KothEntry(
            miner_hotkey=agent.miner_hotkey,
            agent_id=agent.agent_id,
            composite=canonical,
            first_seen=agent.created_at,
            raw_rank=0,
            bench_version=board.bench_version,
        )
    )
    return after.touches_crown(agent.agent_id), after


# --- preview token ---------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign_preview(secret: str, payload: dict[str, Any], issued_at: int) -> str:
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{issued_at}.{body}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"{signed}.{digest}"


def _verify_preview(token: str, secret: str, actor: str) -> dict[str, Any]:
    try:
        issued_text, body, digest = token.split(".", 2)
        issued_at = int(issued_text)
        payload = json.loads(_unb64(body))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid preview token") from None
    expected = hmac.new(
        secret.encode(), f"{issued_at}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    # Bytes: the str form raises TypeError on a non-ASCII token segment.
    if not secrets.compare_digest(digest.encode(), expected.encode()):
        raise HTTPException(status_code=409, detail="preview token signature mismatch")
    now = int(datetime.now(UTC).timestamp())
    if issued_at > now + 30 or now - issued_at > int(BATCH_PREVIEW_TTL.total_seconds()):
        raise HTTPException(status_code=409, detail="preview expired; preview again")
    if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
        raise HTTPException(status_code=422, detail="unsupported preview token")
    if payload.get("actor") != actor:
        raise HTTPException(
            status_code=409, detail="preview token was issued to another operator"
        )
    return payload


# --- per-item preview ------------------------------------------------------


async def _preview_ruling(
    session: AsyncSession,
    *,
    index: int,
    ruling: AdminAthRuling,
    board: BoardSnapshot,
) -> tuple[AdminAthRulingPreviewItem, BoardSnapshot]:
    """Classify one ruling against ``board``; also return the board after it.

    A non-ready item leaves the board as it is. A ready item's
    ``would_change_crown`` is judged against ``board`` -- the state after the
    earlier items of the batch -- and the returned board carries its effect
    forward so the next item is judged after this one.
    """

    def _blocked(**item: Any) -> tuple[AdminAthRulingPreviewItem, BoardSnapshot]:
        return AdminAthRulingPreviewItem(**item), board

    base: dict[str, Any] = {
        "index": index,
        "action": ruling.action,
        "agent_id": ruling.agent_id,
        "reason": ruling.reason,
        "evidence_references": list(ruling.evidence_references),
        "would_change_crown": False,
        "stale_guard": False,
    }
    agent = await session.get(Agent, ruling.agent_id)
    if agent is None:
        return _blocked(
            **base,
            ok=False,
            disposition="not_found",
            conflict_reason="agent not found",
            message="agent not found",
        )
    score_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(Score.agent_id == agent.agent_id)
        )
        or 0
    )
    review = await session.scalar(
        select(AthReview).where(AthReview.agent_id == agent.agent_id)
    )
    base.update(
        agent_name=agent.name,
        agent_version=agent.version,
        miner_hotkey=agent.miner_hotkey,
        agent_status=agent.status.value,
        artifact_sha256=agent.sha256,
        score_count=score_count,
    )
    if agent.sha256 != ruling.expected_sha256:
        base["stale_guard"] = True
        return _blocked(
            **base,
            ok=False,
            disposition="stale_guard",
            conflict_reason="artifact sha256 changed",
            message="artifact sha256 changed since the ruling was prepared",
        )
    if score_count != ruling.expected_score_count:
        base["stale_guard"] = True
        return _blocked(
            **base,
            ok=False,
            disposition="stale_guard",
            conflict_reason="score count changed",
            message="score count changed since the ruling was prepared",
        )
    if ruling.action == "reject" and not ruling.evidence_references:
        return _blocked(
            **base,
            ok=False,
            disposition="invalid",
            conflict_reason="reject requires evidence_references",
            message="a reject ruling must cite at least one path:line",
        )

    held = (
        agent.status == AgentStatus.ATH_PENDING_REVIEW
        and review is not None
        and review.status == "pending"
    )
    if held and review is not None and ruling.action != "open":
        # resolve_copy_review refuses a hold whose evidence moved under the
        # review (admin_copy_review.py); answer the same 409 detail here so a
        # "ready" item cannot fail at execute on a guard the preview could see.
        if agent.duplicate_of != review.original_duplicate_of:
            return _blocked(
                **base,
                ok=False,
                disposition="conflict",
                conflict_reason="agent hold evidence no longer matches review",
                message="resolve_ath_review would answer 409",
            )
        if agent.review_reason != review.original_reason:
            return _blocked(
                **base,
                ok=False,
                disposition="conflict",
                conflict_reason="agent hold reason no longer matches review",
                message="resolve_ath_review would answer 409",
            )
    scored_or_live = agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
    resolved_as = review.resolution if review and review.status == "resolved" else None
    steps: list[Literal["open", "clear", "reject"]]
    if ruling.action == "open":
        if held:
            return _blocked(
                **base,
                ok=True,
                disposition="already_applied",
                message="agent is already held in ath_pending_review",
            )
        if not scored_or_live:
            return _blocked(
                **base,
                ok=False,
                disposition="conflict",
                conflict_reason=f"agent is {agent.status.value}, not scored or live",
                message="open_ath_review would answer 409",
            )
        steps = ["open"]
    elif ruling.action == "clear":
        if held:
            steps = ["clear"]
        elif resolved_as == "clear" and scored_or_live:
            return _blocked(
                **base,
                ok=True,
                disposition="already_applied",
                message="review already resolved clear",
            )
        else:
            reason = (
                "agent is held but its review is not pending"
                if agent.status == AgentStatus.ATH_PENDING_REVIEW
                else f"agent is {agent.status.value}, not held"
            )
            return _blocked(
                **base,
                ok=False,
                disposition="conflict",
                conflict_reason=reason,
                message="resolve_ath_review would answer 409",
            )
    else:  # reject
        if held:
            steps = ["reject"]
        elif scored_or_live:
            steps = ["open", "reject"]
        elif resolved_as == "reject" and agent.status == AgentStatus.BANNED:
            return _blocked(
                **base,
                ok=True,
                disposition="already_applied",
                message="review already resolved reject; agent is banned",
            )
        else:
            return _blocked(
                **base,
                ok=False,
                disposition="conflict",
                conflict_reason=(
                    f"agent is {agent.status.value}, not held, scored, or live"
                ),
                message="neither open nor resolve would be accepted",
            )
    base["would_change_crown"], after = await _crown_effect(
        session, board=board, agent=agent, action=ruling.action
    )
    return (
        AdminAthRulingPreviewItem(
            **base,
            ok=True,
            disposition="ready",
            steps=steps,
            message="will " + " then ".join(steps),
        ),
        after,
    )


def _preview_payload(
    *,
    actor: str,
    upload_key: str | None,
    source: str | None,
    digest: str,
    board: BoardSnapshot,
    items: list[AdminAthRulingPreviewItem],
) -> dict[str, Any]:
    return {
        "v": _TOKEN_VERSION,
        "actor": actor,
        "key": upload_key,
        # An inline preview's provenance travels in the token: execute is not
        # asked for it again, and the audit annotation must still carry it.
        "source": source,
        "digest": digest,
        "champion": str(board.champion_agent_id) if board.champion_agent_id else None,
        "raw_leader": (
            str(board.raw_leader_agent_id) if board.raw_leader_agent_id else None
        ),
        "crown": sorted(
            str(item.agent_id) for item in items if item.would_change_crown
        ),
    }


# --- routes ----------------------------------------------------------------


@router.post("/ath-rulings/upload-url", response_model=AdminAthRulingsUploadResponse)
async def create_ath_rulings_upload(
    payload: AdminAthRulingsUploadRequest,
    request: Request,
    _admin: AdminDep,
    x_admin_actor: ActorDep = None,
) -> AdminAthRulingsUploadResponse:
    """Issue a five-minute presigned PUT for one rulings document."""
    actor = _require_actor(x_admin_actor)
    client = _rulings_store(request)
    now = datetime.now(UTC)
    key = f"{ATH_RULINGS_KEY_PREFIX}{actor_slug(actor)}/{now:%Y-%m-%d}/{uuid4()}.json"
    url = await client.presigned_put_url(
        key=key,
        content_type=payload.content_type,
        expires_in=ATH_RULINGS_UPLOAD_TTL_SECONDS,
    )
    logger.info("admin_actor=%s issued ath rulings upload key=%s", actor, key)
    return AdminAthRulingsUploadResponse(
        bucket=client.bucket,
        key=key,
        url=url,
        content_type=payload.content_type,
        expires_in=ATH_RULINGS_UPLOAD_TTL_SECONDS,
        max_bytes=ATH_RULINGS_MAX_BYTES,
    )


async def _resolve_document(
    request: Request,
    *,
    actor: str,
    upload_key: str | None,
    rulings: list[AdminAthRuling] | None,
    source: str | None,
) -> tuple[AdminAthRulingsDocument, str | None]:
    if upload_key is not None:
        key = _checked_upload_key(upload_key, actor)
        return await _load_uploaded_document(request, key), key
    assert rulings is not None
    return _validate_document({"rulings": rulings, "source": source}), None


@router.post(
    "/ath-rulings/batch-preview", response_model=AdminAthRulingsPreviewResponse
)
async def preview_ath_rulings_batch(
    payload: AdminAthRulingsPreviewRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: ActorDep = None,
) -> AdminAthRulingsPreviewResponse:
    """Dry-run every ruling against live state; this never mutates."""
    actor = _require_actor(x_admin_actor)
    document, key = await _resolve_document(
        request,
        actor=actor,
        upload_key=payload.upload_key,
        rulings=payload.rulings,
        source=payload.source,
    )
    digest = rulings_digest(document.rulings)
    board = await read_board_snapshot(session, request)
    # Walk the batch cumulatively: item i is judged against the board after
    # items < i, so a champion reject flags the runner-up its successor
    # becomes, and a clear behind a reject is measured against the leader that
    # reject leaves behind.
    items: list[AdminAthRulingPreviewItem] = []
    simulated = board
    for index, ruling in enumerate(document.rulings):
        item, simulated = await _preview_ruling(
            session, index=index, ruling=ruling, board=simulated
        )
        items.append(item)
    # A read-only preview must not leave the implicit transaction open.
    await session.rollback()
    issued_at = int(datetime.now(UTC).timestamp())
    secret = request.app.state.config.admin_api_token
    assert secret is not None
    token = _sign_preview(
        secret,
        _preview_payload(
            actor=actor,
            upload_key=key,
            source=document.source,
            digest=digest,
            board=board,
            items=items,
        ),
        issued_at,
    )
    return AdminAthRulingsPreviewResponse(
        preview_token=token,
        expires_at=datetime.fromtimestamp(issued_at, UTC) + BATCH_PREVIEW_TTL,
        rulings_sha256=digest,
        upload_key=key,
        source=document.source,
        board=board.projection_model(),
        items=items,
        ready_count=sum(item.disposition == "ready" for item in items),
        already_applied_count=sum(
            item.disposition == "already_applied" for item in items
        ),
        blocked_count=sum(
            item.disposition not in {"ready", "already_applied"} for item in items
        ),
        crown_moving_count=sum(item.would_change_crown for item in items),
    )


async def _annotate_batch_audit(
    session: AsyncSession,
    *,
    agent_id: UUID,
    actor: str,
    started_at: datetime,
    annotation: dict[str, Any],
) -> None:
    """Attach the batch identity and citations to the audit rows just written.

    ``open_copy_review`` / ``resolve_copy_review`` record the operator action;
    this adds ``batch_id``, ``evidence_references`` and the document digest so
    a ruling can be traced back to the exact previewed batch. Additive JSON
    merges only -- nothing the two routes wrote is rewritten.
    """
    async with session.begin():
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id).with_for_update()
        )
        if review is None:
            return
        review.algorithm_provenance = {
            **review.algorithm_provenance,
            "last_batch_ruling": annotation,
        }
        actions = (
            await session.scalars(
                select(AthReviewAction)
                .where(
                    AthReviewAction.review_id == review.review_id,
                    AthReviewAction.actor == actor,
                    AthReviewAction.created_at >= started_at,
                )
                .with_for_update()
            )
        ).all()
        for action in actions:
            action.evidence = {**action.evidence, "batch_ruling": annotation}


async def _apply_step(
    session: AsyncSession,
    *,
    ruling: AdminAthRuling,
    step: Literal["open", "clear", "reject"],
    actor: str,
) -> None:
    """Apply one step through the only two writers, then end their autobegin."""
    if step == "open":
        await open_copy_review(
            ruling.agent_id,
            AdminCopyReviewOpenRequest(
                expected_sha256=ruling.expected_sha256,
                expected_score_count=ruling.expected_score_count,
                reason=ruling.reason,
            ),
            None,
            session,
            x_admin_actor=actor,
        )
    else:
        await resolve_copy_review(
            ruling.agent_id,
            AdminCopyReviewResolveRequest(resolution=step, reason=ruling.reason),
            None,
            session,
            x_admin_actor=actor,
        )
    # Both routes read coldkeys after their own transaction commits, which
    # autobegins another; end it before the next begin().
    await session.rollback()


async def _annotate_applied(
    session: AsyncSession,
    *,
    agent_id: UUID,
    actor: str,
    started_at: datetime,
    annotation: dict[str, Any],
) -> tuple[bool, str]:
    """``(annotated, message)`` for a ruling that has already landed."""
    try:
        await _annotate_batch_audit(
            session,
            agent_id=agent_id,
            actor=actor,
            started_at=started_at,
            annotation=annotation,
        )
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "ath rulings batch annotation failed actor=%s batch_id=%s index=%s"
            " agent_id=%s",
            actor,
            annotation.get("batch_id"),
            annotation.get("index"),
            agent_id,
        )
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        return False, f"ruling applied; audit annotation failed: {detail}"
    return True, "ruling applied and audit rows annotated"


@router.post(
    "/ath-rulings/batch-execute", response_model=AdminAthRulingsExecuteResponse
)
async def execute_ath_rulings_batch(
    payload: AdminAthRulingsExecuteRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: ActorDep = None,
) -> AdminAthRulingsExecuteResponse:
    """Apply the exact previewed rulings; each item is audited on its own.

    Refuses an item whose identity guards moved, or whose crown outcome
    (``would_change_crown``, champion, raw leader) differs from the preview.
    Failures never hide successful rows.
    """
    actor = _require_actor(x_admin_actor)
    if payload.confirmation != ATH_RULINGS_CONFIRMATION:
        raise HTTPException(status_code=422, detail="confirmation phrase mismatch")
    secret = request.app.state.config.admin_api_token
    assert secret is not None
    token = _verify_preview(payload.preview_token, secret, actor)
    key = token.get("key")
    if key is None and payload.rulings is None:
        raise HTTPException(
            status_code=422,
            detail="this preview was inline; resend the same rulings with the token",
        )
    document, key = await _resolve_document(
        request,
        actor=actor,
        upload_key=key,
        rulings=payload.rulings if key is None else None,
        source=token.get("source"),
    )
    digest = rulings_digest(document.rulings)
    if digest != token.get("digest"):
        raise HTTPException(
            status_code=409, detail="rulings changed after preview; preview again"
        )
    previewed_crown = set(token.get("crown") or [])
    previewed_champion = token.get("champion")
    previewed_leader = token.get("raw_leader")

    batch_id = uuid4()
    board_before = await read_board_snapshot(session, request)
    await session.rollback()
    crown_moved = (
        (
            str(board_before.champion_agent_id)
            if board_before.champion_agent_id
            else None
        )
        != previewed_champion
    ) or (
        (
            str(board_before.raw_leader_agent_id)
            if board_before.raw_leader_agent_id
            else None
        )
        != previewed_leader
    )
    results: list[AdminAthRulingExecuteItem] = []
    # The board each item is judged against: the pre-batch read, then a fresh
    # Postgres read after every ruling that lands. Execute never trusts the
    # preview's simulation of the batch's own writes -- it re-reads the real
    # fold and refuses an item whose flag no longer matches what was confirmed.
    # None means a post-write re-read failed; later ready items are refused.
    board_now: BoardSnapshot | None = board_before
    for index, ruling in enumerate(document.rulings):
        started_at = datetime.now(UTC)
        preview, _projected = await _preview_ruling(
            session, index=index, ruling=ruling, board=board_now or board_before
        )
        await session.rollback()
        base: dict[str, Any] = {
            "index": index,
            "action": ruling.action,
            "agent_id": ruling.agent_id,
            "agent_status": preview.agent_status,
            "would_change_crown": preview.would_change_crown,
        }
        if preview.disposition == "already_applied":
            results.append(
                AdminAthRulingExecuteItem(
                    **base, status="already_applied", message=preview.message
                )
            )
            continue
        if preview.disposition != "ready":
            results.append(
                AdminAthRulingExecuteItem(
                    **base,
                    status="failed",
                    message=preview.conflict_reason or preview.message,
                )
            )
            continue
        if board_now is None:
            results.append(
                AdminAthRulingExecuteItem(
                    **base,
                    status="failed",
                    message=(
                        "board re-read failed after an earlier ruling landed; "
                        "preview again"
                    ),
                )
            )
            continue
        flagged_now = preview.would_change_crown
        flagged_then = str(ruling.agent_id) in previewed_crown
        if flagged_now != flagged_then or (
            crown_moved and (flagged_now or flagged_then)
        ):
            results.append(
                AdminAthRulingExecuteItem(
                    **base,
                    status="failed",
                    message=(
                        "crown arithmetic moved since preview "
                        "(champion, raw leader, or this ruling's crown effect "
                        "changed); preview again"
                    ),
                )
            )
            continue

        steps_applied: list[Literal["open", "clear", "reject"]] = []
        try:
            for step in preview.steps:
                await _apply_step(session, ruling=ruling, step=step, actor=actor)
                steps_applied.append(step)
        except HTTPException as exc:
            await session.rollback()
            results.append(
                AdminAthRulingExecuteItem(
                    **base,
                    status="failed",
                    steps_applied=steps_applied,
                    message=str(exc.detail),
                )
            )
            continue
        except Exception:
            await session.rollback()
            logger.exception(
                "ath rulings batch failed actor=%s batch_id=%s index=%s agent_id=%s",
                actor,
                batch_id,
                index,
                ruling.agent_id,
            )
            results.append(
                AdminAthRulingExecuteItem(
                    **base,
                    status="failed",
                    steps_applied=steps_applied,
                    message="internal error while applying ruling",
                )
            )
            continue

        # The ruling landed. Whatever happens below, the row reports "applied":
        # an operator reading "failed" would hand-run the single tool again or
        # tell the miner nothing changed.
        annotated, message = await _annotate_applied(
            session,
            agent_id=ruling.agent_id,
            actor=actor,
            started_at=started_at,
            annotation={
                "batch_id": str(batch_id),
                "index": index,
                "action": ruling.action,
                "rulings_sha256": digest,
                "upload_key": key,
                "source": document.source,
                "evidence_references": list(ruling.evidence_references),
                "would_change_crown": preview.would_change_crown,
                "board_fingerprint": board_now.fingerprint,
            },
        )
        try:
            agent = await session.get(Agent, ruling.agent_id)
            # Read before the rollback expires the instance's attributes.
            base["agent_status"] = agent.status.value if agent is not None else None
            await session.rollback()
            board_now = await read_board_snapshot(session, request)
            await session.rollback()
        except Exception:
            await session.rollback()
            logger.exception(
                "ath rulings batch board re-read failed actor=%s batch_id=%s"
                " index=%s agent_id=%s",
                actor,
                batch_id,
                index,
                ruling.agent_id,
            )
            board_now = None
            message = f"{message}; board re-read failed, later items are refused"
        logger.info(
            "ath rulings batch actor=%s batch_id=%s index=%s action=%s agent_id=%s"
            " steps=%s crown=%s annotated=%s",
            actor,
            batch_id,
            index,
            ruling.action,
            ruling.agent_id,
            ",".join(steps_applied),
            preview.would_change_crown,
            annotated,
        )
        results.append(
            AdminAthRulingExecuteItem(
                **base,
                status="applied",
                steps_applied=steps_applied,
                annotated=annotated,
                message=message,
            )
        )
    board_after = await read_board_snapshot(session, request)
    await session.rollback()
    return AdminAthRulingsExecuteResponse(
        batch_id=batch_id,
        rulings_sha256=digest,
        upload_key=key,
        board_before=board_before.projection_model(),
        board_after=board_after.projection_model(),
        items=results,
        applied_count=sum(item.status == "applied" for item in results),
        already_applied_count=sum(item.status == "already_applied" for item in results),
        failed_count=sum(item.status == "failed" for item in results),
    )
