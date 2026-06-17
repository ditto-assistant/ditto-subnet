"""HTTP client for the Ditto API server.

Synchronous wrapper around ``httpx.Client`` exposing one method per
endpoint the CLI consumes. The CLI is short-lived and runs single
requests, so the synchronous client keeps the orchestrator readable
without spinning up an event loop.

Error mapping:
- 2xx → parsed response model (via :mod:`ditto.api_models`)
- 4xx / 5xx with an ``error_code`` envelope → mapped to a typed
  exception so the CLI can map exit codes; full envelope body is
  attached to the exception for verbose logging
- 404 on ``/retrieval/agent/{id}/status`` → :class:`AgentNotFoundError`
- 404 on ``/retrieval/agent-by-hotkey`` → :class:`HotkeyAgentNotFoundError`
- Transport-level failures (connection refused, timeout, DNS, TLS) →
  :class:`ApiResponseError` with a friendly message naming the
  base URL, so miners do not see raw ``httpx`` tracebacks when the
  API is down or unreachable.
"""

from __future__ import annotations

import logging
from typing import IO, Any
from uuid import UUID

import httpx

from ditto.api_models import (
    AgentResponse,
    AgentStatusResponse,
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckRequest,
    UploadCheckResponse,
)
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    HotkeyAgentNotFoundError,
    PreCheckRejectedError,
    UploadAgentRejectedError,
)
from ditto.miner_cli.models import PaymentReceipt

logger = logging.getLogger(__name__)

# Default per-request timeout. Upload is the longest call (tar streaming)
# but the server-side payment verification is bounded by a few Pylon
# round trips, so a 60s ceiling covers normal-case latency comfortably.
DEFAULT_TIMEOUT_S = 60.0

# Envelope error codes from ditto.api_server.middleware.error_envelope.
# Imported by literal value so the CLI does not depend on api_server at
# import time. If the API renumbers a code, the matching test in
# api_models contract tests (next PR after this one) will catch it.
_ERROR_CODE_AGENT_NOT_FOUND = 1200
_ERROR_CODE_HOTKEY_AGENT_NOT_FOUND = 1201


