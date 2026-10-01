"""Receipt selectors, never caller assertions of finality or provider credits."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.treasury import Block, Digest, Hash


class TreasuryReceiptSelector(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    stage: Literal["service_distribution", "vendor_payment", "provider_credit"]
    epoch_index: Block
    bucket_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")]
    source_block: Annotated[int, Field(gt=0)] | None = None
    block: Annotated[int, Field(gt=0)]
    block_hash: Hash
    extrinsic_index: Annotated[int, Field(ge=0)]
    extrinsic_hash: Hash
    amount_atomic: Annotated[int, Field(gt=0, le=2**63 - 1)]
    payee_rule_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")] | None = (
        None
    )
    parent_receipt_id: Digest | None = None
    reason: Annotated[str, Field(min_length=8, max_length=240)]

    @model_validator(mode="after")
    def stage_links(self) -> "TreasuryReceiptSelector":
        if len(self.reason.strip()) < 8:
            raise ValueError("receipt audit reason is required")
        if self.stage == "service_distribution":
            if self.payee_rule_id is not None or self.parent_receipt_id is not None:
                raise ValueError("distribution cannot claim a payee or prior payment")
            if self.source_block is None or self.block < self.source_block:
                raise ValueError("distribution precedes its earning")
        elif self.payee_rule_id is None:
            raise ValueError("payment requires an exact historical rule")
        if (
            bool(self.parent_receipt_id) != bool(self.source_block)
            and self.stage != "service_distribution"
        ):
            raise ValueError("optional lineage requires both parent and source block")
        return self


class TreasuryReceiptResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    receipt_id: Digest
    epoch_index: int
    bucket_id: str
    policy_digest: Digest
    source_block: int | None
    block: int
    block_hash: Hash
    extrinsic_index: int
    extrinsic_hash: Hash
    amount_atomic: str
    stage: Literal["service_distribution", "vendor_payment"]
    status: Literal["chain_finalized"] = "chain_finalized"
    public_event_id: int | None
    published: bool
    replayed: bool
    provider_credit_status: Literal["not_proven"] = "not_proven"


class TreasuryReceiptPage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    items: list[TreasuryReceiptResult]
