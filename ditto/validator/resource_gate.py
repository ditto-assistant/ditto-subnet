"""Self-imposed host-resource admission gate for the validator worker.

A benchmark ticket is a *lease*: once claimed, the platform holds it (and the
quorum's two sibling leases) for the full run window. A run that dies because
its host ran out of disk or memory therefore costs far more than the sweep it
occupied -- it burns a whole lease slot, and the agent waits for the next
quorum. The cheapest place to avoid that is before the claim.

This module is the "am I about to fall over?" half of a two-sided gate. The
platform refuses to *hand* work to a host whose last heartbeat looked
overloaded; this refuses to *take* it. Either side alone leaves a window (a
stale heartbeat, a platform that has not been told yet), so both exist.

The thresholds are deliberately high. The point is not to throttle a busy
validator -- a host at 65% memory running four benchmarks is doing exactly its
job -- but to stop a host that is genuinely about to fail. Below the ceiling,
nothing here changes behaviour at all.

The readings come from the same :class:`~ditto.system_health.SystemMetricsCollector`
sample the heartbeat publishes, so the number this gate acted on is the number
an operator sees in the fleet view. Those samples are quantised to a five-point
grid (see ``_coarse_percent``), which is why a ceiling must itself be a
multiple of five: an off-grid ceiling silently fires at the next grid point up
and then misdescribes itself in the logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from ditto.api_models.system_health import SystemMetrics
from ditto.validator.errors import ValidatorConfigError

ConstrainedResource = Literal["cpu", "memory", "disk"]

#: A ceiling of zero disables that resource's gate entirely.
CEILING_DISABLED = 0
#: Refusing work below this is throttling a healthy host, not protecting it.
MIN_ENABLED_CEILING = 50

# Disk is the resource that actually fails a run: a full filesystem breaks the
# image pull, the dataset render and the sandbox tmpfs, and it does not recover
# on its own while work keeps landing.
DEFAULT_DISK_PERCENT_CEILING = 95
# Memory exhaustion kills the sandbox outright. 95 leaves the ordinary
# multi-slot working set (15-20% observed in the fleet) untouched.
DEFAULT_MEMORY_PERCENT_CEILING = 95
# CPU ships disabled by default, and that is a deliberate asymmetry rather than
# an oversight. A saturated CPU makes a benchmark *slower*, not doomed, and
# benchmark hosts are supposed to run pinned. The sample is a 0.1s
# ``psutil.cpu_percent`` reading rounded up to the grid, so a perfectly healthy
# four-slot host reports 100 routinely; gating on it by default would stop the
# competition to protect against nothing. The knob exists for operators whose
# hosts share CPU with something else.
DEFAULT_CPU_PERCENT_CEILING = CEILING_DISABLED

CPU_CEILING_ENV = "VALIDATOR_CPU_PERCENT_CEILING"
MEMORY_CEILING_ENV = "VALIDATOR_MEMORY_PERCENT_CEILING"
DISK_CEILING_ENV = "VALIDATOR_DISK_PERCENT_CEILING"


@dataclass(frozen=True)
class ResourceCeilings:
    """Per-resource percentages at or above which this host stops claiming.

    Each field is either :data:`CEILING_DISABLED` or a multiple of five in
    ``[MIN_ENABLED_CEILING, 100]``. The comparison is ``>=`` so a ceiling of
    ``95`` means "a reported 95 already declines"; combined with the coarse
    grid that is a true reading of roughly 92.5% or worse.
    """

    cpu_percent: int = DEFAULT_CPU_PERCENT_CEILING
    memory_percent: int = DEFAULT_MEMORY_PERCENT_CEILING
    disk_percent: int = DEFAULT_DISK_PERCENT_CEILING

    def exceeded(
        self, metrics: SystemMetrics | None
    ) -> tuple[ConstrainedResource, ...]:
        """Return the resources at or past their ceiling, in report order.

        ``None`` metrics mean "this worker has no collector wired up", which is
        not evidence of pressure: an unobserved host keeps working exactly as
        it did before this gate existed.
        """
        if metrics is None:
            return ()
        readings: tuple[tuple[ConstrainedResource, int, int], ...] = (
            ("disk", metrics.disk_percent, self.disk_percent),
            ("memory", metrics.memory_percent, self.memory_percent),
            ("cpu", metrics.cpu_percent, self.cpu_percent),
        )
        return tuple(
            resource
            for resource, observed, ceiling in readings
            if ceiling != CEILING_DISABLED and observed >= ceiling
        )

    def describe(self, metrics: SystemMetrics | None) -> str:
        """Render a log-safe ``disk 95% >= 95%`` explanation of the decline."""
        if metrics is None:
            return "no host metrics"
        observed: dict[ConstrainedResource, int] = {
            "cpu": metrics.cpu_percent,
            "memory": metrics.memory_percent,
            "disk": metrics.disk_percent,
        }
        ceilings: dict[ConstrainedResource, int] = {
            "cpu": self.cpu_percent,
            "memory": self.memory_percent,
            "disk": self.disk_percent,
        }
        exceeded = self.exceeded(metrics)
        if not exceeded:
            return "within every configured ceiling"
        return ", ".join(
            f"{resource} {observed[resource]}% >= {ceilings[resource]}%"
            for resource in exceeded
        )


DEFAULT_RESOURCE_CEILINGS = ResourceCeilings()


def _parse_ceiling(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValidatorConfigError(
            f"{name} must be a whole percentage, got {raw!r}"
        ) from e


def parse_resource_ceilings_from_env() -> ResourceCeilings:
    """Build :class:`ResourceCeilings` from ``VALIDATOR_*_PERCENT_CEILING``.

    Raises:
        ValidatorConfigError: When a value is not an integer, or is not
            :data:`CEILING_DISABLED` or an in-range multiple of five.
    """
    ceilings = ResourceCeilings(
        cpu_percent=_parse_ceiling(CPU_CEILING_ENV, DEFAULT_CPU_PERCENT_CEILING),
        memory_percent=_parse_ceiling(
            MEMORY_CEILING_ENV, DEFAULT_MEMORY_PERCENT_CEILING
        ),
        disk_percent=_parse_ceiling(DISK_CEILING_ENV, DEFAULT_DISK_PERCENT_CEILING),
    )
    check_resource_ceilings_config(ceilings)
    return ceilings


def check_resource_ceilings_config(ceilings: ResourceCeilings) -> None:
    """Fail fast on a ceiling that cannot mean what it says.

    Raises:
        ValidatorConfigError: On an out-of-range or off-grid ceiling.
    """
    for name, value in (
        (CPU_CEILING_ENV, ceilings.cpu_percent),
        (MEMORY_CEILING_ENV, ceilings.memory_percent),
        (DISK_CEILING_ENV, ceilings.disk_percent),
    ):
        if value == CEILING_DISABLED:
            continue
        if not MIN_ENABLED_CEILING <= value <= 100:
            raise ValidatorConfigError(
                f"{name} must be 0 (disabled) or in "
                f"[{MIN_ENABLED_CEILING}, 100], got {value}"
            )
        if value % 5:
            raise ValidatorConfigError(
                f"{name} must be a multiple of 5 because host metrics are "
                f"reported on a five-point grid, got {value}"
            )
