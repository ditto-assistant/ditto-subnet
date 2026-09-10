"""Router-track harness launch authority (model UNLOCKED) wire model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.router_harness import (
    RouterHarnessLaunchResponse,
    router_harness_launch_signing_message,
)

_AGENT = UUID("33333333-3333-4333-8333-333333333333")
_TICKET = UUID("22222222-2222-4222-8222-222222222222")
_NONCE = UUID("11111111-1111-4111-8111-111111111111")
_DEADLINE = datetime(2026, 8, 23, 7, 0, tzinfo=UTC)
_EXPIRES = datetime(2026, 8, 23, 6, 30, tzinfo=UTC)


def _launch_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "dittobench-router-harness-launch-v1",
        "router_contract_version": 1,
        "weight_eligible": False,
        "agent_id": str(_AGENT),
        "ticket_id": str(_TICKET),
        "ticket_deadline": _DEADLINE.isoformat(),
        "screened_image_sha256": "ab" * 32,
        "screened_image_size_bytes": 1024,
        "screened_image_id": "sha256:" + "cd" * 32,
        "screened_image_ref": f"ditto-screen/{_AGENT}:latest",
        "image_url": "https://screen.internal/blob?sig=abc",
        "expires_at": _EXPIRES.isoformat(),
    }
    payload.update(overrides)
    return payload


def test_router_launch_round_trips_and_hides_image_url() -> None:
    launch = RouterHarnessLaunchResponse.model_validate(_launch_payload())
    assert launch.weight_eligible is False
    assert launch.router_contract_version == 1
    assert launch.screened_image_size_bytes == 1024
    # image_url is a private capability and must not appear in repr.
    assert "image_url" not in repr(launch)
    assert "screen.internal" not in repr(launch)


def test_weight_eligible_true_is_rejected_in_shadow() -> None:
    with pytest.raises(ValidationError):
        RouterHarnessLaunchResponse.model_validate(
            _launch_payload(weight_eligible=True)
        )


def test_image_ref_must_bind_to_agent_id() -> None:
    with pytest.raises(ValidationError):
        RouterHarnessLaunchResponse.model_validate(
            _launch_payload(screened_image_ref="ditto-screen/someone-else:latest")
        )


def test_expiry_after_ticket_deadline_is_incoherent() -> None:
    with pytest.raises(ValidationError):
        RouterHarnessLaunchResponse.model_validate(
            _launch_payload(expires_at=(_DEADLINE + timedelta(minutes=1)).isoformat())
        )


def test_image_url_must_be_private_https_capability() -> None:
    for bad_url in (
        "http://screen.internal/blob?sig=abc",  # not https
        "https://screen.internal/blob",  # no query
        "https://screen.internal?sig=abc",  # no path
        "https://user:pw@screen.internal/blob?sig=abc",  # credentials
        "https://screen.internal:8443/blob?sig=abc",  # non-443 port
    ):
        with pytest.raises(ValidationError):
            RouterHarnessLaunchResponse.model_validate(
                _launch_payload(image_url=bad_url)
            )


def test_signing_domain_is_exact_and_versioned() -> None:
    requested_at = datetime.fromisoformat("2026-08-23T06:00:00+00:00")
    message = router_harness_launch_signing_message(
        validator_hotkey="5" * 48,
        ticket_id=_TICKET,
        nonce=_NONCE,
        requested_at=requested_at,
    )
    parts = message.decode().split("\x00")
    assert parts[0] == "dittobench-router-harness-launch:v1"
    assert parts[1] == "5" * 48
    assert parts[2] == str(_TICKET)
    assert parts[3] == str(_NONCE)
    assert parts[4] == "2026-08-23T06:00:00.000000+00:00"


def test_signing_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        router_harness_launch_signing_message(
            validator_hotkey="5" * 48,
            ticket_id=_TICKET,
            nonce=_NONCE,
            requested_at=datetime(2026, 8, 23, 6, 0),  # naive
        )
