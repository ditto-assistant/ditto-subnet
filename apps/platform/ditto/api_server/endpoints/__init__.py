"""HTTP routers grouped by domain."""

from __future__ import annotations

from ditto.api_server.endpoints.admin_artifact_release_settings import (
    router as admin_artifact_release_settings_router,
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
from ditto.api_server.endpoints.admin_confirmation_bundles import (
    router as admin_confirmation_bundles_router,
)
from ditto.api_server.endpoints.admin_continual_retest_settings import (
    router as admin_continual_retest_settings_router,
)
from ditto.api_server.endpoints.admin_copy_review import (
    router as admin_copy_review_router,
)
from ditto.api_server.endpoints.admin_efficiency_bonus_settings import (
    router as admin_efficiency_bonus_settings_router,
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
from ditto.api_server.endpoints.admin_lease_revocations import (
    router as admin_lease_revocations_router,
)
from ditto.api_server.endpoints.admin_miner_fees import (
    router as admin_miner_fees_router,
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
from ditto.api_server.endpoints.admin_scoring_readiness import (
    router as admin_scoring_readiness_router,
)
from ditto.api_server.endpoints.admin_screener_capacity import (
    router as admin_screener_capacity_router,
)
from ditto.api_server.endpoints.admin_screener_review_settings import (
    router as admin_screener_review_settings_router,
)
from ditto.api_server.endpoints.admin_submission_deposit_address import (
    router as admin_submission_deposit_address_router,
)
from ditto.api_server.endpoints.admin_submission_settings import (
    router as admin_submission_settings_router,
)
from ditto.api_server.endpoints.admin_validation_retry import (
    router as admin_validation_retry_router,
)
from ditto.api_server.endpoints.admin_validator_slot_settings import (
    router as admin_validator_slot_settings_router,
)
from ditto.api_server.endpoints.attestation import router as attestation_router
from ditto.api_server.endpoints.health import router as health_router
from ditto.api_server.endpoints.name_claims import router as name_claims_router
from ditto.api_server.endpoints.inference import router as inference_router
from ditto.api_server.endpoints.metrics import router as metrics_router
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
    "health_router",
    "inference_router",
    "admin_artifact_release_settings_router",
    "admin_attestation_router",
    "admin_benchmark_rollout_router",
    "admin_burn_settings_router",
    "admin_inference_concurrency_settings_router",
    "admin_inference_observability_router",
    "admin_queue_policy_settings_router",
    "admin_efficiency_bonus_settings_router",
    "admin_inference_routes_router",
    "admin_lease_revocations_router",
    "admin_copy_review_router",
    "admin_confirmation_bundles_router",
    "admin_continual_retest_settings_router",
    "admin_miner_fees_router",
    "admin_owner_router",
    "admin_quarantine_router",
    "admin_retirement_router",
    "admin_scoring_readiness_router",
    "admin_screener_review_settings_router",
    "admin_screener_capacity_router",
    "admin_submission_settings_router",
    "admin_submission_deposit_address_router",
    "admin_validation_retry_router",
    "admin_validator_slot_settings_router",
    "metrics_router",
    "public_router",
    "retrieval_router",
    "scoring_router",
    "screener_router",
    "attestation_router",
    "name_claims_router",
    "upload_router",
    "validator_router",
    "validator_confirmation_router",
]
