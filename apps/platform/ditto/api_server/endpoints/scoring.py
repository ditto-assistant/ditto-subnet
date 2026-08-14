"""Scoring-pool endpoint — the validator best-score ledger used for weights.

``GET /scoring/scores`` returns one entry per active miner: that miner's best
*eligible* score (status ``scored``), highest composite first. This is the
persistent ledger that fixes the one-epoch-weight bug — the validator computes
KOTH+ATH weights from this pool every epoch instead of from the transient
``evaluating`` sweep, so a scored agent keeps its weight until genuinely
dethroned rather than being zeroed the moment it leaves the queue.

**The weight fold is NOT here.** Per the repo boundary (CLAUDE.md) and PROJECT.md
D3, weights are computed validator-side: every validator reads this identical
pool and runs the same deterministic function, so Yuma consensus clips any
deviator. The platform only exposes the raw, signed scores.

The raw pool remains validator-only even though public projections expose a safe
subset of benchmark results. Reads require a fresh signature from a
chain-registered, validator-permitted hotkey; a public hotkey is an identity, not
proof that the caller controls it.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from ditto.api_models import ConfirmationScoreRecord, LedgerEntry, LedgerResponse
from ditto.api_models.upload import _SS58_PATTERN
from ditto.api_models.validator import (
    LedgerScoreProof,
    V9BaseEvidence,
    V9ConfirmationReceipt,
)
from ditto.api_server.continual_retest_settings import (
    aggregate_is_active,
    tie_weighting_is_active,
)
from ditto.api_server.efficiency import ensure_efficiency_state
from ditto.api_server.endpoints.validator import (
    ChainDep,
    SessionDep,
    ValidatorAuthError,
    _assert_validator_permitted,
    _verify_signature,
)
from ditto.db.models import Score
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_scores import (
    confirmation_composites_by_seed,
    confirmation_history_by_agent,
)
from ditto.db.queries.heartbeats import (
    live_validator_fleet_supports_protocol,
)
from ditto.db.queries.score_ranking import (
    EfficiencyFactorRequesterNotReady,
    dedupe_owner_rows,
    official_composites,
    resolve_efficiency_adjustments,
)
from ditto.db.queries.scores import (
    LedgerScoreProofRow,
    list_eligible_ledger,
    quorum_composites,
    quorum_ledger_proof_rows,
    v9_confirmation_enforcement_active,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scoring", tags=["scoring"])

# Serve-last-known staleness policy: how long the cached ledger may be served
# after a live DB read fails before it is refused as too stale (503). The ledger
# moves slowly (only when a sweep records a new best) and the validator's fold is
# resilient to a missed epoch, so a few minutes of last-known-good is safe and
# far better than zeroing every miner on a transient DB blip. Beyond this the
# snapshot could hide a genuine change, so we stop vouching for it.
_MAX_STALE_SECONDS = 300
_LEDGER_REQUEST_MAX_AGE = timedelta(minutes=2)
_CONTINUAL_MEAN_PROTOCOL = 14
_TIE_WEIGHTING_PROTOCOL = 20
# The first validator protocol that understands the curve-v3 downside/upside
# factor.  Older validators ignore the additive field, so exposing it to a
# mixed active-benchmark fleet would produce different KOTH/weight folds.
_BOUNDED_EFFICIENCY_FACTOR_PROTOCOL = 19


def _fleet_safe_efficiency_adjustments(
    legacy_bonuses: dict[UUID, float],
    bounded_factors: dict[UUID, float],
    *,
    factor_fleet_ready: bool,
) -> tuple[dict[UUID, float], dict[UUID, float]]:
    """Fail curve-v3 exposure closed without changing legacy bonus policy."""
    return legacy_bonuses, bounded_factors if factor_fleet_ready else {}


def _ledger_signing_message(
    validator_hotkey: str, nonce: UUID, requested_at: datetime
) -> bytes:
    """Canonical proof-of-possession bytes for one scoring-ledger read."""
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return f"validator-ledger:v1:{validator_hotkey}:{nonce}:{requested}".encode()


@dataclass
class _LedgerSnapshot:
    """The last successfully-read ledger, kept per-process for serve-last-known."""

    entries: list[LedgerEntry]
    generated_at: datetime
    active_bench_version: int
    burn_share: float = 0.0
    """The burn share that was current when this snapshot was taken.

    Carried on the snapshot rather than re-resolved on the stale path: the
    resolver reads the same database that just failed, and defaulting the field
    instead would silently hand the fleet a 0% burn for as long as the outage
    lasts. Replaying the last known share is the only answer that does not move
    emissions because of a database problem."""
    v9_confirmation_mode: Literal["enforce"] | None = None
    tie_weighting_mode: Literal["pool"] | None = None


def _composite_stderr(details: dict | None) -> float | None:
    """Read the composite standard error stashed in a score's details blob
    (mirroring how bench_version is surfaced). Returns None when absent or not a
    finite non-negative number, so a malformed value degrades to the flat-margin
    fold rather than corrupting the indifference band."""
    if not isinstance(details, dict):
        return None
    v = details.get("composite_stderr")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if f >= 0.0 and f == f and f != float("inf"):
            return f
    return None


def _score_proof(score: Score | LedgerScoreProofRow) -> LedgerScoreProof:
    details = score.details
    details = details if isinstance(details, dict) else {}
    deadline = details.get("ticket_deadline")
    try:
        parsed_deadline = (
            datetime.fromisoformat(deadline) if isinstance(deadline, str) else None
        )
    except ValueError:
        parsed_deadline = None
    transcript = details.get("transcript_sha256")
    base_evidence = details.get("base_evidence_sha256")
    base_evidence_root = details.get("v9_base")
    return LedgerScoreProof(
        validator_hotkey=score.validator_hotkey,
        run_id=score.run_id,
        composite=score.composite,
        seed=score.seed,
        bench_version=score.bench_version,
        ticket_deadline=parsed_deadline,
        transcript_sha256=transcript if isinstance(transcript, str) else None,
        base_evidence_sha256=(
            base_evidence if isinstance(base_evidence, str) else None
        ),
        base_evidence=(
            V9BaseEvidence.model_validate(base_evidence_root)
            if isinstance(base_evidence_root, dict)
            else None
        ),
        signature=score.signature,
    )


def _confirmation_composites(details: dict | None) -> list[float] | None:
    """Read the P4 per-seed confirmation composites stashed in a score's details
    blob (mirroring :func:`_composite_stderr`). Returns None unless the value is a
    list of finite floats in [0, 1], so a malformed value degrades to the raw-
    composite fold rather than corrupting the dethrone comparison. Surfaced
    as-is (any length); the validator ignores lists shorter than two."""
    if not isinstance(details, dict):
        return None
    v = details.get("confirmation_composites")
    if not isinstance(v, list) or not v:
        return None
    out: list[float] = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
        f = float(x)
        if not (0.0 <= f <= 1.0) or f != f:
            return None
        out.append(f)
    return out


def _confirmation_seeds(details: dict | None) -> list[int] | None:
    """Read the P4 confirmation CRN seeds stashed in a score's details blob,
    aligned 1:1 with :func:`_confirmation_composites`. Returns None unless the
    value is a non-empty list of non-negative ints, so a malformed value degrades
    to the unpaired dethrone band rather than corrupting the paired comparison.
    Surfaced as-is (any length); the validator pairs only over shared seeds."""
    if not isinstance(details, dict):
        return None
    v = details.get("confirmation_seeds")
    if not isinstance(v, list) or not v:
        return None
    out: list[int] = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            return None
        out.append(x)
    return out


def _quorum_stderr(composites: list[float]) -> float | None:
    """The standard error of an agent's composite from its k=3 quorum spread:
    ``stdev(composites) / sqrt(n)`` over the validators' composites, or None with
    fewer than two scores. This turns data the platform ALREADY collects (the
    quorum) into the KOTH z-band's measurement-uncertainty estimate with no
    re-score, so a plain quorum-scored champion still gets a noise-aware dethrone
    band. Only finite composites contribute; a degenerate (identical) quorum
    yields 0.0 (the band collapses to the flat margin, which is correct)."""
    xs = [c for c in composites if isinstance(c, (int, float)) and c == c]
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    var = sum((c - mean) ** 2 for c in xs) / (n - 1)
    return math.sqrt(var / n)


def _ledger_stderr(details: dict | None, quorum: list[float]) -> float | None:
    """The composite standard error to surface on a ledger entry. Prefer the
    run's own stashed ``composite_stderr`` (e.g. a confirmation re-score's pooled
    between-seed SE); else fall back to the between-validator SEM of the k=3
    quorum (:func:`_quorum_stderr`), so the KOTH z-band has an uncertainty
    estimate even for a plain quorum-scored agent, from data already collected."""
    stashed = _composite_stderr(details)
    if stashed is not None:
        return stashed
    return _quorum_stderr(quorum)


def _cached_snapshot(request: Request) -> _LedgerSnapshot | None:
    return getattr(request.app.state, "ledger_snapshot", None)


def _store_snapshot(request: Request, snapshot: _LedgerSnapshot) -> None:
    request.app.state.ledger_snapshot = snapshot


@router.get(
    "/scores",
    response_model=LedgerResponse,
    responses={
        401: {"description": "Missing/invalid validator auth."},
        503: {"description": "Chain unavailable, or no fresh/recent ledger to serve."},
    },
)
async def scores(
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
    x_validator_ledger_nonce: Annotated[UUID | None, Header()] = None,
    x_validator_ledger_requested_at: Annotated[datetime | None, Header()] = None,
    x_validator_ledger_signature: Annotated[str | None, Header()] = None,
) -> LedgerResponse:
    """Return the best eligible score per payment-time coldkey.

    Different names and hotkeys owned by one coldkey compete for one position;
    the highest eligible canonical score wins and its hotkey is returned.

    Serve-last-known: on a transient DB failure the last successfully-read ledger
    is served (flagged ``stale`` with its ``age_seconds``) rather than erroring,
    since zeroing every miner on a blip is the worse failure. Past
    :data:`_MAX_STALE_SECONDS` the cache is refused with 503 so a consumer never
    silently folds a dangerously old pool.
    """
    response.headers["Cache-Control"] = "no-store"
    if (
        x_validator_hotkey is None
        or not re.fullmatch(_SS58_PATTERN, x_validator_hotkey)
        or x_validator_ledger_nonce is None
        or x_validator_ledger_requested_at is None
        or x_validator_ledger_signature is None
        or x_validator_ledger_requested_at.tzinfo is None
    ):
        raise ValidatorAuthError("ledger request proof is missing or malformed")
    signed = _ledger_signing_message(
        x_validator_hotkey,
        x_validator_ledger_nonce,
        x_validator_ledger_requested_at,
    )
    if not _verify_signature(x_validator_hotkey, signed, x_validator_ledger_signature):
        raise ValidatorAuthError("ledger request signature did not verify")
    auth_now = datetime.now(UTC)
    if (
        abs(auth_now - x_validator_ledger_requested_at.astimezone(UTC))
        > _LEDGER_REQUEST_MAX_AGE
    ):
        raise HTTPException(status_code=409, detail="ledger request timestamp is stale")
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        x_validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=x_validator_ledger_nonce,
                validator_hotkey=x_validator_hotkey,
                now=auth_now,
                expires_at=auth_now + _LEDGER_REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError as exc:
            raise HTTPException(
                status_code=409, detail="ledger request nonce has already been used"
            ) from exc
        except SQLAlchemyError as exc:
            # Never serve cached ledger data when replay protection cannot record
            # this proof. Failing closed preserves one-time nonce semantics.
            raise HTTPException(
                status_code=503,
                detail="scoring ledger authorization temporarily unavailable",
            ) from exc
    try:
        # The validator ledger is an authority path, not a dependent of the
        # public leaderboard. Materialize this epoch before reading either the
        # ledger or its adjustment rows so a quiet dashboard cannot leave every
        # validator folding an old/missing efficiency epoch. The resolver uses
        # its independent session and the nonce transaction above is complete,
        # leaving this session clean for ensure_efficiency_state's transaction.
        efficiency_config = await request.app.state.efficiency_settings.resolve(
            getattr(request.app.state, "session_maker", None)
        )
        if efficiency_config.enabled:
            await ensure_efficiency_state(session, efficiency_config, now=auth_now)
        rows = await list_eligible_ledger(
            session,
            include_fingerprints=False,
            details_keys=(
                "composite_stderr",
                "confirmation_composites",
                "confirmation_seeds",
            ),
            dedupe_owners=False,
        )
        v9_confirmation_mode: Literal["enforce"] | None = (
            "enforce" if await v9_confirmation_enforcement_active(session) else None
        )
        # The k=3 quorum spread per agent -> composite_stderr when the run itself
        # did not stash one, so the KOTH z-band is noise-aware with no re-score.
        quorum = await quorum_composites(
            session,
            [r.agent_id for r in rows],
            bench_versions={r.agent_id: r.bench_version for r in rows},
        )
        canonical_version = await active_bench_version(session)
        history = await confirmation_history_by_agent(
            session,
            agent_ids=[r.agent_id for r in rows],
            bench_version=canonical_version,
        )
        confirmation_by_seed = await confirmation_composites_by_seed(
            session,
            agent_ids=[r.agent_id for r in rows],
            bench_version=canonical_version,
        )
        proof_rows = await quorum_ledger_proof_rows(
            session,
            [r.agent_id for r in rows],
            bench_versions={r.agent_id: r.bench_version for r in rows},
        )
        fleet_protocol_ready = await live_validator_fleet_supports_protocol(
            session,
            minimum_protocol=_CONTINUAL_MEAN_PROTOCOL,
            bench_version=canonical_version,
            now=auth_now,
        )
        continual_settings = await request.app.state.continual_retest_settings.resolve(
            getattr(request.app.state, "session_maker", None)
        )
        burn_settings = await request.app.state.burn_settings.resolve(
            getattr(request.app.state, "session_maker", None)
        )
        continual_mean_active = aggregate_is_active(
            continual_settings, fleet_protocol_ready=fleet_protocol_ready
        )
        tie_weighting_fleet_ready = await live_validator_fleet_supports_protocol(
            session,
            minimum_protocol=_TIE_WEIGHTING_PROTOCOL,
            bench_version=canonical_version,
            now=auth_now,
        )
        tie_weighting_active = tie_weighting_is_active(
            continual_settings, fleet_protocol_ready=tie_weighting_fleet_ready
        )
        # Frozen relative token-efficiency bonuses (bench_version >= 7) are
        # surfaced to validators only behind the fold flag; with it off the
        # ledger is byte-identical to the pre-bonus wire shape, and the
        # subnet's weight fold must ship its own consensus change before any
        # validator may consume these advisory fields. The enabled/fold state is
        # read at compute time from the hot-swappable policy (latest revision,
        # short TTL) so a backroom flip lands here with no restart; the
        # `fold requires enabled` invariant is enforced by the resolver.
        efficiency_bonuses, efficiency_factors = await resolve_efficiency_adjustments(
            session,
            rows=rows,
            efficiency_config=efficiency_config,
            now=auth_now,
            requesting_validator_hotkey=x_validator_hotkey,
        )
        ranking_scores = official_composites(
            rows,
            quorum=quorum,
            completed_waves=confirmation_by_seed,
            continual_mean_active=continual_mean_active,
            efficiency_bonuses=efficiency_bonuses,
            efficiency_factors=efficiency_factors,
            efficiency_fold_active=bool(efficiency_bonuses or efficiency_factors),
        )
        rows = dedupe_owner_rows(rows, scores=ranking_scores)
    except EfficiencyFactorRequesterNotReady as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc
    except SQLAlchemyError as e:
        return _serve_last_known(request, x_validator_hotkey, e)

    generated_at = datetime.now(UTC)
    entries = [
        LedgerEntry(
            miner_hotkey=r.miner_hotkey,
            agent_id=r.agent_id,
            composite=r.composite,
            n=r.n,
            # The fold anchor, not this tarball's upload time: the wire field is
            # read by exactly one thing, the validator's champion fold, and that
            # fold must anchor on the lineage. See LedgerRow.crown_first_seen.
            first_seen=r.fold_first_seen,
            sha256=r.sha256,
            size_bytes=r.size_bytes,
            run_id=r.run_id,
            seed=r.seed,
            validator_hotkey=r.validator_hotkey,
            bench_version=r.bench_version,
            signature=r.signature,
            score_proofs=[_score_proof(s) for s in proof_rows.get(r.agent_id, [])],
            composite_stderr=(
                r.v9_confirmation["full_stderr_micros"] / 1_000_000
                if r.v9_confirmation is not None
                else _ledger_stderr(r.details, quorum.get(r.agent_id, []))
            ),
            confirmation_composites=(
                _confirmation_composites(r.details)
                if continual_mean_active and r.v9_confirmation is None
                else None
            ),
            confirmation_seeds=(
                _confirmation_seeds(r.details)
                if continual_mean_active and r.v9_confirmation is None
                else None
            ),
            confirmation_history=(
                [
                    ConfirmationScoreRecord(
                        seed=row.seed,
                        composite=row.composite,
                        validator_hotkey=row.validator_hotkey,
                        bench_version=row.bench_version,
                        signature=row.signature,
                    )
                    for row in history[r.agent_id]
                ]
                if continual_mean_active
                and r.v9_confirmation is None
                and r.agent_id in history
                else None
            ),
            continual_aggregate_method=(
                "mean_after_quorum"
                if continual_mean_active and r.v9_confirmation is None
                else None
            ),
            efficiency_bonus=(efficiency_bonuses.get(r.agent_id)),
            efficiency_factor=efficiency_factors.get(r.agent_id),
            effective_composite=(
                ranking_scores[r.agent_id]
                if r.agent_id in efficiency_bonuses or r.agent_id in efficiency_factors
                else None
            ),
            v9_confirmation=(
                V9ConfirmationReceipt.model_validate(r.v9_confirmation)
                if r.v9_confirmation is not None
                else None
            ),
            status=r.status,
        )
        for r in rows
    ]
    _store_snapshot(
        request,
        _LedgerSnapshot(
            entries=entries,
            generated_at=generated_at,
            active_bench_version=canonical_version,
            burn_share=burn_settings.burn_share,
            v9_confirmation_mode=v9_confirmation_mode,
            tie_weighting_mode="pool" if tie_weighting_active else None,
        ),
    )
    logger.info(
        "validator=%s read scoring ledger: %d miner(s)",
        x_validator_hotkey,
        len(entries),
    )
    return LedgerResponse(
        entries=entries,
        active_bench_version=canonical_version,
        v9_confirmation_mode=v9_confirmation_mode,
        tie_weighting_mode="pool" if tie_weighting_active else None,
        count=len(entries),
        generated_at=generated_at,
        stale=False,
        age_seconds=0,
        # Advisory: how wide the operator currently wants the shared-seed round
        # planned. Read from the same short-TTL policy the lease path enforces,
        # so a Backroom change reaches every validator on its next ledger poll
        # rather than on a redeploy.
        continual_retest_cohort_size=continual_settings.retest_cohort_size,
        # Consensus-relevant, unlike the cohort size: this is the miner/burn
        # split the fold applies. Resolved here so every validator reads one
        # decided scalar instead of evaluating a schedule against its own clock.
        burn_share=burn_settings.burn_share,
    )


def _serve_last_known(
    request: Request, validator_hotkey: str, error: Exception
) -> LedgerResponse:
    """Serve the cached ledger on a DB failure, or 503 if there is none / too old."""
    snapshot = _cached_snapshot(request)
    if snapshot is None:
        logger.warning(
            "validator=%s ledger read failed and no cached snapshot to serve: %s",
            validator_hotkey,
            error,
        )
        raise HTTPException(
            status_code=503, detail="scoring ledger temporarily unavailable"
        ) from error
    age = int((datetime.now(UTC) - snapshot.generated_at).total_seconds())
    if age > _MAX_STALE_SECONDS:
        logger.warning(
            "validator=%s ledger read failed; cached snapshot is %ds old "
            "(> %ds max), refusing to serve stale: %s",
            validator_hotkey,
            age,
            _MAX_STALE_SECONDS,
            error,
        )
        raise HTTPException(
            status_code=503,
            detail=f"scoring ledger unavailable; last snapshot {age}s old exceeds "
            f"the {_MAX_STALE_SECONDS}s staleness limit",
        ) from error
    logger.warning(
        "validator=%s ledger read failed; serving last-known-good (%ds old, "
        "%d miner(s)): %s",
        validator_hotkey,
        age,
        len(snapshot.entries),
        error,
    )
    # Capability truth lives in the same database that just failed.  Strip the
    # additive markers rather than replaying an active snapshot into a fleet
    # that may have regressed to a legacy protocol while the database was
    # unavailable.  Preserve the established v1/v2 bonus behavior; only the new
    # protocol-19 factor and its projection fail closed here.
    entries = []
    for entry in snapshot.entries:
        had_factor = entry.efficiency_factor is not None
        entries.append(
            entry.model_copy(
                update={
                    "continual_aggregate_method": None,
                    "confirmation_composites": None,
                    "confirmation_seeds": None,
                    "confirmation_history": None,
                    "efficiency_factor": None,
                    "effective_composite": (
                        None if had_factor else entry.effective_composite
                    ),
                }
            )
        )
    return LedgerResponse(
        entries=entries,
        # Replayed from the same last-known-good snapshot as the rows. Never
        # infer rollout authority from the highest row while the DB is down.
        active_bench_version=snapshot.active_bench_version,
        v9_confirmation_mode=snapshot.v9_confirmation_mode,
        tie_weighting_mode=snapshot.tie_weighting_mode,
        count=len(entries),
        generated_at=snapshot.generated_at,
        stale=True,
        age_seconds=max(0, age),
        # Replayed, not re-resolved: see _LedgerSnapshot.burn_share.
        burn_share=snapshot.burn_share,
    )
