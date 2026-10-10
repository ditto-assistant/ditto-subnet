"""Prometheus counters shared by the API server and its query layer.

``GET /metrics`` (:mod:`ditto.api_server.endpoints.metrics`) exposes the default
registry, so a call site only has to import a counter and increment it.

Counters live here, above both packages, when the same event can be detected in
either an endpoint or in :mod:`ditto.db.queries` and must land on one series —
``ditto.db`` must not import ``ditto.api_server``.
"""

from __future__ import annotations

from typing import Literal

from prometheus_client import Counter, Gauge, Histogram

# Fires whenever a *signed, authenticated* heartbeat kept its liveness columns
# (``seen_at`` / ``reported_at``) but had its work payload dropped because the
# payload could not be validated. A non-zero rate means the fleet is live but
# some validator's reported capacity/progress is not being believed — the
# opposite of a stale heartbeat, and it must be alerted on separately. ``stage``
# names the guard that degraded (see the call sites); ``reason`` is the
# exception class name, deliberately low-cardinality.
VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED = Counter(
    "ditto_validator_heartbeat_payload_degraded_total",
    "Signed heartbeats stored liveness-only after the work payload failed validation.",
    ("stage", "reason"),
)

DispatchDeclineReason = Literal[
    "allocator_busy",
    "not_accepting",
    "slot_not_healthy",
    "slot_ceiling",
    "disk_breaker",
    "slot_cap",
    "validator_paused",
    "v13_scorer_cohort_pin",
    "inference_slot_cap",
    "provider_outage",
    "slot_occupied",
    "no_candidate",
]
"""Why a fully authenticated ``POST /validator/job`` poll left with no ticket.

Deliberately closed and low-cardinality -- one value per *gate*, never per
validator or slot (those go to the log line instead). The split that matters
operationally is the first six (dispatch refused to issue: an admission,
capacity, or policy decision the operator controls) against ``no_candidate``
(dispatch was willing but the candidate walk found no eligible row). An idle
fleet is one or the other, and telling them apart used to require
reconstructing the queue predicates as raw SQL against production.
"""

# Fires on every 204 from the validator job-dispatch path, labelled with the
# gate that turned the poll away. Observability only: nothing here participates
# in the dispatch decision, and a decline is normal traffic (k=3 means most
# polls get nothing), so alert on the *mix* shifting, never on the raw rate.
VALIDATOR_DISPATCH_DECLINED = Counter(
    "ditto_validator_dispatch_declined_total",
    "Validator job polls that were answered 204, by the gate that declined them.",
    ("reason",),
)

VALIDATOR_NONCE_JANITOR_RUNS = Counter(
    "ditto_validator_nonce_janitor_runs_total",
    "Bounded expired validator nonce sweeps, by outcome.",
    ("outcome",),
)
VALIDATOR_NONCE_JANITOR_DELETED = Counter(
    "ditto_validator_nonce_janitor_deleted_total",
    "Expired validator replay guards deleted by the periodic janitor.",
)
SCREENER_CAPACITY_EVENT_JANITOR_RUNS = Counter(
    "ditto_screener_capacity_event_janitor_runs_total",
    "Bounded screener capacity event retention sweeps, by outcome.",
    ("outcome",),
)
SCREENER_CAPACITY_EVENT_JANITOR_DELETED = Counter(
    "ditto_screener_capacity_event_janitor_deleted_total",
    "Expired screener capacity audit events deleted by the periodic janitor.",
)
SCREENER_CAPACITY_EVENT_JANITOR_DURATION_SECONDS = Histogram(
    "ditto_screener_capacity_event_janitor_duration_seconds",
    "Duration of screener capacity event retention sweeps.",
)
LEDGER_PIN_MATERIALIZATIONS = Counter(
    "ditto_ledger_pin_materializations_total",
    "Epoch-pinned validator ledger builds, by outcome "
    "(pinned, loaded, raced, db_error, error).",
    ("outcome",),
)
LEDGER_PIN_LOOP_RUNS = Counter(
    "ditto_ledger_pin_loop_runs_total",
    "Background ledger pin sweeps, by outcome (pinned, unavailable, disabled).",
    ("outcome",),
)
VALIDATOR_NONCE_JANITOR_DURATION_SECONDS = Histogram(
    "ditto_validator_nonce_janitor_duration_seconds",
    "Duration of bounded validator nonce janitor sweeps.",
)


