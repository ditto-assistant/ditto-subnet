from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from ditto.api_server.confirmation_bundles import (
    MAX_TOP_N,
    CheckedSettings,
    ConfirmationBundleKey,
    ConfirmationBundleSettings,
    ConfirmationCandidate,
    ConfirmationEligibilityMode,
    ConfirmationMode,
    ConfirmationPolicyError,
    ConfirmationProfileRef,
    ConfirmationState,
    authorize_retest,
    reusable_completed_evidence,
    settings_checksum,
    transition_confirmation_state,
)

_PROFILE = ConfirmationProfileRef(revision="launch-v1", checksum="a" * 64)
_T0 = datetime(2026, 8, 8, 12, tzinfo=UTC)


def settings(**overrides: object) -> ConfirmationBundleSettings:
    values: dict[str, object] = {
        "mode": ConfirmationMode.SHADOW,
        "eligibility_mode": ConfirmationEligibilityMode.RANK,
        "top_n": 5,
        "min_base_score_micros": 950_000,
        "daily_bundle_cap": 20,
        "daily_dollar_cap_microusd": 5_000_000,
        "per_bundle_request_cap": 100,
        "per_bundle_token_cap": 1_000_000,
        "profile": _PROFILE,
        "challenger_z": 1.64,
    }
    values.update(overrides)
    return ConfirmationBundleSettings(**values)  # type: ignore[arg-type]


def bundle_key(
    *,
    digest: str = "b" * 64,
    version: int = 9,
    profile_revision: str = "launch-v1",
    profile_checksum: str = "a" * 64,
    generation: int = 0,
) -> ConfirmationBundleKey:
    return ConfirmationBundleKey(
        artifact_sha256=digest,
        bench_version=version,
        profile_revision=profile_revision,
        profile_checksum=profile_checksum,
        retest_generation=generation,
    )


class TestProfileValidation:
    def test_accepts_valid_profile(self) -> None:
        assert _PROFILE.revision == "launch-v1"
        assert _PROFILE.checksum == "a" * 64

    @pytest.mark.parametrize("revision", ["", "x" * 129])
    def test_rejects_invalid_revision(self, revision: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="profile revision"):
            ConfirmationProfileRef(revision=revision, checksum="a" * 64)

    @pytest.mark.parametrize(
        "checksum",
        ["", "a" * 63, "a" * 65, "A" * 64, "z" * 64, "a" * 63 + "\n"],
    )
    def test_rejects_invalid_checksum(self, checksum: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="profile checksum"):
            ConfirmationProfileRef(revision="v1", checksum=checksum)

    def test_profile_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            _PROFILE.revision = "changed"  # type: ignore[misc]


