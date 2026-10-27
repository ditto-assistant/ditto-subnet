"""Shadow DittoBench generator competition: settings, leasing, and scoring.

The endpoints in ``endpoints/generator*.py`` and
``endpoints/admin_generator_competition.py`` stay thin; the decisions live
here so they are testable without HTTP:

* :func:`load_settings` resolves the governing revision (``mode=off`` when the
  table is empty or a row is corrupt, so a bad write closes the lane rather
  than opening it).
* :func:`inspect_upload` is Platform's upload check: the archive rules of the
  Go contract (:mod:`ditto.generator_archive`), so the queue only holds source
  the scorer can build.
* :func:`lease_next` picks the most overdue unqualified generator and draws
  fresh secret seeds nobody saw in advance.
* :func:`record_result` applies a scorer qualification run: rejected (the
  generator failed to build, crashed, emitted an invalid pack, drifted between
  identical runs, or reproduced another owner's generator on the probe seed)
  or qualified for round columns. Rounds, where generators are actually
  scored against agents, live in :mod:`ditto.api_server.generator_rounds`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import text

from ditto.api_models.generator import (
    MAX_SOURCE_ARCHIVE_BYTES,
    GeneratorCaseStatusCounts,
    GeneratorCompetitionSettings,
    GeneratorResultRequest,
    GeneratorRunSummary,
    GeneratorScorerFault,
    GeneratorSeedOutput,
    GeneratorSubmissionSummary,
)
from ditto.api_models.generator_ledger import GeneratorLedgerResponse
from ditto.db.models import GeneratorCompetitionSettingsRevision
from ditto.db.queries.generator import (
    abandon_run,
    clear_lease,
    current_probe_epoch,
    earlier_probe_results,
    finish_run,
    latest_generator_settings_revision,
    lock_next_runnable_submission,
    lock_probe_epoch_if_current,
    new_seeds,
    start_run,
    upsert_probe_result,
)
from ditto.db.queries.scores import emission_owner, list_eligible_ledger
from ditto.generator_archive import ArchiveInfo, ArchiveRejected, inspect_archive
from ditto.score_order import rank_submissions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.generator_identity import GeneratorIdentity
    from ditto.db.models import GeneratorRun, GeneratorSubmission

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = GeneratorCompetitionSettings()

# Serializes result recording so two qualification runs recorded at once
# cannot both pass the copy check against each other.
_SCORING_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"generator-competition-scoring").digest()[:8], "big", signed=True
)


@dataclass(frozen=True)
class ResolvedSettings:
    settings: GeneratorCompetitionSettings
    revision: int
    checksum: str
    valid: bool


def settings_checksum(settings: GeneratorCompetitionSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def settings_from_row(
    row: GeneratorCompetitionSettingsRevision | None,
) -> tuple[GeneratorCompetitionSettings, bool]:
    """Decode a revision; a corrupt payload falls back to the closed default."""
    if row is None:
        return DEFAULT_SETTINGS, True
    try:
        return GeneratorCompetitionSettings.model_validate(row.settings), True
    except ValidationError:
        logger.warning(
            "generator competition settings revision %s is invalid; lane closed",
            row.revision,
            exc_info=True,
        )
        return DEFAULT_SETTINGS, False


async def load_settings(session: AsyncSession) -> ResolvedSettings:
    row = await latest_generator_settings_revision(session)
    settings, valid = settings_from_row(row)
    return ResolvedSettings(
        settings=settings,
        revision=row.revision if row is not None else 0,
        checksum=row.checksum if row is not None else "",
        valid=valid,
    )


async def settings_at(
    session: AsyncSession, revision: int | None
) -> GeneratorCompetitionSettings:
    """The policy as of ``revision``: the one a run or round started under.

    Work leased under one revision is judged by it, so a settings write in
    flight cannot change how already-leased work is scored. A missing or
    corrupt revision falls back to the governing one.
    """
    if revision:
        row = await session.get(GeneratorCompetitionSettingsRevision, revision)
        if row is not None:
            settings, valid = settings_from_row(row)
            if valid:
                return settings
    return (await load_settings(session)).settings


class UploadRejected(ValueError):
    """The upload cannot be a v1 generator source archive."""


@dataclass(frozen=True)
class InspectedUpload:
    raw: bytes
    info: ArchiveInfo


def inspect_upload(*, source_base64: str, source_sha256: str) -> InspectedUpload:
    """Decode and check upload bytes. Raises :class:`UploadRejected`."""
    try:
        raw = base64.b64decode(source_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadRejected("source_base64 is not valid base64") from exc
    return inspect_source(raw, source_sha256=source_sha256)


def inspect_source(raw: bytes, *, source_sha256: str) -> InspectedUpload:
    """Check raw archive bytes. Raises :class:`UploadRejected`."""
    if not raw or len(raw) > MAX_SOURCE_ARCHIVE_BYTES:
        raise UploadRejected(
            f"source archive must be 1..{MAX_SOURCE_ARCHIVE_BYTES} bytes"
        )
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        raise UploadRejected("source_sha256 does not match the uploaded bytes")
    try:
        info = inspect_archive(raw)
    except ArchiveRejected as exc:
        raise UploadRejected(str(exc)) from exc
    return InspectedUpload(raw=raw, info=info)


async def select_panel(
    session: AsyncSession, *, size: int | None
) -> list[dict[str, Any]]:
    """The top ``size`` owners on the authoritative ledger, best first
    (``None``: every ranked owner).

    One agent per owner (the ledger already dedupes owners); the first is the
    champion. Owner keys use the emission owner space, matching packs'
    ``owner_key``, so a generator's own agents are recognized.
    """
    rows = [
        row
        for row in await list_eligible_ledger(
            session, include_fingerprints=False, include_details=False
        )
        if row.eligible
    ]
    ranked = rank_submissions(rows)[:size]
    return [
        {
            "agent_id": str(row.agent_id),
            "owner_key": emission_owner(
                miner_hotkey=row.miner_hotkey, miner_coldkey=row.miner_coldkey
            ),
            "champion": index == 0,
        }
        for index, row in enumerate(ranked)
    ]


# A submission whose leases keep expiring backs off instead of being re-leased
# in a tight loop; a scan bound keeps one lease call cheap.
_MAX_LEASE_SCAN = 8


@dataclass(frozen=True)
class Lease:
    submission: GeneratorSubmission
    run: GeneratorRun
    probe_seed: str


async def lease_next(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> Lease | None:
    """Lease the most overdue unqualified submission on fresh seeds (caller
    commits). Returns ``None`` when nothing needs qualifying."""
    settings = resolved.settings
    if settings.mode == "off":
        return None
    for _ in range(_MAX_LEASE_SCAN):
        row = await lock_next_runnable_submission(session, now=now)
        if row is None:
            return None
        if row.lease_run_id is not None:
            # The previous run's lease expired: its seeds were exposed to a
            # scorer that never reported, so they are retired with the run.
            await abandon_run(session, row.lease_run_id, now=now)
            clear_lease(row, now=now)
        if row.attempts >= settings.max_attempts:
            row.attempts = 0
            row.next_run_at = now + timedelta(seconds=settings.retry_backoff_seconds)
            row.status_reason = "lease attempts exhausted; backing off"
            await session.flush()
            continue
        probe = await current_probe_epoch(session)
        assert probe is not None
        run = await start_run(
            session,
            row,
            now=now,
            ttl=timedelta(seconds=settings.lease_ttl_seconds),
            seeds=new_seeds(settings.seeds_per_run),
            probe_epoch=probe.epoch,
            cases_per_seed=settings.cases_per_seed,
            panel=[],
            settings_revision=resolved.revision,
        )
        return Lease(submission=row, run=run, probe_seed=probe.seed)
    return None


class ResultRejected(ValueError):
    """The scorer result does not match the lease (stale, wrong run, bad refs)."""


@dataclass(frozen=True)
class RecordedResult:
    status: str
    outcome: str
    score: float | None
    submission_status: str


_MAX_ISSUES = 50


def _seed_summary(output: GeneratorSeedOutput) -> dict[str, Any]:
    v = output.validation
    return {
        "seed": output.seed,
        "pack_sha256": output.pack_sha256,
        "valid": output.valid,
        "case_count": v.case_count,
        "pair_count": v.pair_count,
        "issues": [i.model_dump(mode="json") for i in (v.issues or [])[:_MAX_ISSUES]],
        "issue_count": len(v.issues or []),
        "error": output.error,
        "duration_ms": output.duration_ms,
    }


def _generator_failure(payload: GeneratorResultRequest) -> tuple[str, str] | None:
    """The generator's own failure in an evaluation, as (outcome, detail)."""
    if not payload.build.ok:
        return "build_failed", payload.build.error or "build failed"
    for index, seed in enumerate(payload.seeds or ()):
        if seed.error:
            return "generation_failed", f"seed {index}: {seed.error}"
        if not seed.validation.valid:
            issues = seed.validation.issues or []
            first = f"[{issues[0].rule}] {issues[0].message}" if issues else "invalid"
            return "invalid_pack", f"seed {index}: {len(issues)} issue(s); {first}"
    probe = payload.probe
    if probe is not None and not probe.valid:
        return "invalid_probe", probe.error or "the probe seed produced an invalid pack"
    if payload.deterministic is False:
        return "nondeterministic", "re-running the first seed produced different bytes"
    return None


