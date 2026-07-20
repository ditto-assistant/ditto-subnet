"""Unit tests for :mod:`ditto.miner_cli.api_client`.

The HTTP layer is mocked via :class:`httpx.MockTransport` so tests pin
exact wire shapes (paths, query params, multipart fields) without
starting a real ASGI app.

Invariants pinned:

- Path + method for each endpoint
- Multipart form fields exactly match
  ``ditto/api_server/endpoints/upload.py:156-173``
- Envelope code mapping: 1200 → AgentNotFoundError, 1201 →
  HotkeyAgentNotFoundError, others → ApiResponseError subclass
- Successful responses parse into the matching Pydantic model
"""

from __future__ import annotations

import io
import json
from typing import Any
from uuid import UUID

import httpx
import pytest

from ditto.api_models import (
    EvalPricingResponse,
    UploadCheckRequest,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    HotkeyAgentNotFoundError,
    PreCheckRejectedError,
    TransientApiError,
    UploadAgentRejectedError,
)
from ditto.miner_cli.models import PaymentReceipt


def make_client(handler) -> ApiClient:  # type: ignore[no-untyped-def]
    """Build an :class:`ApiClient` whose underlying httpx client is a
    mock transport. The base URL is irrelevant since the transport
    short-circuits the network."""
    transport = httpx.MockTransport(handler)
    client = ApiClient(base_url="http://test")
    client._client.close()
    client._client = httpx.Client(transport=transport, base_url="http://test")
    return client


def _envelope_response(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json={
            "error_code": code,
            "message": message,
        },
    )


class TestEvalPricing:
    def test_happy_path(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={
                    "amount_rao": 1_500_000_000,
                    "send_address": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                },
            )

        with make_client(handler) as client:
            result = client.get_eval_pricing()

        assert captured["method"] == "GET"
        assert captured["url"].endswith("/api/v1/upload/eval-pricing")
        assert isinstance(result, EvalPricingResponse)
        assert result.amount_rao == 1_500_000_000

    def test_non_200_raises_api_response_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(503, 3101, "oracle unreachable")

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_eval_pricing()

        assert "503" in str(e.value)
        assert "3101" in str(e.value)


class TestUploadCheck:
    def _body(self) -> UploadCheckRequest:
        return UploadCheckRequest(
            hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256="ab" * 32,
            file_size_bytes=1024,
            signature="cd" * 64,
        )

    def test_happy_path_returns_response_with_ok_false_payload(self) -> None:
        """ok=False still returns 200 because the server intentionally
        surfaces every rejection in one round trip. The CLI does NOT
        raise on that path; the orchestrator decides."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"ok": False, "error_codes": [1100], "messages": ["bad sig"]},
            )

        with make_client(handler) as client:
            result = client.post_upload_check(self._body())

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/api/v1/upload/check")
        # Body fields match the wire model exactly.
        assert captured["body"]["sha256"] == "ab" * 32
        assert result.ok is False
        assert result.error_codes == [1100]

    def test_non_200_raises_pre_check_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(503, 3001, "validation failure")

        with make_client(handler) as client, pytest.raises(PreCheckRejectedError):
            client.post_upload_check(self._body())


class TestUploadAgent:
    def _kwargs(self) -> dict[str, Any]:
        return {
            "agent_tar": io.BytesIO(b"FAKETARBYTES"),
            "agent_tar_filename": "agent.tar.gz",
            "hotkey": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            "sha256": "ab" * 32,
            "name": "smoke-agent",
            "signature": "cd" * 64,
            "payment": PaymentReceipt(
                block_hash="0x" + "ef" * 32,
                block_number=42,
                extrinsic_index=3,
            ),
        }

    def test_multipart_shape_matches_server_form_fields(self) -> None:
        """The server's Form() declarations (upload.py:156-173) name the
        exact fields we must send. Drift = broken upload."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "agent_id": "11111111-1111-1111-1111-111111111111",
                    "version": 2,
                    "status": "uploaded",
                },
            )

        with make_client(handler) as client:
            result = client.post_upload_agent(**self._kwargs())

        assert "multipart/form-data" in captured["content_type"]

        body_str = captured["body"].decode("latin-1")
        # Every Form() field name must appear as a multipart part name.
        for field in (
            "hotkey",
            "sha256",
            "name",
            "signature",
            "payment_block_hash",
            "payment_block_number",
            "payment_extrinsic_index",
            "agent_tar",
        ):
            assert f'name="{field}"' in body_str, f"missing field {field!r} in body"

        assert str(result.agent_id) == "11111111-1111-1111-1111-111111111111"
        assert result.version == 2

    def test_non_200_after_payment_raises_upload_agent_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(402, 3207, "payment already used")

        with (
            make_client(handler) as client,
            pytest.raises(UploadAgentRejectedError) as e,
        ):
            client.post_upload_agent(**self._kwargs())

        assert "3207" in str(e.value)

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable_status_raises_transient_error(self, status: int) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(status, 3001, "retry shortly")

        with make_client(handler) as client, pytest.raises(TransientApiError):
            client.post_upload_agent(**self._kwargs())


