"""Shadow DittoBench generator competition: settings, panels, and scoring.

The endpoints in ``endpoints/generator*.py`` and
``endpoints/admin_generator_competition.py`` stay thin; the decisions live
here so they are testable without HTTP:

* :func:`load_settings` resolves the governing revision (``mode=off`` when the
  table is empty or a row is corrupt, so a bad write closes the lane rather
  than opening it).
* :func:`inspect_pack` is Platform's cheap upload pre-check. The authoritative
  validation is the Go contract's, run by the scorer; this only refuses bytes
  that obviously cannot be a v1 pack for this hotkey.
* :func:`select_panel` snapshots the top ``panel_size`` owners from the same
  ledger validators fold, so a pack is judged against the agents that are
  actually winning.
* :func:`record_result` applies a scorer result: invalid, re-queued for a
  fuller panel, failed, or scored with per-case verdicts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import text

from ditto.api_models.generator import (
    MAX_PACK_BYTES,
    MAX_PACK_CASES,
    MIN_PACK_CASES,
    PACK_CONTRACT_V1,
    GeneratorCompetitionSettings,
    GeneratorResultRequest,
)
from ditto.api_models.generator_ledger import (
    GeneratorLedgerEntry,
    GeneratorLedgerResponse,
)
from ditto.db.models import GeneratorCaseVerdict
from ditto.db.queries.generator import (
    best_scored_packs,
    claimed_fingerprints,
    finish_pack,
    latest_generator_settings_revision,
)
from ditto.db.queries.scores import emission_owner, list_eligible_ledger
from ditto.generator_scoring import (
    CASE_INSUFFICIENT_PANEL,
    PACK_INSUFFICIENT_PANEL,
    CaseRef,
    CaseResult,
    PanelMember,
    ScoreParams,
    score_cases,
)
from ditto.score_order import rank_submissions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.db.models import GeneratorCompetitionSettingsRevision, GeneratorPack

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = GeneratorCompetitionSettings()

# Serializes result recording so two packs scored at once cannot both claim
# the same new case fingerprint.
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


class PackRejected(ValueError):
    """The upload cannot be a v1 pack for this hotkey."""


@dataclass(frozen=True)
class PackHeader:
    raw: bytes
    case_count: int
    generator_name: str
    generator_version: str


def inspect_pack(*, pack_base64: str, pack_sha256: str, hotkey: str) -> PackHeader:
    """Decode and pre-check upload bytes. Raises :class:`PackRejected`."""
    try:
        raw = base64.b64decode(pack_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PackRejected("pack_base64 is not valid base64") from exc
    if not raw or len(raw) > MAX_PACK_BYTES:
        raise PackRejected(f"pack must be 1..{MAX_PACK_BYTES} bytes")
    if hashlib.sha256(raw).hexdigest() != pack_sha256:
        raise PackRejected("pack_sha256 does not match the uploaded bytes")
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackRejected("pack is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise PackRejected("pack must be a JSON object")
    if doc.get("contract") != PACK_CONTRACT_V1:
        raise PackRejected(f"pack contract must be {PACK_CONTRACT_V1!r}")
    if doc.get("miner_hotkey") != hotkey:
        raise PackRejected("pack miner_hotkey must equal the signing hotkey")
    cases = doc.get("cases")
    if (
        not isinstance(cases, list)
        or not MIN_PACK_CASES <= len(cases) <= MAX_PACK_CASES
    ):
        raise PackRejected(f"pack must have {MIN_PACK_CASES}..{MAX_PACK_CASES} cases")
    generator = doc.get("generator")
    if not isinstance(generator, dict):
        raise PackRejected("pack generator must be an object")
    name, version = generator.get("name"), generator.get("version")
    if not (isinstance(name, str) and 0 < len(name) <= 64):
        raise PackRejected("pack generator.name must be a 1..64 character string")
    if not (isinstance(version, str) and 0 < len(version) <= 64):
        raise PackRejected("pack generator.version must be a 1..64 character string")
    return PackHeader(
        raw=raw, case_count=len(cases), generator_name=name, generator_version=version
    )


async def select_panel(session: AsyncSession, *, size: int) -> list[dict[str, Any]]:
    """The top ``size`` owners on the authoritative ledger, best first.

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


class ResultRejected(ValueError):
    """The scorer result does not match the lease (stale, wrong pack, bad refs)."""


@dataclass(frozen=True)
class RecordedResult:
    status: str
    outcome: str
    score: float | None


