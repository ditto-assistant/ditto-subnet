from __future__ import annotations

import json
import os
import traceback
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import bittensor
import httpx
import pytest

from ditto.api_models.coding_hosted import (
    HostedCodingResult,
    HostedCodingStatus,
    hosted_signing_bytes,
)
from ditto.api_server import factory
from ditto.api_server.coding_hosted_signer import load_hosted_control_signer
from ditto.api_server.coding_hosted_signer_config import (
    HostedControlSignerConfig,
    parse_hosted_signer_config_from_env,
)
from ditto.api_server.config import check_config, parse_api_server_config_from_env
from ditto.api_server.errors import ApiServerConfigError, ApiServerLifespanError
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.test_config import _set_minimum_env
from ditto.tests.db.queries.test_coding_hosted_admission import (
    VALIDATOR,
    _request,
    _seed,
)

SEED = bytes.fromhex("11" * 32)  # Public synthetic test key, never production config.
KEY = bittensor.Keypair.create_from_seed(SEED.hex())
CONTROL_PATH = "/api/v1/validator/coding-hosted/control"


@pytest.fixture
def signer_config(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "seed"
    path.write_bytes(SEED)
    path.chmod(0o600)
    return HostedControlSignerConfig(True, path, KEY.ss58_address)


def status_body(**updates):
    fields = {
        "schema": "dittobench-coding-hosted-status-v2",
        "coding_contract_version": 2,
        "shadow_only": True,
        "weight_eligible": False,
        "evaluation_id": uuid4(),
        "attempt_id": uuid4(),
        "validator_hotkey": VALIDATOR.ss58_address,
        "platform_hotkey": KEY.ss58_address,
        "request_sha256": "a" * 64,
        "artifact_sha256": "b" * 64,
        "assignment_sha256": "c" * 64,
        "policy_sha256": "d" * 64,
        "execution_profile_sha256": "e" * 64,
        "grading_profile_sha256": "f" * 64,
        "state": "admitted",
        "issued_at_unix": 100,
        "expires_at_unix": 200,
        "signature": "0" * 128,
    }
    fields.update(updates)
    return hosted_signing_bytes(HostedCodingStatus.model_validate(fields))


def test_signer_signs_status_and_terminal_and_refuses_after_close(signer_config):
    signer = load_hosted_control_signer(signer_config, process_role="platform")
    assert signer is not None
    body = status_body()
    assert KEY.verify(body, signer.sign(body))
    fields = json.loads(body)
    fields.pop("state")
    fields.update(
        schema="dittobench-coding-hosted-result-v2",
        evidence_sha256="9" * 64,
        outcome="completed",
        signature="0" * 128,
    )
    terminal = hosted_signing_bytes(HostedCodingResult.model_validate(fields))
    assert KEY.verify(terminal, signer.sign(terminal))
    assert SEED.hex() not in repr(signer) and str(signer_config.seed_file) not in repr(
        signer_config
    )
    signer.close()
    signer.close()
    with pytest.raises(ValueError, match="^hosted Coding signer unavailable$"):
        signer.sign(body)


@pytest.mark.parametrize("role,enabled", [("relay", True), ("platform", False)])
def test_disabled_and_relay_never_read_seed(monkeypatch, role, enabled):
    monkeypatch.setattr(
        "ditto.api_server.coding_hosted_signer.read_private",
        lambda *_args: pytest.fail("seed was read"),
    )
    assert (
        load_hosted_control_signer(
            HostedControlSignerConfig(enabled, Path("/missing/private"), "invalid"),
            process_role=role,
        )
        is None
    )


@pytest.mark.parametrize("raw", ["", "yes", "truthy", "2"])
def test_activation_typo_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("DITTO_CODING_HOSTED_CONTROL_ENABLED", raw)
    with pytest.raises(ApiServerConfigError):
        parse_hosted_signer_config_from_env()


def test_config_defaults_disabled_and_enabled_config_is_wired(
    monkeypatch, signer_config
):
    _set_minimum_env(monkeypatch)
    monkeypatch.delenv("DITTO_CODING_HOSTED_CONTROL_ENABLED", raising=False)
    assert not parse_api_server_config_from_env("test").coding_hosted_signer.enabled
    monkeypatch.setenv("DITTO_CODING_HOSTED_CONTROL_ENABLED", "true")
    monkeypatch.setenv(
        "DITTO_CODING_HOSTED_SIGNER_SEED_FILE", str(signer_config.seed_file)
    )
    monkeypatch.setenv("DITTO_CODING_HOSTED_SIGNER_HOTKEY", KEY.ss58_address)
    config = parse_api_server_config_from_env("test")
    assert config.coding_hosted_signer == signer_config
    check_config(config)
    with pytest.raises(ApiServerConfigError):
        check_config(
            replace(config, coding_hosted_signer=HostedControlSignerConfig(True))
        )


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o666])
def test_unsafe_seed_permissions_fail_redacted(signer_config, mode):
    signer_config.seed_file.chmod(mode)
    with pytest.raises(
        ApiServerConfigError, match="^hosted Coding signer unavailable$"
    ) as raised:
        load_hosted_control_signer(signer_config, process_role="platform")
    assert str(signer_config.seed_file) not in str(raised.value)
    trace = "".join(traceback.format_exception(raised.value))
    assert str(signer_config.seed_file) not in trace and SEED.hex() not in trace


