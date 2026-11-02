"""Public per-competition submission terms: the deliverable and its fee.

``GET /public/submission-terms`` lists every competition with an upload lane;
``GET /public/submission-terms/{competition}`` reads one. Each entry is
assembled from the values the upload paths themselves enforce (imported, not
copied), so the terms cannot drift from the checks. Nothing here is a new
rule. See :mod:`ditto.api_models.submission_terms`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator import (
    GENERATOR_CONTRACT_V1,
    MAX_CASES_PER_SEED,
    MIN_PACK_CASES,
    SOURCE_SIGNATURE_DOMAIN,
    GeneratorCompetitionSettings,
)
from ditto.api_models.submission_terms import (
    ArtifactContract,
    ArtifactLimit,
    CompetitionId,
    CompetitionSubmissionTerms,
    SubmissionCooldownTerms,
    SubmissionFeeTerms,
    SubmissionTermsResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.upload import (
    DEFAULT_MAX_TARBALL_SIZE_BYTES,
    MAX_TARBALL_SIZE_BYTES,
)
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.source_inspect import (
    UPLOAD_MAX_MEMBERS,
    UPLOAD_MAX_UNPACKED_BYTES,
)
from ditto.db.queries.submission_settings import (
    EffectiveSubmissionSettings,
    effective_submission_settings,
)
from ditto.generator_archive import (
    BUILDER_GO_VERSION,
    MAX_ARCHIVE_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES,
    MAX_SOURCE_PATH_LENGTH,
)
from ditto_screening_protocol.source_limits import (
    MAX_SOURCE_FILE_BYTES,
    MAX_SOURCE_FILE_LINES,
    MAX_SOURCE_LINE_CHARS,
    SOURCE_LIMITS_EFFECTIVE_AT,
)

router = APIRouter(prefix="/public/submission-terms", tags=["public"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

COMPETITIONS: tuple[CompetitionId, ...] = ("memory", "generator")
_SOURCE_LIMITS = "ditto_screening_protocol.source_limits"


def _memory_deliverable() -> ArtifactContract:
    return ArtifactContract(
        kind="agent_harness_tarball",
        format="gzip-compressed tar of the whole harness crate",
        contract_version=None,
        required_files=["Dockerfile"],
        limits=[
            ArtifactLimit(
                name="max_archive_bytes",
                value=MAX_TARBALL_SIZE_BYTES,
                unit="bytes",
                source=(
                    "code"
                    if MAX_TARBALL_SIZE_BYTES == DEFAULT_MAX_TARBALL_SIZE_BYTES
                    else "environment"
                ),
                reference="DITTO_MAX_TARBALL_SIZE_BYTES",
                description=(
                    "the uploaded tarball, measured from the streamed bytes "
                    f"(default {DEFAULT_MAX_TARBALL_SIZE_BYTES})"
                ),
            ),
            ArtifactLimit(
                name="max_archive_members",
                value=UPLOAD_MAX_MEMBERS,
                unit="files",
                source="code",
                reference="ditto.api_server.source_inspect.UPLOAD_MAX_MEMBERS",
                description="tar members, directories included",
            ),
            ArtifactLimit(
                name="max_unpacked_bytes",
                value=UPLOAD_MAX_UNPACKED_BYTES,
                unit="bytes",
                source="code",
                reference="ditto.api_server.source_inspect.UPLOAD_MAX_UNPACKED_BYTES",
                description="total size of the archive once unpacked",
            ),
            ArtifactLimit(
                name="max_source_file_lines",
                value=MAX_SOURCE_FILE_LINES,
                unit="lines",
                source="code",
                reference=f"{_SOURCE_LIMITS}.MAX_SOURCE_FILE_LINES",
                description="lines per source file",
            ),
            ArtifactLimit(
                name="max_source_file_bytes",
                value=MAX_SOURCE_FILE_BYTES,
                unit="bytes",
                source="code",
                reference=f"{_SOURCE_LIMITS}.MAX_SOURCE_FILE_BYTES",
                description="bytes per source file",
            ),
            ArtifactLimit(
                name="max_source_line_chars",
                value=MAX_SOURCE_LINE_CHARS,
                unit="characters",
                source="code",
                reference=f"{_SOURCE_LIMITS}.MAX_SOURCE_LINE_CHARS",
                description="characters per source line",
            ),
        ],
        upload_endpoint="/api/v1/upload/agent",
        signature_payload=(
            "ditto-upload-v2:{hotkey}:{sha256}:{signature_timestamp}:{signature_nonce}"
        ),
        notes=[
            "a UTF-8 Dockerfile at the archive root; no absolute paths, "
            "traversals, links or special files",
            "source-file limits apply from "
            f"{SOURCE_LIMITS_EFFECTIVE_AT.isoformat()} except to allowlisted "
            "unmodified upstream releases",
            "the screener then builds the image and requires GET /health; "
            "see docs/submission-contract.md",
        ],
    )


def _generator_deliverable(settings: GeneratorCompetitionSettings) -> ArtifactContract:
    return ArtifactContract(
        kind="generator_source_archive",
        format="gzip-compressed tar of a Go main module",
        contract_version=GENERATOR_CONTRACT_V1,
        required_files=["go.mod", "*.go (package main, at the root)"],
        limits=[
            ArtifactLimit(
                name="max_archive_bytes",
                value=MAX_ARCHIVE_BYTES,
                unit="bytes",
                source="code",
                reference="ditto.generator_archive.MAX_ARCHIVE_BYTES",
                description="the compressed source archive",
            ),
            ArtifactLimit(
                name="max_source_bytes",
                value=MAX_SOURCE_BYTES,
                unit="bytes",
                source="code",
                reference="ditto.generator_archive.MAX_SOURCE_BYTES",
                description="total file payload once decompressed",
            ),
            ArtifactLimit(
                name="max_source_files",
                value=MAX_SOURCE_FILES,
                unit="files",
                source="code",
                reference="ditto.generator_archive.MAX_SOURCE_FILES",
                description="archive entries, directories included",
            ),
            ArtifactLimit(
                name="max_source_path_length",
                value=MAX_SOURCE_PATH_LENGTH,
                unit="characters",
                source="code",
                reference="ditto.generator_archive.MAX_SOURCE_PATH_LENGTH",
                description="characters per archive path",
            ),
            ArtifactLimit(
                name="max_go_version",
                value=BUILDER_GO_VERSION,
                unit="version",
                source="code",
                reference="ditto.generator_archive.BUILDER_GO_VERSION",
                description="the newest go directive go.mod may declare",
            ),
            ArtifactLimit(
                name="cases_per_seed",
                value=settings.cases_per_seed,
                unit="cases",
                source="setting",
                reference="generator_competition.cases_per_seed",
                description=(
                    "the exact case count every generated pack must contain "
                    f"(settable {MIN_PACK_CASES}..{MAX_CASES_PER_SEED})"
                ),
            ),
        ],
        upload_endpoint="/api/v1/generator/submissions",
        signature_payload=(
            f"{SOURCE_SIGNATURE_DOMAIN}:{{hotkey}}:{{source_sha256}}:"
            "{signature_timestamp}:{signature_nonce}"
        ),
        notes=[
            "requirements in go.mod need vendor/modules.txt (the build has no network)",
            "an archive whose source files match an earlier upload's is refused "
            "as a re-pack",
        ],
    )


def _fee(
    effective: EffectiveSubmissionSettings,
    competition: CompetitionId,
    *,
    competition_requires_fee: bool,
) -> SubmissionFeeTerms:
    fee = effective.competition_fee(
        competition, competition_requires_fee=competition_requires_fee
    )
    return SubmissionFeeTerms(
        required=fee.fee_required,
        amount_rao=fee.fee_amount_rao,
        amount_source=fee.amount_source,
        required_source=fee.required_source,
        send_address=effective.payment_address,
        settings_revision=effective.revision,
    )


async def _all_terms(
    session: AsyncSession, request: Request
) -> list[CompetitionSubmissionTerms]:
    effective = await effective_submission_settings(
        session,
        default_payment_address=request.app.state.config.upload_payment_address,
    )
    generator = (await load_settings(session)).settings
    return [
        CompetitionSubmissionTerms(
            competition="memory",
            accepting=True,
            deliverable=_memory_deliverable(),
            cooldown=SubmissionCooldownTerms(
                seconds=effective.cooldown_seconds,
                scope="owner_coldkey",
                source="setting",
                reference="submission_settings.cooldown_seconds",
            ),
            fee=_fee(effective, "memory", competition_requires_fee=True),
            terms_endpoint="/api/v1/upload/check",
        ),
        CompetitionSubmissionTerms(
            competition="generator",
            accepting=generator.mode != "off",
            deliverable=_generator_deliverable(generator),
            cooldown=SubmissionCooldownTerms(
                seconds=generator.upload_cooldown_seconds,
                scope="hotkey",
                source="setting",
                reference="generator_competition.upload_cooldown_seconds",
            ),
            fee=_fee(
                effective,
                "generator",
                competition_requires_fee=generator.upload_fee_required,
            ),
            terms_endpoint="/api/v1/generator/upload-terms",
        ),
    ]


@router.get("", response_model=SubmissionTermsResponse)
async def submission_terms(
    request: Request, session: SessionDep, response: Response
) -> SubmissionTermsResponse:
    """Every competition's deliverable, cooldown and fee."""
    response.headers["Cache-Control"] = "no-store"
    return SubmissionTermsResponse(competitions=await _all_terms(session, request))


@router.get("/{competition}", response_model=CompetitionSubmissionTerms)
async def competition_submission_terms(
    competition: str, request: Request, session: SessionDep, response: Response
) -> CompetitionSubmissionTerms:
    """One competition's deliverable, cooldown and fee."""
    response.headers["Cache-Control"] = "no-store"
    if competition not in COMPETITIONS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no upload lane for competition {competition!r}; known: "
                f"{list(COMPETITIONS)}"
            ),
        )
    return next(
        terms
        for terms in await _all_terms(session, request)
        if terms.competition == competition
    )
