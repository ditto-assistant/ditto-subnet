"""Exercise actual Platform revisions through the worker wire contract."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from ditto_screener.gate import BuildGate
from ditto_screener.review_settings import EffectiveReviewSettings


@pytest.fixture
def platform_contract(monkeypatch):
    # Load the standalone API model, not Platform's application/dependencies.
    source = (
        Path(__file__).resolve().parents[3]
        / "apps/platform/ditto/api_models/screener_review_settings.py"
    )
    spec = importlib.util.spec_from_file_location("platform_review_contract", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "timeout_seconds": 1800,
            "max_steps": 256,
            "max_output_tokens": 1_000_000,
            "max_cost_usd": 25,
            "critic_reasoning_effort": "high",
        },
        {"timeout_seconds": 30, "max_steps": 1, "critic_reasoning_effort": "low"},
    ],
)
def test_platform_revision_deserializes_and_applies(
    platform_contract, make_config, overrides
):
    settings = platform_contract.ScreenerReviewSettings(mode="enforce", **overrides)
    wire = {
        "revision": 123,
        "scope": "*",
        "max_age_seconds": 900,
        "settings": settings.model_dump(mode="json"),
        "checksum": platform_contract.review_settings_checksum(settings),
    }
    effective = EffectiveReviewSettings.model_validate(wire)
    runtime = effective.apply_to(make_config())
    assert runtime.l2_timeout_seconds == settings.timeout_seconds
    assert runtime.l2_max_steps == settings.max_steps
    assert runtime.l2_max_output_tokens == settings.max_output_tokens
    assert runtime.l2_max_cost_usd == settings.max_cost_usd
    assert runtime.l2_critic_reasoning_effort == settings.critic_reasoning_effort
    # Applying a revision rebuilds the real layered reviewer. Its constructor
    # must accept the same values, not merely the transport schema.
    BuildGate(runtime, Mock(), policy=Mock(), journal=Mock())


@pytest.mark.parametrize(
    "overrides",
    [
        {"probe_battery_mode": "shadow"},
        {"probe_battery_mode": "shadow", "probe_battery_timeout_seconds": 300},
        {"probe_battery_timeout_seconds": 300},
        {"probe_battery_mode": "shadow", "l2_always_escalate": True},
        {"probe_battery_mode": "shadow", "probe_review_mode": "shadow"},
        {
            "probe_battery_mode": "shadow",
            "probe_review_mode": "shadow",
            "probe_review_timeout_seconds": 600,
            "probe_review_max_cost_usd": 0.5,
        },
        {"probe_review_timeout_seconds": 600},
        {
            "probe_battery_mode": "shadow",
            "fanout_shadow_mode": "shadow",
            "fanout_shadow_image_source_sha": "1" * 40,
        },
    ],
)
def test_probe_battery_revisions_verify_and_apply(platform_contract, overrides):
    settings = platform_contract.ScreenerReviewSettings(mode="shadow", **overrides)
    effective = EffectiveReviewSettings.model_validate(
        {
            "revision": 7,
            "scope": "*",
            "max_age_seconds": 900,
            "settings": settings.model_dump(mode="json"),
            "checksum": platform_contract.review_settings_checksum(settings),
        }
    )
    assert effective.settings.probe_battery_mode == settings.probe_battery_mode
    assert (
        effective.settings.probe_battery_timeout_seconds
        == settings.probe_battery_timeout_seconds
    )
    assert effective.settings.probe_review_mode == settings.probe_review_mode
    assert (
        effective.settings.probe_review_max_cost_usd
        == settings.probe_review_max_cost_usd
    )


_GPT61_SOL_POSTURE = {
    "l2_model": "openai/gpt-6.1-sol",
    "l2_fallback_models": ("openai/gpt-6.1-terra", "z-ai/glm-5.2"),
    "l3_model": "openai/gpt-6.1-sol",
    "timeout_seconds": 2_700,
}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        _GPT61_SOL_POSTURE,
        {**_GPT61_SOL_POSTURE, "l3_fallback_models": ("z-ai/glm-5.2",)},
        {**_GPT61_SOL_POSTURE, "l3_max_raw_input_tokens": 8_000_000},
        {
            **_GPT61_SOL_POSTURE,
            "l3_fallback_models": (),
            "fanout_shadow_mode": "shadow",
            "fanout_shadow_image_source_sha": "1" * 40,
        },
        # A legacy-style L3 model already in the default chain stays valid.
        {"l3_model": "openai/gpt-6.1-terra"},
    ],
)
def test_l3_fallback_revisions_verify_and_apply(
    platform_contract, make_config, overrides
):
    settings = platform_contract.ScreenerReviewSettings(mode="enforce", **overrides)
    effective = EffectiveReviewSettings.model_validate(
        {
            "revision": 9,
            "scope": "*",
            "max_age_seconds": 900,
            "settings": settings.model_dump(mode="json"),
            "checksum": platform_contract.review_settings_checksum(settings),
        }
    )
    runtime = effective.apply_to(make_config())
    assert runtime.l3_review_model == settings.l3_model
    assert runtime.l3_fallback_models == settings.l3_fallback_models
    assert runtime.l3_max_raw_input_tokens == settings.l3_max_raw_input_tokens
    BuildGate(runtime, Mock(), policy=Mock(), journal=Mock())


def test_default_l3_controls_keep_the_pre_upgrade_checksum(platform_contract):
    """A worker that predates the L3 controls drops both keys before hashing."""
    settings = platform_contract.ScreenerReviewSettings(
        mode="enforce", **_GPT61_SOL_POSTURE
    )
    legacy = settings.model_dump(mode="json")
    legacy.pop("l3_fallback_models")
    legacy.pop("l3_max_raw_input_tokens")
    expected = platform_contract.review_settings_checksum(settings)
    # Same hash with the new keys absent, so old workers keep verifying it.
    for name in platform_contract.FANOUT_SHADOW_SETTINGS_FIELDS:
        legacy.pop(name)
    for name in (
        "l2_always_escalate",
        "adjudicator_max_completion_tokens",
        *platform_contract.PROBE_BATTERY_SETTINGS_FIELDS,
        *platform_contract.PROBE_REVIEW_SETTINGS_FIELDS,
    ):
        legacy.pop(name)
    assert (
        hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == expected
    )
    changed = settings.model_copy(update={"l3_max_raw_input_tokens": 8_000_000})
    assert platform_contract.review_settings_checksum(changed) != expected
