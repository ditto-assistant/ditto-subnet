"""Fail-closed queue metric reads for the independent GCE autoscaler."""

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _publisher() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    template = (
        root
        / "infra/ansible/roles/screening_queue_metric/templates"
        / "publish-screening-queue-depth.py.j2"
    )
    source = template.read_text()
    for placeholder, value in (
        ("{{ screening_queue_metric_api_port }}", "8000"),
        ("{{ screening_queue_metric_env }}", "prod"),
        ("{{ screening_queue_metric_type }}", "custom.googleapis.com/test"),
    ):
        source = source.replace(placeholder, value)
    publisher: dict[str, Any] = {"__name__": "metric_publisher"}
    exec(compile(source, str(template), "exec"), publisher)
    return publisher


def test_watchdog_read_failure_publishes_zero_depth() -> None:
    publisher = _publisher()

    def get(url: str, headers: dict | None = None) -> bytes:
        del headers
        if url == publisher["ACTIVITY_URL"]:
            return b'{"status_counts":{"waiting_screening":24,"screening":2}}'
        if url == publisher["WATCHDOG_URL"]:
            raise OSError("watchdog unavailable")
        if url == publisher["PROJECT_URL"]:
            return b"test-project"
        if url == publisher["METADATA_TOKEN_URL"]:
            return b'{"access_token":"test-token"}'
        raise AssertionError(f"unexpected URL: {url}")

    publisher["_get"] = get
    assert publisher["_read_fallback_depth"]() == (0, "platform_read_failed")
    posted: list[dict[str, Any]] = []

    def post(request: Any, timeout: int) -> io.BytesIO:
        assert timeout == 10
        posted.append(json.loads(request.data))
        return io.BytesIO(b"")

    with patch.object(publisher["urllib"].request, "urlopen", side_effect=post):
        assert publisher["main"]() == 0
    assert posted[0]["timeSeries"][0]["points"][0]["value"] == {"int64Value": "0"}


def test_watchdog_malformed_response_publishes_zero_depth() -> None:
    publisher = _publisher()

    def get(url: str, headers: dict | None = None) -> bytes:
        del headers
        if url == publisher["ACTIVITY_URL"]:
            return b'{"status_counts":{"waiting_screening":24}}'
        return b"not-json"

    publisher["_get"] = get
    assert publisher["_read_fallback_depth"]() == (0, "platform_read_failed")


def test_unbounded_count_publishes_zero_depth() -> None:
    publisher = _publisher()

    def get(url: str, headers: dict | None = None) -> bytes:
        del headers
        if url == publisher["ACTIVITY_URL"]:
            return b'{"status_counts":{"waiting_screening":1e999}}'
        return b'{"activate_fallback":true,"reason":"controller_stale"}'

    publisher["_get"] = get
    assert publisher["_read_fallback_depth"]() == (0, "platform_read_failed")


def test_open_watchdog_retains_positive_backlog() -> None:
    publisher = _publisher()

    def get(url: str, headers: dict | None = None) -> bytes:
        del headers
        if url == publisher["ACTIVITY_URL"]:
            return b'{"status_counts":{"waiting_screening":24,"screening":2}}'
        return b'{"activate_fallback":true,"reason":"controller_stale"}'

    publisher["_get"] = get
    assert publisher["_read_fallback_depth"]() == (26, "controller_stale")
