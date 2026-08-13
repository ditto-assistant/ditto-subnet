"""Private validator transport for one bounded Bench v9 confirmation bundle.

Nothing in this module participates in canonical benchmark leasing, ordinary
score submission, continual retests, rollout activation, or the reward ledger.
The production default is closed: claims return 204 until the process owns an
exact :class:`ConfirmationVerificationProfile` registry entry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationBundleState,
)
from ditto.api_models.validator import ArtifactResponse
from ditto.api_models.validator_confirmation import (
    ConfirmationExecutionProfile,
    ConfirmationInferenceGrantOffer,
    V9ConfirmationClaimRequest,
    V9ConfirmationFailRequest,
    V9ConfirmationFailResponse,
    V9ConfirmationJobResponse,
    V9ConfirmationPreparedReport,
    V9ConfirmationPrepareRequest,
    V9ConfirmationSubmitRequest,
    V9ConfirmationSubmitResponse,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.confirmation_candidate_reconciliation import (
    reconcile_v9_confirmation_candidates,
)
from ditto.api_server.confirmation_evidence import (
    ConfirmationEvidenceError,
    ConfirmationVerificationProfile,
    rebuild_confirmation_evidence,
)
from ditto.api_server.confirmation_wire import (
    ConfirmationWireError,
    completion_report_from_go_dimensions,
)
from ditto.api_server.dependencies import (
    get_chain_client,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints.validator import (
    _ARTIFACT_URL_TTL,
    _JOB_REQUEST_MAX_AGE,
    ValidatorAuthError,
    _artifact_key,
    _assert_validator_permitted,
    _screened_image_key,
)
from ditto.api_server.storage import S3StorageClient
from ditto.chain import ChainClient
from ditto.db.models import (
    Agent,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
)
from ditto.db.queries.confirmation_attempt_lock import lock_confirmation_attempt
from ditto.db.queries.confirmation_bundles import (
    ConfirmationBundlePersistenceError,
    StaleConfirmationBudget,
    complete_confirmation_bundle,
    issue_confirmation_bundle_ticket,
    latest_confirmation_bundle_settings_revision,
    lock_confirmation_budget_day,
    reserve_confirmation_bundle_budget,
    settle_confirmation_bundle_budget,
)
from ditto.db.queries.confirmation_inference import (
    ensure_confirmation_inference_grants,
)
from ditto.db.queries.confirmation_policy_lock import lock_confirmation_policy
from ditto.db.queries.confirmation_ticket_recovery import (
    expire_overdue_confirmation_bundle_tickets,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)
from ditto.db.queries.validator_slot_occupancy import (
    live_v9_confirmation_slot_ticket,
    lock_validator_slot,
)

router = APIRouter(prefix="/validator/v9-confirmation", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]

_PURPOSE: Literal["v9_confirmation_bundle"] = "v9_confirmation_bundle"


def v9_confirmation_claim_signing_message(
    *,
    validator_hotkey: str,
    slot_id: str,
    profile_revision: str,
    profile_checksum: str,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None:
        raise ValueError("confirmation claim timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-claim:v1:"
        f"{validator_hotkey}:{slot_id}:{profile_revision}:{profile_checksum}:"
        f"{broker_public_key.rstrip('=')}:{nonce}:{requested}"
    ).encode()


def v9_confirmation_artifact_signing_message(
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None:
        raise ValueError("confirmation artifact timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-artifact:v1:"
        f"{validator_hotkey}:{bundle_id}:{ticket_id}:{nonce}:{requested}"
    ).encode()


def v9_confirmation_prepare_wire_sha256(
    *,
    ablation_coordinator_latency_ms: int,
    longmemeval: Mapping[str, object],
    inference_ablation: Mapping[str, object],
    embedding_ablation: Mapping[str, object],
) -> str:
    """Bind producer digests and measured latency without JSON float drift."""
    if (
        isinstance(ablation_coordinator_latency_ms, bool)
        or not isinstance(ablation_coordinator_latency_ms, int)
        or ablation_coordinator_latency_ms < 0
    ):
        raise ValueError("confirmation coordinator latency must be non-negative")

    def terms(name: str, dimension: Mapping[str, object]) -> str:
        digest = dimension.get("go_evidence_sha256")
        latency = dimension.get("latency_ms")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or isinstance(latency, bool)
            or not isinstance(latency, int)
            or latency < 0
        ):
            raise ValueError(f"confirmation {name} native identity is invalid")
        try:
            bytes.fromhex(digest)
        except ValueError as error:
            raise ValueError(f"confirmation {name} native digest is invalid") from error
        return f"{digest}:{latency}"

    encoded = (
        "validator-v9-confirmation-native-wire:v1:"
        f"{ablation_coordinator_latency_ms}:"
        f"{terms('longmemeval', longmemeval)}:"
        f"{terms('inference', inference_ablation)}:"
        f"{terms('embedding', embedding_ablation)}"
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def v9_confirmation_prepare_signing_message(
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    wire_sha256: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None:
        raise ValueError("confirmation prepare timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-prepare:v1:"
        f"{validator_hotkey}:{bundle_id}:{ticket_id}:{wire_sha256}:"
        f"{nonce}:{requested}"
    ).encode()


def v9_confirmation_fail_signing_message(
    *,
    validator_hotkey: str,
    bundle_id: UUID,
    ticket_id: UUID,
    reason: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if requested_at.tzinfo is None:
        raise ValueError("confirmation failure timestamp must include a timezone")
    requested = (
        requested_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return (
        "validator-v9-confirmation-fail:v1:"
        f"{validator_hotkey}:{bundle_id}:{ticket_id}:{reason}:{nonce}:{requested}"
    ).encode()


def _profile_registry(
    request: Request,
) -> Mapping[tuple[str, str], ConfirmationVerificationProfile]:
    """Return the process-owned exact profile registry, empty by default."""
    registry = getattr(request.app.state, "confirmation_verification_profiles", {})
    if not isinstance(registry, Mapping):
        return {}
    return registry


def _registered_profile(
    request: Request, *, revision: str, checksum: str
) -> ConfirmationVerificationProfile | None:
    profile = _profile_registry(request).get((revision, checksum))
    if not isinstance(profile, ConfirmationVerificationProfile):
        return None
    try:
        if profile.revision != revision or profile.checksum() != checksum:
            return None
    except ConfirmationEvidenceError:
        return None
    return profile


def _execution_profile(
    profile: ConfirmationVerificationProfile,
) -> ConfirmationExecutionProfile:
    return ConfirmationExecutionProfile.model_validate(
        {
            **profile.payload(),
            "checksum": profile.checksum(),
        }
    )


async def _subject_agent_id(session: AsyncSession, *, bundle_id: UUID) -> UUID | None:
    rows = list(
        await session.scalars(
            select(ConfirmationBundleSubject.agent_id).where(
                ConfirmationBundleSubject.bundle_id == bundle_id
            )
        )
    )
    return min(rows, key=str) if rows else None


async def _job_response(
    session: AsyncSession,
    *,
    bundle: ConfirmationBundle,
    ticket: ConfirmationBundleTicket,
    reservation: ConfirmationBudgetReservation,
    settings: ConfirmationBundleSettings,
    profile: ConfirmationVerificationProfile,
    broker_public_key: str,
    proxy_base_url: str,
    now: datetime,
) -> V9ConfirmationJobResponse:
    agent_id = await _subject_agent_id(session, bundle_id=bundle.bundle_id)
    if agent_id is None:
        raise ConfirmationBundlePersistenceError(
            "confirmation bundle has no attached base subject"
        )
    grants = await ensure_confirmation_inference_grants(
        session,
        ticket=ticket,
        broker_public_key=broker_public_key,
        profile=profile,
        now=now,
    )
    if len(grants) != 3:
        raise ConfirmationBundlePersistenceError(
            "confirmation ticket inference grants are not resumable"
        )
    offers = [
        ConfirmationInferenceGrantOffer(
            lane=grant.lane,
            grant_id=grant.grant_id,
            bearer=bearer,
            generation=grant.generation,
            proxy_url=(
                f"{proxy_base_url}/api/v1/inference/confirmation/embeddings"
                if grant.lane == "embedding"
                else f"{proxy_base_url}/api/v1/inference/confirmation/chat/completions"
            ),
            model=grant.model,
            provider=grant.provider,
            route_provider=grant.route_provider,
            receipt_provider=grant.receipt_provider,
            profile_revision=grant.profile_revision,
            request_budget=grant.request_budget,
            token_budget=grant.token_budget,
            cost_budget_microusd=grant.cost_budget_microusd,
            expires_at=grant.expires_at,
        )
        for grant, bearer in grants
    ]
    return V9ConfirmationJobResponse(
        purpose=_PURPOSE,
        bundle_id=bundle.bundle_id,
        ticket_id=ticket.ticket_id,
        reservation_id=reservation.reservation_id,
        agent_id=agent_id,
        slot_id=ticket.slot_id,
        deadline=ticket.deadline,
        artifact_sha256=bundle.artifact_sha256,
        bench_version=9,
        settings_revision=bundle.settings_revision,
        settings_checksum=bundle.settings_checksum,
        retest_generation=bundle.retest_generation,
        mode=settings.mode,
        per_bundle_request_cap=settings.per_bundle_request_cap,
        per_bundle_token_cap=settings.per_bundle_token_cap,
        execution_profile=_execution_profile(profile),
        inference_grants=offers,
    )


def _settings(row: ConfirmationBundleSettingsRevision) -> ConfirmationBundleSettings:
    return ConfirmationBundleSettings.model_validate_json(json.dumps(row.settings))


async def _authenticate_claim(
    payload: V9ConfirmationClaimRequest,
    *,
    request: Request,
    chain: ChainClient,
    header_hotkey: str | None,
) -> datetime:
    if header_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError(
            "v9 confirmation claim header does not match signed hotkey"
        )
    signed = v9_confirmation_claim_signing_message(
        validator_hotkey=payload.validator_hotkey,
        slot_id=payload.slot_id,
        profile_revision=payload.profile_revision,
        profile_checksum=payload.profile_checksum,
        broker_public_key=payload.broker_public_key,
        nonce=payload.nonce,
        requested_at=payload.requested_at,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("v9 confirmation claim signature did not verify")
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(status_code=409, detail="confirmation claim is stale")
    config = request.app.state.config.chain
    await _assert_validator_permitted(
        chain,
        config.netuid,
        payload.validator_hotkey,
        network=config.subtensor_network,
    )
    return now


@router.post(
    "/job",
    response_model=V9ConfirmationJobResponse,
    responses={204: {"description": "No exact-profile confirmation work."}},
)
async def request_v9_confirmation_job(
    payload: V9ConfirmationClaimRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> V9ConfirmationJobResponse | Response:
    """Claim one artifact/profile-deduplicated internal confirmation bundle."""
    response.headers["Cache-Control"] = "no-store"
    now = await _authenticate_claim(
        payload,
        request=request,
        chain=chain,
        header_hotkey=x_validator_hotkey,
    )
    profile = _registered_profile(
        request,
        revision=payload.profile_revision,
        checksum=payload.profile_checksum,
    )
    try:
        async with session.begin():
            try:
                await consume_validator_nonce(
                    session,
                    nonce=payload.nonce,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    expires_at=now + _JOB_REQUEST_MAX_AGE,
                )
            except ValidatorRequestReplayError as error:
                raise HTTPException(
                    status_code=409,
                    detail="confirmation claim nonce has already been used",
                ) from error
            if profile is None:
                response.status_code = 204
                return response

            await lock_validator_slot(
                session,
                validator_hotkey=payload.validator_hotkey,
                slot_id=payload.slot_id,
            )
            await expire_overdue_confirmation_bundle_tickets(session, now=now)
            live_ticket = await live_v9_confirmation_slot_ticket(
                session,
                validator_hotkey=payload.validator_hotkey,
                slot_id=payload.slot_id,
                now=now,
            )
            if live_ticket is not None:
                attempt = await lock_confirmation_attempt(
                    session,
                    bundle_id=live_ticket.bundle_id,
                    ticket_id=live_ticket.ticket_id,
                )
                if attempt is None:
                    raise ConfirmationBundlePersistenceError(
                        "live confirmation ticket is missing its bundle reservation"
                    )
                bundle = attempt.bundle
                # Slot occupancy is profile-agnostic. An exact-profile claim may
                # resume its immutable lease, but a rotated profile must wait
                # for the old lease to complete, fail, or expire rather than
                # creating a second live ticket for the same validator slot.
                if (
                    bundle.profile_revision != payload.profile_revision
                    or bundle.profile_checksum != payload.profile_checksum
                ):
                    response.status_code = 204
                    return response
                settings_row = await session.get(
                    ConfirmationBundleSettingsRevision, bundle.settings_revision
                )
                if settings_row is None:
                    raise ConfirmationBundlePersistenceError(
                        "confirmation settings revision disappeared"
                    )
                return await _job_response(
                    session,
                    bundle=bundle,
                    ticket=attempt.ticket,
                    reservation=attempt.reservation,
                    settings=_settings(settings_row),
                    profile=profile,
                    broker_public_key=payload.broker_public_key,
                    proxy_base_url=request.app.state.config.inference_proxy.public_base_url,
                    now=now,
                )

            # New spend is governed by the latest effective global policy, not
            # merely the frozen policy attached to an older pending bundle.
            # The shared transaction lock gives an OFF update and this claim a
            # total order: either issuance commits first, or OFF wins and this
            # request returns no work. Already-issued tickets above remain
            # resumable and reportable under their immutable contract.
            await lock_confirmation_policy(session)
            latest_settings_row = await latest_confirmation_bundle_settings_revision(
                session
            )
            if latest_settings_row is None:
                response.status_code = 204
                return response
            latest_settings = _settings(latest_settings_row)
            if (
                latest_settings.mode == ConfirmationBundleMode.OFF
                or latest_settings.profile_revision != profile.revision
                or latest_settings.profile_checksum != profile.checksum()
            ):
                response.status_code = 204
                return response

            # New tickets and reservations do not exist yet, so issuance starts
            # at the first pre-existing row in the lifecycle order. Lock the
            # budget before reconciliation can touch a leased or pending bundle:
            # claim now follows the same [ticket -> reservation ->] budget ->
            # bundle suffix as prepare, fail, submit, and recovery.
            budget = await lock_confirmation_budget_day(
                session, utc_day=now.astimezone(UTC).date()
            )
            await reconcile_v9_confirmation_candidates(
                session,
                verification_profiles=_profile_registry(request),
            )

            bundles = list(
                await session.scalars(
                    select(ConfirmationBundle)
                    .where(
                        ConfirmationBundle.state.in_(
                            (
                                ConfirmationBundleState.PENDING.value,
                                ConfirmationBundleState.FAILED.value,
                                ConfirmationBundleState.BLOCKED_BUDGET.value,
                            )
                        ),
                        ConfirmationBundle.bench_version == 9,
                        ConfirmationBundle.profile_revision == payload.profile_revision,
                        ConfirmationBundle.profile_checksum == payload.profile_checksum,
                        ConfirmationBundle.settings_revision
                        == latest_settings_row.revision,
                        ConfirmationBundle.settings_checksum
                        == latest_settings_row.checksum,
                    )
                    .order_by(
                        ConfirmationBundle.created_at, ConfirmationBundle.bundle_id
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            selected_bundle = None
            for candidate in bundles:
                if (
                    await _subject_agent_id(session, bundle_id=candidate.bundle_id)
                    is not None
                ):
                    selected_bundle = candidate
                    break
            if selected_bundle is None:
                response.status_code = 204
                return response
            settings_row = await session.get(
                ConfirmationBundleSettingsRevision, selected_bundle.settings_revision
            )
            if settings_row is None:
                raise ConfirmationBundlePersistenceError(
                    "confirmation settings revision disappeared"
                )
            settings = _settings(settings_row)
            if (
                settings.profile_revision != profile.revision
                or settings.profile_checksum != profile.checksum()
            ):
                response.status_code = 204
                return response
            reserve_microusd = (
                sum(lane.max_cost_usd_micros for lane in profile.provider_lanes)
                + profile.embedding_lane.max_cost_usd_micros
            )
            if reserve_microusd <= 0:
                response.status_code = 204
                return response
            reservation_id = uuid4()
            decision = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=selected_bundle.bundle_id,
                reservation_id=reservation_id,
                now=now,
                expected_revision=budget.revision,
                settings_revision=selected_bundle.settings_revision,
                settings=settings,
                reserve_microusd=reserve_microusd,
            )
            if decision.reservation is None:
                response.status_code = 204
                return response
            ticket = await issue_confirmation_bundle_ticket(
                session,
                bundle_id=selected_bundle.bundle_id,
                reservation_id=decision.reservation.reservation_id,
                validator_hotkey=payload.validator_hotkey,
                slot_id=payload.slot_id,
                now=now,
            )
            issued_attempt = await lock_confirmation_attempt(
                session,
                bundle_id=selected_bundle.bundle_id,
                ticket_id=ticket.ticket_id,
            )
            if issued_attempt is None:  # pragma: no cover - flushed above
                raise ConfirmationBundlePersistenceError(
                    "issued confirmation attempt disappeared"
                )
            return await _job_response(
                session,
                bundle=issued_attempt.bundle,
                ticket=issued_attempt.ticket,
                reservation=issued_attempt.reservation,
                settings=settings,
                profile=profile,
                broker_public_key=payload.broker_public_key,
                proxy_base_url=request.app.state.config.inference_proxy.public_base_url,
                now=now,
            )
    except StaleConfirmationBudget as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ConfirmationBundlePersistenceError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/bundle/{bundle_id}/artifact", response_model=ArtifactResponse)
async def get_v9_confirmation_artifact(
    bundle_id: UUID,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    storage: StorageDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
    x_confirmation_ticket_id: Annotated[UUID | None, Header()] = None,
    x_confirmation_nonce: Annotated[UUID | None, Header()] = None,
    x_confirmation_requested_at: Annotated[datetime | None, Header()] = None,
    x_confirmation_signature: Annotated[str | None, Header()] = None,
) -> ArtifactResponse:
    """Fetch source only for the exact live internal confirmation ticket."""
    response.headers["Cache-Control"] = "no-store"
    if None in {
        x_validator_hotkey,
        x_confirmation_ticket_id,
        x_confirmation_nonce,
        x_confirmation_requested_at,
        x_confirmation_signature,
    }:
        raise ValidatorAuthError("confirmation artifact proof is incomplete")
    assert x_validator_hotkey is not None
    assert x_confirmation_ticket_id is not None
    assert x_confirmation_nonce is not None
    assert x_confirmation_requested_at is not None
    assert x_confirmation_signature is not None
    if x_confirmation_requested_at.tzinfo is None:
        raise ValidatorAuthError(
            "confirmation artifact timestamp must include a timezone"
        )
    signed = v9_confirmation_artifact_signing_message(
        validator_hotkey=x_validator_hotkey,
        bundle_id=bundle_id,
        ticket_id=x_confirmation_ticket_id,
        nonce=x_confirmation_nonce,
        requested_at=x_confirmation_requested_at,
    )
    if not verify_signature(
        signer=x_validator_hotkey,
        payload=signed,
        signature_hex=x_confirmation_signature,
    ):
        raise ValidatorAuthError("confirmation artifact signature did not verify")
    now = datetime.now(UTC)
    if abs(now - x_confirmation_requested_at.astimezone(UTC)) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409, detail="confirmation artifact proof is stale"
        )
    config = request.app.state.config.chain
    await _assert_validator_permitted(
        chain,
        config.netuid,
        x_validator_hotkey,
        network=config.subtensor_network,
    )
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=x_confirmation_nonce,
                validator_hotkey=x_validator_hotkey,
                now=now,
                expires_at=now + _JOB_REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError as error:
            raise HTTPException(
                status_code=409,
                detail="confirmation artifact nonce has already been used",
            ) from error
        ticket = await session.get(
            ConfirmationBundleTicket, x_confirmation_ticket_id, with_for_update=True
        )
        bundle = await session.get(ConfirmationBundle, bundle_id)
        if (
            ticket is None
            or bundle is None
            or ticket.bundle_id != bundle_id
            or ticket.validator_hotkey != x_validator_hotkey
            or ticket.status != "issued"
            or ticket.deadline <= now
            or bundle.state != ConfirmationBundleState.LEASED.value
        ):
            raise HTTPException(
                status_code=409, detail="confirmation ticket is not live"
            )
        agent_id = await _subject_agent_id(session, bundle_id=bundle_id)
        agent = await session.get(Agent, agent_id) if agent_id is not None else None
        if agent is None or agent.sha256 != bundle.artifact_sha256:
            raise HTTPException(
                status_code=409,
                detail="confirmation artifact does not match its base subject",
            )
    url = await storage.presigned_get_url(
        key=_artifact_key(agent.agent_id),
        expires_in=int(_ARTIFACT_URL_TTL.total_seconds()),
    )
    image_url = None
    if agent.screened_image_sha256 and agent.screened_image_upload_id:
        image_url = await storage.presigned_get_url(
            key=_screened_image_key(agent.agent_id, agent.screened_image_upload_id),
            expires_in=int(_ARTIFACT_URL_TTL.total_seconds()),
        )
    return ArtifactResponse(
        agent_id=agent.agent_id,
        sha256=agent.sha256,
        download_url=url,
        expires_at=datetime.now(UTC) + _ARTIFACT_URL_TTL,
        screened_image_url=image_url,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_size_bytes=agent.screened_image_size_bytes,
        screened_image_id=agent.screened_image_id,
        screened_image_ref=agent.screened_image_ref,
        bench_version=9,
        screening_policy_version=agent.screening_policy_version,
    )


@router.post(
    "/bundle/{bundle_id}/prepare-report",
    response_model=V9ConfirmationPreparedReport,
)
async def prepare_v9_confirmation_report(
    bundle_id: UUID,
    payload: V9ConfirmationPrepareRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> V9ConfirmationPreparedReport:
    """Normalize native Go evidence and rebuild the root without persisting it.

    This phase grants no authority. The validator must sign the returned root
    digest and submit the exact typed dimensions to the final endpoint, which
    independently rebuilds everything again inside the settlement transaction.
    """
    response.headers["Cache-Control"] = "no-store"
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError(
            "confirmation prepare header does not match signed hotkey"
        )
    try:
        calculated_wire_sha256 = v9_confirmation_prepare_wire_sha256(
            ablation_coordinator_latency_ms=(payload.ablation_coordinator_latency_ms),
            longmemeval=payload.longmemeval.model_dump(mode="json"),
            inference_ablation=payload.inference_ablation.model_dump(mode="json"),
            embedding_ablation=payload.embedding_ablation.model_dump(mode="json"),
        )
    except ValueError as error:
        raise ValidatorAuthError(
            "confirmation prepare wire identity is invalid"
        ) from error
    if calculated_wire_sha256 != payload.wire_sha256:
        raise ValidatorAuthError("confirmation prepare wire digest is invalid")
    signed = v9_confirmation_prepare_signing_message(
        validator_hotkey=payload.validator_hotkey,
        bundle_id=bundle_id,
        ticket_id=payload.ticket_id,
        wire_sha256=payload.wire_sha256,
        nonce=payload.nonce,
        requested_at=payload.requested_at,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("confirmation prepare signature did not verify")
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(status_code=409, detail="confirmation prepare is stale")
    config = request.app.state.config.chain
    await _assert_validator_permitted(
        chain,
        config.netuid,
        payload.validator_hotkey,
        network=config.subtensor_network,
    )
    try:
        async with session.begin():
            try:
                await consume_validator_nonce(
                    session,
                    nonce=payload.nonce,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    expires_at=now + _JOB_REQUEST_MAX_AGE,
                )
            except ValidatorRequestReplayError as error:
                raise HTTPException(
                    status_code=409,
                    detail="confirmation prepare nonce has already been used",
                ) from error
            attempt = await lock_confirmation_attempt(
                session,
                bundle_id=bundle_id,
                ticket_id=payload.ticket_id,
            )
            if (
                attempt is None
                or attempt.ticket.bundle_id != bundle_id
                or attempt.ticket.validator_hotkey != payload.validator_hotkey
                or attempt.ticket.status != "issued"
                or attempt.ticket.deadline <= now
                or attempt.bundle.state != ConfirmationBundleState.LEASED.value
            ):
                raise ConfirmationBundlePersistenceError(
                    "confirmation prepare does not match a live internal ticket"
                )
            bundle = attempt.bundle
            profile = _registered_profile(
                request,
                revision=bundle.profile_revision,
                checksum=bundle.profile_checksum,
            )
            if profile is None:
                raise ConfirmationBundlePersistenceError(
                    "confirmation verification profile is not registered"
                )
            settings_row = await session.get(
                ConfirmationBundleSettingsRevision, bundle.settings_revision
            )
            if settings_row is None:
                raise ConfirmationBundlePersistenceError(
                    "confirmation settings revision disappeared"
                )
            settings = _settings(settings_row)
            try:
                normalized = completion_report_from_go_dimensions(
                    ablation_coordinator_latency_ms=(
                        payload.ablation_coordinator_latency_ms
                    ),
                    longmemeval=payload.longmemeval.model_dump(mode="json"),
                    inference_ablation=payload.inference_ablation.model_dump(
                        mode="json"
                    ),
                    embedding_ablation=payload.embedding_ablation.model_dump(
                        mode="json"
                    ),
                )
                verified = rebuild_confirmation_evidence(
                    normalized,
                    artifact_sha256=bundle.artifact_sha256,
                    profile_revision=bundle.profile_revision,
                    profile_checksum=bundle.profile_checksum,
                    settings_revision=bundle.settings_revision,
                    settings_checksum=bundle.settings_checksum,
                    retest_generation=bundle.retest_generation,
                    mode=settings.mode,
                    profile=profile,
                )
            except (ConfirmationEvidenceError, ConfirmationWireError) as error:
                raise ConfirmationBundlePersistenceError(str(error)) from error
            return V9ConfirmationPreparedReport(
                bundle_id=bundle_id,
                ticket_id=payload.ticket_id,
                ablation_coordinator_latency_ms=(
                    payload.ablation_coordinator_latency_ms
                ),
                longmemeval=normalized.longmemeval,
                inference_ablation=normalized.inference_ablation,
                embedding_ablation=normalized.embedding_ablation,
                evidence_sha256=verified.evidence_sha256,
            )
    except ConfirmationBundlePersistenceError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/bundle/{bundle_id}/fail",
    response_model=V9ConfirmationFailResponse,
)
async def fail_v9_confirmation_job(
    bundle_id: UUID,
    payload: V9ConfirmationFailRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> V9ConfirmationFailResponse:
    """Hand back failed work and settle unknown partial spend pessimistically."""
    response.headers["Cache-Control"] = "no-store"
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError(
            "confirmation failure header does not match signed hotkey"
        )
    signed = v9_confirmation_fail_signing_message(
        validator_hotkey=payload.validator_hotkey,
        bundle_id=bundle_id,
        ticket_id=payload.ticket_id,
        reason=payload.reason,
        nonce=payload.nonce,
        requested_at=payload.requested_at,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("confirmation failure signature did not verify")
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _JOB_REQUEST_MAX_AGE:
        raise HTTPException(status_code=409, detail="confirmation failure is stale")
    config = request.app.state.config.chain
    await _assert_validator_permitted(
        chain,
        config.netuid,
        payload.validator_hotkey,
        network=config.subtensor_network,
    )
    try:
        async with session.begin():
            try:
                await consume_validator_nonce(
                    session,
                    nonce=payload.nonce,
                    validator_hotkey=payload.validator_hotkey,
                    now=now,
                    expires_at=now + _JOB_REQUEST_MAX_AGE,
                )
            except ValidatorRequestReplayError as error:
                raise HTTPException(
                    status_code=409,
                    detail="confirmation failure nonce has already been used",
                ) from error
            attempt = await lock_confirmation_attempt(
                session,
                bundle_id=bundle_id,
                ticket_id=payload.ticket_id,
            )
            if (
                attempt is None
                or attempt.ticket.validator_hotkey != payload.validator_hotkey
            ):
                raise ConfirmationBundlePersistenceError(
                    "confirmation failure does not match its internal ticket"
                )
            ticket = attempt.ticket
            reservation = attempt.reservation
            budget = attempt.budget
            bundle = attempt.bundle
            if ticket.status not in {"issued", "expired"} or bundle.state not in {
                ConfirmationBundleState.LEASED.value,
                ConfirmationBundleState.FAILED.value,
            }:
                raise ConfirmationBundlePersistenceError(
                    "confirmation ticket cannot be failed in its current state"
                )
            expected_failure_reason = f"confirmation_{payload.reason}"
            if (
                reservation.state == "settled"
                and reservation.failed_attempt is True
                and ticket.failure_reason
                not in {expected_failure_reason, "confirmation_failed"}
            ):
                raise ConfirmationBundlePersistenceError(
                    "confirmation failure replay changed its reason"
                )
            settlement = await settle_confirmation_bundle_budget(
                session,
                reservation_id=reservation.reservation_id,
                expected_revision=budget.revision,
                actual_microusd=reservation.reserved_microusd,
                failed_attempt=True,
                settled_at=now,
            )
            if not settlement.replayed:
                ticket.failure_reason = expected_failure_reason
            return V9ConfirmationFailResponse(
                bundle_id=bundle_id,
                ticket_id=ticket.ticket_id,
                accepted=True,
                state="failed",
                settled_microusd=reservation.reserved_microusd,
                replayed=settlement.replayed,
            )
    except StaleConfirmationBudget as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ConfirmationBundlePersistenceError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/bundle/{bundle_id}/report",
    response_model=V9ConfirmationSubmitResponse,
)
async def submit_v9_confirmation_report(
    bundle_id: UUID,
    payload: V9ConfirmationSubmitRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> V9ConfirmationSubmitResponse:
    """Settle server-derived cost and complete one signed bundle atomically."""
    response.headers["Cache-Control"] = "no-store"
    if x_validator_hotkey != payload.validator_hotkey:
        raise ValidatorAuthError(
            "confirmation report header does not match ticket reporter"
        )
    config = request.app.state.config.chain
    await _assert_validator_permitted(
        chain,
        config.netuid,
        payload.validator_hotkey,
        network=config.subtensor_network,
    )
    now = datetime.now(UTC)
    try:
        async with session.begin():
            attempt = await lock_confirmation_attempt(
                session,
                bundle_id=bundle_id,
                ticket_id=payload.ticket_id,
            )
            if (
                attempt is None
                or attempt.ticket.validator_hotkey != payload.validator_hotkey
            ):
                raise ConfirmationBundlePersistenceError(
                    "confirmation report does not match its internal ticket"
                )
            bundle = attempt.bundle
            reservation = attempt.reservation
            budget = attempt.budget
            profile = _registered_profile(
                request,
                revision=bundle.profile_revision,
                checksum=bundle.profile_checksum,
            )
            if profile is None:
                raise ConfirmationBundlePersistenceError(
                    "confirmation verification profile is not registered"
                )
            replayed = bundle.state == ConfirmationBundleState.COMPLETED.value
            if not replayed:
                settings_row = await session.get(
                    ConfirmationBundleSettingsRevision, bundle.settings_revision
                )
                if settings_row is None:
                    raise ConfirmationBundlePersistenceError(
                        "confirmation settings revision disappeared"
                    )
                settings = _settings(settings_row)
                try:
                    verified = rebuild_confirmation_evidence(
                        payload.report,
                        artifact_sha256=bundle.artifact_sha256,
                        profile_revision=bundle.profile_revision,
                        profile_checksum=bundle.profile_checksum,
                        settings_revision=bundle.settings_revision,
                        settings_checksum=bundle.settings_checksum,
                        retest_generation=bundle.retest_generation,
                        mode=settings.mode,
                        profile=profile,
                    )
                except ConfirmationEvidenceError as error:
                    raise ConfirmationBundlePersistenceError(str(error)) from error
                await settle_confirmation_bundle_budget(
                    session,
                    reservation_id=reservation.reservation_id,
                    expected_revision=budget.revision,
                    actual_microusd=verified.root.totals.provider_cost_microusd,
                    failed_attempt=False,
                    settled_at=now,
                )
            completed = await complete_confirmation_bundle(
                session,
                bundle_id=bundle_id,
                ticket_id=payload.ticket_id,
                report=payload.report,
                verification_profile=profile,
                now=now,
            )
            await reconcile_v9_confirmation_candidates(
                session,
                verification_profiles=_profile_registry(request),
            )
            assert completed.evidence_sha256 is not None
            assert completed.qualification_status in {"qualified", "unqualified"}
            qualification_status = cast(
                Literal["qualified", "unqualified"],
                completed.qualification_status,
            )
            return V9ConfirmationSubmitResponse(
                bundle_id=bundle_id,
                ticket_id=payload.ticket_id,
                accepted=True,
                state="completed",
                qualification_status=qualification_status,
                evidence_sha256=completed.evidence_sha256,
                replayed=replayed,
            )
    except StaleConfirmationBudget as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ConfirmationBundlePersistenceError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


__all__ = [
    "router",
    "v9_confirmation_artifact_signing_message",
    "v9_confirmation_claim_signing_message",
    "v9_confirmation_fail_signing_message",
    "v9_confirmation_prepare_signing_message",
    "v9_confirmation_prepare_wire_sha256",
]
