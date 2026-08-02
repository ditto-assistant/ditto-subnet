"""Versioned, hot-swappable operator policy for how many concurrent benchmark
SLOTS a single validator may hold live tickets for.

A validator advertises its own capacity in the heartbeat
(``BenchmarkCapacity.configured_slots``, bounded to eight by the protocol), and
until now the platform simply honored whatever was advertised: there was no
lever at all. These models back an append-only revision table so an operator can
cap the fleet — instantly, from backroom, with no redeploy. It is both the kill
switch (drop to 1 and multi-slot dispatch stops on the next ticket issue) and
the gradual-ramp control (2 -> 3 -> 4 as confidence grows).

Unlike the efficiency-bonus settings, this policy has **no pre-existing env
var**, so there is no seed to overlay: the module-level default in
``ditto.api_server.validator_slot_settings`` governs when no revision exists,
and every failure path falls back to it rather than to "uncapped".

Each revision carries the COMPLETE policy (never a diff), so a read never has to
merge partial revisions and a historical row always reproduces exactly what was
in force.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HARD_SLOT_CEILING = 8
"""The protocol's own maximum advertised slots (``^slot-[0-7]$``, see
``ditto.api_models.benchmark_capacity``). The operator cap can narrow the fleet
but can never widen it past what a validator is able to advertise."""

DISK_PERCENT_QUANTUM = 5
"""``SystemMetrics.disk_percent`` is reported on a 5% grid (``multiple_of=5``).
A ceiling off that grid would fire at the next grid point up and so silently
misdescribe itself (a ceiling of 87 behaves exactly like 90), which is why the
envelope check below rejects it.

``cpu_percent`` and ``memory_percent`` are quantised identically -- all three go
through the same ``_coarse_percent`` on the validator and carry
``multiple_of=5`` in :class:`~ditto.api_models.system_health.SystemMetrics` --
so every ceiling in this policy is held to the same grid."""

CEILING_DISABLED = 0
"""A ceiling of zero means "do not gate on this resource at all".

CPU ships this way. A saturated CPU makes a benchmark slower, not doomed, and a
benchmark host is *supposed* to run pinned; the sample is a sub-second reading
rounded up to the grid, so a perfectly healthy multi-slot host reports 100
routinely. Gating on it by default would stop the competition to protect
against nothing. Memory ships restricted-disabled for the same shape of reason:
the live fleet runs 3-4 concurrent benchmarks at 15-20% memory, so the only
memory reading worth reacting to is the one just short of an OOM."""

MIN_ENABLED_CEILING = 50
"""Below this a ceiling is throttling a healthy host rather than protecting a
failing one, so the envelope refuses it outright."""


class ValidatorSlotSettings(BaseModel):
    """The full, hot-swappable validator-slot policy.

    Every knob is independently settable in a single revision, and a revision
    always stores all of them, so the row is a complete description of the
    policy that was in force -- there is no partial-revision merge on the read
    path.

    The resource ceilings form two tiers over the same heartbeat sample:

    * ``*_percent_ceiling`` -- the throttle. Cross one and the validator is held
      to :data:`~ditto.api_server.validator_slot_settings.DISK_RESTRICTED_SLOTS`
      concurrent leases.
    * ``resource_block_percent_ceiling`` -- the refusal. Cross it on any enabled
      resource and the validator is issued nothing until it recovers.

    Adding a field here is not free: Backroom parses this object with a
    ``z.object``, which STRIPS undeclared keys, so a field the Backroom schema
    does not declare is dropped from every write body and silently reset to the
    default on the first operator save. Land the matching Backroom schema change
    with any edit to this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_concurrent_slots: Annotated[int, Field(ge=1, le=HARD_SLOT_CEILING)] = 2
    """Maximum benchmark slots the platform will issue live tickets for on any
    ONE validator, regardless of how many it advertises (``1 <= n <= 8``).

    Deliberately defaults to 2, not 8: a fleet that has never been tuned runs
    conservatively, and an operator ramps up explicitly. ``1`` is the kill
    switch -- it restores strictly serial, one-ticket-at-a-time dispatch."""

    disk_percent_ceiling: Annotated[int, Field(ge=CEILING_DISABLED, le=100)] = 90
    """Disk-utilization circuit breaker, as a percentage on the heartbeat's 5%
    grid (:data:`CEILING_DISABLED`, or ``50 <= n <= 100`` and a multiple of 5).

    When a validator's MOST RECENT heartbeat reports
    ``system_metrics.disk_percent`` at or above this, that validator is held to
    a single slot: parallel benchmark slots multiply image pulls and container
    layers, which is exactly what a nearly-full host cannot absorb.

    Evaluated only at ticket ISSUE time. It never revokes a live lease, so a
    validator that crosses the ceiling mid-benchmark still runs and reports the
    work it already holds to completion, and the restriction lifts on its own as
    soon as a fresh heartbeat reports headroom again."""

    memory_percent_ceiling: Annotated[int, Field(ge=CEILING_DISABLED, le=100)] = 90
    """The same circuit breaker for ``system_metrics.memory_percent``.

    Memory has never been the binding constraint on this fleet -- hosts run
    three and four concurrent benchmarks at 15-20% -- so at 90 this default
    costs nothing in practice. It exists because the failure it prevents is the
    expensive one: an OOM mid-benchmark kills the sandbox, burns the lease, and
    with it the two sibling leases the quorum was holding."""

    cpu_percent_ceiling: Annotated[int, Field(ge=CEILING_DISABLED, le=100)] = (
        CEILING_DISABLED
    )
    """The same circuit breaker for ``system_metrics.cpu_percent``, shipped
    :data:`CEILING_DISABLED`.

    Setting this to zero disables CPU for BOTH tiers below -- a disabled
    resource is not consulted by the block ceiling either. See
    :data:`CEILING_DISABLED` for why the default is off."""

    resource_block_percent_ceiling: Annotated[
        int, Field(ge=CEILING_DISABLED, le=100)
    ] = 95
    """The hard stop, shared by every ENABLED resource above.

    The ceilings above are a throttle: cross one and the validator keeps
    working, one benchmark at a time. This is the refusal. When any resource
    that has a ceiling configured reports at or above this, the platform issues
    that validator no tickets at all until a later heartbeat says it recovered.

    Deliberately higher than the throttle ceilings, and deliberately blunt: at
    95% disk or memory a host is not busy, it is about to fail, and a benchmark
    that dies mid-flight costs a full lease (90 minutes, times three for the
    quorum) and starves the queue behind it.

    A resource with ``ceiling == CEILING_DISABLED`` is exempt here too, which is
    what keeps a pinned CPU -- the normal state of a working benchmark host --
    from blocking anything by default. :data:`CEILING_DISABLED` disables the
    hard stop entirely and leaves only the throttle."""

    @model_validator(mode="after")
    def validate_envelope(self) -> ValidatorSlotSettings:
        """Invariants the per-field bounds cannot express on their own."""
        if self.max_concurrent_slots > HARD_SLOT_CEILING:
            raise ValueError(
                "max_concurrent_slots cannot exceed the protocol slot ceiling "
                f"of {HARD_SLOT_CEILING}"
            )
        for name, value in (
            ("disk_percent_ceiling", self.disk_percent_ceiling),
            ("memory_percent_ceiling", self.memory_percent_ceiling),
            ("cpu_percent_ceiling", self.cpu_percent_ceiling),
            ("resource_block_percent_ceiling", self.resource_block_percent_ceiling),
        ):
            if value == CEILING_DISABLED:
                continue
            if value % DISK_PERCENT_QUANTUM:
                raise ValueError(
                    f"{name} must be a multiple of {DISK_PERCENT_QUANTUM} "
                    "because heartbeat host metrics are reported on that grid"
                )
            if value < MIN_ENABLED_CEILING:
                raise ValueError(
                    f"{name} must be {CEILING_DISABLED} (disabled) or at least "
                    f"{MIN_ENABLED_CEILING}"
                )
        highest_throttle = max(
            self.disk_percent_ceiling,
            self.memory_percent_ceiling,
            self.cpu_percent_ceiling,
        )
        if (
            self.resource_block_percent_ceiling != CEILING_DISABLED
            and self.resource_block_percent_ceiling < highest_throttle
        ):
            raise ValueError(
                "resource_block_percent_ceiling must be at or above every "
                f"enabled per-resource ceiling ({highest_throttle}); a hard "
                "stop below the throttle makes the throttle unreachable"
            )
        return self


