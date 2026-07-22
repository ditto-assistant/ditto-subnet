"""Bounded functional probes behind the signed per-component stack health (v9).

The validator observes its five sidecars from its existing position on the
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
* ``ollama`` — the same functional embedding request the scoring preflight
  uses, proving the forwarder, Ollama, and the required embedding model.

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

# A sidecar health/readiness reply larger than this is not a health reply.
_MAX_PROBE_BODY_BYTES = 8192

_SIDECAR_NAMES = ("sandbox_docker", "model_relay", "pylon", "ollama")


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
        model_relay=_unknown_component(),
        pylon=_unknown_component(),
        ollama=_unknown_component(),
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
        observed_at = int(time.time())
        if self._config.dittobench_mock:
            # Local plumbing mode performs no observations; do not invent any.
            return fallback_stack_health()
        relay_path_broken = False
        try:
            sidecars, relay_path_broken = await self._sidecar_snapshot(stack)
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
        self, stack: ValidatorStackIdentity
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
            self._probe_model_relay(stack),
            self._probe_pylon(),
            self._probe_ollama(),
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

    async def _probe_model_relay(
        self, stack: ValidatorStackIdentity
    ) -> ValidatorComponentHealth:
        url = self._config.model_relay_probe_url
        if not url:
            return _unknown_component(required=False)
        observed_at = int(time.time())
        response = await self._get(url)
        if response is None:
            return ValidatorComponentHealth(
                health="unreachable", required=True, observed_at=observed_at
            )
        if response.status_code >= 500:
            return ValidatorComponentHealth(
                health="degraded", required=True, observed_at=observed_at, ready=False
            )
        # Any sub-500 answer proves the relay is up; only a 200 body is
        # trusted for the optional identity / route-readiness observations.
        if response.status_code != 200:
            return ValidatorComponentHealth(
                health="healthy", required=True, observed_at=observed_at, ready=True
            )
        identity, model_ready = _parse_relay_health(response)
        expected = stack.components.model_relay.source_revision
        observed = identity.source_revision if identity is not None else None
        if expected is not None and observed is not None and observed != expected:
            return ValidatorComponentHealth(
                health="identity_mismatch",
                required=True,
                observed_at=observed_at,
                ready=True,
                model_ready=model_ready,
                observed_identity=identity,
            )
        return ValidatorComponentHealth(
            health="healthy",
            required=True,
            observed_at=observed_at,
            ready=True,
            model_ready=model_ready,
            observed_identity=identity,
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

    async def _probe_ollama(self) -> ValidatorComponentHealth:
        url = self._config.embed_preflight_url
        if not url:
            return _unknown_component()
        observed_at = int(time.time())
        try:
            response = await self._client.post(
                url,
                json={"model": "embeddinggemma", "input": "validator stack probe"},
                timeout=self._config.stack_probe_timeout_seconds,
            )
        except httpx.HTTPError:
            return ValidatorComponentHealth(
                health="unreachable", required=True, observed_at=observed_at
            )
        if response.status_code == 200 and _has_embedding_vector(response):
            return ValidatorComponentHealth(
                health="healthy",
                required=True,
                observed_at=observed_at,
                ready=True,
                model_ready=True,
            )
        # The endpoint answered but could not serve the required embedding
        # model — reachable yet not functionally ready.
        return ValidatorComponentHealth(
            health="degraded",
            required=True,
            observed_at=observed_at,
            ready=True,
            model_ready=False,
        )

    async def _probe_scorer_relay_path(self) -> bool:
        """Report whether the scorer cannot reach the model relay it scores with.

        The scorer calls the locked relay at ``host.docker.internal:11435`` inside
        sandbox-docker's shared netns. This validator is off that netns and its
        ``model_relay`` probe hits a service name, so a broken ``host.docker.internal``
        mapping (e.g. a managed stack whose compose predates the fix) stays green
        here even though every scored run fast-fails ``model_relay_unavailable``.
        ``GET /v1/relay-preflight`` lets the scorer report the real path health from
        the right netns, so the failure surfaces on the dashboard instead of looking
        fully healthy. A missing endpoint (older scorer), any non-503 answer, or an
        unreachable scorer is "not observable as broken" — never a fabricated fault.
        """
        base = self._config.dittobench_api_url
        if not base:
            return False
        response = await self._get(f"{base}/v1/relay-preflight")
        if response is None or response.status_code != 503:
            return False
        if len(response.content) > _MAX_PROBE_BODY_BYTES:
            return False
        try:
            failure = response.json().get("failure")
        except ValueError:
            return False
        return (
            isinstance(failure, dict)
            and failure.get("kind") == "validator_infrastructure"
        )


def _parse_relay_health(
    response: httpx.Response,
) -> tuple[ObservedComponentIdentity | None, bool | None]:
    """Extract only the bounded, typed fields from a relay health reply.

    Anything oversized, malformed, or off-pattern degrades to "not observed";
    the reply body is advisory and must never fail the probe.
    """
    if len(response.content) > _MAX_PROBE_BODY_BYTES:
        return None, None
    try:
        payload = response.json()
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    model_ready = payload.get("model_route_ready")
    if not isinstance(model_ready, bool):
        model_ready = None
    identity: ObservedComponentIdentity | None = None
    revision = payload.get("source_revision")
    version = payload.get("version")
    fields = {
        "source_revision": revision if isinstance(revision, str) else None,
        "version": version if isinstance(version, str) else None,
    }
    if any(value is not None for value in fields.values()):
        try:
            identity = ObservedComponentIdentity(**fields)
        except ValueError:
            identity = None
    return identity, model_ready


def _has_embedding_vector(response: httpx.Response) -> bool:
    if len(response.content) > 1 << 20:
        return False
    try:
        embeddings = response.json().get("embeddings")
    except (ValueError, AttributeError):
        return False
    return (
        isinstance(embeddings, list)
        and bool(embeddings)
        and isinstance(embeddings[0], list)
        and bool(embeddings[0])
    )
