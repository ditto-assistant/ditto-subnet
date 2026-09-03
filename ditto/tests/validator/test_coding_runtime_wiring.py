from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

import ditto.validator.__main__ as validator_main
from ditto.validator.coding_supervisor import CodingSupervisorRuntime


def _config(*, remote: bool) -> Any:
    return SimpleNamespace(
        coding_shadow_enabled=True,
        coding_shadow_run_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        coding_shadow_instance_id="coding-shadow-primary",
        coding_shadow_poll_seconds=10.0,
        coding_executor_remote_enabled=remote,
        coding_executor_connectivity_canary_enabled=False,
        coding_executor_base_url=("https://10.23.0.10:9443" if remote else ""),
        coding_executor_ca_path=("/run/secrets/executor-ca.pem" if remote else ""),
        coding_executor_client_cert_path=(
            "/run/secrets/validator-client.pem" if remote else ""
        ),
        coding_executor_client_key_path=(
            "/run/secrets/validator-client-key.pem" if remote else ""
        ),
        coding_executor_timeout_seconds=30.0,
        dittobench_api_url="http://127.0.0.1:18081",
        dittobench_control_token="coding-control-token-00000000000000000001",
        validator_hotkey="5" + "V" * 47,
        http_timeout_seconds=30.0,
    )


async def test_remote_runtime_injects_one_dedicated_client_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")),
        trust_env=False,
    )
    observed: list[Any] = []

    def create(config: Any) -> httpx.AsyncClient:
        observed.append(config)
        return executor_http

    monkeypatch.setattr(validator_main, "create_coding_executor_http_client", create)
    async with AsyncExitStack() as resources:
        worker = await validator_main._create_coding_shadow_worker(
            config=_config(remote=True),
            platform=object(),  # type: ignore[arg-type]
            keypair=object(),
            resources=resources,
        )
        assert worker is not None
        runtime = cast(CodingSupervisorRuntime, worker._runtime)
        assert worker._publication._client is executor_http
        assert runtime._client is executor_http
        assert worker._publication.remote is True
        assert runtime._remote is True
        assert executor_http.is_closed is False
        assert len(observed) == 1
    assert executor_http.is_closed is True


async def test_local_runtime_uses_separate_no_proxy_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        validator_main,
        "create_coding_executor_http_client",
        lambda _: pytest.fail("remote client constructed"),
    )
    coding_http: httpx.AsyncClient | None = None
    async with AsyncExitStack() as resources:
        worker = await validator_main._create_coding_shadow_worker(
            config=_config(remote=False),
            platform=object(),  # type: ignore[arg-type]
            keypair=object(),
            resources=resources,
        )
        assert worker is not None
        runtime = cast(CodingSupervisorRuntime, worker._runtime)
        coding_http = worker._publication._client
        assert coding_http.trust_env is False
        assert runtime._client is coding_http
        assert worker._publication.remote is False
        assert runtime._remote is False
        assert coding_http.is_closed is False
    assert coding_http is not None and coding_http.is_closed is True


async def test_disabled_runtime_constructs_no_executor_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(remote=False)
    config.coding_shadow_enabled = False
    monkeypatch.setattr(
        validator_main,
        "create_coding_executor_http_client",
        lambda _: pytest.fail("remote client constructed"),
    )
    async with AsyncExitStack() as resources:
        assert (
            await validator_main._create_coding_shadow_worker(
                config=config,
                platform=object(),  # type: ignore[arg-type]
                keypair=object(),
                resources=resources,
            )
            is None
        )


async def test_remote_runtime_closes_client_when_atomic_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor_http = httpx.AsyncClient(trust_env=False)
    monkeypatch.setattr(
        validator_main,
        "create_coding_executor_http_client",
        lambda _: executor_http,
    )

    def reject(**_: Any) -> None:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(
        validator_main,
        "CodingPublicationClient",
        reject,
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        async with AsyncExitStack() as resources:
            await validator_main._create_coding_shadow_worker(
                config=_config(remote=True),
                platform=object(),  # type: ignore[arg-type]
                keypair=object(),
                resources=resources,
            )
    assert executor_http.is_closed is True


async def test_connectivity_canary_uses_no_ticket_or_platform_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json={
                "schema": "dittobench-coding-executor-readiness-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "transport": "mtls",
                "supervisor_ready": True,
                "publication_ready": True,
                "ticket_authority_used": False,
            },
        )

    canary_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    )
    monkeypatch.setattr(
        validator_main,
        "create_coding_executor_http_client",
        lambda _: canary_http,
    )
    config = _config(remote=False)
    config.coding_shadow_enabled = False
    config.coding_executor_connectivity_canary_enabled = True
    config.coding_executor_base_url = "https://10.23.0.10:9443"
    config.coding_executor_ca_path = "/run/secrets/executor-ca.pem"
    config.coding_executor_client_cert_path = "/run/secrets/validator-client.pem"
    config.coding_executor_client_key_path = "/run/secrets/validator-client-key.pem"
    await validator_main._run_coding_executor_connectivity_canary(config)
    assert canary_http.is_closed is True
    assert len(observed) == 1
    assert observed[0].method == "GET"
    assert "X-Dittobench-Coding-Control" not in observed[0].headers


async def test_connectivity_canary_exits_before_keypair_platform_and_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(remote=False)
    config.coding_shadow_enabled = False
    config.coding_executor_connectivity_canary_enabled = True
    events: list[str] = []
    monkeypatch.setattr(
        validator_main, "bootstrap_should_start_drained", lambda _: False
    )
    monkeypatch.setattr(
        validator_main, "_install_signal_handlers", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        validator_main, "parse_validator_config_from_env", lambda: config
    )
    monkeypatch.setattr(
        validator_main,
        "_run_coding_executor_connectivity_canary",
        lambda _: _record_async(events, "probe"),
    )
    monkeypatch.setattr(
        validator_main,
        "write_update_state",
        lambda state: events.append(state),
    )
    monkeypatch.setattr(
        validator_main,
        "load_validator_keypair",
        lambda _: pytest.fail("keypair loaded"),
    )
    monkeypatch.setattr(
        validator_main,
        "build_telemetry",
        lambda *_a, **_k: pytest.fail("telemetry constructed"),
    )
    assert await validator_main._amain() == 0
    assert events == ["starting", "probe", "stopping"]


async def _record_async(events: list[str], value: str) -> None:
    events.append(value)
