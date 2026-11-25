"""Wire shapes for chain gamma (Gamma Integration Standard V1.2, SN-501).

Not served by any route yet. ``ChainGammaServicesDocument`` is the document a
service's ``set_service`` url points to (requirement 1.2). Readers skip fields
they do not know, but the document we publish has no unknown fields.
``ChainGammaBalanceResponse`` is the balance endpoint body (requirement 3.1).

Chain gamma is the Bittensor runtime credit, not the SN118 treasury Gamma
bucket (``emission_allocation_math.KIND_GAMMA``).
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

import bittensor
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_SERVICE_URL_BYTES = 256
"""``set_service`` refuses a url longer than this many bytes."""

USD_DECIMAL_PATTERN = r"^\d+\.\d{2,9}$"
"""Balance amounts: decimal strings, two to nine places (requirement 3.2)."""


def check_https_url(url: str) -> str:
    """An absolute ``https`` url with a host, or ``ValueError``."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an absolute https url")
    if parts.username or parts.password:
        raise ValueError("must not carry credentials")
    return url


def check_service_url(url: str) -> str:
    """The url ``set_service`` would publish: https, at most 256 bytes."""
    check_https_url(url)
    if len(url.encode()) > MAX_SERVICE_URL_BYTES:
        raise ValueError(f"set_service url exceeds {MAX_SERVICE_URL_BYTES} bytes")
    return url


def check_ss58(address: str) -> str:
    """A decodable ss58 account address, or ``ValueError``."""
    try:
        bittensor.Keypair(ss58_address=address)
    except (TypeError, ValueError):
        raise ValueError("not an ss58 address") from None
    return address


HttpsUrl = Annotated[str, AfterValidator(check_https_url)]
Ss58 = Annotated[str, AfterValidator(check_ss58)]
UsdDecimal = Annotated[str, Field(pattern=USD_DECIMAL_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChainGammaPublisher(_Strict):
    netuid: int = Field(ge=0, le=65535)
    hotkey: Ss58


class ChainGammaHttpService(_Strict):
    type: Literal["http"] = "http"
    endpoint: HttpsUrl


class ChainGammaPayment(_Strict):
    type: Literal["gamma"] = "gamma"
    signature: Literal["btauth/1"] = "btauth/1"
    balance: HttpsUrl


class ChainGammaServicesDocument(_Strict):
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    publishers: tuple[ChainGammaPublisher, ...] = Field(min_length=1)
    services: tuple[ChainGammaHttpService, ...] = Field(min_length=1)
    payments: tuple[ChainGammaPayment, ...] = Field(min_length=1, max_length=1)
    pricing: HttpsUrl | None = None
    """Where USD prices are published (requirement 4.4)."""

    @model_validator(mode="after")
    def _publishers_unique(self) -> ChainGammaServicesDocument:
        keys = [(p.netuid, p.hotkey) for p in self.publishers]
        if len(set(keys)) != len(keys):
            raise ValueError("publishers must not repeat a (netuid, hotkey)")
        return self

    def publisher_hotkeys(self, netuid: int) -> frozenset[str]:
        """Every hotkey published under on ``netuid``, including old ones."""
        return frozenset(p.hotkey for p in self.publishers if p.netuid == netuid)


class ChainGammaBalanceResponse(_Strict):
    hotkey: str
    credited_usd: UsdDecimal
    used_usd: UsdDecimal
    remaining_usd: UsdDecimal


class ChainGammaInsufficientBalance(_Strict):
    """The exact 402 body of requirement 5.1; it bypasses the error envelope."""

    error: Literal["insufficient_balance"] = "insufficient_balance"
