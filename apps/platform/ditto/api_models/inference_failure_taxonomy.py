"""Private operator models for the hosted-inference failure taxonomy.

Counts and identifiers only. Nothing here can carry a prompt, a response, a
key, a header, or a trace body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

InferenceRequestKind = Literal["chat", "embedding"]

InferenceGateway = Literal["openrouter", "reliable", "direct", "ditto-router"]
"""Which door the call went through, derived from lane + ``fallback_phase``.

Chat phase 0 is the OpenRouter aggregate route and phase 1 is the reserved
``reliable`` route; on the embedding lane phase 0 is the direct provider call
and phase 1 is the OpenRouter fallback. ``ditto-router`` is the dogfood lane.
"""

InferenceRouteBasis = Literal[
    "confirmed_selected",
    "last_attempted",
    "configured",
    "router_internal",
    "unknown",
    "unrecognized",
]
"""How much the ledger actually knows about ``upstream_route``.

``confirmed_selected`` is the only value that means "this upstream served the
call": it is the single selected endpoint parsed from router metadata on a
completed chat row. ``last_attempted`` is the last upstream a failed chat row
was sent to, which is evidence and not a confirmed route. ``configured`` is the
relay's own pinned embedding provider, stamped before the call.
``router_internal`` means the Ditto Router chose an upstream and did not say
which. ``unknown`` means the column was NULL -- the usual case for a failure
whose provider returned no metadata -- and ``unrecognized`` means a value was
present but was not a plain bounded identifier. The last three always carry
``upstream_route: null``.
"""


class InferenceFailureGroup(BaseModel):
    """One (window, lane, model, gateway, route, error code) bucket."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    window_seconds: int
    request_kind: InferenceRequestKind
    model: str
    gateway: InferenceGateway
    upstream_route: str | None
    route_basis: InferenceRouteBasis
    terminal_error_code: str | None
    upstream_http_status: int | None
    calls: int
    completed: int
    failed: int
    canceled: int
    timed_out: int
    openrouter_attempts_max: int
    share_of_settled_calls: float
    """This group's calls as a fraction of the lane's settled calls."""


class InferenceFailureLaneWindow(BaseModel):
    """Lane totals for one window, counted independently of the group cap."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    window_seconds: int
    request_kind: InferenceRequestKind
    calls: int
    settled: int
    completed: int
    failed: int
    canceled: int
    in_flight: int
    timed_out: int
    rate_limited_failures: int
    """Settled rows whose terminal error code is exactly ``upstream_http_429``."""
    failure_share: float
    groups_total: int
    groups_returned: int
    groups_truncated: bool


class InferenceRateLimitedTicket(BaseModel):
    """One validator ticket whose calls hit ``upstream_http_429`` in the window."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    bench_version: int
    validator_hotkey: str
    slot_id: str
    ticket_deadline: datetime
    rate_limited_failures: int


class InferenceRateLimitBurst(BaseModel):
    """Report-only five-minute upstream rate-limit signal for one lane.

    ``active`` means the lane's ``upstream_http_429`` count reached the
    provisional ``threshold`` while the local global in-flight peak stayed below
    the configured limit -- the upstream pool, not Ditto's own admission, was
    the bottleneck. Nothing is enforced, rerouted, or retried on it.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    request_kind: InferenceRequestKind
    window_seconds: int
    rate_limited_failures: int
    threshold: int
    peak_global_concurrency: int
    global_concurrency_limit: int
    active: bool
    tickets_total: int
    tickets_truncated: bool
    tickets: list[InferenceRateLimitedTicket]


class InferenceFailureTaxonomy(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    observed_at: datetime
    window_seconds: list[int]
    group_limit: int
    lanes: list[InferenceFailureLaneWindow]
    groups: list[InferenceFailureGroup]
    rate_limit_bursts: list[InferenceRateLimitBurst]
