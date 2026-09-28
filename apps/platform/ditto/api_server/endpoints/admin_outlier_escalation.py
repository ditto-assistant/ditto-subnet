"""Backroom read of the anomalous-score outlier escalation posture (issue #476).

The escalation that can open ATH holds on out-of-band high composites is
configured only by environment variables read once at import. This endpoint
makes that posture, the source of every value, and the gate's audit-chain
activity visible to operators, and replays the gate over the current ledger so
an operator can see what a threshold would hold before enforcing it.
Read-only: it changes no setting, hold, or row.
"""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Annotated, Any, cast, get_args

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.outlier_escalation import (
    AdminOutlierEscalationDryRunResponse,
    AdminOutlierEscalationResponse,
    OutlierEscalationActivityView,
    OutlierEscalationDryRunEntryView,
    OutlierEscalationEntryView,
    OutlierEscalationEvidence,
    OutlierEscalationMode,
    OutlierEscalationSettingSourcesView,
    OutlierEscalationSettingsView,
    OutlierSettingField,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import validator as validator_endpoints
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.outlier_escalation import (
    OUTLIER_ALGORITHM_VERSION,
    OUTLIER_ESCALATION_ENV_VARS,
    OUTLIER_REVIEW_KIND,
    OutlierEscalationSettings,
    evaluate_score_outlier,
    median_mad,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.outlier_escalation import load_outlier_escalation_activity
from ditto.db.queries.scores import list_eligible_ledger

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

DEFAULT_RECENT_LIMIT = 20
MAX_RECENT_LIMIT = 100
DEFAULT_WINDOW_HOURS = 24 * 7
MAX_WINDOW_HOURS = 24 * 30


def _finite(value: float) -> float | None:
    # The loader accepts "nan"/"inf" (unchanged legacy parsing). Report them as
    # an explicit null rather than letting JSON serialization do it silently.
    return value if math.isfinite(value) else None


def _settings_view(
    settings: OutlierEscalationSettings,
) -> OutlierEscalationSettingsView:
    return OutlierEscalationSettingsView(
        mode=cast(OutlierEscalationMode, settings.mode),
        min_bench_version=settings.min_bench_version,
        min_cohort_size=settings.min_cohort_size,
        modified_z_threshold=_finite(settings.modified_z_threshold),
        min_composite_floor=_finite(settings.min_composite_floor),
    )


@router.get(
    "/admin/outlier-escalation",
    response_model=AdminOutlierEscalationResponse,
)
async def get_outlier_escalation(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_RECENT_LIMIT)] = DEFAULT_RECENT_LIMIT,
    window_hours: Annotated[
        int, Query(ge=1, le=MAX_WINDOW_HOURS)
    ] = DEFAULT_WINDOW_HOURS,
) -> AdminOutlierEscalationResponse:
    """Effective escalation settings with sources, plus recent activity."""
    # Read the module attributes at request time: these are the exact objects
    # the scoring path passes to the escalation, not a re-parse of os.environ.
    loaded = validator_endpoints.OUTLIER_ESCALATION_SETTINGS_LOAD
    effective = validator_endpoints.OUTLIER_ESCALATION_SETTINGS
    sources = asdict(loaded.sources)
    now = datetime.now(UTC)
    activity = await load_outlier_escalation_activity(
        session, now=now, window_hours=window_hours, limit=limit
    )
    return AdminOutlierEscalationResponse(
        generated_at=now,
        settings_loaded_at=loaded.loaded_at,
        settings=_settings_view(effective),
        defaults=_settings_view(OutlierEscalationSettings()),
        sources=OutlierEscalationSettingSourcesView.model_validate(sources),
        env_vars=dict(OUTLIER_ESCALATION_ENV_VARS),
        invalid_env_fields=[
            cast(OutlierSettingField, name)
            for name in get_args(OutlierSettingField)
            if sources[name] == "default_invalid_env"
        ],
        review_kind=OUTLIER_REVIEW_KIND,
        algorithm_version=OUTLIER_ALGORITHM_VERSION,
        pending_review_count=activity.pending_review_count,
        activity=OutlierEscalationActivityView(
            window_hours=activity.window_hours,
            window_started_at=activity.window_started_at,
            observed_total=activity.observed_total,
            enforced_total=activity.enforced_total,
            observed_in_window=activity.observed_in_window,
            enforced_in_window=activity.enforced_in_window,
            latest_recorded_at=activity.latest_recorded_at,
            recent_limit=activity.recent_limit,
            recent=[
                OutlierEscalationEntryView(
                    seq=entry.seq,
                    agent_id=entry.agent_id,
                    recorded_at=entry.recorded_at,
                    enforced=entry.enforced,
                    bench_version=entry.bench_version,
                    algorithm_version=entry.algorithm_version,
                    evidence=OutlierEscalationEvidence.model_validate(entry.evidence),
                )
                for entry in activity.recent
            ],
            recent_truncated=activity.recent_truncated,
        ),
    )


