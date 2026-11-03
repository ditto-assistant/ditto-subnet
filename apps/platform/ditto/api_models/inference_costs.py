"""Recorded lease costs; deliberately distinct from provider-period billing."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class InferenceGrantCostGroup(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    purpose: str
    classification_basis: str
    bench_version: int
    grants: int
    chat_calls: int
    prompt_tokens: int
    completion_tokens: int
    chat_cost_microusd: int
    embedding_calls: int
    embedding_tokens: int
    embedding_cost_microusd: int


class InferenceGrantCostSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    observed_at: datetime
    window_start: datetime
    window_end: datetime
    aggregation_basis: Literal["grant_created_at_full_lifetime"] = (
        "grant_created_at_full_lifetime"
    )
    provider_bill_reconciled: Literal[False] = False
    groups: list[InferenceGrantCostGroup]
    total_cost_microusd: int
    unclassified_cost_microusd: int
    grants: int
    unclassified_grants: int
