"""Private operator reconciliation, distinct from independently verified receipts."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto_screening_protocol.treasury import Address, Hash

Atomic = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
Asset = Literal["TAO", "SN118_ALPHA", "SN28_ALPHA"]
GM_RECIPIENT = "5FqbWhtCvoSD3X3iNxyWXogrKGmeVLnjZXoNWPx16yErYQd9"


class FundingWire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class FundingMovement(FundingWire):
    reference: Annotated[str, Field(pattern=r"^[1-9][0-9]{5,11}-[0-9]{1,6}$")]
    kind: Literal["sale", "swap", "payment"]
    occurred_at: datetime
    block_hash: Hash | None = None
    input_asset: Asset
    input_amount_atomic: Atomic
    output_asset: Asset | None = None
    output_amount_atomic: Atomic | None = None
    recipient: Address | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("explicit observation timezone required")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def movement_shape(self):
        amounts = [self.input_amount_atomic, self.output_amount_atomic]
        if any(v is not None and int(v) >= 2**64 for v in amounts):
            raise ValueError("chain atomic amount exceeds u64")
        if self.kind == "payment":
            if (
                self.input_asset != "TAO"
                or self.recipient != GM_RECIPIENT
                or self.output_asset is not None
                or self.output_amount_atomic is not None
            ):
                raise ValueError(
                    "historical GM payment must name the exact TAO recipient"
                )
        elif (
            self.input_asset == "TAO"
            or self.recipient is not None
            or self.output_amount_atomic is None
            or self.output_asset != ("TAO" if self.kind == "sale" else "SN28_ALPHA")
        ):
            raise ValueError(
                "conversion must preserve explicit input and output assets"
            )
        if self.kind == "swap" and self.input_asset != "SN118_ALPHA":
            raise ValueError(
                "only observed SN118 to SN28 historical swaps are supported"
            )
        return self


class FundingPurchase(FundingWire):
    purchase_id: str
    occurred_at: datetime
    credited_usd_cents: Annotated[int, Field(gt=0, le=100_000_000, strict=True)]
    method: Literal["x402-relayed"] = "x402-relayed"
    status: Literal["completed"] = "completed"

    @field_validator("purchase_id")
    @classmethod
    def purchase_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("canonical provider purchase UUID required")
        return value

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("explicit observation timezone required")
        return value.astimezone(UTC)


class FundingRecord(FundingWire):
    purchase: FundingPurchase
    payment: FundingMovement
    preceding_sale: FundingMovement | None = None
    earlier_conversions: Annotated[list[FundingMovement], Field(max_length=8)] = []
    link_basis: Literal["operator_time_correlation"] = "operator_time_correlation"

    @model_validator(mode="after")
    def correlated_record(self):
        if (
            self.payment.kind != "payment"
            or not 0
            <= (self.purchase.occurred_at - self.payment.occurred_at).total_seconds()
            <= 300
        ):
            raise ValueError(
                "purchase requires an exact payment within the observation window"
            )
        if self.preceding_sale and (
            self.preceding_sale.kind != "sale"
            or self.preceding_sale.occurred_at > self.payment.occurred_at
        ):
            raise ValueError("preceding sale must precede the payment")
        if any(
            e.kind != "swap" or e.occurred_at > self.payment.occurred_at
            for e in self.earlier_conversions
        ):
            raise ValueError("earlier conversions must precede the payment")
        return self


class FundingBatch(FundingWire):
    version: Literal[1] = 1
    holding_wallet: Address
    records: Annotated[list[FundingRecord], Field(min_length=1, max_length=100)]
    reason: Annotated[str, Field(min_length=8, max_length=240)]

    @property
    def digest(self):
        return checksum(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def unique_records(self):
        ids = [r.purchase.purchase_id for r in self.records]
        payments = [r.payment.reference for r in self.records]
        if len(ids) != len(set(ids)) or len(payments) != len(set(payments)):
            raise ValueError("duplicate purchase or payment")
        return self


class FundingSubmit(FundingWire):
    batch: FundingBatch
    confirmation_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    confirmation: str


def checksum(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
