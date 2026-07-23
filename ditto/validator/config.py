"""Env-driven config for the validator worker.

Frozen dataclass + ``parse_validator_config_from_env`` builder, matching the
platform's config convention. The worker is a standalone process (systemd /
pm2) co-located with the API on the platform VM; it talks to the platform only
over the public ``/validator/*`` HTTP API and to ``dittobench-api`` over HTTP,
and writes weights via Pylon identity mode. Nothing here imports the DB.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from ditto.validator.errors import ValidatorConfigError
from ditto.validator.update_control import VALIDATOR_COMPATIBILITY_EPOCH

# --- Frozen consensus constants (KOTH + ATH gate) ---
# NOT env-tunable: every validator must fold the public ledger with byte-identical
# mechanism values or Yuma consensus clips the deviator, so these are pinned in
# code. Changing one is a coordinated network upgrade (roll every validator
# together), never a per-operator setting.
#
# Margin: the flat dethrone gate protects the incumbent from negligible gains,
# while the statistical band below separately protects against measurement noise.
# Keep this frozen across validators because it changes the deterministic KOTH fold.
KOTH_MARGIN = 0.02  # relative dethrone margin (2%)
KOTH_TAIL_SIZE = 4  # runners-up after the champion that split the tail
KOTH_CHAMPION_SHARE = 0.9  # champion weight share (90% champion / 10% tail)
KOTH_DETHRONE_Z = 1.64  # statistical dethrone-band z-multiplier (~95% one-sided)
KOTH_CONFIRMATION_SEEDS = 3  # CRN seeds a version-bump re-score dethrones on (median)
TOP5_MAX_CONFIRMATION_SEEDS = 16
TOP5_CATCH_UP_RATE = 2
# Release the full miner emission through KOTH. The burn hotkey is retained only
# as the safe idle vector: with no eligible miners the whole vector still routes
# to burn rather than zeroing the chain.
MINER_EMISSION_SHARE = 1.0
FINNEY_BURN_HOTKEY = "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz"  # SN118 UID 0


def _is_local_subtensor_network(network: str) -> bool:
    """Return whether a Bittensor network alias or endpoint is local-only."""
    normalized = network.strip().lower()
    if normalized in {"local", "localhost", "127.0.0.1", "::1"}:
        return True

    hostname = urlsplit(normalized).hostname
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname or "").is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ValidatorConfig:
    """Configuration for one validator worker instance."""

    # --- Platform API (HTTP-decoupled; the worker calls the platform, even on
    # localhost, like any external validator would) ---
    platform_api_url: str
    """Base URL of the platform API, e.g. ``http://localhost:8000``."""

    # --- Scoring engine ---
    dittobench_api_url: str
    """Base URL of the hosted dittobench-api (Cloud Run)."""

    dittobench_capabilities_timeout_seconds: float
    """Hard timeout for scorer runtime-capability discovery."""

    run_size: str
    """dittobench run size: ``small`` | ``medium`` | ``full``."""

    dittobench_mock: bool
    """When True, bypass dittobench-api and return a canned ScoreReport.

    For local end-to-end plumbing tests where no dittobench-api is available.
    Enabled via ``VALIDATOR_DITTOBENCH_MOCK``."""

    benchmark_capacity: int
    """Bounded concurrent scoring slots advertised by heartbeat v10+.

    Defaults to one for compatibility and is capped at eight by the wire
    contract. This must not exceed dittobench-api's full-run capacity.
    """

    inference_proxy_required: bool
    """Fail closed when a ticket lacks its platform inference capability."""

    embed_preflight_url: str
    """Ollama ``/api/embed`` URL through the harness-facing TCP forwarder."""

    embed_preflight_timeout_seconds: float
    """Hard timeout for the functional embedding probe before ticket claim."""

    # --- Per-component stack-health probes (heartbeat v9) ---
    sandbox_docker_probe_url: str
    """Optional readiness probe URL for the sandbox-docker sidecar on the
    private Compose network (e.g. its Docker ``/_ping``). Empty disables the
    probe and the component reports ``unknown``. Never published."""

    model_relay_probe_url: str
    """Optional health probe URL for the model relay on the private Compose
    network. Empty disables the probe and the component reports ``unknown``.
    Never published."""

    pylon_probe_url: str
    """Pylon API readiness probe URL. Empty falls back to ``pylon_url``.
    Never published."""

    stack_probe_timeout_seconds: float
    """Hard per-probe timeout for the stack-health probes, so a failed sidecar
    cannot stall heartbeat cadence."""

    stack_health_cache_seconds: float
    """Seconds a sidecar probe snapshot may be reused before re-probing.
    ``0`` re-probes on every heartbeat."""

    # --- Identity / chain ---
    validator_hotkey: str
    """This validator's SS58 hotkey (must match the loaded signing keypair)."""

    wallet_name: str | None
    """bittensor wallet name to load the signing hotkey from (if used)."""

    wallet_hotkey: str | None
    """bittensor wallet hotkey name (paired with ``wallet_name``)."""

    netuid: int
    """Subnet netuid (118 for Ditto)."""

    pylon_url: str
    """Pylon base URL for chain reads + ``put_weights``."""

    pylon_identity_name: str
    """Pylon identity name (write access; required for ``put_weights``)."""

    pylon_token: str | None
    """The Pylon token (``PYLON_TOKEN``). One token guards both the open-access
    reads and the identity write, so the worker uses it for both the
    validator-permit self-check and ``put_weights``."""

    subtensor_network: str
    """Subtensor network identifier for the substrate event reads Pylon does not
    surface. A ``ws://`` endpoint targets a specific node."""

    # --- Incentive mechanism (KOTH + ATH gate). margin / tail_size /
    # champion_share / dethrone_z / confirmation_seeds are all set from the frozen
    # KOTH_* module constants above, not from env. ---
    koth_margin: float
    """Relative margin a challenger must beat the incumbent by to dethrone it.

    ``0.02`` = 2%: a challenger becomes champion only if its composite exceeds the
    incumbent's by more than this. Ties + sub-margin gains keep the incumbent
    (first-seen wins), which is what makes a copy unprofitable."""

    koth_tail_size: int
    """How many runners-up (after the champion) split the participation tail."""

    koth_champion_share: float
    """Fraction of weight the ATH champion receives; the rest splits over the
    tail. ``0.9`` = 90% champion / 10% tail."""

    koth_dethrone_z: float
    """z-multiplier for the **statistical** half of the dethroning band. A
    challenger must beat the incumbent by
    more than ``max(koth_margin * incumbent, koth_dethrone_z * sqrt(se_c² +
    se_champ²))`` — the larger of the flat relative margin and the combined
    measurement uncertainty, when the ledger surfaces a per-entry
    ``composite_stderr``. A **consensus knob** (every validator must agree) like
    ``koth_margin``. Inert until the platform surfaces stderr — with no stderr the
    band is the flat relative margin, byte-identical to today. ``1.64`` ≈ one-sided
    95%; ``0`` disables the statistical half."""

    koth_confirmation_seeds: int
    """How many common CRN seeds the version-bump re-score sweep runs each stale
    champion/tail agent on. With ``K >= 2`` the validator submits the median
    composite over the K
    common seeds and attaches the per-seed list (``confirmation_composites``), so
    the dethrone comparison clears the MEDIAN over seeds and a crown flip must
    replicate across seeds instead of riding one lucky draw. A **consensus knob**
    like ``koth_margin`` (every validator must run the same K to derive the same
    seed set). ``1`` reproduces the single-seed pre-P4 sweep, byte-identical."""

    top5_max_confirmation_seeds: int
    """Maximum champion-anchored seed depth retained by the continual lane."""

    top5_catch_up_rate: int
    """Missing seeds a new top-five entrant may catch up per claimed round."""

    miner_emission_share: float
    """Share of miner emission released through KOTH; the remainder is burned.
    ``1.0`` releases all of it (the deployed value)."""

    burn_hotkey: str
    """Owner-associated hotkey whose miner incentive Subtensor burns. Used for
    the idle vector (no eligible miners) and for any residual share below 1.0."""

    min_stake_tao: float
    """Minimum stake (TAO) this validator expects on its own hotkey before it
    submits weights. ``0`` disables the check. A companion to the
    ``validator_permit`` self-check: on a real
    network a permit implies stake, but the stake read gives an early, explicit
    log line when the hotkey has demonstrably fallen below the threshold."""

    # --- Cadence / limits ---
    sweep_seconds: int
    """Seconds between scoring sweeps — how fast the ``evaluating`` queue drains.

    Decoupled from ``epoch_seconds`` so scoring latency isn't the (much longer)
    weight-set cadence: a submission is picked up within ~one sweep, while
    weights are still only pushed every ``epoch_seconds``. Keep this <=
    ``epoch_seconds``."""

    epoch_seconds: int
    """Minimum seconds between on-chain weight submissions (the weight-set
    cadence). Weights are recomputed from the durable ledger and pushed no more
    often than this. The worker also reads the subnet's on-chain
    ``weights_rate_limit`` each epoch and stretches the effective cadence to
    whichever is longer, so this value is a floor, not a promise — the loop
    never knowingly fights the chain's rate limiter."""

    queue_limit: int
    """Max agents to pull from ``/validator/job`` per sweep."""

    dittobench_poll_seconds: float
    """Interval between ``/v1/runs/{id}`` polls."""

    dittobench_timeout_seconds: float
    """Hard cap on a single agent's dittobench run (build is slow at full)."""

    http_timeout_seconds: float
    """Per-request timeout for platform + dittobench HTTP calls."""

    def signing_source_present(self) -> bool:
        """Whether a usable signing key source is configured (wallet files)."""
        return bool(self.wallet_name and self.wallet_hotkey)


