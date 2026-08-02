"""backfill the shadow reviews lost to the provider-stage cap

Every L2/L3 shadow observation produced between 2026-07-23 and the merge of
ditto-platform#453 was rejected by the ingest endpoint with HTTP 422 and
discarded. ``ShadowReviewObservationRequest`` capped ``response_models`` and
``response_providers`` at eight stages; a real escalation appends one stage per
model call -- analyst turns, the critic, every adjudicator, and each failover
retry -- so production trajectories ran nine to twenty-five stages and failed
request validation before the handler ever ran.

The observations themselves survived in the screener's on-disk L2 result cache
on ditto-screener-prod. Twelve of the fourteen lost attempts are recoverable and
are inserted here as literals. Two (gKat v13, gKat v15) had already been evicted
from that cache and are unrecoverable; they stay absent rather than guessed.

These rows are non-authoritative telemetry. They cannot change a screening
verdict, and every quarantine they describe was already resolved by an operator
by hand -- which is precisely why they are worth having: they are the only
record of what the shadow reviewer would have said about decisions that were
made without it.

Fidelity note. The cache holds one entry per L2 run, and several artifacts were
run more than once with differing outcomes. Each row here is the run whose cache
mtime is nearest the submission attempt recorded in the screener log; ten of the
twelve match within seven seconds. In every multi-run case the nearer entry is
the one that was actually submitted and rejected.

The insert derives ``agent_id``, ``screener_hotkey``, ``artifact_sha256`` and the
three ``settings_*`` columns by joining rows that already exist, so the literals
carry only the verdict. Those joins are also the guard: a database with no
matching screening attempt -- every fresh CI worker database, which is built by
running this chain -- inserts nothing and the migration is a no-op.

Revision ID: d7b3e5c81a94
Revises: c4a91b7e2f68
Create Date: 2026-07-25
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7b3e5c81a94"
down_revision: str | Sequence[str] | None = "c4a91b7e2f68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The shadow settings revision in force for every one of these attempts. The
# screener logged ``applied review settings revision=1 scope=* mode=shadow``
# for the whole window.
_SETTINGS_REVISION = 1

_OBSERVATIONS: list[dict] = [
    # stay-v1
    {
        "attempt_id": "e9762f9f-df8c-4dbb-9af4-dc90210fd472",
        "disposition": "violation",
        "risk_level": "high",
        "categories": [
            "benchmark_emulation",
            "embedded_evaluator_logic",
            "suspicious_static_tables",
        ],
        "finding_digest": (
            "96c30d84a8ed17879887359ee86533be16c690fac99c2492fdf1cd56cc91226c"
        ),
        "resolution_basis": "generator_mirroring",
        "clearance_path": None,
        "critic_disposition": None,
        "adjudicator_disposition": None,
        "response_models": [
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": ["Azure", "Azure", "Azure", "Azure"],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 58752,
            "estimated_cost_usd": 0.8202659999999999,
            "input_tokens": 198840,
            "output_tokens": 3015,
            "reasoning_tokens": 1306,
            "reported_cost_usd": 0.8202659999999999,
        },
    },
    # infinity v7
    {
        "attempt_id": "5c845477-d5cc-45b0-898d-1b838326c35f",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "947236d887deb7c412dbb596a3132764b5df93c69c8767406df41b35eeb41215"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 690944,
            "estimated_cost_usd": 1.6176469999999998,
            "input_tokens": 865579,
            "output_tokens": 13300,
            "reasoning_tokens": 9404,
            "reported_cost_usd": 1.1122754000000001,
        },
    },
    # bot v2
    {
        "attempt_id": "df2ce5ab-b247-472f-a3a9-9d0fa7134c57",
        "disposition": "violation",
        "risk_level": "medium",
        "categories": ["fabricated_tool_trajectory"],
        "finding_digest": (
            "0a8903c7ab7d02544616c525574ae3ca9cfd043c2e8b58ee8c64227ab8a71579"
        ),
        "resolution_basis": "fabricated_tool_trajectory",
        "clearance_path": None,
        "critic_disposition": None,
        "adjudicator_disposition": None,
        "response_models": [
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": ["Azure", "Azure", "Azure", "Azure"],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 72960,
            "estimated_cost_usd": 0.535185,
            "input_tokens": 158367,
            "output_tokens": 2389,
            "reasoning_tokens": 998,
            "reported_cost_usd": 0.535185,
        },
    },
    # infinity v8
    {
        "attempt_id": "51a19a80-cc84-4895-a0a4-16ed5278edb8",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "aa0afdf00024435f45bb7514bd471813309fcae76c999aa5456ce7ba42b53efa"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 760064,
            "estimated_cost_usd": 1.681382,
            "input_tokens": 931474,
            "output_tokens": 14810,
            "reasoning_tokens": 10718,
            "reported_cost_usd": 1.1200204,
        },
    },
    # bot v3
    {
        "attempt_id": "8d60fe8b-996c-4aa6-85ba-6280a3846a5b",
        "disposition": "violation",
        "risk_level": "medium",
        "categories": ["fabricated_tool_trajectory"],
        "finding_digest": (
            "a4ac537dd5015975d30abd1f6362b6de9a4e82d09defdea3d54c54c9e9a94e2e"
        ),
        "resolution_basis": "fabricated_tool_trajectory",
        "clearance_path": "l3_adjudicated_violation",
        "critic_disposition": "challenge",
        "adjudicator_disposition": "uphold_violation",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 1183104,
            "estimated_cost_usd": 2.580482,
            "input_tokens": 1477768,
            "output_tokens": 17187,
            "reasoning_tokens": 10036,
            "reported_cost_usd": 1.9560288,
        },
    },
    # golden v3
    {
        "attempt_id": "ee684ac5-4502-415b-a5fc-d80b7564086a",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "3d4fe76207b8e68e181f1437ffdee36bfcfe9bdb3c99434bb1e884abbbf73ce1"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 1061632,
            "estimated_cost_usd": 1.9386459999999999,
            "input_tokens": 1233938,
            "output_tokens": 18210,
            "reasoning_tokens": 13982,
            "reported_cost_usd": 1.2572286,
        },
    },
    # love-ditto
    {
        "attempt_id": "41cffe0a-5f3d-42b1-90f6-ddbc8f2d9782",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation", "fabricated_tool_trajectory"],
        "finding_digest": (
            "8d03270726dae5d15de67dffab79ca8823a6f36c7a6eeece779d56ac5b354a58"
        ),
        "resolution_basis": "fabricated_tool_trajectory",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 952192,
            "estimated_cost_usd": 1.909146,
            "input_tokens": 1125690,
            "output_tokens": 18852,
            "reasoning_tokens": 15208,
            "reported_cost_usd": 1.1195720000000002,
        },
    },
    # version v3
    {
        "attempt_id": "6a133f1e-849e-47a8-b713-77b064c8c3d7",
        "disposition": "violation",
        "risk_level": "medium",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "2d00c5ea23c1007480b881d6891eee328e567eb5fe1edecaebaa0462f62b7891"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 825344,
            "estimated_cost_usd": 1.900077,
            "input_tokens": 1022517,
            "output_tokens": 16718,
            "reasoning_tokens": 11624,
            "reported_cost_usd": 1.2887452000000001,
        },
    },
    # infinity v9
    {
        "attempt_id": "1b0df1dc-f93b-46f8-a7db-4ae475ecb887",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "297736a2426e7b2d8c1af54816b571fc1d9a82fe0e0f22bc025646ed9c0f43ad"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 855808,
            "estimated_cost_usd": 1.5760690000000002,
            "input_tokens": 1009439,
            "output_tokens": 12667,
            "reasoning_tokens": 8190,
            "reported_cost_usd": 1.0627692,
        },
    },
    # corktown v3
    {
        "attempt_id": "4950fcba-6c4b-4c18-b9e2-b471d7ec6884",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation", "scorer_contract_manipulation"],
        "finding_digest": (
            "6f723f5d5550c181bec329eb4b958f12b92b2077e7a5f2d80c21a37824c0c8fb"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 637824,
            "estimated_cost_usd": 1.7770169999999998,
            "input_tokens": 839709,
            "output_tokens": 14956,
            "reasoning_tokens": 11844,
            "reported_cost_usd": 1.2816322000000002,
        },
    },
    # lihai v4
    {
        "attempt_id": "9fbba567-c6ba-408b-b923-99fc9031a7a9",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "51f512434da4e0cfb05d3bfb9f288d4f795bda2bec3229d667cbc433794eaca4"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 545664,
            "estimated_cost_usd": 1.2307769999999998,
            "input_tokens": 676347,
            "output_tokens": 10151,
            "reasoning_tokens": 5432,
            "reported_cost_usd": 0.8384364,
        },
    },
    # lihai v5
    {
        "attempt_id": "86ef8dcf-8263-45b2-b755-dccacf67eba0",
        "disposition": "violation",
        "risk_level": "high",
        "categories": ["benchmark_emulation"],
        "finding_digest": (
            "86ab103efd78cd7fe8497010f8cf3b1aa78258de2a7900b41ce4c5fb643b1d61"
        ),
        "resolution_basis": "benchmark_answer_replacement",
        "clearance_path": "l3_adjudicated_violation_cause",
        "critic_disposition": "not_required",
        "adjudicator_disposition": "confirm_violation_cause",
        "response_models": [
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/kimi-k3",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-sol",
        ],
        "response_providers": [
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Moonshot AI",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
            "Azure",
        ],
        "usage": {
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 665728,
            "estimated_cost_usd": 1.5951840000000002,
            "input_tokens": 849180,
            "output_tokens": 11502,
            "reasoning_tokens": 6253,
            "reported_cost_usd": 1.1834714,
        },
    },
]

_INSERT = sa.text(
    """
    INSERT INTO screener_shadow_reviews (
        attempt_id, agent_id, screener_hotkey, artifact_sha256,
        settings_revision, settings_scope, settings_checksum,
        disposition, risk_level, categories, finding_digest,
        resolution_basis, clearance_path, critic_disposition,
        adjudicator_disposition, response_models, response_providers, usage
    )
    SELECT
        attempts.attempt_id,
        attempts.agent_id,
        attempts.screener_hotkey,
        agents.sha256,
        settings.revision,
        settings.scope,
        settings.checksum,
        :disposition,
        :risk_level,
        CAST(:categories AS jsonb),
        :finding_digest,
        :resolution_basis,
        :clearance_path,
        :critic_disposition,
        :adjudicator_disposition,
        CAST(:response_models AS jsonb),
        CAST(:response_providers AS jsonb),
        CAST(:usage AS jsonb)
    FROM screening_attempts AS attempts
    JOIN agents ON agents.agent_id = attempts.agent_id
    JOIN screener_review_settings_revisions AS settings
      ON settings.revision = :settings_revision
    WHERE attempts.attempt_id = CAST(:attempt_id AS uuid)
    ON CONFLICT (attempt_id) DO NOTHING
    """
)

_DELETE = sa.text(
    """
    DELETE FROM screener_shadow_reviews
    WHERE attempt_id = CAST(:attempt_id AS uuid)
    """
)


def upgrade() -> None:
    connection = op.get_bind()
    for observation in _OBSERVATIONS:
        connection.execute(
            _INSERT,
            {
                "attempt_id": observation["attempt_id"],
                "settings_revision": _SETTINGS_REVISION,
                "disposition": observation["disposition"],
                "risk_level": observation["risk_level"],
                "categories": json.dumps(observation["categories"]),
                "finding_digest": observation["finding_digest"],
                "resolution_basis": observation["resolution_basis"],
                "clearance_path": observation["clearance_path"],
                "critic_disposition": observation["critic_disposition"],
                "adjudicator_disposition": observation["adjudicator_disposition"],
                "response_models": json.dumps(observation["response_models"]),
                "response_providers": json.dumps(observation["response_providers"]),
                "usage": json.dumps(observation["usage"]),
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    for observation in _OBSERVATIONS:
        connection.execute(_DELETE, {"attempt_id": observation["attempt_id"]})