# The messages challenge.GenerateRequest.Validate returns. A scorer before
# genrun.ErrInvalidRequest wrote them into a seed's error as if the generator
# had failed; they are the scorer refusing the request it was leased, never
# the generator's output (generator failures are prefixed "generator ").
_SCORER_REQUEST_ERROR = re.compile(
    r"^request (?:(?:contract|seed|miner_hotkey|cases|grading_bench_version) must "
    r"|accepts grading policies )"
)


def scorer_request_error(error: str | None) -> bool:
    """Whether a reported generation error is the scorer refusing its request."""
    return bool(error) and _SCORER_REQUEST_ERROR.match(error or "") is not None


def _scorer_request_failure(payload: GeneratorResultRequest) -> str | None:
    """A legacy scorer's request refusal reported as a seed error, if any."""
    outputs = list(payload.seeds or ())
    if payload.probe is not None:
        outputs.append(payload.probe)
    for output in outputs:
        if scorer_request_error(output.error):
            return output.error
    return None


def _reject(row: GeneratorSubmission, reason: str, *, now: datetime) -> None:
    row.status = "rejected"
    row.status_reason = reason[:1000]
    row.updated_at = now


async def record_result(
    session: AsyncSession,
    row: GeneratorSubmission,
    run: GeneratorRun,
    payload: GeneratorResultRequest,
    *,
    probe_seed: str,
    now: datetime,
) -> RecordedResult:
    """Apply one scorer evaluation to a row-locked, leased run (caller commits)."""
    if (
        run.status != "leased"
        or row.lease_id != payload.lease_id
        or row.lease_run_id != run.run_id
    ):
        raise ResultRejected("lease is not current for this run")
    expires = row.lease_expires_at
    if expires is not None and expires.replace(tzinfo=expires.tzinfo or UTC) <= now:
        # The lease lapsed: the submission may already be leased again on new
        # seeds, so this late evaluation must not decide anything.
        raise ResultRejected("lease expired before the result arrived")
    if payload.source_sha256 != row.source_sha256:
        raise ResultRejected("result pins a different source archive")
    seeds = payload.seeds or []
    if payload.build.ok:
        if [s.seed for s in seeds] != list(run.seeds):
            raise ResultRejected("result seeds do not match the run's seeds")
        if payload.probe is None or payload.probe.seed != probe_seed:
            raise ResultRejected("result probe does not match the run's probe seed")
        if all(s.valid for s in seeds) and payload.deterministic is None:
            raise ResultRejected("result is missing the determinism check")

    settings = await settings_at(session, run.settings_revision)
    clear_lease(row, now=now)
    row.attempts = 0
    run.scorer_version = payload.scorer_version
    run.evaluation = {
        "sandbox": payload.sandbox,
        "build": payload.build.model_dump(mode="json"),
        "deterministic": payload.deterministic,
        "seeds": [_seed_summary(s) for s in seeds],
        # The probe seed is shared by every generator in its epoch, so it is
        # never stored with (or served from) a run.
        "probe": (
            {k: v for k, v in _seed_summary(payload.probe).items() if k != "seed"}
            if payload.probe
            else None
        ),
    }

    refused = _scorer_request_failure(payload) if payload.build.ok else None
    if refused is not None:
        _back_off_for_scorer_fault(
            row,
            run,
            settings=settings,
            kind="unsupported_request",
            detail=refused,
            now=now,
        )
        return RecordedResult(
            "abandoned", "scorer_unsupported_request", None, row.status
        )

    failure = _generator_failure(payload)
    if failure is not None:
        outcome, detail = failure
        finish_run(run, status="invalid", outcome=outcome, now=now)
        _reject(row, f"{outcome}: {detail}", now=now)
        return RecordedResult("invalid", outcome, None, row.status)

    for seed in seeds:
        refs = seed.validation.cases or ()
        if len(refs) != run.cases_per_seed or len({r.case_id for r in refs}) != len(
            refs
        ):
            raise ResultRejected(
                "validation case refs do not match the run's case count"
            )
    assert payload.probe is not None
    probe_fingerprints = sorted(
        {c.fingerprint for c in payload.probe.validation.cases or ()}
    )
    if not probe_fingerprints:
        raise ResultRejected("valid probe carries no case refs")

    if not await lock_probe_epoch_if_current(session, run.probe_epoch):
        # The probe rotated while this run was leased. Its probe answers a seed
        # no current submission is compared on, so it cannot qualify; the
        # submission is re-run promptly on the new probe.
        finish_run(run, status="abandoned", outcome="probe_rotated", now=now)
        row.next_run_at = now
        return RecordedResult("abandoned", "probe_rotated", None, row.status)

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCORING_LOCK_KEY}
    )
    await upsert_probe_result(
        session,
        submission_id=row.submission_id,
        epoch=run.probe_epoch,
        run_id=run.run_id,
        fingerprints=probe_fingerprints,
        now=now,
    )
    mine = set(probe_fingerprints)
    for earlier, fingerprints in await earlier_probe_results(
        session,
        epoch=run.probe_epoch,
        exclude_owner_key=row.owner_key,
        submitted_before=row.submitted_at,
    ):
        overlap = len(mine & set(fingerprints)) / len(mine)
        if overlap >= settings.copy_overlap_threshold:
            run.score_summary = {
                "copy_of": str(earlier.submission_id),
                "copy_of_public_default": earlier.public_default,
                "overlap": overlap,
                "probe_epoch": run.probe_epoch,
            }
            finish_run(run, status="copy", outcome="behavioral_copy", now=now)
            _reject(
                row,
                f"behavioral_copy: reproduces {overlap:.0%} of submission "
                f"{earlier.submission_id}'s cases on the probe seed",
                now=now,
            )
            return RecordedResult("copy", "behavioral_copy", None, row.status)

    run.score_summary = {
        "cases_per_seed": run.cases_per_seed,
        "seeds": len(seeds),
        "probe_epoch": run.probe_epoch,
        "probe_cases": len(probe_fingerprints),
    }
    finish_run(run, status="qualified", outcome="qualified", now=now)
    if row.status == "active":
        row.status_reason = None
        # An operator re-qualification requested while this run was leased set
        # next_run_at after the lease began. That request stands: the run is
        # recorded, but the generator stays unqualified until the new run.
        requested = row.next_run_at
        leased = run.leased_at
        if requested is None or requested.replace(
            tzinfo=requested.tzinfo or UTC
        ) <= leased.replace(tzinfo=leased.tzinfo or UTC):
            row.qualified_at = now
    return RecordedResult("qualified", "qualified", None, row.status)


