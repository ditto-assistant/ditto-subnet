"""Pinned public CLA documents, signed receipts, and audited rollout controls."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import false, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.contributor_identity import (
    ClaControl,
    ClaPublishRequest,
    ClaSettingsRequest,
    ClaSignatureView,
    ClaSignRequest,
    ClaStatus,
    ClaVersionView,
    cla_message,
)
from ditto.api_models.contributor_identity import (
    ClaSettingsRevision as SettingsView,
)
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.api_server.contributor_identity import (
    ContributorProofRejected,
    check_contributor_proof,
    finalized_owner,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.miner_github_link import no_store
from ditto.chain import ChainError
from ditto.db.models import (
    Agent,
    AgentStatus,
    ClaSettingsRevision,
    ClaShadowUpload,
    ClaSignature,
    ClaVersion,
    SubmissionRetirement,
)
from ditto.db.queries.contributor_identity import (
    current_cla_settings,
    current_cla_signature,
    current_cla_version,
    lock_identity_writes,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
router = APIRouter(tags=["contributor-cla"], dependencies=[Depends(no_store)])
ENFORCED_CLA_NOTICE = timedelta(hours=24)


def enforced_cla_notice_deadline(mode: str, *, now: datetime) -> datetime | None:
    return now + ENFORCED_CLA_NOTICE if mode == "enforce" else None


async def earliest_cla_enforcement(
    session: AsyncSession, *, now: datetime
) -> datetime | None:
    """When enforcement may start, or None without an effective version.

    Every current or scheduled version must have been published at least the
    notice period before enforcement first applies to it: the current version
    from the moment enforcement starts, a scheduled one from its effective date.
    """
    current = await current_cla_version(session, now=now)
    if current is None:
        return None
    earliest = now
    for row in await session.scalars(
        select(ClaVersion).where(ClaVersion.version >= current.version)
    ):
        noticed_at = row.published_at + ENFORCED_CLA_NOTICE
        if row.effective_at < noticed_at:
            earliest = max(earliest, noticed_at)
    return earliest


def version_view(row: ClaVersion) -> ClaVersionView:
    return ClaVersionView(
        version=row.version,
        sha256=row.sha256,
        published_at=row.published_at,
        effective_at=row.effective_at,
    )


def signature_view(row: ClaSignature) -> ClaSignatureView:
    return ClaSignatureView.model_validate(
        {name: getattr(row, name) for name in ClaSignatureView.model_fields}
    )


def settings_view(row: ClaSettingsRevision) -> SettingsView:
    return SettingsView.model_validate(
        {name: getattr(row, name) for name in SettingsView.model_fields}
    )


async def cla_status(
    session: AsyncSession, *, netuid: int, hotkey: str | None = None
) -> ClaStatus:
    settings = await current_cla_settings(session)
    version = await current_cla_version(session)
    signature = (
        await current_cla_signature(
            session, netuid=netuid, hotkey=hotkey, version=version
        )
        if hotkey
        else None
    )
    is_current = signature is not None
    if hotkey and signature is None:
        signature = await session.scalar(
            select(ClaSignature)
            .where(ClaSignature.netuid == netuid, ClaSignature.hotkey == hotkey)
            .order_by(ClaSignature.signed_at.desc(), ClaSignature.signature_id)
            .limit(1)
        )
    return ClaStatus(
        netuid=netuid,
        mode=settings.mode,
        current=version_view(version) if version else None,
        signature=signature_view(signature) if signature else None,
        is_current=is_current,
    )


@router.get("/public/cla", response_model=ClaStatus)
async def current_document(request: Request, session: SessionDep) -> ClaStatus:
    return await cla_status(session, netuid=request.app.state.config.chain.netuid)


@router.get("/public/cla/status/{hotkey}", response_model=ClaStatus)
async def public_status(
    hotkey: str, request: Request, session: SessionDep
) -> ClaStatus:
    return await cla_status(
        session, netuid=request.app.state.config.chain.netuid, hotkey=hotkey
    )


@router.get(
    "/public/cla/versions/{version}",
    response_model=ClaVersionView,
    responses={
        404: {
            "model": HttpErrorEnvelope,
            "description": "CLA version is not published.",
        }
    },
)
async def public_version(version: int, session: SessionDep) -> ClaVersionView:
    row = await session.get(ClaVersion, version)
    if row is None:
        raise HTTPException(404, "CLA version is not published")
    return version_view(row)


@router.get(
    "/public/cla/versions/{version}/text",
    response_class=Response,
    responses={
        200: {
            "description": "Exact published UTF-8 agreement.",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        304: {"description": "Agreement matches If-None-Match; no response body."},
        404: {
            "model": HttpErrorEnvelope,
            "description": "CLA version is not published.",
        },
    },
)
async def public_text(
    version: int,
    session: SessionDep,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    row = await session.get(ClaVersion, version)
    if row is None:
        raise HTTPException(404, "CLA version is not published")
    etag = f'"{row.sha256}"'
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    validators = (if_none_match or "").split(",")
    if any(
        value.strip() == "*" or value.strip().removeprefix("W/") == etag
        for value in validators
    ):
        return Response(status_code=304, headers=headers)
    return Response(
        content=row.text.encode("utf-8"), media_type="text/plain", headers=headers
    )


@router.post(
    "/cla/sign",
    response_model=ClaSignatureView,
    responses={status: {"model": HttpErrorEnvelope} for status in (400, 409, 429, 503)},
)
async def sign_cla(
    body: ClaSignRequest, request: Request, session: SessionDep
) -> ClaSignatureView:
    now = datetime.now(UTC)
    if body.netuid != request.app.state.config.chain.netuid:
        raise HTTPException(400, "CLA netuid does not match this deployment")
    try:
        check_contributor_proof(
            payload=cla_message(
                netuid=body.netuid,
                cla_version=body.cla_version,
                cla_sha256=body.cla_sha256,
                hotkey=body.hotkey,
                nonce=body.nonce,
                issued_at=body.issued_at,
            ),
            hotkey=body.hotkey,
            key_kind="hotkey",
            signer=body.hotkey,
            signature=body.signature,
            issued_at=body.issued_at,
            now=now,
        )
        # Reject unpublished/mismatched documents and known replays before
        # spending chain RPCs. The insert constraint still closes nonce races.
        async with session.begin():
            version = await session.get(ClaVersion, body.cla_version)
            if version is None or version.sha256 != body.cla_sha256:
                raise HTTPException(
                    409, "CLA version or digest is not the published document"
                )
            if (
                await session.scalar(
                    select(ClaSignature.signature_id).where(
                        ClaSignature.nonce == body.nonce
                    )
                )
                is not None
            ):
                raise HTTPException(409, "CLA signature nonce already used")
        session.expire_all()
        owner = await finalized_owner(request.app.state.chain, body.hotkey)
        async with session.begin():
            version = await session.get(ClaVersion, body.cla_version)
            if version is None or version.sha256 != body.cla_sha256:
                raise HTTPException(
                    409, "CLA version or digest is not the published document"
                )
            row = ClaSignature(
                netuid=body.netuid,
                hotkey=body.hotkey,
                coldkey=owner.coldkey,
                cla_version=body.cla_version,
                cla_sha256=body.cla_sha256,
                signature=body.signature,
                nonce=body.nonce,
                issued_at=body.issued_at,
                signed_at=now,
                owner_block=owner.block,
                owner_block_hash=owner.block_hash,
            )
            session.add(row)
            await session.flush()
            view = signature_view(row)
        return view
    except ContributorProofRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    except ChainError:
        raise HTTPException(
            503, "finalized ownership unavailable; retry shortly"
        ) from None
    except IntegrityError as exc:
        # SQLAlchemy's asyncpg adapter chains the original PostgreSQL error.
        driver_error = getattr(exc.orig, "__cause__", None)
        if (
            getattr(driver_error, "sqlstate", None) == "23505"
            and getattr(driver_error, "constraint_name", None)
            == "uq_cla_signatures_nonce"
        ):
            raise HTTPException(409, "CLA signature nonce already used") from None
        raise


@router.post("/admin/cla/versions", response_model=ClaVersionView)
async def publish(
    body: ClaPublishRequest, _admin: AdminDep, session: SessionDep
) -> ClaVersionView:
    raw = body.text.encode("utf-8")
    if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != body.sha256:
        raise HTTPException(
            400, "CLA text does not match the supplied digest or exceeds the size limit"
        )
    if body.confirmation != f"PUBLISH CLA {body.version} {body.sha256}":
        raise HTTPException(
            409, "explicit CLA version and digest confirmation is required"
        )
    async with session.begin():
        await lock_identity_writes(session, namespace="cla-policy")
        settings = await current_cla_settings(session)
        now = datetime.now(UTC)
        notice_deadline = enforced_cla_notice_deadline(settings.mode, now=now)
        if notice_deadline is not None and body.effective_at < notice_deadline:
            raise HTTPException(
                409, "CLA changes under enforcement require at least 24 hours notice"
            )
        previous = await session.scalar(
            select(ClaVersion).order_by(ClaVersion.version.desc()).limit(1)
        )
        if previous and (
            body.version <= previous.version
            or body.effective_at < previous.effective_at
        ):
            raise HTTPException(
                409,
                "CLA versions must increase and effective dates "
                "must not move backwards",
            )
        row = ClaVersion(
            version=body.version,
            sha256=body.sha256,
            text=body.text,
            published_at=now,
            effective_at=body.effective_at,
            actor=body.actor,
            reason=body.reason,
        )
        session.add(row)
        await session.flush()
        return version_view(row)


@router.get("/admin/cla/settings", response_model=ClaControl)
async def control(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    history_limit: Annotated[int, Query(ge=1, le=100)] = 25,
    history_offset: Annotated[int, Query(ge=0)] = 0,
) -> ClaControl:
    now = datetime.now(UTC)
    settings = await current_cla_settings(session)
    version = await current_cla_version(session, now=now)
    history = list(
        await session.scalars(
            select(ClaSettingsRevision)
            .order_by(ClaSettingsRevision.revision.desc())
            .offset(history_offset)
            .limit(history_limit)
        )
    )
    total = int(
        await session.scalar(select(func.count()).select_from(ClaSettingsRevision)) or 0
    )
    # Agents are this Platform's single-subnet corpus and have no netuid column.
    # Consent proofs use the deployment's current signing context below.
    active = (
        select(Agent.miner_hotkey.label("hotkey"))
        .where(
            Agent.status.not_in((AgentStatus.REJECTED, AgentStatus.BANNED)),
            ~select(SubmissionRetirement.agent_id)
            .where(SubmissionRetirement.agent_id == Agent.agent_id)
            .exists(),
        )
        .distinct()
        .subquery()
    )
    signed = (
        select(ClaSignature.signature_id)
        .where(
            ClaSignature.hotkey == active.c.hotkey,
            ClaSignature.netuid == request.app.state.config.chain.netuid,
            ClaSignature.cla_version == version.version,
            ClaSignature.cla_sha256 == version.sha256,
        )
        .exists()
        if version
        else false()
    )
    # One statement snapshot keeps signed <= total even during new uploads.
    counts = (
        await session.execute(
            select(func.count(), func.count().filter(signed)).select_from(active)
        )
    ).one()
    active_count, signed_count = map(int, counts)
    shadow_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ClaShadowUpload)
            .where(ClaShadowUpload.netuid == request.app.state.config.chain.netuid)
        )
        or 0
    )
    return ClaControl(
        current=settings_view(settings),
        history=[settings_view(row) for row in history],
        effective_version=version_view(version) if version else None,
        active_hotkeys=active_count,
        signed_active_hotkeys=signed_count,
        unsigned_active_hotkeys=active_count - signed_count,
        unsigned_shadow_uploads=shadow_count,
        history_total=total,
        history_limit=history_limit,
        history_offset=history_offset,
        history_has_more=history_offset + len(history) < total,
        minimum_enforced_notice_seconds=int(ENFORCED_CLA_NOTICE.total_seconds()),
        earliest_enforced_effective_at=enforced_cla_notice_deadline(
            settings.mode, now=now
        ),
        earliest_enforcement_at=(
            None
            if settings.mode == "enforce"
            else await earliest_cla_enforcement(session, now=now)
        ),
    )


@router.post("/admin/cla/settings", response_model=SettingsView)
async def set_policy(
    body: ClaSettingsRequest, _admin: AdminDep, session: SessionDep
) -> SettingsView:
    if body.confirmation != f"SET CLA GATE {body.mode.upper()}":
        raise HTTPException(409, "explicit CLA gate confirmation is required")
    async with session.begin():
        await lock_identity_writes(session, namespace="cla-policy")
        previous = await current_cla_settings(session)
        if previous.revision != body.expected_revision:
            raise HTTPException(
                409, "CLA policy revision changed; refresh before applying"
            )
        if body.mode == "enforce" and previous.mode != "enforce":
            now = datetime.now(UTC)
            earliest = await earliest_cla_enforcement(session, now=now)
            if earliest is None:
                raise HTTPException(
                    409, "publish an effective approved CLA before enforcing"
                )
            if earliest > now:
                raise HTTPException(
                    409,
                    "CLA enforcement requires 24 hours notice for every current "
                    f"or scheduled version; earliest allowed {earliest.isoformat()}",
                )
        row = ClaSettingsRevision(
            revision=previous.revision + 1,
            parent_revision=previous.revision,
            mode=body.mode,
            actor=body.actor,
            reason=body.reason,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return settings_view(row)
