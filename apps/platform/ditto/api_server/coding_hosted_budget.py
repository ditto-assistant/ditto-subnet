"""Tokenizer-independent, deliberately conservative native runtime estimator."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from ditto.api_models.coding_hosted_budget import HostedBudgetProfile
from ditto.api_models.coding_hosted_inference import (
    HostedInferencePolicy,
    HostedInferenceSettlement,
)
from ditto.api_models.coding_inference import parse_coding_inference_json
from ditto.api_server.coding_hosted_inference import ReservationCeilings


class HostedBudgetError(ValueError):
    """Safe profile rejection without configuration or model text."""


class ProfiledBudgetEstimator:
    """Requires a profile digest committed by the approved native policy.

    This reserves a reviewed maximum billed input count, not an exact local token
    count. The bound must cover provider framing, tools and failure paths. It is
    NOT inferred from text length, a model catalogue or miner-supplied counts.
    """

    def __init__(
        self,
        *,
        profile_bytes: bytes,
        policy: HostedInferencePolicy,
        now: Callable[[], float] = time.time,
    ):
        try:
            self._profile = parse_coding_inference_json(
                HostedBudgetProfile, profile_bytes, maximum_bytes=16384
            )
            self._policy = HostedInferencePolicy.model_validate_json(
                policy.model_dump_json(by_alias=True)
            )
            if (
                self._policy.runtime_profile_sha256 != self._profile.digest()
                or self._profile.max_billed_prompt_tokens_per_request
                > self._policy.max_prompt_tokens
                or self._policy.max_completion_tokens_per_request
                > self._profile.max_completion_tokens_per_request
                or self._profile.cost_ceiling(
                    self._policy.max_completion_tokens_per_request
                )
                > self._policy.max_cost_usd_micros
            ):
                raise ValueError
        except ValueError:
            raise HostedBudgetError(
                "hosted budget profile does not match policy"
            ) from None
        self._now = now
        self._last_now = 0.0
        self._closed = False
        self.remaining_seconds()

    def require_policy(self, policy: HostedInferencePolicy) -> None:
        if policy.digest() != self._policy.digest():
            raise HostedBudgetError("hosted estimator policy binding differs")
        self.remaining_seconds()

    def canonical_profile(self) -> bytes:
        """Private evidence material for the trusted publisher, not model input."""
        return self._profile.canonical_bytes()

    def verify_usage(self, settlement: HostedInferenceSettlement) -> None:
        # Expiry does not erase billing. Verify against the immutable profile
        # even after expiry, before settlement; output validity is checked later.
        if (
            settlement.policy_sha256 != self._policy.digest()
            or settlement.cost_usd_micros
            > self._profile.cost_for_usage(
                settlement.prompt_tokens, settlement.completion_tokens
            )
        ):
            raise HostedBudgetError("provider billing exceeds reviewed bounds")

    def remaining_seconds(self) -> float:
        value = self._now()
        if (
            self._closed
            or type(value) not in (int, float)
            or not math.isfinite(value)
            or value < self._last_now
            or value < self._profile.valid_from_unix
            or value >= self._profile.valid_until_unix
        ):
            self._closed = True
            raise HostedBudgetError("hosted budget profile is expired or clock-invalid")
        self._last_now = value
        return self._profile.valid_until_unix - value

    def ceilings(self, canonical_request: bytes) -> ReservationCeilings:
        self.remaining_seconds()
        try:
            request, _ = self._policy.locked_request(canonical_request)
        except ValueError:
            raise HostedBudgetError("hosted budget request violates policy") from None
        result = ReservationCeilings(
            prompt_tokens=self._profile.max_billed_prompt_tokens_per_request,
            completion_tokens=request.max_completion_tokens,
            cost_usd_micros=self._profile.cost_ceiling(request.max_completion_tokens),
        )
        result.validate()
        return result
