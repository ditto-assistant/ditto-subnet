"""HTTP routers grouped by domain."""

from __future__ import annotations

from ditto.api_server.endpoints.admin_artifact_release_settings import (
    router as admin_artifact_release_settings_router,
)
from ditto.api_server.endpoints.admin_ath_review_queue_slo import (
    router as admin_ath_review_queue_slo_router,
)
from ditto.api_server.endpoints.admin_ath_rulings import (
    router as admin_ath_rulings_router,
)
from ditto.api_server.endpoints.admin_attestation import (
    router as admin_attestation_router,
)
from ditto.api_server.endpoints.admin_benchmark_rollout import (
    router as admin_benchmark_rollout_router,
)
from ditto.api_server.endpoints.admin_burn_settings import (
    router as admin_burn_settings_router,
)
from ditto.api_server.endpoints.admin_claim_provenance import (
    router as admin_claim_provenance_router,
)
from ditto.api_server.endpoints.admin_confirmation_bundles import (
    router as admin_confirmation_bundles_router,
)
from ditto.api_server.endpoints.admin_confirmation_seed_anchors import (
    router as admin_confirmation_seed_anchors_router,
)
from ditto.api_server.endpoints.admin_continual_retest_settings import (
    router as admin_continual_retest_settings_router,
)
from ditto.api_server.endpoints.admin_copy_court import (
    router as admin_copy_court_router,
)
from ditto.api_server.endpoints.admin_copy_review import (
    router as admin_copy_review_router,
)
from ditto.api_server.endpoints.admin_database_backup import (
    router as admin_database_backup_router,
)
from ditto.api_server.endpoints.admin_efficiency_bonus_settings import (
    router as admin_efficiency_bonus_settings_router,
)
from ditto.api_server.endpoints.admin_emission_allocation import (
    router as admin_emission_allocation_router,
)
from ditto.api_server.endpoints.admin_emission_eligibility import (
    router as admin_emission_eligibility_router,
)
from ditto.api_server.endpoints.admin_generator_competition import (
    router as admin_generator_competition_router,
)
from ditto.api_server.endpoints.admin_generator_rounds import (
    router as admin_generator_rounds_router,
)
from ditto.api_server.endpoints.admin_hotkey_bans import (
    router as admin_hotkey_bans_router,
)
from ditto.api_server.endpoints.admin_inference_admission import (
    router as admin_inference_admission_router,
)
from ditto.api_server.endpoints.admin_inference_concurrency_settings import (
    router as admin_inference_concurrency_settings_router,
)
from ditto.api_server.endpoints.admin_inference_observability import (
    router as admin_inference_observability_router,
)
from ditto.api_server.endpoints.admin_inference_routes import (
    router as admin_inference_routes_router,
)
from ditto.api_server.endpoints.admin_leaderboard import (
    router as admin_leaderboard_router,
)
from ditto.api_server.endpoints.admin_lease_revocations import (
    router as admin_lease_revocations_router,
)
from ditto.api_server.endpoints.admin_miner_fees import (
    router as admin_miner_fees_router,
)
from ditto.api_server.endpoints.admin_outlier_escalation import (
    router as admin_outlier_escalation_router,
)
from ditto.api_server.endpoints.admin_owner import (
    router as admin_owner_router,
)
from ditto.api_server.endpoints.admin_quarantine import (
    router as admin_quarantine_router,
)
from ditto.api_server.endpoints.admin_queue_policy_settings import (
    router as admin_queue_policy_settings_router,
)
from ditto.api_server.endpoints.admin_retirement import (
    router as admin_retirement_router,
)
from ditto.api_server.endpoints.admin_scoring_lease_settings import (
    router as admin_scoring_lease_settings_router,
)
from ditto.api_server.endpoints.admin_scoring_readiness import (
    router as admin_scoring_readiness_router,
)
from ditto.api_server.endpoints.admin_screener_capacity import (
    router as admin_screener_capacity_router,
)
from ditto.api_server.endpoints.admin_screener_fanout_shadow import (
    router as admin_screener_fanout_shadow_router,
)
from ditto.api_server.endpoints.admin_screener_policy_activation import (
    router as admin_screener_policy_activation_router,
)
from ditto.api_server.endpoints.admin_screener_review_settings import (
    router as admin_screener_review_settings_router,
)
from ditto.api_server.endpoints.admin_screening_infra_retry import (
    router as admin_screening_infra_retry_router,
)
from ditto.api_server.endpoints.admin_source_review_queue_slo import (
    router as admin_source_review_queue_slo_router,
)
from ditto.api_server.endpoints.admin_submission_deposit_address import (
    router as admin_submission_deposit_address_router,
)
from ditto.api_server.endpoints.admin_submission_settings import (
    router as admin_submission_settings_router,
)
from ditto.api_server.endpoints.admin_traces import (
    router as admin_traces_router,
)
from ditto.api_server.endpoints.admin_track_share_settings import (
    router as admin_track_share_settings_router,
)
from ditto.api_server.endpoints.admin_transcript_mirror_settings import (
    router as admin_transcript_mirror_settings_router,
)
from ditto.api_server.endpoints.admin_treasury_quote import (
    router as admin_treasury_quote_router,
)
from ditto.api_server.endpoints.admin_treasury_receipts import (
    router as admin_treasury_receipts_router,
)
from ditto.api_server.endpoints.admin_treasury_settings import (
    router as admin_treasury_settings_router,
)
from ditto.api_server.endpoints.admin_v13_private_generation import (
    router as admin_v13_private_generation_router,
)
from ditto.api_server.endpoints.admin_v13_scorer_cohort import (
    router as admin_v13_scorer_cohort_router,
)
from ditto.api_server.endpoints.admin_validation_retry import (
    router as admin_validation_retry_router,
)
from ditto.api_server.endpoints.admin_validator_capacity import (
    router as admin_validator_capacity_router,
)
from ditto.api_server.endpoints.admin_validator_slot_settings import (
    router as admin_validator_slot_settings_router,
)
from ditto.api_server.endpoints.admin_validator_weights import (
    router as admin_validator_weights_router,
)
from ditto.api_server.endpoints.attestation import router as attestation_router
from ditto.api_server.endpoints.ditto_callback_challenge import (
    router as ditto_callback_challenge_router,
)
from ditto.api_server.endpoints.feedback_track import router as feedback_track_router
from ditto.api_server.endpoints.generator import router as generator_router
from ditto.api_server.endpoints.generator_scorer import (
    router as generator_scorer_router,
)
from ditto.api_server.endpoints.health import router as health_router
from ditto.api_server.endpoints.inference import router as inference_router
from ditto.api_server.endpoints.l2_report_canary import (
    admin_router as admin_l2_report_canary_router,
)
from ditto.api_server.endpoints.l2_report_canary import (
    screener_router as screener_l2_report_canary_router,
)
from ditto.api_server.endpoints.metrics import router as metrics_router
from ditto.api_server.endpoints.miner_auth import router as miner_auth_router
from ditto.api_server.endpoints.miner_avatars import router as miner_avatars_router
from ditto.api_server.endpoints.miner_ditto_link import (
    router as miner_ditto_link_router,
)
from ditto.api_server.endpoints.miner_mcp import router as miner_mcp_router
from ditto.api_server.endpoints.miner_me import router as miner_me_router
from ditto.api_server.endpoints.name_claims import router as name_claims_router
from ditto.api_server.endpoints.public import router as public_router
from ditto.api_server.endpoints.retrieval import router as retrieval_router
from ditto.api_server.endpoints.scoring import router as scoring_router
from ditto.api_server.endpoints.screener import router as screener_router
from ditto.api_server.endpoints.upload import router as upload_router
from ditto.api_server.endpoints.validator import router as validator_router
from ditto.api_server.endpoints.validator_confirmation import (
    router as validator_confirmation_router,
)