class TestSettingsValidation:
    def test_valid_shadow_settings(self) -> None:
        value = settings()
        assert value.mode == ConfirmationMode.SHADOW
        assert value.eligibility_mode == ConfirmationEligibilityMode.RANK
        assert value.top_n == 5
        assert value.min_base_score_micros == 950_000

    def test_valid_enforce_settings(self) -> None:
        value = settings(mode=ConfirmationMode.ENFORCE)
        assert value.mode == ConfirmationMode.ENFORCE

    def test_off_allows_zero_daily_caps(self) -> None:
        value = ConfirmationBundleSettings()
        assert value.mode == ConfirmationMode.OFF
        assert value.daily_bundle_cap == 0
        assert value.daily_dollar_cap_microusd == 0

    @pytest.mark.parametrize(
        "mode", [ConfirmationMode.SHADOW, ConfirmationMode.ENFORCE]
    )
    @pytest.mark.parametrize(
        "field,value",
        [("daily_bundle_cap", 0), ("daily_dollar_cap_microusd", 0)],
    )
    def test_active_mode_requires_positive_daily_caps(
        self, mode: ConfirmationMode, field: str, value: int
    ) -> None:
        with pytest.raises(ConfirmationPolicyError, match="positive daily"):
            settings(mode=mode, **{field: value})

    @pytest.mark.parametrize("mode", ["off", "shadow", "enforce", None, True])
    def test_requires_mode_enum(self, mode: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="ConfirmationMode"):
            settings(mode=mode)

    @pytest.mark.parametrize("top_n", [1, 5, MAX_TOP_N])
    def test_accepts_top_n_bounds(self, top_n: int) -> None:
        assert settings(top_n=top_n).top_n == top_n

    @pytest.mark.parametrize("top_n", [0, -1, MAX_TOP_N + 1, True, 5.0])
    def test_rejects_top_n_outside_hard_bounds(self, top_n: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="top_n"):
            settings(top_n=top_n)

    @pytest.mark.parametrize(
        "mode",
        [ConfirmationEligibilityMode.RANK, ConfirmationEligibilityMode.SCORE_THRESHOLD],
    )
    def test_accepts_eligibility_modes(self, mode: ConfirmationEligibilityMode) -> None:
        assert settings(eligibility_mode=mode).eligibility_mode == mode

    @pytest.mark.parametrize("mode", ["rank", "score_threshold", None, True])
    def test_requires_eligibility_mode_enum(self, mode: object) -> None:
        with pytest.raises(
            ConfirmationPolicyError, match="ConfirmationEligibilityMode"
        ):
            settings(eligibility_mode=mode)

    @pytest.mark.parametrize("threshold", [0, 950_000, 1_000_000])
    def test_accepts_min_base_score_bounds(self, threshold: int) -> None:
        assert (
            settings(min_base_score_micros=threshold).min_base_score_micros == threshold
        )

    @pytest.mark.parametrize("threshold", [-1, 1_000_001, True, 0.95])
    def test_rejects_min_base_score_outside_bounds(self, threshold: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="min_base_score_micros"):
            settings(min_base_score_micros=threshold)

    @pytest.mark.parametrize("cap", [1, 20, 1_000])
    def test_accepts_daily_bundle_cap_bounds(self, cap: int) -> None:
        assert settings(daily_bundle_cap=cap).daily_bundle_cap == cap

    @pytest.mark.parametrize("cap", [-1, 1_001, True, 1.0])
    def test_rejects_daily_bundle_cap_outside_bounds(self, cap: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="daily_bundle_cap"):
            settings(daily_bundle_cap=cap)

    @pytest.mark.parametrize("cap", [1, 5_000_000, 1_000_000_000, 2_000_000_000])
    def test_accepts_daily_dollar_cap_bounds(self, cap: int) -> None:
        assert settings(daily_dollar_cap_microusd=cap).daily_dollar_cap_microusd == cap

    @pytest.mark.parametrize("cap", [-1, 2_000_000_001, True, 1.0])
    def test_rejects_daily_dollar_cap_outside_bounds(self, cap: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="daily_dollar"):
            settings(daily_dollar_cap_microusd=cap)

    @pytest.mark.parametrize("cap", [1, 100, 100_000])
    def test_accepts_per_bundle_request_cap_bounds(self, cap: int) -> None:
        assert settings(per_bundle_request_cap=cap).per_bundle_request_cap == cap

    @pytest.mark.parametrize("cap", [0, -1, 100_001, True, 1.0])
    def test_rejects_request_cap_outside_bounds(self, cap: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="per_bundle_request_cap"):
            settings(per_bundle_request_cap=cap)

    @pytest.mark.parametrize("cap", [1, 1_000_000, 100_000_000])
    def test_accepts_per_bundle_token_cap_bounds(self, cap: int) -> None:
        assert settings(per_bundle_token_cap=cap).per_bundle_token_cap == cap

    @pytest.mark.parametrize("cap", [0, -1, 100_000_001, True, 1.0])
    def test_rejects_token_cap_outside_bounds(self, cap: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="per_bundle_token_cap"):
            settings(per_bundle_token_cap=cap)

    @pytest.mark.parametrize("z", [0.0, 1.64, 3.0])
    def test_accepts_challenger_z_bounds(self, z: float) -> None:
        assert settings(challenger_z=z).challenger_z == z

    @pytest.mark.parametrize("z", [-0.01, 3.01, float("nan"), float("inf"), True])
    def test_rejects_invalid_challenger_z(self, z: float) -> None:
        with pytest.raises(ConfirmationPolicyError, match="challenger_z"):
            settings(challenger_z=z)

    def test_settings_are_frozen(self) -> None:
        value = settings()
        with pytest.raises(FrozenInstanceError):
            value.top_n = 6  # type: ignore[misc]


