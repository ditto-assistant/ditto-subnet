"""Loopback-only JSON-over-HTTP transport for the pilot client.

Stdlib ``urllib`` only. Every call has its own timeout, proxies are disabled
(an ``HTTP_PROXY`` environment variable must never route a loopback miner
call off-host), and redirects are refused (a miner must not bounce Ditto to a
non-loopback host). Transport failures are mapped to contract outcomes:

* connection refused / reset / any other ``URLError`` -> ``Outcome.UNAVAILABLE``
* socket timeout -> ``Outcome.TIMEOUT``
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

from ..contract import TOKEN_HEADER, Outcome

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
# Largest response body the client will read. A valid adapt response is ~4 KiB.
MAX_RESPONSE_BYTES: Final = 1024 * 1024


class TransportFailure(Exception):
    """The miner could not be reached or did not answer in time."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(f"{outcome}: {detail}")
        self.outcome = outcome
        self.detail = detail


@dataclass(frozen=True)
class HttpResult:
    """An HTTP answer from the miner. ``body`` is ``None`` when the response
    was not a JSON value or exceeded ``MAX_RESPONSE_BYTES``."""

    status: int
    body: Any


def validate_miner_url(url: str) -> str:
    """Return the normalized base URL, or raise ``ValueError`` unless it is a
    plain ``http://`` URL on a loopback host (127.0.0.1, ::1, localhost)."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"miner URL {url!r} is malformed") from exc
    if parts.scheme != "http":
        raise ValueError(f"miner URL {url!r} must use http://")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"miner URL {url!r} must not carry credentials")
    if parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"miner URL {url!r} is not loopback (127.0.0.1, ::1, localhost)")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError(f"miner URL {url!r} must be a bare origin")
    host = f"[{parts.hostname}]" if parts.hostname == "::1" else parts.hostname
    return f"http://{host}" + (f":{port}" if port is not None else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _is_timeout(reason: object) -> bool:
    # socket.timeout is TimeoutError since Python 3.10.
    return isinstance(reason, TimeoutError)


def _decode(raw: bytes) -> Any:
    if len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers bad JSON and bad UTF-8; RecursionError is a deeply
        # nested body under the size cap. Either way the body is unparseable,
        # so the client rejects it and falls back instead of aborting the run.
        return None


class Transport:
    """JSON calls against one validated loopback miner."""

    def __init__(self, miner_url: str, token: str) -> None:
        self.base_url = validate_miner_url(miner_url)
        self._token = token

    def call(self, method: str, route: str, body: dict[str, Any], timeout_s: float) -> HttpResult:
        data = json.dumps(body, separators=(",", ":")).encode()
        req = urllib.request.Request(
            self.base_url + route,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", TOKEN_HEADER: self._token},
        )
        try:
            with _OPENER.open(req, timeout=timeout_s) as resp:
                status = int(resp.status)
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # A non-2xx answer (including a refused 3xx) still carries a body.
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                exc.close()
            return HttpResult(int(exc.code), _decode(raw))
        except urllib.error.URLError as exc:
            if _is_timeout(exc.reason):
                raise TransportFailure(Outcome.TIMEOUT, str(exc.reason)) from exc
            raise TransportFailure(Outcome.UNAVAILABLE, str(exc.reason)) from exc
        except TimeoutError as exc:
            raise TransportFailure(Outcome.TIMEOUT, str(exc)) from exc
        except (OSError, http.client.HTTPException) as exc:
            # ConnectionRefusedError, ConnectionResetError, RemoteDisconnected,
            # IncompleteRead, BadStatusLine: the miner is not serving.
            raise TransportFailure(Outcome.UNAVAILABLE, f"{type(exc).__name__}: {exc}") from exc
        return HttpResult(status, _decode(raw))