__all__ = [
    "admin_database_backup_router",
    "health_router",
    "inference_router",
    "admin_artifact_release_settings_router",
    "admin_transcript_mirror_settings_router",
    "admin_ath_review_queue_slo_router",
    "admin_ath_rulings_router",
    "admin_attestation_router",
    "admin_benchmark_rollout_router",
    "admin_burn_settings_router",
    "admin_track_share_settings_router",
    "admin_emission_allocation_router",
    "admin_emission_eligibility_router",
    "admin_treasury_settings_router",
    "admin_treasury_receipts_router",
    "admin_treasury_quote_router",
    "admin_inference_admission_router",
    "admin_inference_concurrency_settings_router",
    "admin_inference_observability_router",
    "admin_generator_competition_router",
    "admin_generator_rounds_router",
    "admin_scoring_lease_settings_router",
    "generator_router",
    "generator_scorer_router",
    "admin_traces_router",
    "admin_queue_policy_settings_router",
    "admin_efficiency_bonus_settings_router",
    "admin_inference_routes_router",
    "admin_hotkey_bans_router",
    "admin_leaderboard_router",
    "admin_validator_weights_router",
    "admin_lease_revocations_router",
    "admin_confirmation_seed_anchors_router",
    "admin_copy_court_router",
    "admin_copy_review_router",
    "admin_claim_provenance_router",
    "admin_confirmation_bundles_router",
    "admin_continual_retest_settings_router",
    "admin_miner_fees_router",
    "admin_outlier_escalation_router",
    "admin_owner_router",
    "admin_quarantine_router",
    "admin_retirement_router",
    "admin_scoring_readiness_router",
    "admin_screener_review_settings_router",
    "admin_screener_fanout_shadow_router",
    "admin_l2_report_canary_router",
    "admin_screener_capacity_router",
    "admin_screening_infra_retry_router",
    "admin_screener_policy_activation_router",
    "admin_source_review_queue_slo_router",
    "admin_v13_private_generation_router",
    "admin_submission_settings_router",
    "admin_submission_deposit_address_router",
    "admin_validation_retry_router",
    "admin_validator_capacity_router",
    "admin_validator_slot_settings_router",
    "admin_v13_scorer_cohort_router",
    "metrics_router",
    "miner_auth_router",
    "miner_avatars_router",
    "miner_ditto_link_router",
    "miner_github_link_router",
    "contributor_cla_router",
    "bounties_admin_router",
    "bounties_claims_router",
    "bounties_public_router",
    "feedback_track_router",
    "ditto_callback_challenge_router",
    "miner_mcp_router",
    "miner_me_router",
    "public_router",
    "retrieval_router",
    "scoring_router",
    "screener_router",
    "screener_l2_report_canary_router",
    "attestation_router",
    "name_claims_router",
    "upload_router",
    "validator_router",
    "validator_confirmation_router",
]

from ditto.api_server.endpoints.bounties import admin_router as bounties_admin_router
from ditto.api_server.endpoints.bounties import (
    claims_router as bounties_claims_router,
)
from ditto.api_server.endpoints.bounties import public_router as bounties_public_router
from ditto.api_server.endpoints.contributor_cla import router as contributor_cla_router
from ditto.api_server.endpoints.miner_github_link import (
    router as miner_github_link_router,
)
