"""Bounded functional probes behind signed per-component stack health.

The validator observes its current sidecars from its existing position on the
private Compose network — plain HTTP probes against endpoints it already
depends on — and reports one closed
:class:`~ditto.api_models.stack_health.ValidatorStackHealth` per heartbeat.
No Docker socket is mounted and no new privilege is added for telemetry:

* ``ditto_subnet`` — the reporting process itself (worker loop is running).
* ``dittobench_api`` — derived from the identity-bound ``/v1/capabilities``
  observation the heartbeat already performs (:meth:`DittobenchClient.
  scorer_benchmark_capability`), so capability verification and health stay
  one observation.
* ``sandbox_docker`` / ``pylon`` — bounded reachability / readiness GETs
  against operator-configured internal probe URLs. ``model_relay`` remains an
  optional, disabled compatibility component and is not probed by default.
Every probe is capped by ``stack_probe_timeout_seconds`` and the sidecar
snapshot is cached for ``stack_health_cache_seconds``, so a wedged sidecar can
never stall heartbeat cadence. A probe that cannot run (no URL configured,
mock mode, collection error) reports ``unknown`` — never a copied pin and
never a fabricated observation. Probe URLs are config, not payload: nothing
host-shaped is ever serialized.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from ditto import __version__
from ditto.api_models.stack_health import (
    ObservedComponentIdentity,
    ValidatorComponentHealth,
    ValidatorStackHealth,
)

if TYPE_CHECKING:
    from ditto.api_models.validator_capabilities import (
        ScorerBenchmarkCapability,
        ValidatorStackIdentity,
    )
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

_SIDECAR_NAMES = ("sandbox_docker", "pylon")


def _unknown_component(*, required: bool = True) -> ValidatorComponentHealth:
    return ValidatorComponentHealth(health="unknown", required=required)


def _self_component(observed_at: int) -> ValidatorComponentHealth:
    """The reporting worker loop: alive by construction of this heartbeat."""
    return ValidatorComponentHealth(
        health="healthy",
        required=True,
        observed_at=observed_at,
        ready=True,
        observed_identity=ObservedComponentIdentity(version=__version__),
    )


def _scorer_component(
    scorer: ScorerBenchmarkCapability, observed_at: int
) -> ValidatorComponentHealth:
    """Map the live scorer capability observation onto component health.

    ``legacy_v2`` (reachable, no capability surface) is *degraded*: the scorer
    answers but its running identity cannot be verified, so it must never look
    equivalent to a fresh, identity-matched scorer.
    """
    when = scorer.observed_at if scorer.observed_at is not None else observed_at
    if scorer.status == "fresh_verified":
        return ValidatorComponentHealth(
            health="healthy",
            required=True,
            observed_at=when,
            ready=True,
            observed_identity=ObservedComponentIdentity(
                source_revision=scorer.source_revision,
                version=scorer.software_version,
            ),
        )
    if scorer.status == "identity_mismatch" and (
        scorer.source_revision is not None or scorer.software_version is not None
    ):
        return ValidatorComponentHealth(
            health="identity_mismatch",
            required=True,
            observed_at=when,
            ready=True,
            observed_identity=ObservedComponentIdentity(
                source_revision=scorer.source_revision,
                version=scorer.software_version,
            ),
        )
    if scorer.status == "unreachable":
        return ValidatorComponentHealth(
            health="unreachable", required=True, observed_at=when
        )
    # legacy_v2, or a defensive identity_mismatch that carried no identity
    # fields: the scorer answered but its running identity is unverifiable.
    return ValidatorComponentHealth(
        health="degraded", required=True, observed_at=when, ready=True
    )


def fallback_stack_health() -> ValidatorStackHealth:
    """Conservative v9 snapshot when no probe collector ran.

    Only the reporting process itself is claimed healthy; every sidecar is
    ``unknown``. The scorer capability observation still travels separately in
    ``capabilities.scorer_benchmarks``, so nothing is silently invented here.
    """
    return ValidatorStackHealth(
        ditto_subnet=_self_component(int(time.time())),
        dittobench_api=_unknown_component(),
        sandbox_docker=_unknown_component(),
        pylon=_unknown_component(),
    )


class StackHealthCollector:
    """Owns the bounded sidecar probes and their freshness cache."""

    def __init__(self, config: ValidatorConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._sidecar_cache: tuple[dict[str, ValidatorComponentHealth], bool] | None = (
            None
        )
        self._sidecar_cache_monotonic: float = 0.0

    async def collect(
        self,
        *,
        stack: ValidatorStackIdentity,
        scorer: ScorerBenchmarkCapability,
    ) -> ValidatorStackHealth:
        """Return the current per-component snapshot; never raises.

        ``ditto_subnet`` and ``dittobench_api`` are rebuilt from this
        heartbeat's own observations; the four network sidecars come from the
        cached probe sweep, refreshed at most every
        ``stack_health_cache_seconds``. Stale sidecar entries keep their
        original ``observed_at``, which is exactly how component-probe
        staleness stays distinguishable from heartbeat staleness.
        """
        del stack  # retained as a rolling-transition call-site contract
        observed_at = int(time.time())
        if self._config.dittobench_mock:
            # Local plumbing mode performs no observations; do not invent any.
            return fallback_stack_health()
        relay_path_broken = False
        try:
            sidecars, relay_path_broken = await self._sidecar_snapshot()
        except Exception as e:  # noqa: BLE001 - telemetry must never gate work
            logger.warning("stack-health probe sweep failed: %s", e)
            sidecars = {name: _unknown_component() for name in _SIDECAR_NAMES}
        dittobench_api = _scorer_component(scorer, observed_at)
        if relay_path_broken and dittobench_api.health == "healthy":
            # The scorer is reachable and identity-verified, but it cannot reach
            # the locked model relay on the path it actually uses to score
            # (host.docker.internal, resolvable only inside the scorer's netns).
            # The model_relay sidecar probe hits a service name and stays green,
            # so without this the dashboard shows a fully healthy validator that
            # fast-fails every scored run. Surface it as a degraded scorer.
            dittobench_api = ValidatorComponentHealth(
                health="degraded",
                required=True,
                observed_at=observed_at,
                ready=False,
                observed_identity=dittobench_api.observed_identity,
            )
        return ValidatorStackHealth(
            ditto_subnet=_self_component(observed_at),
            dittobench_api=dittobench_api,
            **sidecars,
        )

    async def _sidecar_snapshot(
        self,
    ) -> tuple[dict[str, ValidatorComponentHealth], bool]:
        now = time.monotonic()
        if (
            self._sidecar_cache is not None
            and now - self._sidecar_cache_monotonic
            < self._config.stack_health_cache_seconds
        ):
            return self._sidecar_cache
        sidecar_results = await asyncio.gather(
            self._probe_sandbox_docker(),
            self._probe_pylon(),
        )
        snapshot = dict(zip(_SIDECAR_NAMES, sidecar_results, strict=True))
        # Ticket inference is probed only after ticket activation. The
        # deprecated process-wide relay is not a live stack dependency.
        result = (snapshot, False)
        self._sidecar_cache = result
        self._sidecar_cache_monotonic = now
        return result

    async def _get(self, url: str) -> httpx.Response | None:
        """One bounded GET; ``None`` means the endpoint was unreachable."""
        try:
            return await self._client.get(
                url, timeout=self._config.stack_probe_timeout_seconds
            )
        except httpx.HTTPError:
            return None

    async def _probe_sandbox_docker(self) -> ValidatorComponentHealth:
        url = self._config.sandbox_docker_probe_url
        if not url:
            return _unknown_component()
        observed_at = int(time.time())
        response = await self._get(url)
        if response is None:
            return ValidatorComponentHealth(
                health="unreachable", required=True, observed_at=observed_at
            )
        if response.status_code == 200:
            return ValidatorComponentHealth(
                health="healthy", required=True, observed_at=observed_at, ready=True
            )
        return ValidatorComponentHealth(
            health="degraded", required=True, observed_at=observed_at, ready=False
        )

    async def _probe_pylon(self) -> ValidatorComponentHealth:
        url = self._config.pylon_probe_url or self._config.pylon_url
        if not url:
            return _unknown_component()
        observed_at = int(time.time())
        response = await self._get(url)
        if response is None:
            return ValidatorComponentHealth(
                health="unreachable", required=True, observed_at=observed_at
            )
        # Any HTTP answer below 500 proves the API is up and serving; the
        # probe is unauthenticated so 401/404 are still "reachable and ready".
        if response.status_code < 500:
            return ValidatorComponentHealth(
                health="healthy", required=True, observed_at=observed_at, ready=True
            )
        return ValidatorComponentHealth(
            health="degraded", required=True, observed_at=observed_at, ready=False
        )
