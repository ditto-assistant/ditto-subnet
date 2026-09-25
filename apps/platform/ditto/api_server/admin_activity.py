"""Common administrative audit boundary and deliberately narrow public projection.

No raw bodies, URLs, headers, errors, reasons or response bodies enter this log.
The actor is private. Domain ledgers retain their existing transactional evidence.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from ditto.api_models.burn_settings import BurnSettings
from ditto.api_models.confirmation_bundles import ConfirmationBundleSettings
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.copy_court_settings import CopyCourtSettings
from ditto.api_models.efficiency_settings import EfficiencyBonusSettings
from ditto.api_models.inference_concurrency_settings import InferenceConcurrencySettings
from ditto.api_models.queue_policy_settings import QueuePolicySettings
from ditto.api_models.screener_provider_settings import ScreenerProviderSettings
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.db.models import AdminActivity, AdminActivityOutcome

logger = logging.getLogger(__name__)

# Frozen public field inventory: new schema fields require an explicit privacy review.
_SETTINGS: dict[str, tuple[type[BaseModel], frozenset[str]]] = {
    "screener-review-settings": (
        ScreenerReviewSettings,
        frozenset(
            (
                "adjudicator_max_steps",
                "adjudicator_mode",
                "adjudicator_model",
                "adjudicator_timeout_seconds",
                "audit_retention_days",
                "cache_ttl_seconds",
                "clear_min_notes",
                "concern_hold_count",
                "critic_reasoning_effort",
                "fanout_shadow_concurrency",
                "fanout_shadow_daily_cost_usd",
                "fanout_shadow_global_concurrency",
                "fanout_shadow_image_source_sha",
                "fanout_shadow_max_cost_usd",
                "fanout_shadow_max_groups",
                "fanout_shadow_max_requests",
                "fanout_shadow_max_steps",
                "fanout_shadow_max_total_tokens",
                "fanout_shadow_mode",
                "fanout_shadow_model",
                "fanout_shadow_reserved_targon_slots",
                "fanout_shadow_timeout_seconds",
                "l2_always_escalate",
                "l2_fallback_models",
                "l2_model",
                "l3_enabled",
                "l3_model",
                "max_completion_tokens",
                "max_cost_usd",
                "max_input_tokens",
                "max_output_tokens",
                "max_steps",
                "mode",
                "policy_manifest_profile",
                "source_review_max_completion_tokens",
                "source_review_max_read_bytes",
                "source_review_max_steps",
                "source_review_model",
                "source_review_reasoning_effort",
                "source_review_timeout_seconds",
                "timeout_seconds",
            )
        ),
    ),
    "inference-concurrency-settings": (
        InferenceConcurrencySettings,
        frozenset(
            (
                "benchmark_runtime",
                "chat_global_concurrency",
                "chat_global_requests_per_minute",
                "chat_per_ticket_concurrency",
                "chat_per_ticket_requests_per_minute",
                "chat_per_validator_concurrency",
                "chat_per_validator_requests_per_minute",
                "chat_request_budget",
                "chat_token_budget",
                "embedding_global_concurrency",
                "embedding_global_requests_per_minute",
                "embedding_per_ticket_concurrency",
                "embedding_per_ticket_requests_per_minute",
                "embedding_per_validator_concurrency",
                "embedding_per_validator_requests_per_minute",
            )
        ),
    ),
    "validator-slot-settings": (
        ValidatorSlotSettings,
        frozenset(
            (
                "cpu_percent_ceiling",
                "disk_percent_ceiling",
                "max_concurrent_slots",
                "memory_percent_ceiling",
                "paused_validator_hotkeys",
                "resource_block_percent_ceiling",
            )
        ),
    ),
    "burn-settings": (BurnSettings, frozenset(("burn_share",))),
    "efficiency-bonus-settings": (
        EfficiencyBonusSettings,
        frozenset(
            (
                "cap",
                "cohort_size",
                "deep_cap",
                "deep_frontier_ratio",
                "enabled",
                "epoch_hours",
                "factor_alpha",
                "fold_enabled",
                "maximum_factor",
                "memory_floor",
                "min_cohort",
                "minimum_factor",
                "quality_floor",
            )
        ),
    ),
    "continual-retest-settings": (
        ContinualRetestSettings,
        frozenset(
            (
                "aggregate_mode",
                "crown_incumbent_mode",
                "idle_retests_enabled",
                "ledger_pin_mode",
                "retest_cohort_max_size",
                "retest_cohort_size",
                "retest_eligibility_mode",
                "retest_eligibility_z",
                "rollout_standdown",
                "tie_weighting_mode",
                "wave_membership",
            )
        ),
    ),
    "queue-policy-settings": (
        QueuePolicySettings,
        frozenset(
            (
                "deferred_source_review",
                "fresh_submission_slots",
                "lane_cycle_size",
                "owner_concurrent_submission_limit",
                "prev_gen_carryover",
                "priority_cohort_size",
                "rescore_cohort_size",
                "similarity_budget",
            )
        ),
    ),
    "copy-court/settings": (
        CopyCourtSettings,
        frozenset(
            (
                "byte_identical_resubmission_mode",
                "cross_miner_resubmission_mode",
                "interval_seconds",
                "max_recommendations_per_tick",
                "mode",
                "near_duplicate_mode",
                "repack_resubmission_mode",
            )
        ),
    ),
    "confirmation-bundle-settings": (
        ConfirmationBundleSettings,
        frozenset(
            (
                "challenger_z",
                "daily_bundle_cap",
                "daily_dollar_cap_microusd",
                "eligibility_mode",
                "min_base_score_micros",
                "mode",
                "per_bundle_request_cap",
                "per_bundle_token_cap",
                "profile_checksum",
                "profile_revision",
                "top_n",
            )
        ),
    ),
    "screener-provider-settings": (
        ScreenerProviderSettings,
        frozenset(
            (
                "build_provider_priority",
                "gce_overflow_backlog_multiplier",
                "gce_overflow_enabled",
                "gce_overflow_max_instances",
                "gce_overflow_min_backlog",
                "runtime_provider_priority",
                "source_review_provider_priority",
            )
        ),
    ),
}


_NESTED_FIELDS: dict[str, tuple[str, ...]] = {
    "benchmark_runtime": (
        "case_concurrency",
        "relay_delay_fingerprint_mode",
        "relay_delay_fingerprint_min_ms",
        "relay_delay_fingerprint_max_ms",
    ),
    "similarity_budget": (
        "enabled",
        "concurrent_submission_limit",
        "jaccard_threshold",
        "containment_threshold",
    ),
    "prev_gen_carryover": (
        "enabled",
        "max_agents",
        "min_score_count",
        "include_exhausted",
        "dedupe_scope",
        "require_desired_era_drained",
        "require_cohort_complete",
    ),
    "deferred_source_review": (
        "mode",
        "integrity_double_check_mode",
        "min_cohort_size",
        "composite_mad_multiplier",
        "axis_mad_multiplier",
        "min_composite_delta",
        "min_axis_delta",
    ),
}


def public_details(action: str, body: object) -> dict:
    """Allowlist for live and historical rows; unknown fields stay private."""
    if not isinstance(body, dict):
        return {}
    result: dict = {}
    for key in ("agent_id", "canary_id", "rollout_id"):
        with suppress(KeyError, ValueError, TypeError):
            result[key] = str(UUID(str(body[key])))
    for key in ("bench_version", "revision", "expected_revision", "parent_revision"):
        value = body.get(key)
        if type(value) is int and 0 <= value <= 2**31 - 1:
            result[key] = value
    flat_fields = {
        "submission-settings": ("cooldown_seconds", "fee_amount_rao"),
        "artifact-release-settings": ("embargo_hours",),
        "conversation-settings": ("enabled",),
    }.get(action.removeprefix("/api/v1/admin/"), ())
    for field in flat_fields:
        value = body.get(field)
        if isinstance(value, int) and 0 <= value <= 10**12:
            result[field] = value
    if body.get("scope") in ("*", "integrity-double-check"):
        result["scope"] = body["scope"]
    # Validate using the typed policy and publish only reviewed, supplied fields.
    key = action.removeprefix("/api/v1/admin/")
    key = {"copy-court-settings": "copy-court/settings"}.get(key, key)
    specification = _SETTINGS.get(key)
    raw = body.get("settings")
    if specification is not None and isinstance(raw, dict):
        model, allowed = specification
        try:
            settings = model.model_validate_json(json.dumps(raw))
            result["settings"] = settings.model_dump(
                mode="json", include=set(raw) & allowed, exclude_unset=True
            )
            for field, fields in _NESTED_FIELDS.items():
                nested = result["settings"].get(field)
                if isinstance(nested, dict):
                    result["settings"][field] = {
                        name: value for name, value in nested.items() if name in fields
                    }
        except (ValidationError, ValueError, TypeError):
            pass
    return result


def _session_maker(request: Request):
    # The dedicated binding lets isolated test apps attach the real audit DB
    # without enabling unrelated lifespan-backed services.
    return getattr(request.app.state, "admin_activity_session_maker", None) or (
        request.app.state.session_maker
    )


async def begin_admin_activity(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if getattr(request.state, "admin_activity_id", None) is not None:
        return
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    # Use the matched template, never a URL containing arbitrary private identifiers.
    action = request.scope["route"].path
    details = public_details(action, body)
    details.update(public_details(action, request.path_params))
    actor = request.headers.get("x-admin-actor")
    if not actor and isinstance(body, dict) and isinstance(body.get("actor"), str):
        actor = body["actor"]
    row = AdminActivity(
        action=action,
        method=request.method,
        actor=(actor or "")[:320] or None,
        details=details,
        source="request",
    )
    # Separate transaction is intentional: failed/rolled-back mutations remain visible.
    # Failure here prevents the mutation from running at all.
    async with _session_maker(request)() as session:
        session.add(row)
        await session.flush()
        activity_id = row.id
        await session.commit()
    request.state.admin_activity_id = activity_id


class AdminActivityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            activity_id = getattr(request.state, "admin_activity_id", None)
            if activity_id is not None:
                code = response.status_code if response is not None else 500
                try:
                    async with _session_maker(request)() as session:
                        session.add(
                            AdminActivityOutcome(
                                activity_id=activity_id,
                                status="succeeded" if code < 400 else "failed",
                                http_status=code,
                            )
                        )
                        await session.commit()
                except Exception:
                    # The intent survives. Do not make an applied mutation retryable.
                    # HTTP error; readers see an unknown outcome until reconciled.
                    logger.exception(
                        "Could not append admin activity outcome %s", activity_id
                    )