class TestSettingsChecksum:
    def test_checksum_is_lowercase_sha256(self) -> None:
        checksum = settings_checksum(settings())
        assert len(checksum) == 64
        assert checksum == checksum.lower()
        int(checksum, 16)

    def test_property_matches_function(self) -> None:
        value = settings()
        assert value.checksum == settings_checksum(value)

    def test_checksum_is_deterministic(self) -> None:
        assert settings_checksum(settings()) == settings_checksum(settings())

    @pytest.mark.parametrize(
        "field,value",
        [
            ("mode", ConfirmationMode.ENFORCE),
            ("eligibility_mode", ConfirmationEligibilityMode.SCORE_THRESHOLD),
            ("top_n", 6),
            ("min_base_score_micros", 900_000),
            ("daily_bundle_cap", 21),
            ("daily_dollar_cap_microusd", 5_000_001),
            ("per_bundle_request_cap", 101),
            ("per_bundle_token_cap", 1_000_001),
            ("challenger_z", 1.65),
            (
                "profile",
                ConfirmationProfileRef(revision="launch-v2", checksum="b" * 64),
            ),
        ],
    )
    def test_every_policy_field_changes_checksum(
        self, field: str, value: object
    ) -> None:
        baseline = settings()
        changed = replace(baseline, **cast(Any, {field: value}))
        assert settings_checksum(changed) != settings_checksum(baseline)

    def test_checked_settings_factory_accepts_exact_checksum(self) -> None:
        value = settings()
        checked = CheckedSettings.create(value)
        assert checked.settings == value
        assert checked.checksum == value.checksum

    def test_checked_settings_rejects_payload_mismatch(self) -> None:
        first = settings(top_n=5)
        second = settings(top_n=6)
        with pytest.raises(ConfirmationPolicyError, match="does not match"):
            CheckedSettings(settings=second, checksum=first.checksum)

    @pytest.mark.parametrize("checksum", ["", "a" * 63, "A" * 64, "x" * 64])
    def test_checked_settings_rejects_malformed_checksum(self, checksum: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="settings checksum"):
            CheckedSettings(settings=settings(), checksum=checksum)

    def test_checked_settings_are_frozen(self) -> None:
        checked = CheckedSettings.create(settings())
        with pytest.raises(FrozenInstanceError):
            checked.checksum = "b" * 64  # type: ignore[misc]


class TestBundleKey:
    def test_valid_key(self) -> None:
        key = bundle_key()
        assert key.retest_generation == 0
        assert key.bench_version == 9

    def test_key_is_frozen(self) -> None:
        key = bundle_key()
        with pytest.raises(FrozenInstanceError):
            key.retest_generation = 1  # type: ignore[misc]

    @pytest.mark.parametrize("digest", ["", "b" * 63, "B" * 64, "x" * 64, "b" * 65])
    def test_rejects_invalid_artifact_digest(self, digest: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="artifact sha256"):
            bundle_key(digest=digest)

    @pytest.mark.parametrize("checksum", ["", "a" * 63, "A" * 64, "x" * 64])
    def test_rejects_invalid_profile_checksum(self, checksum: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="profile checksum"):
            bundle_key(profile_checksum=checksum)

    @pytest.mark.parametrize("version", [0, -1, True, 1_000_001])
    def test_rejects_invalid_version(self, version: int) -> None:
        with pytest.raises(ConfirmationPolicyError, match="bench_version"):
            bundle_key(version=version)

    @pytest.mark.parametrize("generation", [-1, True, 1_000_001])
    def test_rejects_invalid_generation(self, generation: int) -> None:
        with pytest.raises(ConfirmationPolicyError, match="retest_generation"):
            bundle_key(generation=generation)

    @pytest.mark.parametrize("revision", ["", "x" * 129])
    def test_rejects_invalid_profile_revision(self, revision: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="profile revision"):
            bundle_key(profile_revision=revision)

    def test_for_candidate_ignores_agent_name_and_owner(self) -> None:
        a = ConfirmationCandidate(
            agent_id="one",
            owner_id="owner-a",
            artifact_sha256="d" * 64,
            bench_version=9,
            base_composite=0.8,
            base_stderr=0.01,
            first_seen=_T0,
        )
        b = ConfirmationCandidate(
            agent_id="renamed",
            owner_id="owner-b",
            artifact_sha256="d" * 64,
            bench_version=9,
            base_composite=0.8,
            base_stderr=0.01,
            first_seen=_T0,
        )
        assert ConfirmationBundleKey.for_candidate(
            a, _PROFILE
        ) == ConfirmationBundleKey.for_candidate(b, _PROFILE)

    def test_for_candidate_changes_with_artifact(self) -> None:
        a = ConfirmationCandidate(
            agent_id="one",
            owner_id="owner",
            artifact_sha256="d" * 64,
            bench_version=9,
            base_composite=0.8,
            base_stderr=0.01,
            first_seen=_T0,
        )
        b = replace(a, agent_id="two", artifact_sha256="e" * 64)
        assert ConfirmationBundleKey.for_candidate(
            a, _PROFILE
        ) != ConfirmationBundleKey.for_candidate(b, _PROFILE)

    def test_for_candidate_changes_with_profile_revision(self) -> None:
        candidate = ConfirmationCandidate(
            agent_id="one",
            owner_id="owner",
            artifact_sha256="d" * 64,
            bench_version=9,
            base_composite=0.8,
            base_stderr=0.01,
            first_seen=_T0,
        )
        v2 = ConfirmationProfileRef(revision="launch-v2", checksum="a" * 64)
        assert ConfirmationBundleKey.for_candidate(
            candidate, _PROFILE
        ) != ConfirmationBundleKey.for_candidate(candidate, v2)

    def test_for_candidate_changes_with_profile_checksum(self) -> None:
        candidate = ConfirmationCandidate(
            agent_id="one",
            owner_id="owner",
            artifact_sha256="d" * 64,
            bench_version=9,
            base_composite=0.8,
            base_stderr=0.01,
            first_seen=_T0,
        )
        changed = ConfirmationProfileRef(revision="launch-v1", checksum="b" * 64)
        assert ConfirmationBundleKey.for_candidate(
            candidate, _PROFILE
        ) != ConfirmationBundleKey.for_candidate(candidate, changed)


