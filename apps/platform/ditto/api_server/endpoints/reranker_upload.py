"""Season 2 reranker intake (SN-207): upload plus public reads. Shadow only.

``POST /upload/reranker`` accepts a reranker artifact tarball (``model.onnx``,
``vocab.txt``, ``manifest.json``; see ``docs/reranker-season.md``), statically
validates it with :mod:`dittobench_reranker.artifact`, stores it and records
one ``reranker_submissions`` row in status ``accepted``.

What this endpoint deliberately does not do:

- **No payment and no weights.** The track is in shadow. Nothing here touches
  ``agents``, ``evaluation_payments``, the screener queue, validator
  allocation, the leaderboard or anti-copy. A separate table keeps a reranker
  artifact out of every one of those paths.
- **No seed.** ``seed`` and ``seed_block_hash`` stay null. The trusted scorer
  worker (SN-209) derives them after the commit from a finalized block, so a
  miner cannot know the scored pools when it uploads.
- **No model execution.** Validation is static. Latency and scoring belong to
  the scorer worker.

The request is ordered cheap-before-expensive: signature (CPU), ban (one PK
read), registration (cached metagraph), the per-owner window (one count), then
the streamed body, archive validation, the duplicate check, storage and the
insert. Rejections never echo server internals; artifact rejections carry the
contract violation so a miner can fix the artifact.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import inspect
import logging
import os
import tarfile
import tempfile
import time
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, BinaryIO

import bittensor
from dittobench_reranker.artifact import (
    MANIFEST_FILE,
    Artifact,
    ArtifactError,
    load_artifact,
)
from dittobench_reranker.contract import CONTRACT, CONTRACT_ID
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import UUID4
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.reranker import (
    RerankerChampionEntry,
    RerankerChampionResponse,
    RerankerIncumbent,
    RerankerSubmission,
    RerankerSubmissionList,
    RerankerSubmissionStatus,
    RerankerUploadResponse,
)
from ditto.api_models.upload import (
    _SHA256_PATTERN,
    _SIGNATURE_HEX_PATTERN,
    _SS58_PATTERN,
)
from ditto.api_server.dependencies import (
    get_chain_client,
    get_session,
    get_storage_client,
)
from ditto.api_server.storage import S3StorageClient
from ditto.chain import ChainError
from ditto.db.models import RerankerChampion as RerankerChampionRow
from ditto.db.models import RerankerSubmission as RerankerSubmissionRow
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.reranker import (
    DuplicateRerankerArtifactError,
    count_recent_reranker_submissions,
    count_reranker_submissions,
    get_current_reranker_champion,
    get_reranker_submission,
    get_reranker_submission_by_artifact,
    insert_reranker_submission,
    list_reranker_submissions,
    lock_reranker_owner,
    oldest_recent_reranker_submission_at,
)

if TYPE_CHECKING:
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reranker"])

ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

SIGNATURE_MAX_SKEW_SECONDS = 300

# Tar headers, PAX records, padding and the end-of-archive blocks around three
# files. Generous: the deterministic ``pack()`` output uses a few KiB.
_TAR_OVERHEAD_BYTES = 1024 * 1024
_LIMITS = CONTRACT.artifact
# One cap serves both the streamed (compressed) body and the decompressed tar
# stream: gzip never grows a payload by more than a few bytes per block.
MAX_RERANKER_UPLOAD_BYTES = (
    _LIMITS.max_model_bytes
    + _LIMITS.max_vocab_bytes
    + _LIMITS.max_manifest_bytes
    + _TAR_OVERHEAD_BYTES
)
# A valid artifact has three files plus, at most, a root directory entry and
# PAX headers (which tarfile folds into the members). Anything past this is a
# tar bomb of empty entries and is refused before load_artifact lists members.
_MAX_TAR_MEMBERS = 8
_MAX_BASE_MODEL_CHARS = 256
_MAX_REASON_CHARS = 500
_CHUNK_SIZE_BYTES = 256 * 1024

# Static ONNX validation parses up to 100 MiB of protobuf. Bound how many run
# at once per process so a burst of uploads cannot exhaust memory or the
# default thread pool the rest of the API shares.
VALIDATION_CONCURRENCY = 2

# Per-owner submission window. Shadow intake has no fee, so this is the only
# brake on how much storage and validation one owner can consume. The owner is
# the registered coldkey (one budget across its hotkeys). One hour matches the
# memory track's cooldown. Only an accepted row counts against it: a request
# refused before the insert (bad signature, invalid artifact, duplicate, size)
# never consumes the slot. Override with
# DITTO_RERANKER_UPLOAD_WINDOW_SECONDS / DITTO_RERANKER_UPLOADS_PER_WINDOW.
DEFAULT_UPLOAD_WINDOW_SECONDS = 60 * 60
DEFAULT_UPLOADS_PER_WINDOW = 1


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("non-positive %s=%r; using %d", name, raw, default)
        return default
    return value


def upload_window() -> timedelta:
    """The configured per-owner window (read per request; cheap)."""
    return timedelta(
        seconds=_positive_int_env(
            "DITTO_RERANKER_UPLOAD_WINDOW_SECONDS", DEFAULT_UPLOAD_WINDOW_SECONDS
        )
    )


def uploads_per_window() -> int:
    return _positive_int_env(
        "DITTO_RERANKER_UPLOADS_PER_WINDOW", DEFAULT_UPLOADS_PER_WINDOW
    )


def build_reranker_upload_payload(
    *, hotkey: str, sha256: str, signature_timestamp: int, signature_nonce: uuid.UUID
) -> bytes:
    """The exact bytes a miner signs for ``POST /upload/reranker``.

    Domain separated from the memory upload (``ditto-upload-v2:...``) and bound
    to the season contract id, so neither a memory-track signature nor a
    signature from another season can be replayed here.
    """
    return (
        f"{CONTRACT_ID}:upload:{hotkey}:{sha256}:{signature_timestamp}:"
        f"{signature_nonce}"
    ).encode("ascii")


def _verify_reranker_signature(
    hotkey: str,
    sha256: str,
    signature_hex: str,
    signature_timestamp: int,
    signature_nonce: uuid.UUID,
) -> bool:
    if (
        signature_nonce.version != 4
        or abs(time.time() - signature_timestamp) > SIGNATURE_MAX_SKEW_SECONDS
    ):
        return False
    payload = build_reranker_upload_payload(
        hotkey=hotkey,
        sha256=sha256,
        signature_timestamp=signature_timestamp,
        signature_nonce=signature_nonce,
    )
    try:
        keypair = bittensor.Keypair(ss58_address=hotkey)
        return bool(keypair.verify(payload, bytes.fromhex(signature_hex)))
    except (ValueError, TypeError):
        return False


async def _end_read_transaction(session: AsyncSession) -> None:
    """Return the pooled connection before slow work (streaming, storage)."""
    if session.in_transaction():
        result = session.rollback()
        if inspect.isawaitable(result):
            await result


class _BoundedReader:
    """File-like wrapper that refuses to yield more than ``limit`` bytes.

    Placed under ``tarfile`` so a gzip bomb, or a PAX header that declares a
    huge record, fails at the cap instead of being decompressed into memory.
    """

    def __init__(self, raw: Any, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._limit - self._read + 1
        data = self._raw.read(min(size, self._limit - self._read + 1))
        self._read += len(data)
        if self._read > self._limit:
            raise ArtifactError("archive expands beyond the artifact size limit")
        return data


def _prescan_archive(path: Path) -> None:
    """Stream the archive once with hard bounds before ``load_artifact``.

    ``load_artifact`` lists every member, so an archive of millions of empty
    entries (a few MiB of gzip) would exhaust memory there. This pass bounds the
    decompressed bytes and the member count without holding either.
    """
    with path.open("rb") as raw:
        magic = raw.read(2)
        raw.seek(0)
        stream: Any = gzip.GzipFile(fileobj=raw) if magic == b"\x1f\x8b" else raw
        bounded = _BoundedReader(stream, MAX_RERANKER_UPLOAD_BYTES)
        try:
            with tarfile.open(fileobj=bounded, mode="r|") as tar:  # type: ignore[call-overload]
                for count, _member in enumerate(tar, start=1):
                    if count > _MAX_TAR_MEMBERS:
                        raise ArtifactError("archive has too many entries")
        except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
            raise ArtifactError("artifact is not a readable tar or tar.gz") from exc


def _validate_archive(path: Path) -> Artifact:
    """Bound, then statically validate (CPU-bound; runs in a worker thread)."""
    _prescan_archive(path)
    try:
        artifact = load_artifact(path)
    except ArtifactError:
        raise
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise ArtifactError("artifact is not a readable tar or tar.gz") from exc
    base_model = artifact.manifest["base_model"].strip()
    if len(base_model) > _MAX_BASE_MODEL_CHARS:
        raise ArtifactError(
            f"{MANIFEST_FILE} base_model exceeds {_MAX_BASE_MODEL_CHARS} characters"
        )
    return artifact


def _validation_semaphore(request: Request) -> asyncio.Semaphore:
    # Created lazily per app so it binds to the serving event loop.
    state = request.app.state
    semaphore = getattr(state, "reranker_validation_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(VALIDATION_CONCURRENCY)
        state.reranker_validation_semaphore = semaphore
    return semaphore


async def _spool_capped(upload: UploadFile, dest: BinaryIO, max_bytes: int) -> str:
    """Copy the multipart file to ``dest`` with a hard cap; return its sha256."""
    sha = hashlib.sha256()
    size = 0
    while True:
        chunk = await upload.read(_CHUNK_SIZE_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"artifact exceeds {max_bytes} bytes"
            )
        sha.update(chunk)
        await asyncio.to_thread(dest.write, chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail="artifact is empty")
    return sha.hexdigest()


def _reason(exc: ArtifactError) -> str:
    text = str(exc).strip() or "artifact violates the season contract"
    return text[:_MAX_REASON_CHARS]


def _retry_after_seconds(oldest: datetime | None, window: timedelta) -> int:
    if oldest is None:
        return int(window.total_seconds())
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return max(1, int((oldest + window - datetime.now(UTC)).total_seconds()) + 1)


def _rate_limited(retry_after: int, limit: int, window: timedelta) -> HTTPException:
    hours = window.total_seconds() / 3600
    return HTTPException(
        status_code=429,
        detail=(
            f"reranker uploads are limited to {limit} per {hours:g} hours per "
            f"owner; retry in {retry_after} seconds"
        ),
        headers={"Retry-After": str(retry_after)},
    )


def to_public(row: RerankerSubmissionRow) -> RerankerSubmission:
    return RerankerSubmission(
        submission_id=row.id,
        miner_hotkey=row.miner_hotkey,
        status=row.status,  # type: ignore[arg-type]
        artifact_sha256=row.artifact_sha256,
        contract_sha256=row.contract_sha256,
        size_bytes=row.size_bytes,
        base_model=row.base_model,
        base_model_license=row.base_model_license,
        invalid_reason=row.invalid_reason,
        seed=str(row.seed) if row.seed is not None else None,
        seed_block_hash=row.seed_block_hash,
        score=row.score,
        scored_at=row.scored_at,
        created_at=row.created_at,
        seed_block=row.seed_block,
        failure_reason=row.failure_reason,
    )


def incumbent_view() -> RerankerIncumbent:
    incumbent = CONTRACT.incumbent
    return RerankerIncumbent(
        base_model=incumbent.base_model,
        quantization=incumbent.quantization,
        model_sha256=incumbent.model_sha256,
        vocab_sha256=incumbent.vocab_sha256,
    )


def champion_entry(
    crown: RerankerChampionRow, submission: RerankerSubmissionRow
) -> RerankerChampionEntry:
    return RerankerChampionEntry(
        submission_id=crown.submission_id,
        miner_hotkey=submission.miner_hotkey,
        artifact_sha256=submission.artifact_sha256,
        previous_submission_id=crown.previous_submission_id,
        crowned_at=crown.crowned_at,
        superseded_at=crown.superseded_at,
        contract_sha256=crown.contract_sha256,
        pools_sha256=crown.pools_sha256,
        comparison=crown.comparison,
    )


async def current_champion_entry(
    session: AsyncSession,
) -> RerankerChampionEntry | None:
    """The current shadow champion, or ``None`` while the incumbent holds it."""
    crown = await get_current_reranker_champion(session)
    if crown is None:
        return None
    submission = await get_reranker_submission(
        session, submission_id=crown.submission_id
    )
    if submission is None:  # FK makes this unreachable
        return None
    return champion_entry(crown, submission)


@router.post(
    "/upload/reranker",
    response_model=RerankerUploadResponse,
    status_code=201,
)
async def upload_reranker(
    request: Request,
    artifact: Annotated[
        UploadFile,
        File(description="Reranker artifact as tar.gz (see docs/reranker-season.md)"),
    ],
    hotkey: Annotated[str, Form(pattern=_SS58_PATTERN)],
    sha256: Annotated[str, Form(pattern=_SHA256_PATTERN)],
    signature: Annotated[str, Form(pattern=_SIGNATURE_HEX_PATTERN)],
    signature_timestamp: Annotated[int, Form(ge=1, le=2**63 - 1)],
    signature_nonce: Annotated[UUID4, Form()],
    chain: ChainDep,
    storage: StorageDep,
    session: SessionDep,
) -> RerankerUploadResponse:
    """Accept one Season 2 reranker artifact into the shadow track.

    ``sha256`` is the digest of the uploaded file. The hotkey signs
    ``reranker-season-v1:upload:{hotkey}:{sha256}:{timestamp}:{nonce}``
    (fresh within five minutes). No payment is taken and no weight is set.
    """
    netuid = request.app.state.config.chain.netuid

    if not _verify_reranker_signature(
        hotkey, sha256, signature, signature_timestamp, signature_nonce
    ):
        raise HTTPException(
            status_code=401, detail="signature did not verify against the hotkey"
        )

    if await is_hotkey_banned(session, hotkey=hotkey):
        raise HTTPException(status_code=403, detail="hotkey is banned from submitting")
    await _end_read_transaction(session)

    try:
        coldkey = await chain.get_registered_coldkey(hotkey, netuid=netuid)
    except ChainError as exc:
        logger.warning("chain unreachable during /upload/reranker: %s", exc)
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    if coldkey is None:
        raise HTTPException(
            status_code=403, detail=f"hotkey not registered on netuid {netuid}"
        )

    window = upload_window()
    limit = uploads_per_window()
    since = datetime.now(UTC) - window
    if (
        await count_recent_reranker_submissions(
            session, since=since, miner_hotkey=hotkey, miner_coldkey=coldkey
        )
        >= limit
    ):
        oldest = await oldest_recent_reranker_submission_at(
            session, since=since, miner_hotkey=hotkey, miner_coldkey=coldkey
        )
        raise _rate_limited(_retry_after_seconds(oldest, window), limit, window)
    await _end_read_transaction(session)

    with tempfile.TemporaryDirectory(prefix="reranker-upload-") as scratch:
        path = Path(scratch) / "artifact.tar.gz"
        with path.open("wb") as dest:
            actual_sha = await _spool_capped(artifact, dest, MAX_RERANKER_UPLOAD_BYTES)
        if actual_sha != sha256:
            raise HTTPException(
                status_code=400, detail="sha256 of received artifact does not match"
            )
        async with _validation_semaphore(request):
            try:
                validated = await asyncio.to_thread(_validate_archive, path)
            except ArtifactError as exc:
                raise HTTPException(
                    status_code=422, detail=f"artifact rejected: {_reason(exc)}"
                ) from exc
        body = await asyncio.to_thread(path.read_bytes)

    existing = await get_reranker_submission_by_artifact(
        session, artifact_sha256=validated.sha256
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "an identical artifact was already submitted "
                f"(submission {existing.id})"
            ),
        )
    await _end_read_transaction(session)

    submission_id = uuid.uuid4()
    # Storage before the row, as in /upload/agent: an orphan object is cheap
    # and invisible; a row without its object would be unscorable.
    await storage.put_object(
        key=f"reranker/{submission_id}/artifact.tar.gz",
        body=body,
        content_type="application/gzip",
    )

    contract_sha256 = CONTRACT.digest()
    try:
        async with session.begin():
            await lock_reranker_owner(
                session, miner_hotkey=hotkey, miner_coldkey=coldkey
            )
            # Re-check under the owner lock: two concurrent uploads must not
            # both pass the window.
            recent = await count_recent_reranker_submissions(
                session,
                since=datetime.now(UTC) - window,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
            )
            if recent >= limit:
                raise _rate_limited(int(window.total_seconds()), limit, window)
            row = await insert_reranker_submission(
                session,
                submission_id=submission_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                upload_sha256=sha256,
                artifact_sha256=validated.sha256,
                contract_sha256=contract_sha256,
                size_bytes=len(body),
                base_model=validated.manifest["base_model"].strip(),
                base_model_license=validated.manifest["base_model_license"].lower(),
            )
            created_at = row.created_at
    except DuplicateRerankerArtifactError as exc:
        raise HTTPException(
            status_code=409, detail="an identical artifact was already submitted"
        ) from exc

    logger.info(
        "reranker upload accepted hotkey=%s submission_id=%s artifact_sha256=%s",
        hotkey,
        submission_id,
        validated.sha256,
    )
    return RerankerUploadResponse(
        submission_id=submission_id,
        status="accepted",
        artifact_sha256=validated.sha256,
        upload_sha256=sha256,
        contract_id=CONTRACT_ID,
        contract_sha256=contract_sha256,
        created_at=created_at,
        warnings=list(validated.warnings),
    )


_PUBLIC_CACHE_CONTROL = "public, max-age=15, stale-while-revalidate=60"


@router.get(
    "/public/reranker/submissions",
    response_model=RerankerSubmissionList,
)
async def list_public_reranker_submissions(
    response: Response,
    session: SessionDep,
    hotkey: Annotated[str | None, Query(pattern=_SS58_PATTERN)] = None,
    status: RerankerSubmissionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RerankerSubmissionList:
    """Shadow reranker submissions, newest first. Scores are null until scored."""
    total = await count_reranker_submissions(
        session, miner_hotkey=hotkey, status=status
    )
    rows = await list_reranker_submissions(
        session, limit=limit, offset=offset, miner_hotkey=hotkey, status=status
    )
    response.headers["Cache-Control"] = _PUBLIC_CACHE_CONTROL
    return RerankerSubmissionList(
        total=total,
        limit=limit,
        offset=offset,
        submissions=[to_public(row) for row in rows],
    )


@router.get(
    "/public/reranker/submissions/{submission_id}",
    response_model=RerankerSubmission,
)
async def get_public_reranker_submission(
    submission_id: uuid.UUID, response: Response, session: SessionDep
) -> RerankerSubmission:
    """One shadow reranker submission by id."""
    row = await get_reranker_submission(session, submission_id=submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="reranker submission not found")
    response.headers["Cache-Control"] = _PUBLIC_CACHE_CONTROL
    return to_public(row)


@router.get(
    "/public/reranker/champion",
    response_model=RerankerChampionResponse,
)
async def get_public_reranker_champion(
    response: Response, session: SessionDep
) -> RerankerChampionResponse:
    """The shadow champion (``champion`` is null while the incumbent holds it).

    Shadow only: a crown pays nothing and sets no weight. Until the transfer
    gate (SN-226) is in force the scorer crowns nobody by default.
    """
    response.headers["Cache-Control"] = _PUBLIC_CACHE_CONTROL
    return RerankerChampionResponse(
        contract_id=CONTRACT_ID,
        contract_sha256=CONTRACT.digest(),
        incumbent=incumbent_view(),
        champion=await current_champion_entry(session),
    )
