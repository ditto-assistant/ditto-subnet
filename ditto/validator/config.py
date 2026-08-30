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
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

from ditto.validator.errors import ValidatorConfigError
from ditto.validator.resource_gate import (
    ResourceCeilings,
    parse_resource_ceilings_from_env,
)
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
KOTH_MARGIN = 0.007  # absolute composite-point dethrone hysteresis
# Bench v6 and later shrink the entire dethrone band smoothly as the incumbent
# approaches a perfect score. Legacy or mixed-version comparisons stay unchanged.
KOTH_BAND_DECAY_MIN_BENCH_VERSION = 6
KOTH_BAND_DECAY_START_COMPOSITE = 0.60
KOTH_BAND_DECAY_RATE = 2.0
# That decay is open-loop: it shrinks the band against an assumed perfect score
# of 1.0 without knowing what the challenger can actually reach. On a saturated
# benchmark it therefore still asks for more than the whole attainable
# remainder -- at a 0.997012 incumbent the decayed band is 0.00315 while only
# 0.00299 of score exists above it, so no agent can dethrone at any skill. From
# protocol 24 the band is additionally capped at this share of the score the
# challenger has left to gain, which keeps the required dethrone score strictly
# below the challenger's ceiling and the crown reachable by a perfect run.
# Half is the conservative choice: it binds only inside two band widths of the
# ceiling and never widens a band.
KOTH_CEILING_HEADROOM_SHARE = 0.5
KOTH_TAIL_SIZE = 4  # ranked runners-up after the champion
KOTH_RANK_SHARES = (0.65, 0.14, 0.10, 0.07, 0.04)
KOTH_DETHRONE_Z = 1.64  # statistical dethrone-band z-multiplier (~95% one-sided)
# Uncertainty may raise the bar above ``KOTH_MARGIN``, but a 3-seed paired
# outlier must not demand a 0.03 lead on a 0.92-vs-0.88 board. Cap the
# statistical term at this multiple of the flat margin before high-score decay.
KOTH_STATISTICAL_BAND_CAP_MULTIPLE = 2.0
KOTH_CONFIRMATION_SEEDS = 3  # CRN seeds a version-bump re-score dethrones on (median)
# Continual evidence targets a one-sided 95% uncertainty half-width no wider
# than KOTH_MARGIN. Eight is the minimum credible variance sample; 32 is the
# hard fleet-work stop when current variance would otherwise ask for more.
TOP5_MIN_CONFIRMATION_SEEDS = 8
TOP5_MAX_CONFIRMATION_SEEDS = 32
TOP5_CATCH_UP_RATE = 2
# Hard ceiling on the operator-requested continual retest cohort. Mirrors the
# platform's own cap so the two agree on what an operator may ask for.
TOP5_MAX_COHORT_SIZE = 25
# Fallback share of miner emission released through KOTH, used when the platform
# ledger carries no burn policy (an older platform) or serves one that fails
# validation. The live value is ``LedgerResponse.burn_share``, resolved by the
# platform so the whole fleet folds the same number without agreeing about
# clocks -- see ``weights.resolve_miner_emission_share``. Releasing everything is
# the right fallback precisely because it is what the subnet did before the field
# existed, so an omission is never a change. The burn hotkey is retained in
# either case as the safe idle vector: with no eligible miners the whole vector
# still routes to burn rather than zeroing the chain.
MINER_EMISSION_SHARE = 1.0
FINNEY_BURN_HOTKEY = "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz"  # SN118 UID 0


# --- Lease-derived run bounds (not env-tunable, not a consensus knob) ---
# The platform issues a scoring ticket with a deadline. A validator still
# holding that ticket when the deadline passes has said nothing about the
# agent, and the ledger cannot tell that apart from a validator that died: the
# ticket simply reads ``expired``. So the abort point is derived from the
# ticket's own deadline instead of from a constant that has to be manually kept
# below a TTL this repo does not control -- that TTL has already moved
# 30 -> 45 -> 90 minutes, and any of those moves could have inverted the
# relationship silently.
#
# The margin is what an abort keeps in hand to cancel the scorer run and land a
# signed ``POST /validator/job/fail``: a bounded cancel plus one signed round
# trip, with room to spare. It is also >= the platform's own two-minute
# freshness bound on a signed validator request, so a report prepared at the
# abort point is still acceptable when it arrives.
LEASE_REPORT_MARGIN_SECONDS = 120.0


