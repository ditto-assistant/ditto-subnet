"""Ticketless readiness probe for the dedicated coding executor transport."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ditto.validator.coding_executor_transport import private_executor_endpoint
from ditto.validator.errors import ValidatorInfrastructureError

_READINESS_PATH = "/v1/coding/ready"
_MAX_RESPONSE_BYTES = 4 << 10


class _Readiness(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_name: Literal["dittobench-coding-executor-readiness-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    transport: Literal["mtls"]
    supervisor_ready: Literal[True]
    publication_ready: Literal[True]
    ticket_authority_used: Literal[False]


class CodingExecutorConnectivityCanary:
    """Prove mTLS and scorer control readiness without ticket authority."""

    def __init__(self, *, base_url: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(base_url)
        if (
            not private_executor_endpoint(parsed)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or getattr(client, "trust_env", True)
        ):
            raise ValueError("coding executor connectivity canary is invalid")
        self._base = base_url.rstrip("/")
        self._client = client

    async def probe(self) -> None:
        received = bytearray()
        try:
            async with self._client.stream(
                "GET",
                f"{self._base}{_READINESS_PATH}",
                headers={"Accept": "application/json", "Cache-Control": "no-store"},
                follow_redirects=False,
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                    or "no-store"
                    not in {
                        directive.strip().lower()
                        for directive in response.headers.get(
                            "Cache-Control", ""
                        ).split(",")
                    }
                    or response.headers.get("Content-Encoding")
                ):
                    raise ValidatorInfrastructureError(
                        "coding executor connectivity canary rejected"
                    )
                async for chunk in response.aiter_bytes(chunk_size=4 << 10):
                    if len(received) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ValidatorInfrastructureError(
                            "coding executor connectivity response is invalid"
                        )
                    received.extend(chunk)
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                "coding executor connectivity request failed"
            ) from error
        try:
            _Readiness.model_validate_json(received)
        except ValidationError:
            raise ValidatorInfrastructureError(
                "coding executor connectivity response is invalid"
            ) from None

    def __repr__(self) -> str:
        return "CodingExecutorConnectivityCanary(private=True)"


__all__ = ["CodingExecutorConnectivityCanary"]