def _require(name: str, value: str) -> str:
    if not value:
        raise ValidatorConfigError(f"{name} is required")
    return value


def _parse_float(name: str, default: str) -> float:
    """Parse a float env var into a typed ``ValidatorConfigError`` on garbage."""
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except ValueError as e:
        raise ValidatorConfigError(f"{name} must be a number, got {raw!r}") from e


def check_validator_compatibility_config(expected_epoch: str) -> None:
    """Fail closed when deployment and image compatibility epochs differ."""
    if expected_epoch != str(VALIDATOR_COMPATIBILITY_EPOCH):
        raise ValidatorConfigError(
            "validator compatibility epoch mismatch: image is "
            f"{VALIDATOR_COMPATIBILITY_EPOCH}, deployment expects {expected_epoch}"
        )


def parse_validator_config_from_env() -> ValidatorConfig:
    """Build a :class:`ValidatorConfig` from ``VALIDATOR_*`` / ``PYLON_*`` env.

    Raises:
        ValidatorConfigError: When a required value is missing, no signing
            source is configured, or ``run_size`` is invalid.
    """
    expected_compatibility_epoch = os.environ.get(
        "VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH",
        str(VALIDATOR_COMPATIBILITY_EPOCH),
    )
    check_validator_compatibility_config(expected_compatibility_epoch)

    run_size = os.environ.get("VALIDATOR_RUN_SIZE", "full")
    if run_size not in {"small", "medium", "full"}:
        raise ValidatorConfigError(
            f"VALIDATOR_RUN_SIZE must be small|medium|full, got {run_size!r}"
        )

    # Mock mode bypasses dittobench-api, so its URL is optional (local
    # end-to-end plumbing without a scoring engine).
    _truthy = {"1", "true", "yes"}
    dittobench_mock = os.environ.get("VALIDATOR_DITTOBENCH_MOCK", "").lower() in _truthy

    # Every validator both scores and sets weights (the one-validator-type model),
    # so all of it is required: dittobench-api for scoring (unless mock) and the
    # Pylon identity + token for put_weights.
    dittobench_api_url = os.environ.get("VALIDATOR_DITTOBENCH_API_URL", "")
    if not dittobench_mock:
        _require("VALIDATOR_DITTOBENCH_API_URL", dittobench_api_url)
    embed_preflight_url = os.environ.get("VALIDATOR_EMBED_PREFLIGHT_URL", "")
    if not dittobench_mock:
        _require("VALIDATOR_EMBED_PREFLIGHT_URL", embed_preflight_url)
    embed_preflight_timeout_seconds = _parse_float(
        "VALIDATOR_EMBED_PREFLIGHT_TIMEOUT_SECONDS", "5"
    )
    if (
        not math.isfinite(embed_preflight_timeout_seconds)
        or embed_preflight_timeout_seconds <= 0
    ):
        raise ValidatorConfigError(
            "VALIDATOR_EMBED_PREFLIGHT_TIMEOUT_SECONDS must be a finite number > 0, "
            f"got {embed_preflight_timeout_seconds}"
        )
    stack_probe_timeout_seconds = _parse_float(
        "VALIDATOR_STACK_PROBE_TIMEOUT_SECONDS", "2"
    )
    if (
        not math.isfinite(stack_probe_timeout_seconds)
        or stack_probe_timeout_seconds <= 0
        or stack_probe_timeout_seconds > 10
    ):
        raise ValidatorConfigError(
            "VALIDATOR_STACK_PROBE_TIMEOUT_SECONDS must be in (0, 10]"
        )
    stack_health_cache_seconds = _parse_float(
        "VALIDATOR_STACK_HEALTH_CACHE_SECONDS", "60"
    )
    if (
        not math.isfinite(stack_health_cache_seconds)
        or stack_health_cache_seconds < 0
        or stack_health_cache_seconds > 3600
    ):
        raise ValidatorConfigError(
            "VALIDATOR_STACK_HEALTH_CACHE_SECONDS must be in [0, 3600]"
        )
    capabilities_timeout_seconds = _parse_float(
        "VALIDATOR_DITTOBENCH_CAPABILITIES_TIMEOUT_SECONDS", "3"
    )
    if (
        not math.isfinite(capabilities_timeout_seconds)
        or capabilities_timeout_seconds <= 0
        or capabilities_timeout_seconds > 10
    ):
        raise ValidatorConfigError(
            "VALIDATOR_DITTOBENCH_CAPABILITIES_TIMEOUT_SECONDS must be in (0, 10]"
        )

    # One Pylon token guards both the open-access reads and the identity write, so
    # the worker uses it for the permit self-check and for put_weights.
    pylon_identity_name = os.environ.get("PYLON_IDENTITY_NAME", "")
    pylon_token = os.environ.get("PYLON_TOKEN", "")
    _require("PYLON_IDENTITY_NAME", pylon_identity_name)
    _require("PYLON_TOKEN", pylon_token)

    validator_hotkey = _require(
        "VALIDATOR_HOTKEY", os.environ.get("VALIDATOR_HOTKEY", "")
    )
    subtensor_network = os.environ.get("SUBTENSOR_NETWORK", "finney")
    # Finney SN118 has a fixed owner hotkey at UID 0. Production validators may
    # use a named network or a custom non-loopback endpoint, so only explicit
    # local aliases/endpoints self-target the local owner validator.
    burn_hotkey = (
        validator_hotkey
        if _is_local_subtensor_network(subtensor_network)
        else FINNEY_BURN_HOTKEY
    )

    # All KOTH + ATH mechanism values are frozen (the KOTH_* module constants),
    # not env, so every validator folds identically.
    min_stake_tao = _parse_float("VALIDATOR_MIN_STAKE_TAO", "0")
    if not math.isfinite(min_stake_tao) or min_stake_tao < 0:
        raise ValidatorConfigError(
            f"VALIDATOR_MIN_STAKE_TAO must be a finite number >= 0, got {min_stake_tao}"
        )

    config = ValidatorConfig(
        platform_api_url=_require(
            "VALIDATOR_PLATFORM_API_URL",
            os.environ.get("VALIDATOR_PLATFORM_API_URL", "http://localhost:8000"),
        ),
        dittobench_api_url=dittobench_api_url,
        dittobench_capabilities_timeout_seconds=capabilities_timeout_seconds,
        run_size=run_size,
        dittobench_mock=dittobench_mock,
        benchmark_capacity=int(os.environ.get("VALIDATOR_BENCHMARK_CAPACITY", "1")),
        inference_proxy_required=(
            os.environ.get("VALIDATOR_INFERENCE_PROXY_REQUIRED", "false").lower()
            in _truthy
        ),
        embed_preflight_url=embed_preflight_url,
        embed_preflight_timeout_seconds=embed_preflight_timeout_seconds,
        sandbox_docker_probe_url=os.environ.get(
            "VALIDATOR_SANDBOX_DOCKER_PROBE_URL", ""
        ),
        model_relay_probe_url=os.environ.get("VALIDATOR_MODEL_RELAY_PROBE_URL", ""),
        pylon_probe_url=os.environ.get("VALIDATOR_PYLON_PROBE_URL", ""),
        stack_probe_timeout_seconds=stack_probe_timeout_seconds,
        stack_health_cache_seconds=stack_health_cache_seconds,
        validator_hotkey=validator_hotkey,
        wallet_name=os.environ.get("VALIDATOR_WALLET_NAME") or None,
        wallet_hotkey=os.environ.get("VALIDATOR_WALLET_HOTKEY") or None,
        netuid=int(os.environ.get("NETUID", "118")),
        pylon_url=os.environ.get("PYLON_URL", "http://localhost:8001"),
        pylon_identity_name=pylon_identity_name,
        pylon_token=pylon_token or None,
        subtensor_network=subtensor_network,
        koth_margin=KOTH_MARGIN,
        koth_tail_size=KOTH_TAIL_SIZE,
        koth_champion_share=KOTH_CHAMPION_SHARE,
        koth_dethrone_z=KOTH_DETHRONE_Z,
        koth_confirmation_seeds=KOTH_CONFIRMATION_SEEDS,
        top5_max_confirmation_seeds=TOP5_MAX_CONFIRMATION_SEEDS,
        top5_catch_up_rate=TOP5_CATCH_UP_RATE,
        miner_emission_share=MINER_EMISSION_SHARE,
        burn_hotkey=burn_hotkey,
        min_stake_tao=min_stake_tao,
        sweep_seconds=int(os.environ.get("VALIDATOR_SWEEP_SECONDS", "120")),
        epoch_seconds=int(os.environ.get("VALIDATOR_EPOCH_SECONDS", "3600")),
        queue_limit=int(os.environ.get("VALIDATOR_QUEUE_LIMIT", "50")),
        dittobench_poll_seconds=float(
            os.environ.get("VALIDATOR_DITTOBENCH_POLL_SECONDS", "10")
        ),
        dittobench_timeout_seconds=float(
            os.environ.get("VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS", "4500")
        ),
        http_timeout_seconds=float(
            os.environ.get("VALIDATOR_HTTP_TIMEOUT_SECONDS", "30")
        ),
    )
    if not config.signing_source_present():
        raise ValidatorConfigError(
            "no signing key: set VALIDATOR_WALLET_NAME + VALIDATOR_WALLET_HOTKEY"
        )
    if not 1 <= config.benchmark_capacity <= 8:
        raise ValidatorConfigError("VALIDATOR_BENCHMARK_CAPACITY must be in [1, 8]")
    return config
