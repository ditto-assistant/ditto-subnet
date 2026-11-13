"""Node transports: signed HTTP to Platform, or in-process to a coordinator.

Both serialize every request to JSON and sign the exact bytes sent, so the
dry run exercises the same signatures, nonces and wire models as a real node.
The HTTP transport uses only the standard library: no proxies, no redirects,
and bounded response sizes.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from ditto_edge_protocol import paths
from ditto_edge_protocol.coordinator import Coordinator, CoordinatorError
from ditto_edge_protocol.limits import MAX_OBJECT_BYTES_CEILING
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.wire import (
    AckRequest,
    AckResponse,
    AnswerResponse,
    ChallengeAnswer,
    EnrollRequest,
    EnrollResponse,
    LeaveRequest,
    NodeStatusResponse,
    PollRequest,
    PollResponse,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_MAX_JSON_BYTES = 4 * 1024 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


LANE_OFF_CODE = 6001
"""Platform's error-envelope code for a lane that is off."""


class TransportError(RuntimeError):
    """Platform refused a request or could not be reached."""

    def __init__(self, status: int, message: str, *, lane_off: bool = False) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.lane_off = lane_off
        """Platform said the lane is off: its kill switch, not a fault."""


def _lane_off(status: int, detail: str) -> bool:
    if status != 503:
        return False
    try:
        envelope = json.loads(detail)
    except ValueError:
        return False
    return isinstance(envelope, dict) and envelope.get("error_code") == LANE_OFF_CODE


def _encode(model: BaseModel) -> bytes:
    return model.model_dump_json(exclude_none=True).encode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class HttpTransport:
    """Signed HTTPS to Platform. ``http://`` is allowed only for loopback."""

    def __init__(
        self,
        *,
        api_url: str,
        key: DeviceKey,
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        base = api_url.rstrip("/")
        parts = urllib.parse.urlsplit(base)
        # Compare the parsed hostname exactly: a prefix test would accept
        # http://127.0.0.1.attacker.example and send it signed plaintext.
        loopback = parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
        if not ((parts.scheme == "https" and parts.hostname) or loopback):
            raise ValueError(
                "Platform must be reached over https (http only on loopback)"
            )
        self.base = base
        self.key = key
        self.timeout = timeout_seconds
        self.clock = clock
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _send(self, method: str, path: str, body: bytes, limit: int) -> bytes:
        signed = self.key.sign_request(
            method=method, path=path, body=body, timestamp=int(self.clock())
        )
        headers = {
            **signed.as_dict(),
            "Accept": "application/json",
            "User-Agent": "ditto-edge-node/1",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            data=body if method == "POST" else None,
            method=method,
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                data: bytes = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise TransportError(
                exc.code, detail, lane_off=_lane_off(exc.code, detail)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(0, str(exc)) from exc
        if len(data) > limit:
            raise TransportError(0, "response exceeded its size limit")
        return data

    def _json(self, method: str, path: str, body: bytes, model: type[ModelT]) -> ModelT:
        return model.model_validate_json(
            self._send(method, path, body, _MAX_JSON_BYTES)
        )

    def enroll(self, request: EnrollRequest) -> EnrollResponse:
        return self._json("POST", paths.ENROLL, _encode(request), EnrollResponse)

    def poll(self, request: PollRequest) -> PollResponse:
        return self._json("POST", paths.POLL, _encode(request), PollResponse)

    def fill(self, digest: str) -> bytes:
        return self._send(
            "GET", paths.object_path(digest), b"", MAX_OBJECT_BYTES_CEILING
        )

    def ack(self, request: AckRequest) -> AckResponse:
        return self._json("POST", paths.ACK, _encode(request), AckResponse)

    def answer(self, challenge_id: str, answer: ChallengeAnswer) -> AnswerResponse:
        return self._json(
            "POST", paths.answer_path(challenge_id), _encode(answer), AnswerResponse
        )

    def status(self) -> NodeStatusResponse:
        return self._json("GET", paths.STATUS, b"", NodeStatusResponse)

    def leave(self, request: LeaveRequest) -> None:
        self._send("POST", paths.LEAVE, _encode(request), 4096)


_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "gone": 410,
    "bad_request": 422,
    "lane_off": 503,
    "capacity": 503,
}


class LocalTransport:
    """In-process transport to a :class:`Coordinator`, signed like HTTP."""

    def __init__(self, *, coordinator: Coordinator, key: DeviceKey) -> None:
        self.coordinator = coordinator
        self.key = key

    def _auth(self, method: str, path: str, body: bytes) -> str:
        signed = self.key.sign_request(
            method=method,
            path=path,
            body=body,
            timestamp=int(self.coordinator.clock()),
        )
        try:
            return self.coordinator.authenticate(
                method=method, path=path, headers=signed.as_dict(), body=body
            )
        except CoordinatorError as exc:
            raise TransportError(
                _STATUS.get(exc.code, 400), str(exc), lane_off=exc.code == "lane_off"
            ) from exc

    def _call(self, fn: Callable[[], ModelT], model: type[ModelT]) -> ModelT:
        try:
            result = fn()
        except CoordinatorError as exc:
            raise TransportError(
                _STATUS.get(exc.code, 400), str(exc), lane_off=exc.code == "lane_off"
            ) from exc
        # Round-trip through JSON so the node sees exactly what HTTP delivers.
        return model.model_validate_json(result.model_dump_json())

    def enroll(self, request: EnrollRequest) -> EnrollResponse:
        body = _encode(request)
        self._auth("POST", paths.ENROLL, body)
        parsed = EnrollRequest.model_validate_json(body)
        return self._call(lambda: self.coordinator.enroll(parsed), EnrollResponse)

    def poll(self, request: PollRequest) -> PollResponse:
        body = _encode(request)
        node_id = self._auth("POST", paths.POLL, body)
        parsed = PollRequest.model_validate_json(body)
        return self._call(lambda: self.coordinator.poll(node_id, parsed), PollResponse)

    def fill(self, digest: str) -> bytes:
        node_id = self._auth("GET", paths.object_path(digest), b"")
        try:
            return self.coordinator.fill(node_id, digest)
        except CoordinatorError as exc:
            raise TransportError(
                _STATUS.get(exc.code, 400), str(exc), lane_off=exc.code == "lane_off"
            ) from exc

    def ack(self, request: AckRequest) -> AckResponse:
        body = _encode(request)
        node_id = self._auth("POST", paths.ACK, body)
        parsed = AckRequest.model_validate_json(body)
        return self._call(lambda: self.coordinator.ack(node_id, parsed), AckResponse)

    def answer(self, challenge_id: str, answer: ChallengeAnswer) -> AnswerResponse:
        body = _encode(answer)
        node_id = self._auth("POST", paths.answer_path(challenge_id), body)
        parsed = ChallengeAnswer.model_validate_json(body)
        return self._call(
            lambda: self.coordinator.answer(node_id, challenge_id, parsed),
            AnswerResponse,
        )

    def status(self) -> NodeStatusResponse:
        node_id = self._auth("GET", paths.STATUS, b"")
        return self._call(lambda: self.coordinator.status(node_id), NodeStatusResponse)

    def leave(self, request: LeaveRequest) -> None:
        body = _encode(request)
        node_id = self._auth("POST", paths.LEAVE, body)
        try:
            self.coordinator.leave(node_id, LeaveRequest.model_validate_json(body))
        except CoordinatorError as exc:
            raise TransportError(
                _STATUS.get(exc.code, 400), str(exc), lane_off=exc.code == "lane_off"
            ) from exc
