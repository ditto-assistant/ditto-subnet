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

    subtensor_network: str
    """Subtensor network identifier for the substrate event reads.

    Also the chain target for the SDK weight fallback; a ``ws://`` endpoint is
    passed straight to :class:`bittensor.Subtensor`."""

    use_sdk_weights: bool
    """When True, submit weights via the bittensor SDK instead of Pylon identity.

    Localnet fallback (``VALIDATOR_USE_SDK_WEIGHTS``): Pylon identity-write isn't
    stood up on the dev chain, so the worker calls ``Subtensor.set_weights``
    directly. Pylon identity creds are not required in this mode."""

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

    ``0.01`` = 1%: a challenger becomes champion only if its composite exceeds the
    incumbent's by more than this. Ties + sub-margin gains keep the incumbent
    (first-seen wins), which is what makes a copy unprofitable."""

    koth_tail_size: int
    """How many runners-up (after the champion) split the participation tail."""

    koth_champion_share: float
    """Fraction of weight the ATH champion receives; the rest splits over the
    tail. ``0.9`` = 90% champion / 10% tail."""

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
    often than this — approximately the subnet tempo / ``weights_rate_limit``
    window, so the loop doesn't fight the chain's rate limiter."""

    queue_limit: int
    """Max agents to pull from ``/validator/queue`` per sweep."""

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
    dittobench_api_url = os.environ.get("VALIDATOR_DITTOBENCH_API_URL", "")
    openrouter_key = os.environ.get("VALIDATOR_OPENROUTER_KEY", "")
    if not dittobench_mock:
        _require("VALIDATOR_DITTOBENCH_API_URL", dittobench_api_url)
        _require("VALIDATOR_OPENROUTER_KEY", openrouter_key)

    # Pylon identity is only needed for the Pylon ``put_weights`` path; the SDK
    # weight fallback signs with the local hotkey, so don't require it there.
    pylon_identity_name = os.environ.get("PYLON_IDENTITY_NAME", "")
    pylon_identity_token = os.environ.get("PYLON_IDENTITY_TOKEN", "")
    if not use_sdk_weights:
        _require("PYLON_IDENTITY_NAME", pylon_identity_name)
        _require("PYLON_IDENTITY_TOKEN", pylon_identity_token)

    # KOTH+ATH mechanism knobs. Every validator must agree on these or Yuma
    # consensus clips the deviator, so they are env-tunable but default to the
    # team-locked values (90/10 split, 1% margin).
    koth_margin = _parse_float("VALIDATOR_KOTH_MARGIN", "0.01")
    koth_tail_size = _parse_int("VALIDATOR_KOTH_TAIL_SIZE", "4")
    koth_champion_share = _parse_float("VALIDATOR_KOTH_CHAMPION_SHARE", "0.9")
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

    config = ValidatorConfig(
        platform_api_url=_require(
            "VALIDATOR_PLATFORM_API_URL",
            os.environ.get("VALIDATOR_PLATFORM_API_URL", "http://localhost:8000"),
        ),
        dittobench_api_url=dittobench_api_url,
        openrouter_key=openrouter_key,
        run_size=run_size,
        dittobench_mock=dittobench_mock,
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
        subtensor_network=os.environ.get("SUBTENSOR_NETWORK", "finney"),
        use_sdk_weights=use_sdk_weights,
        weight_version_key=_parse_int(
            "VALIDATOR_WEIGHT_VERSION_KEY", str(__spec_version__)
        ),
        koth_margin=koth_margin,
        koth_tail_size=koth_tail_size,
        koth_champion_share=koth_champion_share,
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
