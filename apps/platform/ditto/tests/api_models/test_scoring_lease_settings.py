"""The scoring lease clock board's contract (#1156).

Pinned here:

* the shipped default is exactly the pre-#1156 180-minute constant, so the
  deploy that adds the board changes no lease;
* the ceiling never exceeds what a validator restart will wait for, read from
  the real Compose file, so a Backroom raise cannot SIGKILL live work; and
* a write is whole-object, strict, and ignores unknown JSON.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ditto.api_models.scoring_lease_settings import (
    DEFAULT_SCORING_TICKET_TTL,
    DEFAULT_SCORING_TICKET_TTL_MINUTES,
    MAX_SCORING_TICKET_TTL_MINUTES,
    MIN_SCORING_TICKET_TTL_MINUTES,
    SIGNED_REPORT_MARGIN_MINUTES,
    VALIDATOR_DRAIN_MINUTES,
    AdminScoringLeaseSettingsRequest,
    ScoringLeaseSettings,
    scoring_lease_confirmation,
)
from ditto.db.queries.score_retests import REPLACEMENT_TICKET_TTL

_COMPOSE = Path(__file__).resolve().parents[5] / "docker-compose.yml"


def _request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "expected_revision": 0,
        "settings": {"scoring_ticket_ttl_minutes": 150},
        "reason": "shorten the canonical scoring lease",
        "confirmation": scoring_lease_confirmation(150),
    }
    body.update(overrides)
    return body


class TestDefaults:
    def test_default_is_the_pre_board_constant(self) -> None:
        assert DEFAULT_SCORING_TICKET_TTL_MINUTES == 180
        assert ScoringLeaseSettings().scoring_ticket_ttl == timedelta(minutes=180)
        assert timedelta(minutes=180) == DEFAULT_SCORING_TICKET_TTL

    def test_replacement_default_stays_paired_with_the_canonical_default(
        self,
    ) -> None:
        assert REPLACEMENT_TICKET_TTL == DEFAULT_SCORING_TICKET_TTL


class TestBounds:
    @pytest.mark.parametrize(
        "minutes", [MIN_SCORING_TICKET_TTL_MINUTES, 120, MAX_SCORING_TICKET_TTL_MINUTES]
    )
    def test_accepts_the_inclusive_range(self, minutes: int) -> None:
        settings = ScoringLeaseSettings(scoring_ticket_ttl_minutes=minutes)
        assert settings.scoring_ticket_ttl == timedelta(minutes=minutes)

    @pytest.mark.parametrize(
        "minutes",
        [
            0,
            MIN_SCORING_TICKET_TTL_MINUTES - 1,
            MAX_SCORING_TICKET_TTL_MINUTES + 1,
            430,
        ],
    )
    def test_refuses_outside_the_range(self, minutes: int) -> None:
        with pytest.raises(ValidationError):
            ScoringLeaseSettings(scoring_ticket_ttl_minutes=minutes)

    def test_refuses_a_string_number(self) -> None:
        with pytest.raises(ValidationError):
            ScoringLeaseSettings.model_validate({"scoring_ticket_ttl_minutes": "150"})

    def test_ceiling_leaves_the_signed_report_margin_inside_the_drain(self) -> None:
        assert (
            MAX_SCORING_TICKET_TTL_MINUTES + SIGNED_REPORT_MARGIN_MINUTES
            == VALIDATOR_DRAIN_MINUTES
        )
        # The 8-hour broker session cap must stay above every Backroom maximum.
        assert timedelta(minutes=MAX_SCORING_TICKET_TTL_MINUTES) < timedelta(hours=8)

    def test_ceiling_matches_the_shipped_validator_drain(self) -> None:
        """A raise above the Compose stop grace would SIGKILL live work."""
        compose = _COMPOSE.read_text()
        grace = re.search(r"^\s+stop_grace_period:\s*(\d+)m\s*$", compose, re.M)
        assert grace is not None
        assert int(grace.group(1)) == VALIDATOR_DRAIN_MINUTES

    def test_default_still_funds_the_validator_harness_cap(self) -> None:
        compose = _COMPOSE.read_text()
        cap = re.search(r'VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS:\s*"(\d+)"', compose)
        assert cap is not None
        # Harness cap plus 15 minutes of setup and report margin.
        assert int(cap.group(1)) + 15 * 60 <= DEFAULT_SCORING_TICKET_TTL_MINUTES * 60


class TestWriteRequest:
    def test_accepts_a_complete_policy_and_ignores_extra_json(self) -> None:
        request = AdminScoringLeaseSettingsRequest.model_validate(
            _request(
                surprise="ignored",
                settings={"scoring_ticket_ttl_minutes": 150, "future_clock": 3},
            )
        )
        assert request.settings.scoring_ticket_ttl_minutes == 150
        assert not hasattr(request, "surprise")

    def test_rejects_a_partial_policy(self) -> None:
        with pytest.raises(ValidationError, match="WHOLE policy"):
            AdminScoringLeaseSettingsRequest.model_validate(_request(settings={}))

    def test_rejects_a_short_reason(self) -> None:
        with pytest.raises(ValidationError):
            AdminScoringLeaseSettingsRequest.model_validate(_request(reason="short"))

    def test_confirmation_names_the_resulting_ttl(self) -> None:
        assert scoring_lease_confirmation(150) == "APPLY SCORING TICKET TTL 150 MINUTES"