@router.get(
    "/admin/outlier-escalation/dry-run",
    response_model=AdminOutlierEscalationDryRunResponse,
)
async def get_outlier_escalation_dry_run(
    _admin: AdminDep,
    session: SessionDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
    min_cohort_size: Annotated[int | None, Query(ge=1, le=1000)] = None,
    modified_z_threshold: Annotated[float | None, Query(gt=0, le=1000)] = None,
    min_composite_floor: Annotated[float | None, Query(ge=0, le=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_RECENT_LIMIT)] = DEFAULT_RECENT_LIMIT,
) -> AdminOutlierEscalationDryRunResponse:
    """Which current ledger rows the escalation would hold, whatever the mode.

    Replays :func:`evaluate_score_outlier` -- the exact decision scoring calls
    -- over the ledger scoring reads at finalization
    (``list_eligible_ledger(bench_version=...)``, one ``scored`` row per owner,
    median-row composite). Each row is judged against the other rows, as the
    finalizing candidate is judged against a ledger it is not yet in. Agents
    already held are outside that ledger and are not replayed.

    Where it differs from each row's own finalization: the cohort is today's
    ledger, not the ledger at that time; only an owner's representative row is
    replayed, and its cohort omits that owner, whose earlier best was a peer at
    finalization; and the candidate composite is the median score row, equal
    to the finalization ``statistics.median`` for an odd score count such as
    the three-validator quorum.
    """
    effective = validator_endpoints.OUTLIER_ESCALATION_SETTINGS
    overrides: dict[OutlierSettingField, Any] = {
        "min_cohort_size": min_cohort_size,
        "modified_z_threshold": modified_z_threshold,
        "min_composite_floor": min_composite_floor,
    }
    settings = replace(
        effective,
        **{name: value for name, value in overrides.items() if value is not None},
    )
    version = (
        bench_version
        if bench_version is not None
        else await active_bench_version(session)
    )
    # The same selection scoring uses; the sketch and details columns are only
    # dropped because the replay reads nothing but composites.
    ledger = await list_eligible_ledger(
        session,
        bench_version=version,
        include_fingerprints=False,
        include_details=False,
    )
    composites = [row.composite for row in ledger]
    held = []
    for index, row in enumerate(ledger):
        decision = evaluate_score_outlier(
            composite=row.composite,
            cohort=composites[:index] + composites[index + 1 :],
            settings=settings,
        )
        if decision.held:
            held.append((row, decision.evidence))
    held.sort(key=lambda item: (-item[0].composite, str(item[0].agent_id)))
    ledger_median, ledger_mad = median_mad(composites) if composites else (None, None)
    cohort_size = max(len(ledger) - 1, 0)
    return AdminOutlierEscalationDryRunResponse(
        generated_at=datetime.now(UTC),
        bench_version=version,
        bench_version_in_scope=version >= settings.min_bench_version,
        settings=_settings_view(settings),
        overridden_fields=[
            name for name, value in overrides.items() if value is not None
        ],
        ledger_size=len(ledger),
        cohort_size=cohort_size,
        cohort_too_small=cohort_size < settings.min_cohort_size,
        ledger_median=ledger_median,
        ledger_mad=ledger_mad,
        would_trigger_count=len(held),
        limit=limit,
        would_trigger=[
            OutlierEscalationDryRunEntryView(
                agent_id=row.agent_id,
                miner_hotkey=row.miner_hotkey,
                evidence=OutlierEscalationEvidence.model_validate(evidence),
            )
            for row, evidence in held[:limit]
        ],
        truncated=len(held) > limit,
    )