@pytest.mark.parametrize(
    "seed", [b"", b"x" * 31, b"x" * 33, bytes(32), b"//Alice", SEED.hex().encode()]
)
def test_only_raw_nonzero_32_byte_seed_is_accepted(signer_config, seed):
    signer_config.seed_file.write_bytes(seed)
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(signer_config, process_role="platform")


def test_hotkey_mismatch_symlinks_and_hardlinks_fail(signer_config):
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(
            replace(signer_config, expected_hotkey=VALIDATOR.ss58_address),
            process_role="platform",
        )
    alias = signer_config.seed_file.parent / "alias"
    alias.symlink_to(signer_config.seed_file)
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(
            replace(signer_config, seed_file=alias), process_role="platform"
        )
    alias.unlink()
    os.link(signer_config.seed_file, alias)
    with pytest.raises(ApiServerConfigError):
        load_hosted_control_signer(signer_config, process_role="platform")


@pytest.mark.parametrize(
    "mutation",
    ["extra", "wrong_hotkey", "request", "weights", "noncanonical", "oversize"],
)
def test_signer_cannot_sign_other_authority(signer_config, mutation):
    signer = load_hosted_control_signer(signer_config, process_role="platform")
    fields = json.loads(status_body())
    if mutation == "extra":
        fields["private_bundle"] = "PRIVATE_MARKER"
    elif mutation == "wrong_hotkey":
        fields["platform_hotkey"] = VALIDATOR.ss58_address
    elif mutation == "request":
        fields["schema"] = "dittobench-coding-hosted-request-v2"
    elif mutation == "weights":
        fields["weight_eligible"] = True
    body = (json.dumps(fields, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if mutation == "noncanonical":
        body += b" "
    elif mutation == "oversize":
        body = b"x" * 8193
    with pytest.raises(ValueError, match="^hosted Coding signer unavailable$"):
        signer.sign(body)


def mock_dependencies(monkeypatch, session_maker):
    resource = SimpleNamespace(aclose=AsyncMock(), start=AsyncMock())
    engine = SimpleNamespace(dispose=AsyncMock())
    chain = SimpleNamespace(
        get_recent_neurons=AsyncMock(
            return_value=[
                SimpleNamespace(hotkey=VALIDATOR.ss58_address, validator_permit=True)
            ]
        )
    )

    @asynccontextmanager
    async def chain_context(_config):
        yield chain

    @asynccontextmanager
    async def storage_context(_config):
        yield resource

    monkeypatch.setattr(factory, "create_db_engine", lambda _config: engine)
    monkeypatch.setattr(factory, "create_session_maker", lambda _engine: session_maker)
    monkeypatch.setattr(factory, "create_chain_client", chain_context)
    monkeypatch.setattr(factory, "create_storage_client", storage_context)
    for name in (
        "create_price_oracle",
        "create_embedder",
        "create_generator",
        "ProviderRouteRefresher",
        "ValidatorNonceJanitor",
    ):
        monkeypatch.setattr(factory, name, lambda *_a, **_kw: resource)
    monkeypatch.setattr(factory, "create_payment_verifier", MagicMock())
    monkeypatch.setattr(
        factory, "create_hippius_evidence_runtime_from_env", lambda **_kw: None
    )
    monkeypatch.setattr(factory, "coding_inference_transport_from_env", lambda: None)
    monkeypatch.setattr(
        "ditto.api_server.hippius.create_hippius_client", lambda *_a: None
    )
    monkeypatch.setattr(
        "ditto.api_server.hippius.parse_traces_hippius_config_from_env", lambda: None
    )


async def test_real_startup_serves_signed_admission_and_closes(
    signer_config, monkeypatch, session_maker
):
    monkeypatch.setenv("DITTO_ROLE", "platform")
    mock_dependencies(monkeypatch, session_maker)
    app = factory.create_api_server(
        make_api_server_config(coding_hosted_signer=signer_config)
    )
    app.state.validator_names.start = AsyncMock()
    app.state.validator_names.aclose = AsyncMock()
    assert app.state.coding_hosted_control is None
    authority = await _seed(session_maker)
    async with app.router.lifespan_context(app):
        signer = app.state.coding_hosted_control.signer
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                CONTROL_PATH,
                json=_request(authority).model_dump(mode="json", by_alias=True),
            )
        assert response.status_code == 202, response.text
        status = HostedCodingStatus.model_validate(response.json())
        assert status.platform_hotkey == KEY.ss58_address
        assert KEY.verify(hosted_signing_bytes(status), bytes.fromhex(status.signature))
        assert response.headers["cache-control"] == "no-store"
    assert app.state.coding_hosted_control is None
    with pytest.raises(ValueError):
        signer.sign(status_body())


async def test_startup_failure_closes_preloaded_signer(signer_config, monkeypatch):
    monkeypatch.setenv("DITTO_ROLE", "platform")
    signer = load_hosted_control_signer(signer_config, process_role="platform")
    monkeypatch.setattr(
        factory, "load_hosted_control_signer", lambda *_a, **_kw: signer
    )

    def fail(_config):
        raise RuntimeError("synthetic database startup failure")

    monkeypatch.setattr(factory, "create_db_engine", fail)
    app = factory.create_api_server(
        make_api_server_config(coding_hosted_signer=signer_config)
    )
    with pytest.raises(ApiServerLifespanError):
        async with app.router.lifespan_context(app):
            pytest.fail("startup succeeded")
    assert app.state.coding_hosted_control is None
    with pytest.raises(ValueError):
        signer.sign(status_body())


async def test_relay_lifespan_never_loads_signing_file(
    signer_config, monkeypatch, session_maker
):
    monkeypatch.setenv("DITTO_ROLE", "relay")
    mock_dependencies(monkeypatch, session_maker)
    monkeypatch.setattr(
        "ditto.api_server.coding_hosted_signer.read_private",
        lambda *_args: pytest.fail("relay seed read"),
    )
    app = factory.create_api_server(
        make_api_server_config(coding_hosted_signer=signer_config)
    )
    app.state.validator_names.start = AsyncMock()
    app.state.validator_names.aclose = AsyncMock()
    async with app.router.lifespan_context(app):
        assert app.state.coding_hosted_control is None
    assert app.state.coding_hosted_control is None


async def test_shutdown_error_still_closes_signer(
    signer_config, monkeypatch, session_maker
):
    monkeypatch.setenv("DITTO_ROLE", "platform")
    mock_dependencies(monkeypatch, session_maker)
    engine = SimpleNamespace(
        dispose=AsyncMock(side_effect=RuntimeError("synthetic close failure"))
    )
    monkeypatch.setattr(factory, "create_db_engine", lambda _config: engine)
    app = factory.create_api_server(
        make_api_server_config(coding_hosted_signer=signer_config)
    )
    app.state.validator_names.start = AsyncMock()
    app.state.validator_names.aclose = AsyncMock()
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        async with app.router.lifespan_context(app):
            signer = app.state.coding_hosted_control.signer
    assert app.state.coding_hosted_control is None
    with pytest.raises(ValueError):
        signer.sign(status_body())
