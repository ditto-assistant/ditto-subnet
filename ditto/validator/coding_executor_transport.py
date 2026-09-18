"""Shared validator-side authority for the private coding executor."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from ditto.api_models.coding_executor_control import CodingExecutorOperation
from ditto.validator.signing import sign_coding_executor_control

# The validator Compose service reaches the scorer through sandbox-docker's
# network namespace on the stack's private bridge. That fixed service origin is
# the only plaintext, non-loopback scorer control origin; miner containers run
# inside the nested daemon and cannot reach port 8000.
COMPOSE_SCORER_ORIGIN = "http://sandbox-docker:8000"

_PRIVATE_EXECUTOR_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


@dataclass(frozen=True, repr=False)
class CodingExecutorRequestAuthority:
    """Durable claim identity attached to one executor request."""

    agent_id: UUID
    agent_artifact_sha256: str
    coding_run_id: str
    ticket_id: UUID
    deadline: datetime

    def __post_init__(self) -> None:
        if (
            self.agent_id.int == 0
            or len(self.agent_artifact_sha256) != 64
            or self.agent_artifact_sha256 != self.agent_artifact_sha256.lower()
            or any(
                character not in "0123456789abcdef"
                for character in self.agent_artifact_sha256
            )
            or not self.coding_run_id
            or len(self.coding_run_id) > 256
            or any(
                character.isspace() or ord(character) < 32
                for character in self.coding_run_id
            )
            or self.ticket_id.int == 0
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise ValueError("coding executor request authority is invalid")


def sign_coding_executor_request(
    *,
    keypair: Any,
    validator_hotkey: str,
    authority: CodingExecutorRequestAuthority,
    operation: CodingExecutorOperation,
    body: bytes,
    now: datetime,
) -> str:
    """Return the base64url control envelope for exact request bytes."""

    if not body or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("coding executor signing authority is invalid")
    issued_at = now.astimezone(UTC).replace(microsecond=0)
    lifetime_seconds = min(
        60,
        int((authority.deadline.astimezone(UTC) - issued_at).total_seconds()),
    )
    if lifetime_seconds <= 0:
        raise ValueError("coding executor signing authority expired")
    envelope = sign_coding_executor_control(
        keypair,
        validator_hotkey=validator_hotkey,
        agent_id=authority.agent_id,
        agent_artifact_sha256=authority.agent_artifact_sha256,
        coding_run_id=authority.coding_run_id,
        ticket_id=authority.ticket_id,
        operation=operation,
        request_body_sha256=hashlib.sha256(body).hexdigest(),
        nonce=uuid4(),
        issued_at=issued_at,
        lifetime=timedelta(seconds=lifetime_seconds),
    )
    encoded = envelope.model_dump_json(by_alias=True).encode()
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def tls_or_loopback(scheme: str, hostname: str | None) -> bool:
    if hostname is None or hostname == "":
        return False
    if scheme == "https":
        return True
    if scheme != "http":
        return False
    host = hostname.casefold()
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def scorer_control_origin(url: str) -> bool:
    """Whether ``url`` is an accepted local scorer control-plane origin.

    One rule for every validator runtime that sends the scorer control bearer:
    any HTTPS origin, a loopback HTTP origin, or exactly the Compose service
    origin. Paths, userinfo, queries, fragments, and malformed ports are
    refused.
    """

    parsed = urlsplit(url)
    try:
        _ = parsed.port
    except ValueError:
        return False
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False
    if f"{parsed.scheme}://{parsed.netloc}" == COMPOSE_SCORER_ORIGIN:
        return True
    return tls_or_loopback(parsed.scheme, parsed.hostname)


def private_executor_endpoint(parsed: Any) -> bool:
    if parsed.scheme != "https" or parsed.port != 9443 or not parsed.hostname:
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return False
    return any(address in network for network in _PRIVATE_EXECUTOR_NETWORKS)


__all__ = [
    "COMPOSE_SCORER_ORIGIN",
    "CodingExecutorRequestAuthority",
    "private_executor_endpoint",
    "scorer_control_origin",
    "sign_coding_executor_request",
    "tls_or_loopback",
]