def _back_off_for_scorer_fault(
    row: GeneratorSubmission,
    run: GeneratorRun,
    *,
    settings: GeneratorCompetitionSettings,
    kind: str,
    detail: str,
    now: datetime,
) -> None:
    """Retire a run the scorer could not do, without blaming the generator.

    The run's seeds were exposed, so it is abandoned, and the submission
    stays active and backs off for ``retry_backoff_seconds`` (a stale scorer
    would otherwise fail every lease in a tight loop).
    """
    finish_run(run, status="abandoned", outcome=f"scorer_{kind}", now=now)
    clear_lease(row, now=now)
    row.next_run_at = now + timedelta(seconds=settings.retry_backoff_seconds)
    if row.status == "active":
        row.status_reason = f"scorer fault ({kind}); retrying: {detail}"[:1000]
    row.updated_at = now


async def record_scorer_fault(
    session: AsyncSession,
    row: GeneratorSubmission,
    run: GeneratorRun,
    payload: GeneratorScorerFault,
    *,
    now: datetime,
) -> RecordedResult:
    """Apply a scorer-reported fault to a row-locked, leased run (caller commits)."""
    if (
        run.status != "leased"
        or row.lease_id != payload.lease_id
        or row.lease_run_id != run.run_id
    ):
        raise ResultRejected("lease is not current for this run")
    expires = row.lease_expires_at
    if expires is not None and expires.replace(tzinfo=expires.tzinfo or UTC) <= now:
        raise ResultRejected("lease expired before the fault arrived")
    settings = await settings_at(session, run.settings_revision)
    run.scorer_version = payload.scorer_version
    run.evaluation = {
        "scorer_fault": {
            "error_kind": payload.error_kind,
            "detail": payload.detail[:2000],
        }
    }
    _back_off_for_scorer_fault(
        row,
        run,
        settings=settings,
        kind=payload.error_kind,
        detail=payload.detail,
        now=now,
    )
    return RecordedResult("abandoned", f"scorer_{payload.error_kind}", None, row.status)


