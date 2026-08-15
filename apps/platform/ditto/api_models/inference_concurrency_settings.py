"""Operator-tunable admission policy for the hosted v7 inference lanes.

This board governs both hosted lanes: chat completions and embeddings through
the platform proxy.

* The chat lane's **concurrency** limits are live admission controls here.
  Request-per-minute limits remain boot-time safety rails so widening
  simultaneous work does not also widen provider bursts.
* The chat lane's **request and token budgets** are here because they are
  per-lease resource allowances, not rates. See ``chat_request_budget`` and
  ``chat_token_budget`` for why each moved.
* The **local Ollama** lane (bench_version 2-6, one container per validator
  host) is not reachable from here at all. dittobench-api #93 made v7 bypass it
  (``inference_broker.go``: ``if benchVersion < 7 { acquire b.embeddingSlots }``),
  and ``DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES`` still pins it at one. Nothing
  in this module can widen it.

Why the shipped defaults are much larger than the values they replace:

The old numbers (1 / 8 / 32) were sized when v7 embeddings still ran against
that local Ollama container -- a scarce, host-local, single-tenant resource. The
embedding route is now a hosted, network-bound provider call, so per-ticket
serialisation is protecting nothing. Embeddings are roughly 63% of a v7 run's
~1,067 inference requests (671 of them), and at ``per_ticket = 1`` every one of
them is strictly serial.

The defaults below are chosen so the **validator** is the binding limit, not the
platform: dittobench-api admits 8 concurrent embeddings per run, so a per-ticket
ceiling of 12 is pure headroom that is never reached in normal operation. That
ordering is intentional. A limit that binds at the platform costs a network
round trip to discover, while the same limit enforced in the broker is a local
semaphore -- and, until the fleet carries a capacity-aware build, a platform
decline is the more expensive way to find out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CHAT_PER_TICKET_CONCURRENCY = 16
DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY = 48
DEFAULT_CHAT_GLOBAL_CONCURRENCY = 96
MAX_CHAT_CONCURRENCY = 512

# The shipped values. Every one of these is a raise -- there is no configuration
# of this board that reproduces the old serialised behaviour by default, because
# a knob whose default is the old value is a knob nobody turns.
DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY = 12
DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY = 48
DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY = 96

# Ceilings, not recommendations. The active fleet now exposes up to eight
# benchmark slots per validator and can run several validators concurrently.
# A global ceiling of 128 can therefore bind before the operator-selected
# per-ticket and per-validator hierarchy does: eight tickets at 32 chat calls
# each already express 256 concurrent calls on one validator.
#
# Keep one shared 512 ceiling across both lanes. This is large enough for two
# fully occupied eight-slot validators at 32 calls per ticket, while remaining
# a finite guard that the Platform, Go relay, Backroom, and provider transport
# all enforce identically. The live policy remains the load-shedding control;
# raising this hard ceiling alone does not widen production admission.
MAX_EMBEDDING_PER_TICKET_CONCURRENCY = 512
MAX_EMBEDDING_PER_VALIDATOR_CONCURRENCY = 512
MAX_EMBEDDING_GLOBAL_CONCURRENCY = 512

# The chat request budget, sized against the observed distribution rather than
# against the round number it replaces (1024, which was never justified against
# a real run).
#
#   * a typical v7 agent spends ~1.25 chat requests per check, ~355 per run
#   * Jupiter and KOTH_v7_1 spend ~3.85 per check, ~1090 per run
#   * a run is 279-283 checks depending on the dataset
#
# 1024 sat *below* the heaviest observed strategy, so those agents exhausted
# around check 266 and had every remaining call refused -- a resource limit
# behaving as a run failure. 8192 is ~23x the median run, ~7.5x the heaviest
# run observed, and ~29 requests per check at 283 checks. It is a real raise
# (8x) rather than a nudge, which is the point: a ceiling that a legitimate
# strategy can reach by being thorough is a ceiling in the wrong place.
DEFAULT_CHAT_REQUEST_BUDGET = 8192

# The ceiling is 2x the default, not unbounded. The request budget is not the
# only thing bounding a grant's spend -- ``chat_token_budget`` is the other, and
# the two are now deliberately sized so that neither is vestigial (see below).
# Keeping a finite request ceiling matters regardless: it is the bound that
# survives a pathological loop of tiny requests, which the token budget would
# absorb slowly and the concurrency board would not catch at all.
MAX_CHAT_REQUEST_BUDGET = 16384

# The chat *token* budget, which is what actually ended the runs #473 set out to
# save. #473 raised the request budget to 8192 and the same agents kept failing,
# because the request count was never the binding allowance:
#
#   * 1009 consecutive chat declines on one lease, every one of them the
#     unnamed 4100 -- never 4102 ("spent its request budget"), never 4101
#     ("revoked") -- with 1h21m still on the lease
#   * 1009 chat declines and *zero* embedding declines on the same grant, and
#     the token budget is the only per-lane quantity in the system (chat 4M vs
#     ``embedding_token_budget`` 1B)
#
# Sizing, and why this number is larger than it first looks. The old 4,000,000
# was never four million *real* tokens. Admission reserves ``max_tokens +
# len(body)`` and byte length is roughly 4x the token count of the same JSON, so
# a 4M allowance permitted only ~1M tokens an agent could actually spend. The
# reservation is honest as of the companion change, so this number is now a
# true token count and the two effects multiply.
#
# Against observed usage:
#
#   * a v7 run's chat traffic measures ~2,300 tokens per call (the calibration
#     fleet's own accounting: 92% prompt, 8% completion)
#   * the heaviest observed strategies (Jupiter, KOTH_v7_1) spend ~1090 calls,
#     so ~2.7-3.8M real tokens for a complete run
#
# 25,000,000 is therefore ~7x the heaviest run observed -- deliberately the same
# safety factor #473 chose on the request axis (8192 vs ~1090). The two
# allowances cross over at ~3,050 tokens a call: below that the request budget
# binds first, above it the token budget does. That crossover sits right at the
# observed median, which is the point -- at the old pairing (4M / 8192) the
# crossover was 488 tokens a call, so the token budget bound *everything* and
# the request budget was decoration.
DEFAULT_CHAT_TOKEN_BUDGET = 25_000_000

# Production runs have exhausted 25M grants after more than an hour, and the
# fastest observed legitimate strategy projects to about 46.6M over a full
# 90-minute lease. Keep enough distance above that measured tail for a 75M
# operating cap while retaining the request budget, lease deadline, and this
# finite hard stop as independent runaway guards. This is also the bound
# ``check_config`` enforces at boot -- imported there rather than repeated, so a
# revision this board accepts can never be a value the next restart rejects.
MAX_CHAT_TOKEN_BUDGET = 100_000_000

DEFAULT_BENCHMARK_CASE_CONCURRENCY = 1
MAX_BENCHMARK_CASE_CONCURRENCY = 16
DEFAULT_RELAY_DELAY_FINGERPRINT_MIN_MS = 25
DEFAULT_RELAY_DELAY_FINGERPRINT_MAX_MS = 250
MAX_RELAY_DELAY_FINGERPRINT_MS = 5_000


class BenchmarkRuntimeSettings(BaseModel):
    """Per-ticket v10 execution controls delivered to capable validators.

    These defaults reproduce the deployed behavior exactly: scored cases are
    serial and relay delay fingerprinting is disabled.  The object is additive
    on both the settings JSON and validator job wire, so an older Platform,
    validator, scorer, or harness keeps that behavior during a rolling upgrade.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    case_concurrency: Annotated[int, Field(ge=1, le=MAX_BENCHMARK_CASE_CONCURRENCY)] = (
        DEFAULT_BENCHMARK_CASE_CONCURRENCY
    )
    relay_delay_fingerprint_mode: Literal["off", "shadow"] = "off"
    relay_delay_fingerprint_min_ms: Annotated[
        int, Field(ge=0, le=MAX_RELAY_DELAY_FINGERPRINT_MS)
    ] = DEFAULT_RELAY_DELAY_FINGERPRINT_MIN_MS
    relay_delay_fingerprint_max_ms: Annotated[
        int, Field(ge=0, le=MAX_RELAY_DELAY_FINGERPRINT_MS)
    ] = DEFAULT_RELAY_DELAY_FINGERPRINT_MAX_MS

    @model_validator(mode="after")
    def _delay_range_is_ordered(self) -> BenchmarkRuntimeSettings:
        if self.relay_delay_fingerprint_min_ms > self.relay_delay_fingerprint_max_ms:
            raise ValueError(
                "relay_delay_fingerprint_min_ms may not exceed "
                "relay_delay_fingerprint_max_ms"
            )
        return self


