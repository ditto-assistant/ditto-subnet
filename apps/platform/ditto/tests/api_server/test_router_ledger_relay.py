"""Unit tests for the shadow router-ledger relay reader.

The reader is the platform's outbound half: it fetches the ledger the offloaded
scorer publishes and must *never* raise into a validator's fold. These tests pin
that fail-closed contract (empty on failure, bounded last-known reuse) and the
security properties of ``create_router_ledger_reader_from_env`` (both env vars
required, http(s) only, the control token sent solely as a ``Bearer`` header).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from ditto.api_models.router_ledger import (
    RouterHarness,
    RouterHarnessResult,
    RouterLedgerEntry,
    RouterLedgerResponse,
)
from ditto.api_server import router_ledger_relay
from ditto.api_server.router_ledger_relay import (
    RouterLedgerReader,
    create_router_ledger_reader_from_env,
)


def _sample_ledger() -> RouterLedgerResponse:
    return RouterLedgerResponse(
        entries=[
            RouterLedgerEntry(
                miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                agent_id=uuid4(),
                router_contract_version=1,
                weight_eligible=False,
                combined_score=0.0,
                shadow_composite=0.37,
                harnesses=(
                    RouterHarnessResult(
                        harness=RouterHarness.CODEX,
                        operational=True,
                        floor_pass=True,
                        efficiency=0.37,
                        upstream_token_cost_micros=999,
                    ),
                ),
                first_seen=datetime.now(UTC),
            )
        ],
        count=1,
    )


def _reader_with_handler(handler) -> RouterLedgerReader:
    """A reader whose internal client is backed by a MockTransport."""
    reader = RouterLedgerReader(
        base_url="https://scorer.internal:8000", control_token="s3cr3t-token"
    )
    reader._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return reader


class TestRouterLedgerReader:
    async def test_successful_read_returns_ledger_and_sends_bearer_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            seen["url"] = str(request.url)
            return httpx.Response(
                200, content=_sample_ledger().model_dump_json().encode()
            )

        reader = _reader_with_handler(handler)
        try:
            ledger = await reader.read()
        finally:
            await reader.aclose()

        assert ledger.count == 1
        assert ledger.entries[0].shadow_composite == pytest.approx(0.37)
        # The secret is sent only as a Bearer header, to the configured path.
        assert seen["auth"] == "Bearer s3cr3t-token"
        assert seen["url"].endswith("/v1/router/ledger")

    async def test_non_200_with_no_cache_serves_empty(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="scorer down")

        reader = _reader_with_handler(handler)
        try:
            ledger = await reader.read()
        finally:
            await reader.aclose()
        assert ledger.entries == []
        assert ledger.count == 0
        assert ledger.stale is False

    async def test_failure_after_success_serves_last_known_flagged_stale(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200, content=_sample_ledger().model_dump_json().encode()
                )
            return httpx.Response(500, text="boom")

        reader = _reader_with_handler(handler)
        try:
            first = await reader.read()
            assert first.stale is False
            second = await reader.read()
        finally:
            await reader.aclose()
        # The still-fresh last-known snapshot is reused, flagged stale.
        assert second.count == 1
        assert second.stale is True

    async def test_last_known_past_max_stale_serves_empty(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        reader = _reader_with_handler(handler)
        # Seed a last-known snapshot older than the staleness bound.
        reader._last_known = router_ledger_relay._CachedLedger(
            ledger=_sample_ledger(),
            fetched_at=datetime.now(UTC)
            - (router_ledger_relay._MAX_STALE + timedelta(seconds=1)),
        )
        try:
            ledger = await reader.read()
        finally:
            await reader.aclose()
        assert ledger.entries == []
        assert ledger.stale is False


class TestCreateFromEnv:
    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DITTOBENCH_ROUTER_SCORER_URL", raising=False)
        monkeypatch.delenv("DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN", raising=False)
        assert create_router_ledger_reader_from_env() is None

    def test_token_without_url_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DITTOBENCH_ROUTER_SCORER_URL", raising=False)
        monkeypatch.setenv("DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN", "tok")
        assert create_router_ledger_reader_from_env() is None

    def test_non_http_scheme_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DITTOBENCH_ROUTER_SCORER_URL", "ftp://scorer.internal")
        monkeypatch.setenv("DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN", "tok")
        assert create_router_ledger_reader_from_env() is None

    async def test_both_set_builds_reader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "DITTOBENCH_ROUTER_SCORER_URL", "https://scorer.internal:8000"
        )
        monkeypatch.setenv("DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN", "tok")
        reader = create_router_ledger_reader_from_env()
        assert isinstance(reader, RouterLedgerReader)
        await reader.aclose()

    def test_control_token_is_never_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(
            "DITTOBENCH_ROUTER_SCORER_URL", "https://scorer.internal:8000"
        )
        monkeypatch.setenv("DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN", "super-secret")
        with caplog.at_level("DEBUG"):
            reader = create_router_ledger_reader_from_env()
        assert "super-secret" not in caplog.text
        assert reader is not None
