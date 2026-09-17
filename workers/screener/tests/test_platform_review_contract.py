"""Exercise actual Platform revisions through the worker wire contract."""

import importlib.util
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
        {"timeout_seconds": 1800, "max_steps": 48, "critic_reasoning_effort": "high"},
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
    assert runtime.l2_critic_reasoning_effort == settings.critic_reasoning_effort
    # Applying a revision rebuilds the real layered reviewer. Its constructor
    # must accept the same values, not merely the transport schema.
    BuildGate(runtime, Mock(), policy=Mock(), journal=Mock())
