"""Bounded provider-refusal reasons for Targon API errors."""

from __future__ import annotations

import httpx

from ditto.api_server.targon_client import _ERROR_REASON_LIMIT, _error_reason


def test_error_reason_prefers_provider_error_message() -> None:
    response = httpx.Response(
        409, json={"error": "no cpu-small capacity available in any region"}
    )
    assert _error_reason(response) == "no cpu-small capacity available in any region"


def test_error_reason_unwraps_nested_error_object() -> None:
    response = httpx.Response(
        409, json={"error": {"message": "workload name already exists"}}
    )
    assert _error_reason(response) == "workload name already exists"


def test_error_reason_falls_back_to_message_and_detail_keys() -> None:
    assert _error_reason(httpx.Response(402, json={"message": "billing hold"})) == (
        "billing hold"
    )
    assert _error_reason(httpx.Response(409, json={"detail": "quota"})) == "quota"


def test_error_reason_bounds_and_flattens_plain_text() -> None:
    body = "line one\nline   two " + "x" * 400
    reason = _error_reason(httpx.Response(502, text=body))
    assert reason.startswith("line one line two")
    assert len(reason) <= _ERROR_REASON_LIMIT
    assert "\n" not in reason


def test_error_reason_empty_body_stays_generic() -> None:
    assert _error_reason(httpx.Response(409, text="")) == "HTTP error"