def lease_budget_seconds(
    ticket_deadline: datetime, *, now: datetime | None = None
) -> float:
    """Seconds of work this lease can still fund while keeping the report margin.

    Negative means the lease cannot fund any more work *and* has already eaten
    into the margin; callers must stop and report immediately rather than start
    or continue anything.
    """
    reference = now if now is not None else datetime.now(UTC)
    remaining = (ticket_deadline - reference).total_seconds()
    return remaining - LEASE_REPORT_MARGIN_SECONDS


def run_budget_seconds(
    cap_seconds: float,
    ticket_deadline: datetime | None,
    *,
    now: datetime | None = None,
) -> float:
    """Wall-clock seconds a validator may still spend on one leased run.

    The smaller of the operator's harness cap and what the lease can fund. The
    minimum is the whole point: ``VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS`` is an
    operator dial with a fixed default, so it is only ever an upper bound on
    effort — never a promise that the lease will still be alive when it fires.
    Keeping the two related by hand is what produced the original incident: a
    fixed harness cap looked safe against one ticket TTL only by arithmetic
    nobody re-checked when the TTL moved, and it measured from the wrong instant
    regardless.

    An unticketed run (no deadline) keeps the plain cap, which is all the bound
    that exists for it.
    """
    cap = float(cap_seconds)
    if ticket_deadline is None:
        return cap
    return min(cap, lease_budget_seconds(ticket_deadline, now=now))


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

    dittobench_control_token: str
    """Shared secret for the scorer's inference control plane.

    The scorer authorizes ``/v1/inference/session*`` for a loopback peer or a
    matching ``Authorization: Bearer``. It joins sandbox-docker's network
    namespace while this worker stays on the Compose bridge, so the validator
    is never a loopback peer and the bearer is the only path that works. Empty
    only for mock/local runs against a scorer that has no token configured.
    Sent to ``dittobench_api_url`` and nowhere else; never logged.
    """

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

    longmem_capacity: int
    """Independent LongMemEval slots, bounded to half ordinary capacity.

    These slots never claim canonical benchmark tickets. Zero disables the
    optional lane; the shipped default is half ordinary capacity rounded up.
    """

    resource_ceilings: ResourceCeilings
    """Host-resource percentages at or above which this worker stops claiming.

    Set from ``VALIDATOR_{CPU,MEMORY,DISK}_PERCENT_CEILING``. See
    :mod:`ditto.validator.resource_gate` for why the defaults are high and why
    CPU ships disabled.
    """

    inference_proxy_required: bool
    """Fail closed when a ticket lacks its platform inference capability."""

    # --- Per-component stack-health probes (heartbeat v9) ---
    sandbox_docker_probe_url: str
    """Optional readiness probe URL for the sandbox-docker sidecar on the
    private Compose network (e.g. its Docker ``/_ping``). Empty disables the
    probe and the component reports ``unknown``. Never published."""

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
    # rank_shares / dethrone_z / confirmation_seeds are all set from the frozen
    # KOTH_* module constants above, not from env. ---
    koth_margin: float
    """Absolute composite-point lead required to dethrone the incumbent.

    ``0.007`` means a challenger becomes champion only if its composite exceeds
    the incumbent's by more than 0.007 points. Ties + sub-margin gains keep the
    incumbent (first-seen wins), which is what makes a copy unprofitable without
    giving high-scoring incumbents a growing percentage-based moat."""

    koth_tail_size: int
    """How many runners-up (after the champion) split the participation tail."""

    koth_rank_shares: tuple[float, ...]
    """Frozen rank shares for champion then runners-up: 65/14/10/7/4."""

    koth_dethrone_z: float
    """z-multiplier for the **statistical** half of the dethroning band. A
    challenger must beat the incumbent by
    more than ``max(koth_margin, koth_dethrone_z * sqrt(se_c² +
    se_champ²))`` — the larger of the fixed composite-point hysteresis and the combined
    measurement uncertainty, when the ledger surfaces a per-entry
    ``composite_stderr``. A **consensus knob** (every validator must agree) like
    ``koth_margin``. Inert until the platform surfaces stderr — with no stderr the
    band is the fixed composite-point hysteresis. ``1.64`` ≈ one-sided
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
    """Hard ceiling on the variance-sized continual shared-seed target."""

    top5_catch_up_rate: int
    """Missing seeds a new top-five entrant may catch up per claimed round."""

    top5_max_cohort_size: int
    """Ceiling on the operator-requested continual retest cohort this validator
    will plan for. The depth itself is the platform operator's call, arriving on
    each ledger read as ``continual_retest_cohort_size``; this only bounds how
    far a validator will follow it, so a misconfigured or hostile platform
    cannot spend a validator's whole cycle on rescores. Not a consensus knob:
    membership is re-derived and enforced by the platform at lease time, and
    emissions are the top five at any cohort size."""

    miner_emission_share: float
    """Fallback share of miner emission released through KOTH; the remainder is
    burned. ``1.0`` releases all of it. The live share comes from the platform
    ledger's ``burn_share``; this is what the fold uses when that field is absent
    or invalid."""

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

    scorer_require_binary_provenance: bool = False
    """Whether the pinned scorer revision must prove its identity from its binary.

    Committed next to the dittobench-api pin in ``docker-compose.yml`` and moved
    with it (``VALIDATOR_SCORER_REQUIRE_BINARY_PROVENANCE``). An environment
    variable states what an operator believes is running; only a value linked
    into the binary states what *is* running, and the two diverge precisely when
    a container is recreated against a cached image. Enable it for a pin whose
    scorer stamps its revision at build time; leave it off for an older pin,
    which could never satisfy it."""

    platform_inference_base_url: str = ""
    """Base URL the platform mints ticket inference URLs from.

    The platform serves its inference plane under its own public hostname
    (``DITTO_INFERENCE_PUBLIC_BASE_URL``), which need not equal the API host a
    validator posts jobs and scores to. When set, it joins ``platform_api_url``
    in a two-entry allowlist for a ticket's exchange URL, so a malicious ticket
    still cannot redirect a grant exchange to a host of its choosing. Empty
    means "same host as the API", which is the historical behaviour."""

    coding_shadow_enabled: bool = False
    """Run the separate shadow coding worker. Disabled by default."""

    coding_shadow_instance_id: str = ""
    """Stable, non-secret instance identity used by the exclusive claim ledger."""

    coding_shadow_poll_seconds: float = 10.0
    """Idle polling interval for the separate shadow coding queue."""

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

    # Committed beside the dittobench-api pin, so it moves with the pin instead
    # of being an operator tuning knob.
    scorer_require_binary_provenance = (
        os.environ.get("VALIDATOR_SCORER_REQUIRE_BINARY_PROVENANCE", "").lower()
        in _truthy
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

    platform_api_url = _require(
        "VALIDATOR_PLATFORM_API_URL",
        os.environ.get("VALIDATOR_PLATFORM_API_URL", "http://localhost:8000"),
    )
    benchmark_capacity = int(os.environ.get("VALIDATOR_BENCHMARK_CAPACITY", "1"))
    longmem_capacity = int(
        os.environ.get("VALIDATOR_LONGMEM_CAPACITY", str((benchmark_capacity + 1) // 2))
    )
    coding_shadow_enabled = (
        os.environ.get("VALIDATOR_CODING_SHADOW_ENABLED", "false").lower() in _truthy
    )
    coding_shadow_instance_id = os.environ.get(
        "VALIDATOR_CODING_SHADOW_INSTANCE_ID", ""
    ).strip()
    coding_shadow_poll_seconds = (
        _parse_float("VALIDATOR_CODING_SHADOW_POLL_SECONDS", "10")
        if coding_shadow_enabled
        else 10.0
    )
    config = ValidatorConfig(
        platform_api_url=platform_api_url,
        platform_inference_base_url=(
            os.environ.get("VALIDATOR_PLATFORM_INFERENCE_BASE_URL", "").strip()
            or platform_api_url
        ),
        dittobench_api_url=dittobench_api_url,
        dittobench_capabilities_timeout_seconds=capabilities_timeout_seconds,
        dittobench_control_token=os.environ.get(
            "VALIDATOR_DITTOBENCH_CONTROL_TOKEN", ""
        ).strip(),
        run_size=run_size,
        dittobench_mock=dittobench_mock,
        benchmark_capacity=benchmark_capacity,
        longmem_capacity=longmem_capacity,
        resource_ceilings=parse_resource_ceilings_from_env(),
        inference_proxy_required=(
            os.environ.get("VALIDATOR_INFERENCE_PROXY_REQUIRED", "false").lower()
            in _truthy
        ),
        sandbox_docker_probe_url=os.environ.get(
            "VALIDATOR_SANDBOX_DOCKER_PROBE_URL", ""
        ),
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
        koth_rank_shares=KOTH_RANK_SHARES,
        koth_dethrone_z=KOTH_DETHRONE_Z,
        koth_confirmation_seeds=KOTH_CONFIRMATION_SEEDS,
        top5_max_confirmation_seeds=TOP5_MAX_CONFIRMATION_SEEDS,
        top5_catch_up_rate=TOP5_CATCH_UP_RATE,
        top5_max_cohort_size=TOP5_MAX_COHORT_SIZE,
        miner_emission_share=MINER_EMISSION_SHARE,
        burn_hotkey=burn_hotkey,
        min_stake_tao=min_stake_tao,
        sweep_seconds=int(os.environ.get("VALIDATOR_SWEEP_SECONDS", "30")),
        epoch_seconds=int(os.environ.get("VALIDATOR_EPOCH_SECONDS", "3600")),
        queue_limit=int(os.environ.get("VALIDATOR_QUEUE_LIMIT", "50")),
        dittobench_poll_seconds=float(
            os.environ.get("VALIDATOR_DITTOBENCH_POLL_SECONDS", "10")
        ),
        dittobench_timeout_seconds=float(
            os.environ.get("VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS", "9900")
        ),
        http_timeout_seconds=float(
            os.environ.get("VALIDATOR_HTTP_TIMEOUT_SECONDS", "30")
        ),
        scorer_require_binary_provenance=scorer_require_binary_provenance,
        coding_shadow_enabled=coding_shadow_enabled,
        coding_shadow_instance_id=coding_shadow_instance_id,
        coding_shadow_poll_seconds=coding_shadow_poll_seconds,
    )
    if not config.signing_source_present():
        raise ValidatorConfigError(
            "no signing key: set VALIDATOR_WALLET_NAME + VALIDATOR_WALLET_HOTKEY"
        )
    if not 1 <= config.benchmark_capacity <= 8:
        raise ValidatorConfigError("VALIDATOR_BENCHMARK_CAPACITY must be in [1, 8]")
    if not 0 <= config.longmem_capacity <= 4:
        raise ValidatorConfigError("VALIDATOR_LONGMEM_CAPACITY must be in [0, 4]")
    if config.longmem_capacity > (config.benchmark_capacity + 1) // 2:
        raise ValidatorConfigError(
            "VALIDATOR_LONGMEM_CAPACITY cannot exceed half rounded up of "
            "VALIDATOR_BENCHMARK_CAPACITY"
        )
    if config.coding_shadow_enabled and (
        not config.coding_shadow_instance_id
        or len(config.coding_shadow_instance_id.encode()) > 128
        or any(
            character.isspace() or not character.isprintable()
            for character in config.coding_shadow_instance_id
        )
        or not 32 <= len(config.dittobench_control_token.encode()) <= 256
        or not all(
            character.isascii() and (character.isalnum() or character in "_-")
            for character in config.dittobench_control_token
        )
    ):
        raise ValidatorConfigError(
            "enabled shadow coding requires a stable instance ID and control token"
        )
    if config.coding_shadow_enabled and (
        not math.isfinite(config.coding_shadow_poll_seconds)
        or not 1 <= config.coding_shadow_poll_seconds <= 300
    ):
        raise ValidatorConfigError(
            "VALIDATOR_CODING_SHADOW_POLL_SECONDS must be in [1, 300]"
        )
    return config
