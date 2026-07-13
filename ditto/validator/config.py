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

    # --- Roles (which halves of the loop this instance runs) ---
    enable_scoring: bool
    """Run the scoring sweep: pull the ``evaluating`` queue, score via
    dittobench-api, submit signed scores, and re-score stale champions. Defaults
    true (``VALIDATOR_ENABLE_SCORING``). In the one-validator-type model every
    validator both scores and sets weights, so an agent is scored by up to k=3
    independent validators and the platform finalizes on the median. Splitting
    the roles (a scoring-only or weights-only instance) is an optional deployment
    knob, not a central scorer; a weights-only instance needs no dittobench-api
    URL."""

    enable_weights: bool
    """Run the weight path: fold the durable (median-aggregated) ledger and set
    weights on chain. Defaults true (``VALIDATOR_ENABLE_WEIGHTS``). Every
    validator folds the same public ledger deterministically and sets its own
    weights, so chain consensus converges with no central weight authority. A
    scoring-only instance clears this and needs no Pylon identity. Both true is
    the default one-validator-type behaviour."""

    # --- Identity / chain ---
    validator_hotkey: str
    """This validator's SS58 hotkey (must match the loaded signing keypair)."""

    wallet_name: str | None
    """bittensor wallet name to load the signing hotkey from (if used)."""

    wallet_hotkey: str | None
    """bittensor wallet hotkey name (paired with ``wallet_name``)."""

    validator_mnemonic: str | None
    """Alternative signing source: a hotkey mnemonic (secret). Prefer a wallet."""

    netuid: int
    """Subnet netuid (118 for Ditto)."""

    pylon_url: str
    """Pylon base URL for chain reads + ``put_weights``."""

    pylon_identity_name: str
    """Pylon identity name (write access; required for ``put_weights``)."""

    pylon_identity_token: str
    """Pylon identity token paired with ``pylon_identity_name``."""

    pylon_open_access_token: str | None
    """Pylon open-access (read) token. Optional, but without it the worker's
    validator-permit self-check (a Pylon open-access ``get_recent_neurons`` read)
    cannot run in identity mode and fails open. Set ``PYLON_OPEN_ACCESS_TOKEN`` so
    the self-check works in production, where identity is the weight path."""

    subtensor_network: str
    """Subtensor network identifier for the substrate event reads Pylon does not
    surface. A ``ws://`` endpoint targets a specific node."""

    require_commit_reveal: bool
    """Cutover guard: expect commit-reveal to be ON for this network.

    ``VALIDATOR_REQUIRE_COMMIT_REVEAL``. Under commit-reveal v3 (bittensor >= 9)
    the weight sink (``set_weights`` / Pylon) does the timelock commit itself and
    the chain auto-reveals after ``RevealPeriodEpochs`` — the worker makes **no**
    separate reveal call. This flag is observability-only: when set and the chain
    reports commit-reveal OFF, the worker logs an error each epoch (weights would
    be front-runnable) but still submits — refusing would zero the chain, a worse
    failure. Set it on finney so a mis-set hyperparameter is loud."""

    # --- Incentive mechanism (KOTH + ATH gate). margin / tail_size /
    # champion_share / dethrone_z are set from the frozen KOTH_* module constants
    # above, not from env. ---
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
        """Whether a usable signing key source is configured."""
        return bool(self.validator_mnemonic) or bool(
            self.wallet_name and self.wallet_hotkey
        )


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