class TestEvidenceReuse:
    def test_exact_completed_key_is_reusable(self) -> None:
        key = bundle_key()
        assert reusable_completed_evidence(key, ConfirmationState.COMPLETED, key)

    @pytest.mark.parametrize(
        "state",
        [
            ConfirmationState.BASE_ONLY,
            ConfirmationState.BLOCKED_BUDGET,
            ConfirmationState.PENDING,
            ConfirmationState.LEASED,
            ConfirmationState.FAILED,
            ConfirmationState.SUPERSEDED,
        ],
    )
    def test_noncompleted_evidence_is_never_reusable(
        self, state: ConfirmationState
    ) -> None:
        key = bundle_key()
        assert not reusable_completed_evidence(key, state, key)

    @pytest.mark.parametrize(
        "changed",
        [
            bundle_key(digest="c" * 64),
            bundle_key(version=10),
            bundle_key(profile_revision="launch-v2"),
            bundle_key(profile_checksum="b" * 64),
            bundle_key(generation=1),
        ],
    )
    def test_any_identity_change_prevents_reuse(
        self, changed: ConfirmationBundleKey
    ) -> None:
        assert not reusable_completed_evidence(
            bundle_key(), ConfirmationState.COMPLETED, changed
        )

    def test_renamed_submission_reuses_same_digest_profile(self) -> None:
        source = bundle_key()
        renamed = bundle_key()
        assert source == renamed
        assert reusable_completed_evidence(source, ConfirmationState.COMPLETED, renamed)


class TestRetestGeneration:
    def test_operator_authorized_retest_increments_generation(self) -> None:
        original = bundle_key(generation=2)
        retest = authorize_retest(
            original, ConfirmationState.COMPLETED, operator_authorized=True
        )
        assert retest.retest_generation == 3
        assert retest.artifact_sha256 == original.artifact_sha256
        assert retest.profile_checksum == original.profile_checksum
        assert original.retest_generation == 2

    def test_retest_generation_is_not_reusable_as_original(self) -> None:
        original = bundle_key()
        retest = authorize_retest(
            original, ConfirmationState.COMPLETED, operator_authorized=True
        )
        assert not reusable_completed_evidence(
            original, ConfirmationState.COMPLETED, retest
        )

    def test_retest_requires_operator_authorization(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="requires operator"):
            authorize_retest(
                bundle_key(),
                ConfirmationState.COMPLETED,
                operator_authorized=False,
            )

    @pytest.mark.parametrize(
        "state",
        [
            ConfirmationState.BASE_ONLY,
            ConfirmationState.BLOCKED_BUDGET,
            ConfirmationState.PENDING,
            ConfirmationState.LEASED,
            ConfirmationState.FAILED,
            ConfirmationState.SUPERSEDED,
        ],
    )
    def test_retest_requires_completed_evidence(self, state: ConfirmationState) -> None:
        with pytest.raises(ConfirmationPolicyError, match="only completed"):
            authorize_retest(bundle_key(), state, operator_authorized=True)


class TestStateTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            (ConfirmationState.BASE_ONLY, ConfirmationState.PENDING),
            (ConfirmationState.BASE_ONLY, ConfirmationState.BLOCKED_BUDGET),
            (ConfirmationState.BLOCKED_BUDGET, ConfirmationState.PENDING),
            (ConfirmationState.PENDING, ConfirmationState.LEASED),
            (ConfirmationState.PENDING, ConfirmationState.BLOCKED_BUDGET),
            (ConfirmationState.LEASED, ConfirmationState.COMPLETED),
            (ConfirmationState.LEASED, ConfirmationState.FAILED),
            (ConfirmationState.FAILED, ConfirmationState.PENDING),
            (ConfirmationState.COMPLETED, ConfirmationState.SUPERSEDED),
        ],
    )
    def test_all_valid_transitions(
        self, current: ConfirmationState, target: ConfirmationState
    ) -> None:
        assert transition_confirmation_state(current, target) == target

    @pytest.mark.parametrize("state", list(ConfirmationState))
    def test_self_transition_is_invalid(self, state: ConfirmationState) -> None:
        with pytest.raises(ConfirmationPolicyError, match="invalid confirmation"):
            transition_confirmation_state(state, state)

    @pytest.mark.parametrize(
        "current,target",
        [
            (ConfirmationState.BASE_ONLY, ConfirmationState.COMPLETED),
            (ConfirmationState.BASE_ONLY, ConfirmationState.LEASED),
            (ConfirmationState.BLOCKED_BUDGET, ConfirmationState.LEASED),
            (ConfirmationState.PENDING, ConfirmationState.COMPLETED),
            (ConfirmationState.LEASED, ConfirmationState.BLOCKED_BUDGET),
            (ConfirmationState.FAILED, ConfirmationState.COMPLETED),
            (ConfirmationState.COMPLETED, ConfirmationState.PENDING),
            (ConfirmationState.COMPLETED, ConfirmationState.FAILED),
            (ConfirmationState.SUPERSEDED, ConfirmationState.PENDING),
            (ConfirmationState.SUPERSEDED, ConfirmationState.COMPLETED),
        ],
    )
    def test_invalid_shortcuts_and_revivals(
        self, current: ConfirmationState, target: ConfirmationState
    ) -> None:
        with pytest.raises(ConfirmationPolicyError, match="invalid confirmation"):
            transition_confirmation_state(current, target)

    def test_normal_success_path(self) -> None:
        state = ConfirmationState.BASE_ONLY
        for target in (
            ConfirmationState.PENDING,
            ConfirmationState.LEASED,
            ConfirmationState.COMPLETED,
            ConfirmationState.SUPERSEDED,
        ):
            state = transition_confirmation_state(state, target)
        assert state == ConfirmationState.SUPERSEDED

    def test_budget_block_can_resume_without_fabricating_lease(self) -> None:
        state = transition_confirmation_state(
            ConfirmationState.BASE_ONLY, ConfirmationState.BLOCKED_BUDGET
        )
        state = transition_confirmation_state(state, ConfirmationState.PENDING)
        state = transition_confirmation_state(state, ConfirmationState.LEASED)
        assert state == ConfirmationState.LEASED

    def test_failed_attempt_retries_through_pending(self) -> None:
        state = ConfirmationState.PENDING
        state = transition_confirmation_state(state, ConfirmationState.LEASED)
        state = transition_confirmation_state(state, ConfirmationState.FAILED)
        state = transition_confirmation_state(state, ConfirmationState.PENDING)
        state = transition_confirmation_state(state, ConfirmationState.LEASED)
        state = transition_confirmation_state(state, ConfirmationState.COMPLETED)
        assert state == ConfirmationState.COMPLETED