class ValidatorSlotSettingsRevision(BaseModel):
    """One append-only, operator-audited revision of the slot policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: ValidatorSlotSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveValidatorSlotSettings(BaseModel):
    """What the dispatch path actually reads: the latest revision (or the
    module default when none exists), plus provenance for the operator
    console."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    """The governing revision number, or 0 when no revision exists."""

    scope: str
    settings: ValidatorSlotSettings
    checksum: str
    source: Literal["revision", "default"]
    """``revision`` when a stored revision governs, ``default`` for the
    module-level default (which is also every failure path's fallback)."""

    hard_slot_ceiling: int = HARD_SLOT_CEILING
    """The protocol maximum a validator can advertise -- the top of the ramp the
    operator console renders ``max_concurrent_slots`` against."""

    disk_restricted_slots: int = 1
    """How many slots a validator is held to once ``disk_percent_ceiling`` is
    tripped."""

    max_age_seconds: float
    """The resolver TTL: an upper bound on how long a backroom change can take
    to land on the dispatch path (0 means every read re-reads)."""


class AdminValidatorSlotSettingsRequest(BaseModel):
    """Append one optimistic, confirmation-gated revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scope: str = "*"
    """Slot policy is subnet-global; only ``*`` is accepted."""

    expected_revision: Annotated[int, Field(ge=0)]
    """The revision the operator believes is current (0 = none yet). A mismatch
    is a 409 so a concurrent change is never silently clobbered."""

    settings: ValidatorSlotSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
    """Must equal ``APPLY VALIDATOR SLOT CAP <max_concurrent_slots>`` (the
    resulting cap), typed exactly -- so the number being applied is stated twice
    and a fat-fingered ramp cannot land silently."""


class AdminValidatorSlotSettingsResponse(BaseModel):
    """Current policy per scope, append-only history, the module default, and
    the settings actually in force right now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current: list[ValidatorSlotSettingsRevision]
    history: list[ValidatorSlotSettingsRevision]
    default: ValidatorSlotSettings
    effective: EffectiveValidatorSlotSettings
