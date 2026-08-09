"""Wire-contract tests for Bench v9 confirmation bundle controls."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.confirmation_bundles import (
    MAX_BUNDLE_REQUEST_CAP,
    MAX_BUNDLE_TOKEN_CAP,
    MAX_CONFIRMATION_TOP_N,
    MAX_DAILY_BUNDLE_CAP,
    MAX_DAILY_DOLLAR_MICROUSD,
    AdminConfirmationBundleRetestRequest,
    AdminConfirmationBundleSettingsRequest,
    ConfirmationBundleMode,
    ConfirmationBundleSettings,
    ConfirmationCompletionReport,
    ConfirmationDailyBudgetView,
    ConfirmationShadowCalibrationView,
    LongMemProviderLaneEvidence,
)
from ditto.tests.confirmation_evidence_fixtures import (
    ablation_envelope,
    longmem_envelope,
    unsigned_report,
)

_PROFILE_SHA = "a" * 64


def active_settings(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "mode": "shadow",
        "top_n": 5,
        "daily_bundle_cap": 10,
        "daily_dollar_cap_microusd": 1_000_000,
        "per_bundle_request_cap": 100,
        "per_bundle_token_cap": 10_000,
        "profile_revision": "v9-launch-1",
        "profile_checksum": _PROFILE_SHA,
        "challenger_z": 1.64,
    }
    payload.update(overrides)
    return payload


class TestConfirmationBundleSettings:
    def test_default_is_unconfigured_and_nonactivating(self) -> None:
        settings = ConfirmationBundleSettings()
        assert settings.mode == ConfirmationBundleMode.OFF
        assert settings.top_n == 5
        assert settings.daily_bundle_cap == 0
        assert settings.daily_dollar_cap_microusd == 0
        assert settings.per_bundle_request_cap == 0
        assert settings.per_bundle_token_cap == 0
        assert settings.profile_revision is None
        assert settings.profile_checksum is None

    def test_default_json_contains_no_sentinel_profile(self) -> None:
        payload = ConfirmationBundleSettings().model_dump(mode="json")
        assert payload["profile_revision"] is None
        assert payload["profile_checksum"] is None
        assert "unconfigured" not in json.dumps(payload)
        assert "0000000000000000" not in json.dumps(payload)

    @pytest.mark.parametrize("mode", ["shadow", "enforce"])
    def test_active_modes_accept_complete_policy(self, mode: str) -> None:
        settings = ConfirmationBundleSettings.model_validate_json(
            json.dumps(active_settings(mode=mode))
        )
        assert settings.mode.value == mode
        assert settings.profile_checksum == _PROFILE_SHA

    def test_off_may_store_a_complete_frozen_profile(self) -> None:
        settings = ConfirmationBundleSettings.model_validate_json(
            json.dumps(
                active_settings(
                    mode="off",
                    daily_bundle_cap=0,
                    daily_dollar_cap_microusd=0,
                    per_bundle_request_cap=0,
                    per_bundle_token_cap=0,
                )
            )
        )
        assert settings.mode == ConfirmationBundleMode.OFF
        assert settings.profile_revision == "v9-launch-1"

    @pytest.mark.parametrize("mode", ["shadow", "enforce"])
    @pytest.mark.parametrize(
        "field",
        [
            "daily_bundle_cap",
            "daily_dollar_cap_microusd",
            "per_bundle_request_cap",
            "per_bundle_token_cap",
        ],
    )
    def test_active_mode_rejects_each_zero_cap(self, mode: str, field: str) -> None:
        with pytest.raises(ValidationError, match=field):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(mode=mode, **{field: 0}))
            )

    @pytest.mark.parametrize("mode", ["shadow", "enforce"])
    def test_active_mode_rejects_unconfigured_profile(self, mode: str) -> None:
        with pytest.raises(ValidationError, match="confirmation_profile"):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(
                    active_settings(
                        mode=mode,
                        profile_revision=None,
                        profile_checksum=None,
                    )
                )
            )

    @pytest.mark.parametrize(
        ("revision", "checksum"),
        [(None, _PROFILE_SHA), ("profile", None)],
    )
    def test_profile_identity_is_all_or_nothing(
        self, revision: str | None, checksum: str | None
    ) -> None:
        with pytest.raises(ValidationError, match="configured together"):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(
                    active_settings(
                        mode="off",
                        profile_revision=revision,
                        profile_checksum=checksum,
                    )
                )
            )

    @pytest.mark.parametrize(
        "checksum",
        ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + " "],
    )
    def test_profile_checksum_is_canonical_sha256(self, checksum: str) -> None:
        with pytest.raises(ValidationError, match="profile_checksum"):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(profile_checksum=checksum))
            )

    @pytest.mark.parametrize("top_n", [1, 5, MAX_CONFIRMATION_TOP_N])
    def test_top_n_accepts_every_boundary(self, top_n: int) -> None:
        assert (
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(top_n=top_n))
            ).top_n
            == top_n
        )

    @pytest.mark.parametrize("top_n", [0, MAX_CONFIRMATION_TOP_N + 1])
    def test_top_n_rejects_outside_hard_bounds(self, top_n: int) -> None:
        with pytest.raises(ValidationError, match="top_n"):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(top_n=top_n))
            )

    @pytest.mark.parametrize(
        ("field", "maximum"),
        [
            ("daily_bundle_cap", MAX_DAILY_BUNDLE_CAP),
            ("daily_dollar_cap_microusd", MAX_DAILY_DOLLAR_MICROUSD),
            ("per_bundle_request_cap", MAX_BUNDLE_REQUEST_CAP),
            ("per_bundle_token_cap", MAX_BUNDLE_TOKEN_CAP),
        ],
    )
    def test_caps_accept_hard_maxima(self, field: str, maximum: int) -> None:
        settings = ConfirmationBundleSettings.model_validate_json(
            json.dumps(active_settings(**{field: maximum}))
        )
        assert getattr(settings, field) == maximum

    @pytest.mark.parametrize(
        ("field", "maximum"),
        [
            ("daily_bundle_cap", MAX_DAILY_BUNDLE_CAP),
            ("daily_dollar_cap_microusd", MAX_DAILY_DOLLAR_MICROUSD),
            ("per_bundle_request_cap", MAX_BUNDLE_REQUEST_CAP),
            ("per_bundle_token_cap", MAX_BUNDLE_TOKEN_CAP),
        ],
    )
    def test_caps_reject_above_hard_maxima(self, field: str, maximum: int) -> None:
        with pytest.raises(ValidationError, match=field):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(**{field: maximum + 1}))
            )

    @pytest.mark.parametrize("challenger_z", [0.0, 1.64, 3.0])
    def test_uncertainty_width_accepts_bounds(self, challenger_z: float) -> None:
        assert (
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(challenger_z=challenger_z))
            ).challenger_z
            == challenger_z
        )

    @pytest.mark.parametrize("challenger_z", [-0.001, 3.001])
    def test_uncertainty_width_rejects_outside_bounds(
        self, challenger_z: float
    ) -> None:
        with pytest.raises(ValidationError, match="challenger_z"):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(challenger_z=challenger_z))
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("top_n", "5"),
            ("daily_bundle_cap", "10"),
            ("daily_dollar_cap_microusd", 1.5),
            ("per_bundle_request_cap", True),
            ("per_bundle_token_cap", "100"),
            ("challenger_z", "1.64"),
        ],
    )
    def test_numeric_controls_are_strict(self, field: str, value: object) -> None:
        with pytest.raises(ValidationError, match=field):
            ConfirmationBundleSettings.model_validate(active_settings(**{field: value}))

    def test_unknown_settings_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ConfirmationBundleSettings.model_validate_json(
                json.dumps(active_settings(seed_count=32))
            )

    def test_settings_are_frozen(self) -> None:
        settings = ConfirmationBundleSettings()
        with pytest.raises(ValidationError, match="frozen"):
            settings.mode = ConfirmationBundleMode.SHADOW


class TestAdminWriteContracts:
    def request_payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "scope": "*",
            "expected_revision": 0,
            "settings": active_settings(),
            "reason": "operator approved the bounded confirmation policy",
            "actor": "operator@example.com",
            "confirmation": "APPLY V9 CONFIRMATION MODE SHADOW",
        }
        payload.update(overrides)
        return payload

    def test_complete_settings_write_is_valid(self) -> None:
        request = AdminConfirmationBundleSettingsRequest.model_validate_json(
            json.dumps(self.request_payload())
        )
        assert request.expected_revision == 0
        assert request.settings.mode == ConfirmationBundleMode.SHADOW

    @pytest.mark.parametrize(
        "missing",
        ["expected_revision", "settings", "reason", "confirmation"],
    )
    def test_partial_settings_writes_are_rejected(self, missing: str) -> None:
        payload = self.request_payload()
        del payload[missing]
        with pytest.raises(ValidationError, match=missing):
            AdminConfirmationBundleSettingsRequest.model_validate_json(
                json.dumps(payload)
            )

    def test_negative_expected_revision_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="expected_revision"):
            AdminConfirmationBundleSettingsRequest.model_validate_json(
                json.dumps(self.request_payload(expected_revision=-1))
            )

    @pytest.mark.parametrize("reason", ["", "short", "       "])
    def test_short_reason_is_rejected(self, reason: str) -> None:
        with pytest.raises(ValidationError, match="reason"):
            AdminConfirmationBundleSettingsRequest.model_validate_json(
                json.dumps(self.request_payload(reason=reason))
            )

    def test_unknown_write_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            AdminConfirmationBundleSettingsRequest.model_validate_json(
                json.dumps(self.request_payload(patch=True))
            )

    def test_retest_authorization_is_strict_and_audited(self) -> None:
        request_id = uuid4()
        request = AdminConfirmationBundleRetestRequest.model_validate_json(
            json.dumps(
                {
                    "request_id": str(request_id),
                    "expected_generation": 2,
                    "reason": "operator approved fresh confirmation evidence",
                    "actor": "operator@example.com",
                    "confirmation": "AUTHORIZE CONFIRMATION BUNDLE RETEST",
                }
            )
        )
        assert request.request_id == request_id
        assert request.expected_generation == 2

    @pytest.mark.parametrize("generation", [-1, "0", True])
    def test_retest_generation_rejects_invalid_values(self, generation: object) -> None:
        with pytest.raises(ValidationError, match="expected_generation"):
            AdminConfirmationBundleRetestRequest.model_validate(
                {
                    "request_id": uuid4(),
                    "expected_generation": generation,
                    "reason": "operator approved fresh confirmation evidence",
                    "actor": "operator@example.com",
                    "confirmation": "AUTHORIZE CONFIRMATION BUNDLE RETEST",
                }
            )


class TestTypedEvidenceWireModels:
    def test_completion_report_round_trips_as_json(self) -> None:
        report = unsigned_report()
        parsed = ConfirmationCompletionReport.model_validate_json(
            report.model_dump_json()
        )
        assert parsed == report
        assert [
            lane.lane for lane in parsed.longmemeval.evidence.provider_evidence
        ] == ["judge", "reader"]

    def test_completion_has_one_signature_and_no_caller_composite_or_root_digest(
        self,
    ) -> None:
        fields = ConfirmationCompletionReport.model_fields
        assert set(fields) == {
            "ablation_coordinator_latency_ms",
            "longmemeval",
            "inference_ablation",
            "embedding_ablation",
            "bundle_signature",
        }
        assert "full_composite" not in fields
        assert "evidence_sha256" not in fields

    @pytest.mark.parametrize("extra", ["full_composite", "evidence_sha256", "metadata"])
    def test_opaque_or_authoritative_top_level_fields_are_rejected(
        self, extra: str
    ) -> None:
        payload = unsigned_report().model_dump(mode="json")
        payload[extra] = 0
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ConfirmationCompletionReport.model_validate(payload)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("fallback_used", True),
            ("cost_source", "price_estimate"),
            ("currency", "EUR"),
            ("requests", 0),
            ("successes", 0),
            ("receipted_requests", 0),
            ("total_tokens", 999),
            ("receipt_set_sha256", "A" * 64),
        ],
    )
    def test_provider_lane_rejects_untrusted_accounting(
        self, field: str, value: object
    ) -> None:
        payload = (
            longmem_envelope().evidence.provider_evidence[0].model_dump(mode="json")
        )
        payload[field] = value
        with pytest.raises(ValidationError):
            LongMemProviderLaneEvidence.model_validate(payload)

    @pytest.mark.parametrize("intervention", ["inference", "embedding"])
    def test_ablation_has_no_provider_or_model_surface(
        self, intervention: Literal["inference", "embedding"]
    ) -> None:
        evidence = ablation_envelope(intervention).evidence
        assert "provider" not in type(evidence).model_fields
        assert "model" not in type(evidence).model_fields
        assert evidence.synthetic_usage.upstream_requests == 0
        assert evidence.synthetic_usage.upstream_provider_cost_microusd == 0

    @pytest.mark.parametrize(
        "field,value",
        [
            ("request_count", 1),
            ("input_tokens", 1),
            ("output_tokens", 1),
            ("provider_cost_microusd", 1),
            ("synthetic", False),
        ],
    )
    def test_ablation_envelope_cannot_claim_upstream_usage(
        self, field: str, value: object
    ) -> None:
        payload = ablation_envelope("inference").model_dump(mode="json")
        payload[field] = value
        with pytest.raises(ValidationError, match=field):
            type(ablation_envelope("inference")).model_validate(payload)

    @pytest.mark.parametrize("signature", ["", "0", "zz", "AA", "a" * 513])
    def test_bundle_signature_is_canonical_bounded_hex(self, signature: str) -> None:
        payload = unsigned_report().model_dump(mode="json")
        payload["bundle_signature"] = signature
        with pytest.raises(ValidationError, match="bundle_signature"):
            ConfirmationCompletionReport.model_validate(payload)

    def test_unknown_nested_metadata_is_rejected(self) -> None:
        payload = unsigned_report().model_dump(mode="json")
        payload["longmemeval"]["evidence"]["metadata"] = {"trust_me": True}
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ConfirmationCompletionReport.model_validate(payload)


class TestBudgetWireModel:
    def test_budget_committed_cost_includes_reserved_and_settled(self) -> None:
        budget = ConfirmationDailyBudgetView(
            utc_day=date(2026, 8, 8),
            revision=3,
            issued_attempts=2,
            outstanding_reserved_microusd=25_000,
            settled_microusd=75_000,
        )
        assert budget.committed_microusd == 100_000

    def test_shadow_calibration_preserves_measured_cost_formula(self) -> None:
        calibration = ConfirmationShadowCalibrationView(
            observed_from_utc_day=date(2026, 8, 1),
            observed_through_utc_day=date(2026, 8, 8),
            observation_days=8,
            confirmation_profile_revision="confirmation-v9-test-1",
            confirmation_profile_checksum=_PROFILE_SHA,
            base_run_count=40,
            measured_base_cost_microusd=130_000,
            confirmation_bundle_count=10,
            measured_bundle_cost_microusd=60_000,
            completed_bundle_count=8,
            qualified_bundle_count=2,
            promotion_rate_bps=2_500,
            projected_daily_spend_microusd=725_000,
            epoch_duration_seconds=None,
            projected_epoch_spend_microusd=None,
            epoch_projection_unavailable_reason="epoch duration is not configured",
        )
        assert calibration.measured_base_cost_microusd == 130_000
        assert calibration.measured_bundle_cost_microusd == 60_000
        assert calibration.promotion_rate_bps == 2_500

    @pytest.mark.parametrize(
        "overrides",
        [
            {"base_run_count": 0},
            {"observation_days": 7},
            {"qualified_bundle_count": 9},
            {"confirmation_profile_checksum": None},
            {"epoch_projection_unavailable_reason": None},
        ],
    )
    def test_shadow_calibration_rejects_inconsistent_aggregates(
        self, overrides: dict[str, object]
    ) -> None:
        payload: dict[str, object] = {
            "observed_from_utc_day": date(2026, 8, 1),
            "observed_through_utc_day": date(2026, 8, 8),
            "observation_days": 8,
            "confirmation_profile_revision": "confirmation-v9-test-1",
            "confirmation_profile_checksum": _PROFILE_SHA,
            "base_run_count": 40,
            "measured_base_cost_microusd": 130_000,
            "confirmation_bundle_count": 10,
            "measured_bundle_cost_microusd": 60_000,
            "completed_bundle_count": 8,
            "qualified_bundle_count": 2,
            "promotion_rate_bps": 2_500,
            "projected_daily_spend_microusd": 725_000,
            "epoch_duration_seconds": None,
            "projected_epoch_spend_microusd": None,
            "epoch_projection_unavailable_reason": "epoch duration is not configured",
        }
        payload.update(overrides)
        with pytest.raises(ValidationError):
            ConfirmationShadowCalibrationView.model_validate(payload)