def run_summary(
    run: GeneratorRun | None, counts: dict[str, int] | None
) -> GeneratorRunSummary | None:
    if run is None:
        return None
    return GeneratorRunSummary(
        run_id=run.run_id,
        attempt=run.attempt,
        status=run.status,  # type: ignore[arg-type]
        outcome=run.outcome,
        score=run.score,
        case_counts=GeneratorCaseStatusCounts(**counts) if counts else None,
        leased_at=run.leased_at,
        finished_at=run.finished_at,
    )


def submission_summary(
    row: GeneratorSubmission,
    latest: GeneratorRun | None,
    counts: dict[str, int] | None,
    identity: GeneratorIdentity | None = None,
) -> GeneratorSubmissionSummary:
    """A submission's public summary; ``identity`` adds its public name,
    avatar and version (operator views may omit it)."""
    return GeneratorSubmissionSummary(
        submission_id=row.submission_id,
        miner_hotkey=row.miner_hotkey,
        source_sha256=row.source_sha256,
        tree_sha256=row.tree_sha256,
        module=row.module,
        go_version=row.go_version,
        file_count=row.file_count,
        size_bytes=row.size_bytes,
        status=row.status,  # type: ignore[arg-type]
        status_reason=row.status_reason,
        next_run_at=(
            row.next_run_at
            if row.status == "active" and row.qualified_at is None
            else None
        ),
        submitted_at=row.submitted_at,
        latest_run=run_summary(latest, counts),
        qualified_at=row.qualified_at,
        public_default=row.public_default,
        round_score=row.round_score,
        round_scored_index=row.round_scored_index,
        name=identity.name if identity is not None else row.name,
        name_handle=identity.name_handle if identity is not None else None,
        avatar_url=identity.avatar_url if identity is not None else None,
        version=identity.version if identity is not None else None,
    )


async def build_ledger(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> GeneratorLedgerResponse:
    """The generator ledger: each hotkey's latest round column score."""
    from ditto.api_server.generator_rounds import build_round_ledger

    return await build_round_ledger(session, resolved=resolved, now=now)
