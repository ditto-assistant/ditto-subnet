"""Pure bounded-intent checks shared by future signing and dry-run workflows.

Passing this preflight does not authorize a payment. It proves only that one
proposed action fits the recorded numeric and identity bounds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

Route = Literal["tao", "gm_alpha"]


@dataclass(frozen=True)
class TopUpIntent:
    route: Route
    policy_revision: int
    quote_block_hash: str
    quoted_at: datetime
    source_alpha_rao: int
    tao_value_rao: int
    price_impact_bps: int
    linked_wallet: str
    payment_instructions_sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class TopUpBounds:
    max_source_alpha_rao: int
    max_single_topup_rao: int
    max_daily_outflow_rao: int
    max_slippage_bps: int
    daily_spent_rao: int
    expected_linked_wallet: str
    expected_payment_instructions_sha256: str
    reconciled: bool


def preflight(intent: TopUpIntent, bounds: TopUpBounds, *, now: datetime) -> None:
    if intent.route not in ("tao", "gm_alpha"):
        raise ValueError("unsupported GM deposit asset")
    if intent.policy_revision <= 0 or not intent.idempotency_key:
        raise ValueError("revision and idempotency key are required")
    if not re.fullmatch(r"0x[0-9a-f]{64}", intent.quote_block_hash):
        raise ValueError("finalized quote block hash is required")
    if intent.quoted_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("quote times must carry a timezone")
    quote_age = now.astimezone(UTC) - intent.quoted_at.astimezone(UTC)
    if not timedelta(0) <= quote_age <= timedelta(seconds=120):
        raise ValueError("quote is stale or from the future")
    if not bounds.reconciled:
        raise ValueError("treasury or GM balance has not reconciled")
    if not 0 < intent.source_alpha_rao <= bounds.max_source_alpha_rao:
        raise ValueError("source amount exceeds limit")
    if not 0 < intent.tao_value_rao <= bounds.max_single_topup_rao:
        raise ValueError("single top-up exceeds limit")
    if bounds.daily_spent_rao + intent.tao_value_rao > bounds.max_daily_outflow_rao:
        raise ValueError("daily outflow exceeds limit")
    if not 0 <= intent.price_impact_bps <= bounds.max_slippage_bps <= 500:
        raise ValueError("price impact exceeds limit")
    if (
        not intent.linked_wallet
        or intent.linked_wallet != bounds.expected_linked_wallet
    ):
        raise ValueError("sender is not the reviewed GM-linked wallet")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", intent.payment_instructions_sha256)
        or intent.payment_instructions_sha256
        != bounds.expected_payment_instructions_sha256
    ):
        raise ValueError("GM payment instructions are not current")
