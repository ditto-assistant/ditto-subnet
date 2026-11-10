"""Overflow routing names one enrolled primary provider ahead of GCE."""

import pytest
from pydantic import ValidationError

from ditto.api_models.screener_provider_settings import (
    ScreenerProviderSettings,
    provider_settings_confirmation,
)


def _overflow(**lanes: tuple[str, ...]) -> ScreenerProviderSettings:
    return ScreenerProviderSettings.model_validate(
        {
            "runtime_provider_priority": ("aws", "gcp"),
            "source_review_provider_priority": ("aws", "gcp"),
            "build_provider_priority": ("aws", "gcp"),
            "gce_overflow_enabled": True,
            "primary_node_id": "subnet-screener-3",
            **lanes,
        }
    )


@pytest.mark.parametrize("provider", ["hetzner", "aws"])
def test_overflow_accepts_either_primary_provider(provider: str) -> None:
    settings = _overflow(
        runtime_provider_priority=(provider, "gcp"),
        source_review_provider_priority=(provider, "gcp"),
        build_provider_priority=(provider, "gcp"),
    )

    assert settings.primary_provider() == provider
    assert provider_settings_confirmation(settings).startswith(
        f"APPLY SCREENER PROVIDERS BUILDS={provider}>gcp "
    )


def test_overflow_rejects_lanes_that_disagree_on_the_primary() -> None:
    with pytest.raises(ValidationError, match="same primary provider"):
        _overflow(build_provider_priority=("hetzner", "gcp"))


def test_overflow_still_requires_a_primary_first_lane() -> None:
    with pytest.raises(ValidationError, match="primary-first"):
        _overflow(build_provider_priority=("gcp", "aws"))


def test_gcp_only_routing_has_no_primary() -> None:
    assert ScreenerProviderSettings().primary_provider() is None