async def record_result(
    session: AsyncSession,
    row: GeneratorPack,
    payload: GeneratorResultRequest,
    *,
    resolved: ResolvedSettings,
    now: datetime,
) -> RecordedResult:
    """Apply one scorer result to a row-locked, leased pack (caller commits)."""
    if row.status != "leased" or row.lease_id != payload.lease_id:
        raise ResultRejected("lease is not current for this pack")
    if payload.pack_sha256 != row.pack_sha256:
        raise ResultRejected("result pins a different pack")
    settings = resolved.settings
    validation = payload.validation
    row.validation = validation.model_dump(mode="json")
    row.scorer_version = payload.scorer_version
    row.settings_revision = resolved.revision

    if not validation.valid:
        finish_pack(row, status="invalid", outcome="invalid")
        row.scored_at = now
        return RecordedResult(status="invalid", outcome="invalid", score=None)

    refs = [CaseRef(c.case_id, c.fingerprint) for c in validation.cases or ()]
    if len(refs) != row.case_count or len({r.case_id for r in refs}) != len(refs):
        raise ResultRejected("validation case refs do not match the stored pack")

    snapshot = {slot["agent_id"]: slot for slot in row.panel or ()}
    members: list[PanelMember] = []
    for member in payload.panel or ():
        slot = snapshot.get(member.agent_id)
        if slot is None:
            # Only agents Platform leased the pack against may count, under the
            # owner key and champion flag Platform assigned.
            logger.warning(
                "generator pack %s: ignoring result for non-panel agent %s",
                row.pack_id,
                member.agent_id,
            )
            continue
        members.append(
            PanelMember(
                agent_id=member.agent_id,
                owner_key=str(slot["owner_key"]),
                champion=bool(slot.get("champion", False)),
                results=tuple(
                    CaseResult(r.case_id, r.score, r.fault)
                    for r in member.results or ()
                ),
            )
        )
    row.panel_results = [
        {
            "agent_id": m.agent_id,
            "owner_key": m.owner_key,
            "champion": m.champion,
            "results": [r.__dict__ for r in m.results],
        }
        for m in members
    ]

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCORING_LOCK_KEY}
    )
    claimed = await claimed_fingerprints(
        session, fingerprints=[r.fingerprint for r in refs], exclude_pack_id=row.pack_id
    )
    scored = score_cases(
        refs,
        members,
        ScoreParams.from_mapping(settings.score_params()),
        generator_owner_key=row.owner_key,
        claimed_fingerprints=claimed,
    )

    if scored.status == PACK_INSUFFICIENT_PANEL:
        short = sum(1 for c in scored.cases if c.status == CASE_INSUFFICIENT_PANEL)
        row.score_summary = {"insufficient_panel_cases": short, "panel": len(members)}
        if row.attempts >= settings.max_attempts:
            finish_pack(row, status="failed", outcome="insufficient_panel_exhausted")
            return RecordedResult(
                status="failed", outcome="insufficient_panel_exhausted", score=None
            )
        finish_pack(row, status="queued", outcome="insufficient_panel")
        row.not_before = now + timedelta(seconds=settings.retry_backoff_seconds)
        return RecordedResult(status="queued", outcome="insufficient_panel", score=None)

    for position, verdict in enumerate(scored.cases):
        session.add(
            GeneratorCaseVerdict(
                pack_id=row.pack_id,
                case_id=verdict.case_id,
                position=position,
                fingerprint=verdict.fingerprint,
                status=verdict.status,
                panel_size=verdict.panel_size,
                solvers=verdict.solvers,
                solver_owners=verdict.solver_owners,
                mean_score=verdict.mean_score,
                champion_failed=verdict.champion_failed,
                credit=verdict.credit,
            )
        )
    finish_pack(row, status="scored", outcome="scored")
    row.score = scored.score
    row.scored_at = now
    row.score_summary = {
        "raw_credit": scored.raw_credit,
        "discriminating": scored.discriminating,
        "trivial": scored.trivial,
        "unsolved": scored.unsolved,
        "duplicate": scored.duplicate,
        "unsolved_rate": scored.unsolved_rate,
        "panel": len(members),
        "params": settings.score_params(),
    }
    return RecordedResult(status="scored", outcome="scored", score=scored.score)


async def build_ledger(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> GeneratorLedgerResponse:
    """Each hotkey's best pack scored within the freshness window.

    Shadow only: every entry is ``weight_eligible=False``. ``mode=off`` serves
    an empty ledger, which folds to zero generator emission.
    """
    settings = resolved.settings
    if settings.mode == "off":
        return GeneratorLedgerResponse(mode="off", window_days=settings.window_days)
    rows = await best_scored_packs(
        session, since=now - timedelta(days=settings.window_days)
    )
    entries = [
        GeneratorLedgerEntry(
            miner_hotkey=row.miner_hotkey,
            pack_id=row.pack_id,
            weight_eligible=False,
            score=float(row.score or 0.0),
            discriminating_cases=int(
                (row.score_summary or {}).get("discriminating", 0)
            ),
            first_seen=row.submitted_at,
            scored_at=row.scored_at or row.submitted_at,
        )
        for row in rows
    ]
    return GeneratorLedgerResponse(
        entries=entries,
        count=len(entries),
        window_days=settings.window_days,
        mode=settings.mode,
        generated_at=now,
    )
