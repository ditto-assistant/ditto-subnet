"""Backroom read of the anomalous-score outlier escalation posture (issue #476).

The escalation that can open ATH holds on out-of-band high composites is
configured only by environment variables read once at import. This endpoint
makes that posture, the source of every value, and the gate's audit-chain
activity visible to operators. Read-only: it changes no setting, hold, or row.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, cast, get_args

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.outlier_escalation import (
    AdminOutlierEscalationResponse,
    OutlierEscalationActivityView,
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
)
from ditto.db.queries.outlier_escalation import load_outlier_escalation_activity

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