class InferenceConcurrencySettings(BaseModel):
    """The whole hosted-inference admission policy, stored as one object.

    Chat and embedding each have a strict hierarchy: one ticket may not exceed
    its validator's allowance, and no validator may exceed the fleet's.

    ``chat_request_budget`` and ``chat_token_budget`` stand apart from the
    concurrency controls. Neither is a rate and neither is enforced fleet-wide
    at admission -- each is *stamped onto a grant when the grant is minted* and
    thereafter read from the grant's own row. That is deliberate, and it is
    what makes both fields safe to sit on a live board: a revision changes what
    the **next** lease is issued, never what a running lease is already spending
    against. An operator cannot exhaust a run in flight by lowering either
    number, which is exactly the hazard that forced the embedding limits to grow a
    capacity-decline path.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    chat_request_budget: Annotated[int, Field(ge=1, le=MAX_CHAT_REQUEST_BUDGET)] = (
        DEFAULT_CHAT_REQUEST_BUDGET
    )
    """Chat completions one scoring ticket's grant may spend, in total.

    Raising this is the lever for "let a heavier strategy finish"; lowering it is
    the lever for "stop paying for a runaway". Neither takes effect on a lease
    that has already been minted -- see the class docstring.
    """

    chat_token_budget: Annotated[int, Field(ge=1, le=MAX_CHAT_TOKEN_BUDGET)] = (
        DEFAULT_CHAT_TOKEN_BUDGET
    )
    """Chat tokens (prompt + completion) one scoring ticket's grant may spend.

    The other half of "let a heavier strategy finish", and empirically the half
    that was actually binding: raising ``chat_request_budget`` alone left the
    heaviest agents failing in exactly the same place, because they were running
    out of tokens rather than out of calls.

    This is the number to move when a legitimate strategy stuffs large contexts.
    Note that it is a *cap*, not a spend: an agent is charged what it consumes,
    so raising the ceiling changes only which runs are permitted to finish.
    """

    chat_per_ticket_concurrency: Annotated[
        int, Field(ge=1, le=MAX_CHAT_CONCURRENCY)
    ] = DEFAULT_CHAT_PER_TICKET_CONCURRENCY
    """Concurrent hosted chat requests one scoring ticket may hold."""

    chat_per_validator_concurrency: Annotated[
        int, Field(ge=1, le=MAX_CHAT_CONCURRENCY)
    ] = DEFAULT_CHAT_PER_VALIDATOR_CONCURRENCY
    """Concurrent hosted chat requests summed over one validator's grants."""

    chat_global_concurrency: Annotated[int, Field(ge=1, le=MAX_CHAT_CONCURRENCY)] = (
        DEFAULT_CHAT_GLOBAL_CONCURRENCY
    )
    """Concurrent hosted chat requests across the fleet."""

    embedding_per_ticket_concurrency: Annotated[
        int, Field(ge=1, le=MAX_EMBEDDING_PER_TICKET_CONCURRENCY)
    ] = DEFAULT_EMBEDDING_PER_TICKET_CONCURRENCY
    """Concurrent hosted embedding requests one scoring ticket's grant may hold.

    This is the number the old ``1`` lived at, and the one an operator will
    actually turn. Lowering it is the emergency brake: it takes effect on the
    next admission fleet-wide, with no release and no restart.
    """

    embedding_per_validator_concurrency: Annotated[
        int, Field(ge=1, le=MAX_EMBEDDING_PER_VALIDATOR_CONCURRENCY)
    ] = DEFAULT_EMBEDDING_PER_VALIDATOR_CONCURRENCY
    """Concurrent hosted embedding requests summed over one validator's grants.

    Sized as four slots at the per-ticket allowance, which is the wire contract's
    practical ceiling on concurrent benchmark slots per host.
    """

    embedding_global_concurrency: Annotated[
        int, Field(ge=1, le=MAX_EMBEDDING_GLOBAL_CONCURRENCY)
    ] = DEFAULT_EMBEDDING_GLOBAL_CONCURRENCY
    """Concurrent hosted embedding requests across the whole fleet.

    The one number to move cautiously. It is enforced by a **cross-grant**
    aggregate over every in-flight request, so unlike the per-ticket limit it
    is best-effort under a simultaneous burst: concurrent admissions can
    overshoot it by at most the number of racers. Size it as a load-shedding
    backstop with headroom, not as an exact valve.
    """

    benchmark_runtime: BenchmarkRuntimeSettings = Field(
        default_factory=BenchmarkRuntimeSettings
    )
    """Safe, lease-stamped v10 case scheduling and delay-observation policy."""

    @model_validator(mode="after")
    def _hierarchy_holds(self) -> InferenceConcurrencySettings:
        if self.chat_per_ticket_concurrency > self.chat_per_validator_concurrency:
            raise ValueError(
                "chat_per_ticket_concurrency "
                f"({self.chat_per_ticket_concurrency}) may not exceed "
                "chat_per_validator_concurrency "
                f"({self.chat_per_validator_concurrency})"
            )
        if self.chat_per_validator_concurrency > self.chat_global_concurrency:
            raise ValueError(
                "chat_per_validator_concurrency "
                f"({self.chat_per_validator_concurrency}) may not exceed "
                f"chat_global_concurrency ({self.chat_global_concurrency})"
            )
        if (
            self.embedding_per_ticket_concurrency
            > self.embedding_per_validator_concurrency
        ):
            raise ValueError(
                "embedding_per_ticket_concurrency "
                f"({self.embedding_per_ticket_concurrency}) may not exceed "
                "embedding_per_validator_concurrency "
                f"({self.embedding_per_validator_concurrency}): a ticket cannot "
                "be allowed more concurrency than the validator hosting it"
            )
        if self.embedding_per_validator_concurrency > self.embedding_global_concurrency:
            raise ValueError(
                "embedding_per_validator_concurrency "
                f"({self.embedding_per_validator_concurrency}) may not exceed "
                f"embedding_global_concurrency ({self.embedding_global_concurrency}): "
                "a single validator cannot be allowed more concurrency than the fleet"
            )
        return self


class InferenceConcurrencySettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: InferenceConcurrencySettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveInferenceConcurrencySettings(BaseModel):
    """What the admission path is enforcing right now, and where it came from."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: InferenceConcurrencySettings
    checksum: str
    source: str
    """``"revision"`` when an operator revision governs, ``"default"`` otherwise."""


class AdminInferenceConcurrencySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: InferenceConcurrencySettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _require_complete_policy(self) -> AdminInferenceConcurrencySettingsRequest:
        """Reject a partial policy on a whole-object store.

        Every field has a default, which is what makes an empty board behave
        like the shipped configuration. On a *write* that is a footgun: a
        revision stores the whole policy, so sending only
        ``{"embedding_global_concurrency": 512}`` would silently reset the two
        limits below it to their defaults while the operator believed they
        changed one number. ``expected_revision`` cannot catch that -- they hold
        the current revision, they just under-specified the body.
        """
        # ``benchmark_runtime`` was added after this whole-object board shipped.
        # A pre-upgrade Backroom is allowed to omit only that additive object;
        # the endpoint preserves its current value instead of resetting it.
        missing = sorted(
            set(InferenceConcurrencySettings.model_fields)
            - {"benchmark_runtime"}
            - self.settings.model_fields_set
        )
        if missing:
            raise ValueError(
                "an inference concurrency revision stores the WHOLE policy, so "
                f"every field must be sent explicitly; missing {missing}. Read "
                "GET /admin/inference-concurrency-settings, change the fields "
                "you want, and send back the complete object."
            )
        return self


class AdminInferenceConcurrencySettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[InferenceConcurrencySettingsRevision]
    history: list[InferenceConcurrencySettingsRevision]
    default: InferenceConcurrencySettings
    effective: EffectiveInferenceConcurrencySettings