class ApiClient:
    """Thin synchronous wrapper around the Ditto API.

    One ``ApiClient`` per CLI invocation. The underlying ``httpx.Client``
    is closed via context manager so connections release promptly.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        """Build an ApiClient.

        Args:
            base_url: Base URL for the API. Used only when ``client`` is
                None (the production path).
            timeout_s: Per-request timeout. Used only when ``client`` is
                None.
            client: Pre-built ``httpx.Client``. When supplied, the
                ApiClient reuses it verbatim and ``base_url`` /
                ``timeout_s`` are ignored. Integration tests inject a
                :class:`fastapi.testclient.TestClient` here so requests
                hit the ASGI app via anyio instead of the network.
        """
        if client is not None:
            self._client = client
            self._base_url = str(client.base_url).rstrip("/")
        else:
            self._base_url = base_url.rstrip("/")
            self._client = httpx.Client(base_url=self._base_url, timeout=timeout_s)

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- transport-level wrapper ----------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP request, translating transport faults to typed errors.

        ``httpx.ConnectError``, ``httpx.TimeoutException``, and any other
        :class:`httpx.RequestError` subclass surface as
        :class:`ApiResponseError` with a friendly message naming the
        base URL so miners see something they can act on instead of a
        raw traceback. Response-level errors (non-2xx status) are NOT
        mapped here; each caller maps them based on endpoint semantics.
        """
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.ConnectError as e:
            raise ApiResponseError(
                f"api unreachable at {self._base_url}: {e}. "
                f"Hint: confirm the API is running and --network matches "
                f"your deployment."
            ) from e
        except httpx.TimeoutException as e:
            raise ApiResponseError(
                f"api request timed out at {self._base_url}: {e}"
            ) from e
        except httpx.RequestError as e:
            # Catch-all for other transport faults (DNS, TLS, etc.).
            raise ApiResponseError(
                f"api request failed at {self._base_url}: {e}"
            ) from e

    # ---- /upload/eval-pricing -------------------------------------------

    def get_eval_pricing(self) -> EvalPricingResponse:
        """Fetch the current upload fee + send address."""
        response = self._request("GET", "/api/v1/upload/eval-pricing")
        if response.status_code != 200:
            raise ApiResponseError(_format_error(response, prefix="eval-pricing"))
        return EvalPricingResponse.model_validate(response.json())

    # ---- /upload/check --------------------------------------------------

    def post_upload_check(self, body: UploadCheckRequest) -> UploadCheckResponse:
        """Run pre-payment validation. Returns the raw response body.

        A response with ``ok=False`` is NOT raised here: the server
        intentionally returns 200 with parallel ``error_codes`` +
        ``messages`` so the CLI can show every rejection at once.
        Callers decide whether to bail on a non-empty error_codes list.

        Non-2xx HTTP responses (server-side failures, validation errors)
        still raise :class:`ApiResponseError`.
        """
        response = self._request(
            "POST",
            "/api/v1/upload/check",
            json=body.model_dump(mode="json"),
        )
        if response.status_code != 200:
            raise PreCheckRejectedError(_format_error(response, prefix="upload-check"))
        return UploadCheckResponse.model_validate(response.json())

    # ---- /upload/agent --------------------------------------------------

    def post_upload_agent(
        self,
        *,
        agent_tar: IO[bytes],
        agent_tar_filename: str,
        hotkey: str,
        sha256: str,
        name: str,
        signature: str,
        payment: PaymentReceipt,
    ) -> UploadAgentResponse:
        """Submit the tarball + payment proof.

        Multipart shape mirrors ``ditto/api_server/endpoints/upload.py``
        lines 156-173 exactly. Any drift here breaks every upload.
        """
        files = {
            "agent_tar": (
                agent_tar_filename,
                agent_tar,
                "application/gzip",
            ),
        }
        data = {
            "hotkey": hotkey,
            "sha256": sha256,
            "name": name,
            "signature": signature,
            "payment_block_hash": payment.block_hash,
            "payment_block_number": str(payment.block_number),
            "payment_extrinsic_index": str(payment.extrinsic_index),
        }
        response = self._request(
            "POST",
            "/api/v1/upload/agent",
            files=files,
            data=data,
        )
        if response.status_code != 200:
            raise UploadAgentRejectedError(
                _format_error(response, prefix="upload-agent")
            )
        return UploadAgentResponse.model_validate(response.json())

    # ---- /retrieval/agent/{id}/status -----------------------------------

    def get_agent_status(self, *, agent_id: UUID) -> AgentStatusResponse:
        response = self._request("GET", f"/api/v1/retrieval/agent/{agent_id}/status")
        if response.status_code == 404:
            envelope = _safe_envelope(response)
            if envelope.get("error_code") == _ERROR_CODE_AGENT_NOT_FOUND:
                raise AgentNotFoundError(f"agent {agent_id} not found")
        if response.status_code != 200:
            raise ApiResponseError(_format_error(response, prefix="agent-status"))
        return AgentStatusResponse.model_validate(response.json())

    # ---- /retrieval/agent-by-hotkey -------------------------------------

    def get_agent_by_hotkey(self, *, miner_hotkey: str) -> AgentResponse:
        response = self._request(
            "GET",
            "/api/v1/retrieval/agent-by-hotkey",
            params={"miner_hotkey": miner_hotkey},
        )
        if response.status_code == 404:
            envelope = _safe_envelope(response)
            if envelope.get("error_code") == _ERROR_CODE_HOTKEY_AGENT_NOT_FOUND:
                raise HotkeyAgentNotFoundError(
                    f"no agent found for hotkey {miner_hotkey}"
                )
        if response.status_code != 200:
            raise ApiResponseError(_format_error(response, prefix="agent-by-hotkey"))
        return AgentResponse.model_validate(response.json())


def _safe_envelope(response: httpx.Response) -> dict[str, Any]:
    """Decode the JSON envelope; tolerate non-JSON bodies for diagnostics."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _format_error(response: httpx.Response, *, prefix: str) -> str:
    """Build a human-readable error string from a failed response."""
    envelope = _safe_envelope(response)
    code = envelope.get("error_code", "?")
    message = envelope.get("message", response.text[:200])
    return f"{prefix} failed: HTTP {response.status_code} code={code} {message}"