def _parse_int(name: str, default: str) -> int:
    """Parse an int env var into a typed ``ValidatorConfigError`` on garbage."""
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError as e:
        raise ValidatorConfigError(f"{name} must be an integer, got {raw!r}") from e


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
    require_commit_reveal = (
        os.environ.get("VALIDATOR_REQUIRE_COMMIT_REVEAL", "").lower() in _truthy
    )

    # Roles: an instance runs the scoring half, the weight half, or both. Both
    # (the default) is the one-validator-type model: every validator scores and
    # sets weights. Splitting the roles is an optional deployment knob.
    enable_scoring = (
        os.environ.get("VALIDATOR_ENABLE_SCORING", "true").lower() in _truthy
    )
    enable_weights = (
        os.environ.get("VALIDATOR_ENABLE_WEIGHTS", "true").lower() in _truthy
    )
    if not (enable_scoring or enable_weights):
        raise ValidatorConfigError(
            "at least one of VALIDATOR_ENABLE_SCORING / VALIDATOR_ENABLE_WEIGHTS "
            "must be true"
        )

    # dittobench-api is only needed by the scoring half (and not in mock mode).
    # A weights-only validator consumes the ledger and does not need it.
    dittobench_api_url = os.environ.get("VALIDATOR_DITTOBENCH_API_URL", "")
    if enable_scoring and not dittobench_mock:
        _require("VALIDATOR_DITTOBENCH_API_URL", dittobench_api_url)

    # Pylon identity is only needed by the weight half's Pylon ``put_weights``
    # path; a scoring-only instance sets no weights at all, so don't require it
    # there.
    pylon_identity_name = os.environ.get("PYLON_IDENTITY_NAME", "")
    pylon_identity_token = os.environ.get("PYLON_IDENTITY_TOKEN", "")
    if enable_weights:
        _require("PYLON_IDENTITY_NAME", pylon_identity_name)
        _require("PYLON_IDENTITY_TOKEN", pylon_identity_token)

    # margin / tail_size / champion_share / dethrone_z are frozen (the KOTH_*
    # module constants), not env. Confirmation-seed count stays operator-set but
    # must match network-wide, so validate it.
    koth_confirmation_seeds = _parse_int("VALIDATOR_KOTH_CONFIRMATION_SEEDS", "3")
    if koth_confirmation_seeds < 1:
        raise ValidatorConfigError(
            "VALIDATOR_KOTH_CONFIRMATION_SEEDS must be >= 1, "
            f"got {koth_confirmation_seeds}"
        )
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
        enable_scoring=enable_scoring,
        enable_weights=enable_weights,
        validator_hotkey=_require(
            "VALIDATOR_HOTKEY", os.environ.get("VALIDATOR_HOTKEY", "")
        ),
        wallet_name=os.environ.get("VALIDATOR_WALLET_NAME") or None,
        wallet_hotkey=os.environ.get("VALIDATOR_WALLET_HOTKEY") or None,
        validator_mnemonic=os.environ.get("VALIDATOR_MNEMONIC") or None,
        netuid=int(os.environ.get("NETUID", "118")),
        pylon_url=os.environ.get("PYLON_URL", "http://localhost:8001"),
        pylon_identity_name=pylon_identity_name,
        pylon_identity_token=pylon_identity_token,
        pylon_open_access_token=os.environ.get("PYLON_OPEN_ACCESS_TOKEN") or None,
        subtensor_network=os.environ.get("SUBTENSOR_NETWORK", "finney"),
        require_commit_reveal=require_commit_reveal,
        koth_margin=KOTH_MARGIN,
        koth_tail_size=KOTH_TAIL_SIZE,
        koth_champion_share=KOTH_CHAMPION_SHARE,
        koth_dethrone_z=KOTH_DETHRONE_Z,
        koth_confirmation_seeds=koth_confirmation_seeds,
        min_stake_tao=min_stake_tao,
        sweep_seconds=int(os.environ.get("VALIDATOR_SWEEP_SECONDS", "120")),
        epoch_seconds=int(os.environ.get("VALIDATOR_EPOCH_SECONDS", "3600")),
        queue_limit=int(os.environ.get("VALIDATOR_QUEUE_LIMIT", "50")),
        dittobench_poll_seconds=float(
            os.environ.get("VALIDATOR_DITTOBENCH_POLL_SECONDS", "10")
        ),
        dittobench_timeout_seconds=float(
            os.environ.get("VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS", "2400")
        ),
        http_timeout_seconds=float(
            os.environ.get("VALIDATOR_HTTP_TIMEOUT_SECONDS", "30")
        ),
    )
    if not config.signing_source_present():
        raise ValidatorConfigError(
            "no signing key: set VALIDATOR_MNEMONIC or "
            "VALIDATOR_WALLET_NAME + VALIDATOR_WALLET_HOTKEY"
        )
    return config
