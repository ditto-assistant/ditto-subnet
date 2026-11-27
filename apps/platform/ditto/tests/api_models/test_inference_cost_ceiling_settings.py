"""The per-run cost ceiling knobs on the inference board (#3271)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ditto.api_models.inference_concurrency_settings import (
    ADDITIVE_SETTINGS_FIELDS,
    MAX_RUN_COST_CEILING_MICROUSD,
    AdminInferenceConcurrencySettingsRequest,
    InferenceConcurrencySettings,
)
from ditto.api_server.inference_concurrency_settings import apply_settings
from ditto.tests.db.queries.test_inference import _config


def test_ships_off_with_no_ceiling() -> None:
    settings = InferenceConcurrencySettings()
    assert settings.run_cost_ceiling_microusd is None
    assert settings.cost_ceiling_mode == "off"
    assert settings.case_cost_report_microusd is None


@pytest.mark.parametrize(
    "field", ["run_cost_ceiling_microusd", "case_cost_report_microusd"]
)
def test_cost_fields_are_bounded(field: str) -> None:
    InferenceConcurrencySettings.model_validate({field: 1})
    InferenceConcurrencySettings.model_validate(
        {
            "run_cost_ceiling_microusd": MAX_RUN_COST_CEILING_MICROUSD,
            field: MAX_RUN_COST_CEILING_MICROUSD,
        }
    )
    for bad in (0, -1, MAX_RUN_COST_CEILING_MICROUSD + 1):
        with pytest.raises(ValidationError):
            InferenceConcurrencySettings.model_validate({field: bad})
    with pytest.raises(ValidationError):
        InferenceConcurrencySettings.model_validate({field: 1.5})  # strict ints


def test_mode_is_a_closed_vocabulary() -> None:
    for mode in ("off", "shadow", "enforce"):
        InferenceConcurrencySettings.model_validate(
            {"run_cost_ceiling_microusd": 10, "cost_ceiling_mode": mode}
        )
    for mode in ("ENFORCE", "on", ""):
        with pytest.raises(ValidationError):
            InferenceConcurrencySettings.model_validate(
                {"run_cost_ceiling_microusd": 10, "cost_ceiling_mode": mode}
            )


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_a_live_mode_needs_a_ceiling(mode: str) -> None:
    with pytest.raises(ValidationError, match="needs run_cost_ceiling_microusd"):
        InferenceConcurrencySettings.model_validate({"cost_ceiling_mode": mode})


def test_case_report_threshold_cannot_exceed_the_run_ceiling() -> None:
    InferenceConcurrencySettings(
        run_cost_ceiling_microusd=100, case_cost_report_microusd=100
    )
    InferenceConcurrencySettings(case_cost_report_microusd=500)  # no run ceiling
    with pytest.raises(ValidationError, match="may not exceed"):
        InferenceConcurrencySettings(
            run_cost_ceiling_microusd=100, case_cost_report_microusd=101
        )


def test_a_pre_upgrade_backroom_may_omit_only_the_additive_fields() -> None:
    complete = InferenceConcurrencySettings().model_dump(mode="json")
    for field in ADDITIVE_SETTINGS_FIELDS:
        complete.pop(field)
    AdminInferenceConcurrencySettingsRequest.model_validate(
        {
            "expected_revision": 0,
            "settings": complete,
            "reason": "legacy backroom write",
            "confirmation": "x",
        }
    )
    complete.pop("chat_token_budget")
    with pytest.raises(ValidationError, match="chat_token_budget"):
        AdminInferenceConcurrencySettingsRequest.model_validate(
            {
                "expected_revision": 0,
                "settings": complete,
                "reason": "legacy backroom write",
                "confirmation": "x",
            }
        )


def test_overlay_carries_ceiling_and_mode_to_the_mint_config() -> None:
    config = apply_settings(
        _config(),
        InferenceConcurrencySettings(
            run_cost_ceiling_microusd=7_000_000, cost_ceiling_mode="enforce"
        ),
    )
    assert config.run_cost_ceiling_microusd == 7_000_000
    assert config.cost_ceiling_mode == "enforce"
    assert config.embedding_price_microusd_per_token == 0.004
