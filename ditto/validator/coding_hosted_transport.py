"""Opt-in transport for signed Platform-hosted Coding control projections."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from types import TracebackType

import httpx

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    hosted_message_digest,
)
from ditto.validator.coding_hosted import (
    MAX_HOSTED_RESULT_BYTES,
    HostedResultExpectation,
    SignatureVerifier,
    verify_hosted_result,
)

HOSTED_CONTROL_PATH = "/api/v1/validator/coding-hosted/control"
HOSTED_CONTROL_TIMEOUT_SECONDS = 30


class HostedCodingTransportError(RuntimeError):
    """A control-plane failure, never evidence of candidate patch failure."""


class HostedCodingTransport:
    """No worker imports this adapter; the corresponding API is not active yet.

    The origin and verification keys come from trusted operator configuration,
    never a candidate, assignment URL or received result. No automatic retry or
    private task download is supported.
    """

    def __init__(
        self,
        *,
        platform_origin: str,
        trusted_verifiers: Mapping[str, SignatureVerifier],
        clock: Callable[[], int] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            origin = httpx.URL(platform_origin)
            if (
                origin.scheme != "https"
                or not origin.host
                or origin.userinfo
                or origin.path != "/"
                or origin.query
                or origin.fragment
                or str(origin).rstrip("/") != platform_origin.rstrip("/")
                or not trusted_verifiers
            ):
                raise ValueError("origin or signer configuration")
        except Exception:
            raise HostedCodingTransportError(
                "hosted Coding transport configuration is invalid"
            ) from None
        self._url = origin.copy_with(path=HOSTED_CONTROL_PATH)
        self._verifiers = dict(trusted_verifiers)
        self._clock = clock or (lambda: int(time.time()))
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(HOSTED_CONTROL_TIMEOUT_SECONDS),
        )

    async def __aenter__(self) -> HostedCodingTransport:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def exchange(
        self, *, request: HostedCodingRequest, expected: HostedResultExpectation
    ) -> HostedCodingResult:
        """Return a verified terminal projection or a fixed, redacted error.

        A pending or unavailable HTTP response is not a terminal result. Durable
        admission/status orchestration must handle polling separately; this
        adapter deliberately never retries an evaluate request.
        """
        try:
            request = HostedCodingRequest.model_validate(
                request.model_dump(mode="json", by_alias=True)
            )
            now = self._clock()
            if (
                type(now) is not int
                or not request.issued_at_unix <= now < request.expires_at_unix
                or expected.request_sha256 != hosted_message_digest(request)
                or expected.platform_hotkey not in self._verifiers
                or any(
                    getattr(request, field) != getattr(expected, field)
                    for field in (
                        "evaluation_id",
                        "validator_hotkey",
                        "artifact_sha256",
                        "assignment_sha256",
                        "policy_sha256",
                    )
                )
            ):
                raise ValueError("request authority")
            payload = (
                json.dumps(
                    request.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            if len(payload) > MAX_HOSTED_RESULT_BYTES:
                raise ValueError("request bounds")
            async with asyncio.timeout(
                min(HOSTED_CONTROL_TIMEOUT_SECONDS, request.expires_at_unix - now)
            ):
                body = await self._post(payload, request.validator_hotkey)
            return verify_hosted_result(
                body=body,
                expected=expected,
                trusted_verifiers=self._verifiers,
                now_unix=self._clock(),
            )
        except Exception:
            raise HostedCodingTransportError(
                "hosted Coding control exchange failed"
            ) from None

    async def _post(self, payload: bytes, validator_hotkey: str) -> bytes:
        async with self._client.stream(
            "POST",
            self._url,
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-store",
                "X-Validator-Hotkey": validator_hotkey,
            },
        ) as response:
            directives = {
                part.strip().lower()
                for part in response.headers.get("cache-control", "").split(",")
            }
            if (
                response.status_code != 200
                or "no-store" not in directives
                or response.headers.get("content-encoding", "identity").lower()
                != "identity"
                or response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
                != "application/json"
            ):
                raise ValueError("response headers")
            length = response.headers.get("content-length")
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or len(length) > 4
                or not 0 < int(length) <= MAX_HOSTED_RESULT_BYTES
            ):
                raise ValueError("response length")
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_HOSTED_RESULT_BYTES:
                    raise ValueError("response bounds")
                body.extend(chunk)
            if not body or (length is not None and len(body) != int(length)):
                raise ValueError("response length")
            return bytes(body)