# Fires whenever hosted-inference admission answers ``AT_CAPACITY`` — the
# retryable decline the endpoint turns into ``503`` + ``Retry-After``.
#
# This counter exists because the platform had no way to answer the one question
# an operator actually asks before turning a concurrency knob: *is this limit
# ever the binding constraint?* Every admission limit shares a single anonymous
# exit, so the only way to find out was to reconstruct in-flight intervals from
# ``inference_requests`` after the fact. That reconstruction is what showed the
# embedding ceiling had never bound at all — peak fleet-wide in-flight of 14
# against a ceiling of 128 — which is a fact that should have been a scrape away.
#
# ``lane`` is ``chat`` or ``embedding``. ``scope`` names which of the five
# admission gates tripped, all of which are otherwise indistinguishable to the
# caller: ``token_reservation`` (in-flight reservations, not spend, overflow the
# allowance), ``per_ticket`` / ``per_validator`` / ``global`` (concurrency), and
# ``per_ticket_rpm`` / ``per_validator_rpm`` / ``global_rpm`` (rate). Both labels
# are closed sets, so cardinality is bounded at 2 x 7.
#
# A zero rate on ``embedding``/``per_ticket`` is the positive signal that the
# operator-tunable emergency brake is open. A non-zero rate on it during a
# deliberate brake application is the confirmation that the brake engaged.
INFERENCE_ADMISSION_AT_CAPACITY = Counter(
    "ditto_inference_admission_at_capacity_total",
    "Hosted-inference admissions declined as retryable backpressure, by lane and gate.",
    ("lane", "scope"),
)

# Fires on every 429 from the opt-in per-client-IP limit on unauthenticated
# routes (``DITTO_PUBLIC_RATE_LIMIT_PER_MINUTE``). ``route`` is the limited
# router -- ``upload``, ``retrieval``, or ``public`` -- a closed set; the client
# address stays off the label so a flood cannot inflate cardinality. A steady
# non-zero rate on ``upload`` is worth a look: miners share that path.
PUBLIC_RATE_LIMITED = Counter(
    "ditto_public_rate_limited_total",
    "Unauthenticated requests refused by the per-client-IP rate limit, by router.",
    ("route",),
)

# Source-emission collector health, refreshed after every collector sweep in the
# process that runs the collector (``DITTO_ROLE=platform``). The collector is
# fail-closed by design -- an unaudited subtensor runtime fingerprint stops it
# until a human audits the new runtime -- but twice (#2231, #2703) that stop sat
# unnoticed for days while crowned kings stayed embargoed. ``stalled`` is 1 when
# the cursor has a recorded blocked reason AND has not advanced for longer than
# ``SOURCE_EMISSION_COLLECTOR_STALL_SECONDS``: page on it. ``lag_seconds`` is the
# age of the last durable cursor advance whether or not a reason was recorded,
# for a softer staleness alert. Processes that do not run the collector export
# the zero default, so aggregate with ``max``.
SOURCE_EMISSION_COLLECTOR_STALLED = Gauge(
    "ditto_source_emission_collector_stalled",
    "1 while the source-emission collector is blocked and its cursor is stale.",
)
SOURCE_EMISSION_COLLECTOR_CURSOR_LAG_SECONDS = Gauge(
    "ditto_source_emission_collector_cursor_lag_seconds",
    "Seconds since the source-emission collector cursor last advanced.",
)