class TestAgentStatus:
    def test_happy_path(self) -> None:
        agent_id = UUID("11111111-1111-1111-1111-111111111111")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith(
                f"/api/v1/retrieval/agent/{agent_id}/status"
            )
            return httpx.Response(
                200,
                json={
                    "agent_id": str(agent_id),
                    "status": "rejected",
                    "screening_reason": "Remove the bundled credential and resubmit",
                },
            )

        with make_client(handler) as client:
            result = client.get_agent_status(agent_id=agent_id)

        assert result.agent_id == agent_id
        assert result.screening_reason == "Remove the bundled credential and resubmit"

    def test_404_with_1200_raises_agent_not_found(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(404, 1200, "agent not found")

        with make_client(handler) as client, pytest.raises(AgentNotFoundError):
            client.get_agent_status(agent_id=UUID(int=0))

    def test_404_with_other_code_raises_generic(self) -> None:
        """A 404 with an unexpected envelope falls through to the
        generic ApiResponseError; we don't want to silently mask
        unknown envelope codes as agent-not-found."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(404, 9999, "unknown")

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        # Must NOT be the specific subclass.
        assert not isinstance(e.value, AgentNotFoundError)


class TestAgentByHotkey:
    HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"

    def test_happy_path_passes_query_param(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "agent_id": "11111111-1111-1111-1111-111111111111",
                    "miner_hotkey": self.HOTKEY,
                    "name": "alpha",
                    "version": 2,
                    "status": "uploaded",
                    "sha256": "ab" * 32,
                    "created_at": "2026-06-15T12:00:00Z",
                    "screening_reason": "Remove the bundled credential and resubmit",
                },
            )

        with make_client(handler) as client:
            result = client.get_agent_by_hotkey(miner_hotkey=self.HOTKEY)

        assert captured["query"] == {"miner_hotkey": self.HOTKEY}
        assert result.miner_hotkey == self.HOTKEY
        assert result.version == 2
        assert result.screening_reason == "Remove the bundled credential and resubmit"

    def test_404_with_1201_raises_hotkey_not_found(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(404, 1201, "no agent for hotkey")

        with make_client(handler) as client, pytest.raises(HotkeyAgentNotFoundError):
            client.get_agent_by_hotkey(miner_hotkey=self.HOTKEY)


class TestTransportErrors:
    """Transport-level failures must surface as :class:`ApiResponseError`
    with a friendly message, not raw ``httpx`` tracebacks.

    The mock transport's handler raises the relevant ``httpx`` exception
    so the request never gets a response; ``_request`` is the
    translation point.
    """

    def test_connect_error_maps_to_api_response_error(self) -> None:
        """API is down. Miner should see a single friendly line, not a
        50-line traceback."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        msg = str(e.value)
        assert "api unreachable" in msg
        assert "http://test" in msg
        # Message includes a hint so miners know what to check first.
        assert "Hint" in msg or "hint" in msg

    def test_timeout_maps_to_api_response_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout", request=request)

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_eval_pricing()

        assert "timed out" in str(e.value)
        assert "http://test" in str(e.value)

    def test_other_request_error_maps_to_api_response_error(self) -> None:
        """Catch-all path: DNS / TLS / other ``httpx.RequestError``
        subclasses surface as ApiResponseError too, so the orchestrator
        only ever needs to catch one symbol."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.UnsupportedProtocol("bad scheme", request=request)

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        # Specifically NOT the more-specific subclasses.
        assert not isinstance(e.value, HotkeyAgentNotFoundError)
        assert not isinstance(e.value, AgentNotFoundError)
        assert "http://test" in str(e.value)

    def test_connect_error_chained_for_postmortem(self) -> None:
        """The original httpx exception is chained via ``from e`` so a
        ``-v`` run can still inspect the underlying cause without losing
        the friendly message."""
        original = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal original
            original = httpx.ConnectError("refused", request=request)
            raise original

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        assert isinstance(e.value.__cause__, httpx.ConnectError)
