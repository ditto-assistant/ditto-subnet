"""Reading back a revision written before a policy field was retired.

``prev_gen_carryover`` is stored WHOLE, so every revision written before a field
was retired still carries it. Models ignore unknown fields during rolling
upgrades, preserving the rest of the operator's policy rather than resetting it.

The same holds for the top-level ``deferred_source_review`` block retired
with source review: every production revision written before 2026-11-07
carries it.

Both stored documents and new writes drop the dead key. It cannot become an
authoritative setting or re-open retired behavior.
"""

from __future__ import annotations

from typing import cast

from ditto.api_models.queue_policy_settings import QueuePolicySettings
from ditto.api_server.queue_policy_settings import (
    DEFAULT_SETTINGS,
    settings_from_row,
)
from ditto.db.models import QueuePolicySettingsRevision


def _stored(**carryover_overrides: object) -> QueuePolicySettingsRevision:
    """A stored payload that still carries the retired key.

    Built as the real row type rather than a stand-in: ``settings_from_row``
    reads a revision straight out of the table, and a duck-typed stub would let
    the signature drift without anything noticing.
    """
    payload = QueuePolicySettings().model_dump(mode="json")
    payload["prev_gen_carryover"] = {
        **payload["prev_gen_carryover"],
        "allow_retired_era_backfill": False,
        **carryover_overrides,
    }
    return QueuePolicySettingsRevision(revision=7, settings=payload)


def test_a_revision_carrying_the_retired_key_still_decodes() -> None:
    decoded = settings_from_row(_stored())
    assert isinstance(decoded, QueuePolicySettings)
    assert not hasattr(decoded.prev_gen_carryover, "allow_retired_era_backfill")


def test_the_operators_other_settings_survive_the_removal() -> None:
    """The failure this guards against is a SILENT RESET, not a crash.

    A revision where the operator deliberately widened the carryover must come
    back with those choices intact. If the retired key had been left to fail
    validation, this would return the shipped defaults instead and nothing
    would surface but a log line.
    """
    decoded = settings_from_row(
        _stored(enabled=True, max_agents=11, dedupe_scope="none")
    )
    assert decoded.prev_gen_carryover.enabled is True
    assert decoded.prev_gen_carryover.max_agents == 11
    assert decoded.prev_gen_carryover.dedupe_scope == "none"
    assert decoded.prev_gen_carryover.max_agents != (
        DEFAULT_SETTINGS.prev_gen_carryover.max_agents
    )


def test_a_genuinely_corrupt_revision_still_fails_open() -> None:
    """Stripping retired keys must not swallow real corruption."""
    corrupt = QueuePolicySettingsRevision(
        revision=8, settings=cast(dict[str, object], {"prev_gen_carryover": "nonsense"})
    )
    assert settings_from_row(corrupt) == DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# deferred_source_review: a whole top-level block retired with source review
# ---------------------------------------------------------------------------

# Exactly what live revisions written before 2026-11-07 store.
_RETIRED_DEFERRED_SOURCE_REVIEW = {
    "mode": "bypass",
    "integrity_double_check_mode": "enforce",
    "min_cohort_size": 8,
    "composite_mad_multiplier": 6.0,
    "axis_mad_multiplier": 6.0,
    "min_composite_delta": 0.1,
    "min_axis_delta": 0.15,
}


def _stored_with_deferred_block(**overrides: object) -> QueuePolicySettingsRevision:
    payload = QueuePolicySettings().model_dump(mode="json")
    payload.update(overrides)
    payload["deferred_source_review"] = dict(_RETIRED_DEFERRED_SOURCE_REVIEW)
    return QueuePolicySettingsRevision(revision=16, settings=payload)


def test_a_revision_carrying_the_deferred_review_block_still_decodes() -> None:
    decoded = settings_from_row(_stored_with_deferred_block(rescore_cohort_size=8))
    assert not hasattr(decoded, "deferred_source_review")
    assert "deferred_source_review" not in decoded.model_dump()
    # Not a silent reset to the defaults: the operator's real change survives.
    assert decoded.rescore_cohort_size == 8
    assert decoded.rescore_cohort_size != DEFAULT_SETTINGS.rescore_cohort_size


def test_a_new_write_drops_the_deferred_review_block() -> None:
    payload = QueuePolicySettings().model_dump(mode="json")
    payload["deferred_source_review"] = dict(_RETIRED_DEFERRED_SOURCE_REVIEW)
    assert "deferred_source_review" not in QueuePolicySettings.model_validate(
        payload
    ).model_dump(mode="json")
