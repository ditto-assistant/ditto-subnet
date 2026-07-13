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

from ditto import __spec_version__
from ditto.validator.errors import ValidatorConfigError


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

    openrouter_key: str
    """BYOK OpenRouter key forwarded to dittobench-api's run_size pipeline."""

    run_size: str
    """dittobench run size: ``small`` | ``medium`` | ``full``."""

    dittobench_mock: bool
    """When True, bypass dittobench-api and return a canned ScoreReport.

    For local end-to-end plumbing tests where no dittobench-api / OpenRouter
    key is available. Enabled via ``VALIDATOR_DITTOBENCH_MOCK``."""

    # --- Roles (which halves of the loop this instance runs) ---
    enable_scoring: bool
    """Run the scoring sweep: pull the ``evaluating`` queue, score via
    dittobench-api, submit signed scores, and re-score stale champions. Defaults
    true (``VALIDATOR_ENABLE_SCORING``). In the one-validator-type model every
    validator both scores and sets weights, so an agent is scored by up to k=3
    independent validators and the platform finalizes on the median. Splitting
    the roles (a scoring-only or weights-only instance) is an optional deployment
    knob, not a central scorer; a weights-only instance needs no dittobench-api
    URL / OpenRouter key."""

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
    """Subtensor network identifier for the substrate event reads.

    Also the chain target for the SDK weight fallback; a ``ws://`` endpoint is
    passed straight to :class:`bittensor.Subtensor`."""

    use_sdk_weights: bool
    """When True, submit weights via the bittensor SDK instead of Pylon identity.

    Localnet fallback (``VALIDATOR_USE_SDK_WEIGHTS``): Pylon identity-write isn't
    stood up on the dev chain, so the worker calls ``Subtensor.set_weights``
    directly. Pylon identity creds are not required in this mode."""

    require_commit_reveal: bool
    """Cutover guard: expect commit-reveal to be ON for this network.

    ``VALIDATOR_REQUIRE_COMMIT_REVEAL``. Under commit-reveal v3 (bittensor >= 9)
    the weight sink (``set_weights`` / Pylon) does the timelock commit itself and
    the chain auto-reveals after ``RevealPeriodEpochs`` — the worker makes **no**
    separate reveal call. This flag is observability-only: when set and the chain
    reports commit-reveal OFF, the worker logs an error each epoch (weights would
    be front-runnable) but still submits — refusing would zero the chain, a worse
    failure. Leave off on the localnet (commit-reveal is disabled there); set it
    on finney so a mis-set hyperparameter is loud."""

    weight_version_key: int
    """Mechanism version stamped on ``set_weights`` (the SDK path).

    Bittensor's ``version_key`` lets validators signal which mechanism version
    they scored under; the chain groups weights by it so an old validator that
    hasn't upgraded doesn't get averaged against a new mechanism. Defaults to
    ``ditto.__spec_version__`` so it advances with the package version. Every
    validator on a network must agree, like the KOTH knobs. (The Pylon path
    derives its own ``version_key`` from subnet hyperparams, so this applies to
    the SDK/localnet path.)"""

    # --- Incentive mechanism (KOTH + ATH gate) ---
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
    submits weights. ``0`` disables the check (the localnet has staking
    disabled). A companion to the ``validator_permit`` self-check: on a real
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

    # Mock mode bypasses dittobench-api, so its URL + OpenRouter key are
    # optional (local end-to-end plumbing without a scoring engine).
    _truthy = {"1", "true", "yes"}
    dittobench_mock = os.environ.get("VALIDATOR_DITTOBENCH_MOCK", "").lower() in _truthy
    use_sdk_weights = os.environ.get("VALIDATOR_USE_SDK_WEIGHTS", "").lower() in _truthy
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

    # dittobench-api + OpenRouter are only needed by the scoring half (and not in
    # mock mode). A weights-only validator consumes the ledger and needs neither.
    dittobench_api_url = os.environ.get("VALIDATOR_DITTOBENCH_API_URL", "")
    openrouter_key = os.environ.get("VALIDATOR_OPENROUTER_KEY", "")
    if enable_scoring and not dittobench_mock:
        _require("VALIDATOR_DITTOBENCH_API_URL", dittobench_api_url)
        _require("VALIDATOR_OPENROUTER_KEY", openrouter_key)

    # Pylon identity is only needed by the weight half's Pylon ``put_weights``
    # path; the SDK weight fallback signs with the local hotkey, and a
    # scoring-only instance sets no weights at all, so don't require it there.
    pylon_identity_name = os.environ.get("PYLON_IDENTITY_NAME", "")
    pylon_identity_token = os.environ.get("PYLON_IDENTITY_TOKEN", "")
    if enable_weights and not use_sdk_weights:
        _require("PYLON_IDENTITY_NAME", pylon_identity_name)
        _require("PYLON_IDENTITY_TOKEN", pylon_identity_token)

    # KOTH+ATH mechanism knobs. Every validator must agree on these or Yuma
    # consensus clips the deviator, so they are env-tunable but default to the
    # team-locked values (90/10 split).
    #
    # Margin retune for DittoBench v2 / bench_version 2:
    # the dethrone margin must exceed the between-seed composite noise so a
    # verbatim copy cannot win a lucky seed. v1's 1% margin assumed a small σ it
    # never had. v2 targets between-seed σ ≤ 0.01 composite and sets
    # the margin to ≥ 3σ/composite: at composite ~0.6, 3·0.01/0.6 = 5%. The
    # offline calibrator (dittobench-api cmd/benchcal) reports a hermetic
    # composite σ ≈ 0.017 as a weak-harness upper bound; the champion-region σ
    # from the hosted 30-seed frozen-starter-kit run MUST reconfirm ≤ 0.01 before
    # mainnet — if it is higher, raise this margin (and adopt median-of-3
    # sub-seeds) and re-match the platform score_tol.
    koth_margin = _parse_float("VALIDATOR_KOTH_MARGIN", "0.05")
    koth_tail_size = _parse_int("VALIDATOR_KOTH_TAIL_SIZE", "4")
    koth_champion_share = _parse_float("VALIDATOR_KOTH_CHAMPION_SHARE", "0.9")
    koth_dethrone_z = _parse_float("VALIDATOR_KOTH_DETHRONE_Z", "1.64")
    koth_confirmation_seeds = _parse_int("VALIDATOR_KOTH_CONFIRMATION_SEEDS", "3")
    # ``math.isfinite`` rejects NaN/Inf, which slip past a bare ``<= 0`` (e.g.
    # ``nan <= 0`` is False) and would silently disable the ATH gate — a
    # consensus-divergence footgun since the fold multiplies by ``1 + margin``.
    if not math.isfinite(koth_margin) or koth_margin <= 0:
        raise ValidatorConfigError(
            f"VALIDATOR_KOTH_MARGIN must be a finite number > 0, got {koth_margin}"
        )
    if koth_tail_size < 0:
        raise ValidatorConfigError(
            f"VALIDATOR_KOTH_TAIL_SIZE must be >= 0, got {koth_tail_size}"
        )
    if not (math.isfinite(koth_champion_share) and 0 < koth_champion_share <= 1):
        raise ValidatorConfigError(
            "VALIDATOR_KOTH_CHAMPION_SHARE must be a finite number in (0, 1], "
            f"got {koth_champion_share}"
        )
    # z >= 0; 0 disables the statistical band (pure relative margin). NaN/Inf
    # would poison the deterministic fold, so reject them (consensus footgun).
    if not math.isfinite(koth_dethrone_z) or koth_dethrone_z < 0:
        raise ValidatorConfigError(
            "VALIDATOR_KOTH_DETHRONE_Z must be a finite number >= 0, "
            f"got {koth_dethrone_z}"
        )
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
        openrouter_key=openrouter_key,
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
        use_sdk_weights=use_sdk_weights,
        require_commit_reveal=require_commit_reveal,
        weight_version_key=_parse_int(
            "VALIDATOR_WEIGHT_VERSION_KEY", str(__spec_version__)
        ),
        koth_margin=koth_margin,
        koth_tail_size=koth_tail_size,
        koth_champion_share=koth_champion_share,
        koth_dethrone_z=koth_dethrone_z,
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
