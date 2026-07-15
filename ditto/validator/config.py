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

from ditto.validator.errors import ValidatorConfigError

# --- Frozen consensus constants (KOTH + ATH gate) ---
# NOT env-tunable: every validator must fold the public ledger with byte-identical
# mechanism values or Yuma consensus clips the deviator, so these are pinned in
# code. Changing one is a coordinated network upgrade (roll every validator
# together), never a per-operator setting.
#
# Margin: the dethrone margin must exceed the between-seed composite noise so a
# verbatim copy cannot win a lucky seed. v2 targets between-seed σ ≤ 0.01 and sets
# the margin to ≥ 3σ/composite (at composite ~0.6, 3·0.01/0.6 = 5%). The offline
# calibrator (dittobench-api cmd/benchcal) reports a hermetic σ ≈ 0.017 as a
# weak-harness upper bound; the champion-region σ must reconfirm ≤ 0.01 on the
# hosted multi-seed run before mainnet.
KOTH_MARGIN = 0.05  # relative dethrone margin (5%)
KOTH_TAIL_SIZE = 4  # runners-up after the champion that split the tail
KOTH_CHAMPION_SHARE = 0.9  # champion weight share (90% champion / 10% tail)
KOTH_DETHRONE_Z = 1.64  # statistical dethrone-band z-multiplier (~95% one-sided)
KOTH_CONFIRMATION_SEEDS = 3  # CRN seeds a version-bump re-score dethrones on (median)
MINER_EMISSION_SHARE = 0.2  # release 20% of miner emission; burn the other 80%
FINNEY_BURN_HOTKEY = "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz"  # SN118 UID 0


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

    run_size: str
    """dittobench run size: ``small`` | ``medium`` | ``full``."""

    dittobench_mock: bool
    """When True, bypass dittobench-api and return a canned ScoreReport.

    For local end-to-end plumbing tests where no dittobench-api is available.
    Enabled via ``VALIDATOR_DITTOBENCH_MOCK``."""

    embed_preflight_url: str
    """Ollama ``/api/embed`` URL through the harness-facing TCP forwarder."""

    embed_preflight_timeout_seconds: float
    """Hard timeout for the functional embedding probe before ticket claim."""

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

    ``0.05`` = 5%: a challenger becomes champion only if its composite exceeds the
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

    miner_emission_share: float
    """Share of miner emission released through KOTH; the remainder is burned."""

    burn_hotkey: str
    """Owner-associated hotkey whose miner incentive Subtensor burns."""

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


def parse_validator_config_from_env() -> ValidatorConfig:
    """Build a :class:`ValidatorConfig` from ``VALIDATOR_*`` / ``PYLON_*`` env.

    Raises:
        ValidatorConfigError: When a required value is missing, no signing
            source is configured, or ``run_size`` is invalid.
    """
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
    # Finney SN118 has a fixed owner hotkey at UID 0. Localnet setup uses its
    # validator as the subnet owner, so self-targeting preserves the same burn
    # path without making the production target operator-configurable.
    burn_hotkey = (
        FINNEY_BURN_HOTKEY if subtensor_network == "finney" else validator_hotkey
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
        run_size=run_size,
        dittobench_mock=dittobench_mock,
        embed_preflight_url=embed_preflight_url,
        embed_preflight_timeout_seconds=embed_preflight_timeout_seconds,
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
    return config
