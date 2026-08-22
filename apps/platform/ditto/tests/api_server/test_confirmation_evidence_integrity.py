"""Hostile replay tests for the server-authoritative v9 evidence root."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4

import pytest

from ditto.api_models.confirmation_bundles import (
    ConfirmationBundleMode,
    ConfirmationCompletionReport,
)
from ditto.api_server.confirmation_evidence import (
    AblationVerificationPolicy,
    CompositeVerificationPolicy,
    ConfirmationEvidenceError,
    ConfirmationVerificationProfile,
    ProviderLanePolicy,
    SyntheticBudgetPolicy,
    _validate_longmem,
    compute_subject_projection,
    confirmation_signing_message,
    evidence_digest,
    rebuild_confirmation_evidence,
)
from ditto.tests.confirmation_evidence_fixtures import (
    ARTIFACT_SHA256,
    ablation_envelope,
    longmem_envelope,
    unsigned_report,
    verification_profile,
)
from ditto_screening_protocol.confirmation_wire import longmem_envelope_from_go

_SETTINGS_SHA256 = "9" * 64


class SigningValues(TypedDict):
    reporter_hotkey: str
    bundle_id: UUID
    ticket_id: UUID
    deadline: datetime
    artifact_sha256: str
    profile_revision: str
    profile_checksum: str
    settings_revision: int
    settings_checksum: str
    retest_generation: int
    evidence_sha256: str


def rebuild(
    report: ConfirmationCompletionReport | None = None,
    *,
    mode: ConfirmationBundleMode = ConfirmationBundleMode.SHADOW,
    profile: ConfirmationVerificationProfile | None = None,
    artifact_sha256: str = ARTIFACT_SHA256,
    profile_revision: str | None = None,
    profile_checksum: str | None = None,
    settings_revision: int = 17,
    settings_checksum: str = _SETTINGS_SHA256,
    generation: int = 0,
):
    frozen = profile or verification_profile()
    return rebuild_confirmation_evidence(
        report or unsigned_report(artifact_sha256=artifact_sha256, mode=mode),
        artifact_sha256=artifact_sha256,
        profile_revision=profile_revision or frozen.revision,
        profile_checksum=profile_checksum or frozen.checksum(),
        settings_revision=settings_revision,
        settings_checksum=settings_checksum,
        retest_generation=generation,
        mode=mode,
        profile=frozen,
    )


class TestFrozenProfile:
    def test_checksum_is_order_independent_for_provider_lanes(self) -> None:
        profile = verification_profile()
        assert (
            replace(
                profile, provider_lanes=tuple(reversed(profile.provider_lanes))
            ).checksum()
            == profile.checksum()
        )

    def test_projection_key_is_outer_provenance_not_longmem_profile_identity(
        self,
    ) -> None:
        profile = verification_profile()
        changed = replace(profile, longmem_projection_key_sha256="7" * 64)
        assert changed.longmem_checksum() == profile.longmem_checksum()
        assert changed.checksum() != profile.checksum()

    def test_checksum_binds_every_profile_component(self) -> None:
        profile = verification_profile()
        mutations = (
            replace(profile, revision="confirmation-v9-test-2"),
            replace(profile, longmem_profile_revision="longmem-v9-test-2"),
            replace(profile, longmem_profile_checksum="0" * 64),
            replace(profile, longmem_dataset_revision="dataset-2"),
            replace(profile, longmem_dataset_sha256="0" * 64),
            replace(profile, longmem_selector_revision="selector-v2"),
            replace(profile, longmem_selection_seed=388),
            replace(profile, longmem_cases_per_capability=3),
            replace(profile, longmem_seed_batch_pairs=32),
            replace(profile, longmem_projection_key_sha256="7" * 64),
            replace(
                profile,
                provider_lanes=(
                    replace(profile.provider_lanes[0], model="fallback/model"),
                    profile.provider_lanes[1],
                ),
            ),
            replace(
                profile,
                ablation_coordinator_policy=replace(
                    profile.ablation_coordinator_policy,
                    total_timeout_milliseconds=6_000,
                ),
            ),
            replace(profile, ablation_profile_revision="ablation-v9-test-2"),
            replace(profile, ablation_profile_checksum="0" * 64),
            replace(profile, ablation_dataset_sha256="0" * 64),
            replace(profile, ablation_threshold_manifest_sha256="0" * 64),
            replace(profile, ablation_selection_key_sha256="0" * 64),
            replace(profile, ablation_projection_key_sha256="8" * 64),
            replace(
                profile,
                embedding_ablation=replace(
                    profile.embedding_ablation, threshold_micros=300_000
                ),
            ),
            replace(
                profile,
                composite=replace(profile.composite, base_weight_bps=5_000),
            ),
        )
        for changed in mutations:
            if changed.composite.base_weight_bps + changed.composite.longmem_weight_bps:
                try:
                    checksum = changed.checksum()
                except ConfirmationEvidenceError:
                    continue
                assert checksum != profile.checksum()

    @pytest.mark.parametrize(
        "mutate,match",
        [
            (
                lambda p: replace(p, schema_version=2),
                "profile schema",
            ),
            (
                lambda p: replace(p, provider_lanes=()),
                "unconfigured",
            ),
            (
                lambda p: replace(p, longmem_projection_key_sha256="A" * 64),
                "projection key checksum",
            ),
            (
                lambda p: replace(
                    p, provider_lanes=(p.provider_lanes[0], p.provider_lanes[0])
                ),
                "duplicate",
            ),
            (
                lambda p: replace(
                    p,
                    provider_lanes=(
                        replace(p.provider_lanes[0], max_requests=0),
                        p.provider_lanes[1],
                    ),
                ),
                "positive caps",
            ),
            (
                lambda p: replace(
                    p,
                    composite=replace(p.composite, base_weight_bps=0),
                ),
                "positive",
            ),
            (
                lambda p: replace(
                    p,
                    composite=replace(p.composite, base_weight_bps=5_000),
                ),
                "sum",
            ),
        ],
    )
    def test_invalid_profiles_fail_closed(self, mutate, match: str) -> None:
        with pytest.raises(ConfirmationEvidenceError, match=match):
            mutate(verification_profile()).checksum()

    @pytest.mark.parametrize(
        "intervention,budget",
        [
            (
                "inference",
                SyntheticBudgetPolicy(8, 4096, 1, 0, 0),
            ),
            (
                "embedding",
                SyntheticBudgetPolicy(1, 0, 8, 16, 4096),
            ),
        ],
    )
    def test_ablation_budgets_cannot_mix_intervention_lanes(
        self,
        intervention: Literal["inference", "embedding"],
        budget: SyntheticBudgetPolicy,
    ) -> None:
        profile = verification_profile()
        policy = (
            profile.inference_ablation
            if intervention == "inference"
            else profile.embedding_ablation
        )
        if intervention == "inference":
            changed = replace(
                profile, inference_ablation=replace(policy, budget=budget)
            )
        else:
            changed = replace(
                profile, embedding_ablation=replace(policy, budget=budget)
            )
        with pytest.raises(ConfirmationEvidenceError, match="budget"):
            changed.checksum()


class TestLongMemReplay:
    def test_platform_accepts_real_go_official_zero_without_synthetic_receipts(
        self,
    ) -> None:
        path = (
            Path(__file__).resolve().parents[5]
            / "services"
            / "dittobench-api"
            / "internal"
            / "longmemeval"
            / "testdata"
            / "go_longmem_official_zero_v2.json"
        )
        envelope = longmem_envelope_from_go(json.loads(path.read_text()))
        base = verification_profile()
        lanes = tuple(
            sorted(envelope.evidence.provider_evidence, key=lambda row: row.lane)
        )
        policies = tuple(sorted(base.provider_lanes, key=lambda row: row.lane))
        profile = replace(
            base,
            longmem_profile_checksum=envelope.evidence.profile_checksum,
            longmem_dataset_revision=envelope.evidence.dataset_revision,
            longmem_dataset_sha256=envelope.evidence.dataset_sha256,
            provider_lanes=tuple(
                replace(
                    policy,
                    provider=row.provider,
                    profile_revision=row.profile_revision,
                    model=row.model,
                )
                for row, policy in zip(lanes, policies, strict=True)
            ),
        )

        verified = _validate_longmem(
            envelope,
            artifact_sha256=envelope.evidence.artifact_sha256,
            profile=profile,
        )
        assert verified.request_count == 0
        assert verified.provider_cost_microusd == 0
        assert all(
            row.receipt_set_sha256 == "" for row in verified.evidence.provider_evidence
        )

    def test_platform_accepts_exact_unused_reader_judged_official_zero(
        self,
    ) -> None:
        base = longmem_envelope()
        score = base.evidence.score.model_copy(
            update={
                "longmem_mean_micros": 0,
                "longmem_stderr_micros": 0,
                "per_capability": [
                    row.model_copy(update={"correct": 0, "mean_micros": 0})
                    for row in base.evidence.score.per_capability
                ],
            }
        )
        lanes = []
        for row in base.evidence.provider_evidence:
            if row.lane == "reader":
                lanes.append(
                    row.model_copy(
                        update={
                            "requests": 0,
                            "successes": 0,
                            "receipted_requests": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "cost_usd_micros": 0,
                            "receipt_set_sha256": "",
                        }
                    )
                )
            else:
                lanes.append(
                    row.model_copy(
                        update={
                            "requests": 12,
                            "successes": 12,
                            "receipted_requests": 12,
                        }
                    )
                )
        evidence = base.evidence.model_copy(
            update={"score": score, "provider_evidence": lanes}
        )
        envelope = base.model_copy(
            update={
                "evidence": evidence,
                "evidence_sha256": evidence_digest(evidence),
                "request_count": 12,
                "input_tokens": 50,
                "output_tokens": 10,
                "provider_cost_microusd": 5_000,
            }
        )
        profile = verification_profile()
        profile = replace(
            profile,
            provider_lanes=tuple(
                replace(row, max_requests=12) if row.lane == "judge" else row
                for row in profile.provider_lanes
            ),
        )
        profile = replace(
            profile,
            longmem_profile_checksum=profile.longmem_checksum(),
        )
        evidence = evidence.model_copy(
            update={"profile_checksum": profile.longmem_profile_checksum}
        )
        envelope = envelope.model_copy(
            update={
                "evidence": evidence,
                "evidence_sha256": evidence_digest(evidence),
            }
        )
        verified = _validate_longmem(
            envelope,
            artifact_sha256=envelope.evidence.artifact_sha256,
            profile=profile,
        )
        assert verified.evidence.score.longmem_mean_micros == 0
        assert [row.requests for row in verified.evidence.provider_evidence] == [
            12,
            0,
        ]
        rebuilt = rebuild(
            unsigned_report().model_copy(update={"longmemeval": envelope}),
            profile=profile,
        )
        assert rebuilt.root.longmemeval.evidence.score.longmem_mean_micros == 0
        assert rebuilt.root.totals.request_count == 12
        assert rebuilt.root.totals.input_tokens == 50
        assert rebuilt.root.totals.output_tokens == 10
        assert rebuilt.root.totals.provider_cost_microusd == 5_000

        for changed in {
            "positive score": evidence.model_copy(
                update={"score": score.model_copy(update={"longmem_mean_micros": 1})}
            ),
            "judge count drift": evidence.model_copy(
                update={
                    "provider_evidence": [
                        row.model_copy(update={"requests": 11})
                        if row.lane == "judge"
                        else row
                        for row in lanes
                    ]
                }
            ),
        }.values():
            changed_envelope = envelope.model_copy(
                update={
                    "evidence": changed,
                    "evidence_sha256": evidence_digest(changed),
                }
            )
            with pytest.raises(
                (ConfirmationEvidenceError, ValueError),
                match="LongMem (macro mean|mixed provider form)",
            ):
                _validate_longmem(
                    changed_envelope,
                    artifact_sha256=changed.artifact_sha256,
                    profile=profile,
                )

    def test_preserves_distinct_provider_lanes_and_derived_totals(self) -> None:
        verified = rebuild()
        evidence = verified.root.longmemeval.evidence
        assert [row.lane for row in evidence.provider_evidence] == ["judge", "reader"]
        assert [row.requests for row in evidence.provider_evidence] == [1, 2]
        assert verified.root.totals.request_count == 3
        assert verified.root.totals.input_tokens == 150
        assert verified.root.totals.output_tokens == 30
        assert verified.root.totals.provider_cost_microusd == 15_000
        assert verified.longmem_mean_micros == 500_000
        assert verified.longmem_stderr_micros == 204_124

    def test_provider_input_order_is_normalized_before_root_hashing(self) -> None:
        original = longmem_envelope()
        reversed_evidence = original.evidence.model_copy(
            update={
                "provider_evidence": tuple(
                    reversed(original.evidence.provider_evidence)
                )
            }
        )
        changed = original.model_copy(update={"evidence": reversed_evidence})
        report = unsigned_report().model_copy(update={"longmemeval": changed})
        assert rebuild(report).root.longmemeval == original

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("artifact_sha256", "0" * 64, "artifact"),
            ("profile_checksum", "0" * 64, "profile"),
            ("dataset_revision", "other", "dataset"),
            ("dataset_sha256", "0" * 64, "dataset"),
        ],
    )
    def test_longmem_identity_tampering_is_rejected(
        self, field: str, value: object, match: str
    ) -> None:
        envelope = longmem_envelope()
        evidence = envelope.evidence.model_copy(update={field: value})
        report = unsigned_report().model_copy(
            update={"longmemeval": envelope.model_copy(update={"evidence": evidence})}
        )
        with pytest.raises(ConfirmationEvidenceError, match=match):
            rebuild(report)

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("longmem_mean_micros", 500_001, "macro mean"),
            ("longmem_stderr_micros", 204_125, "stderr"),
            ("case_count", 13, "case count"),
        ],
    )
    def test_longmem_aggregate_is_recomputed(
        self, field: str, value: int, match: str
    ) -> None:
        envelope = longmem_envelope()
        score = envelope.evidence.score.model_copy(update={field: value})
        evidence = envelope.evidence.model_copy(update={"score": score})
        report = unsigned_report().model_copy(
            update={"longmemeval": envelope.model_copy(update={"evidence": evidence})}
        )
        with pytest.raises(ConfirmationEvidenceError, match=match):
            rebuild(report)

    @pytest.mark.parametrize(
        "mutation,match",
        [
            ("missing", "frozen order"),
            ("duplicate", "frozen order"),
            ("reorder", "frozen order"),
            ("wrong_mean", "mean is not derived"),
            ("too_few", "needs two"),
        ],
    )
    def test_capability_rows_are_replayed(self, mutation: str, match: str) -> None:
        envelope = longmem_envelope()
        rows = list(envelope.evidence.score.per_capability)
        if mutation == "missing":
            rows.pop()
        elif mutation == "duplicate":
            rows[-1] = rows[0]
        elif mutation == "reorder":
            rows[0], rows[1] = rows[1], rows[0]
        elif mutation == "wrong_mean":
            rows[0] = rows[0].model_copy(update={"mean_micros": 500_001})
        else:
            rows[0] = rows[0].model_copy(update={"count": 1, "correct": 1})
        score = envelope.evidence.score.model_copy(
            update={"per_capability": tuple(rows)}
        )
        evidence = envelope.evidence.model_copy(update={"score": score})
        report = unsigned_report().model_copy(
            update={"longmemeval": envelope.model_copy(update={"evidence": evidence})}
        )
        with pytest.raises(ConfirmationEvidenceError, match=match):
            rebuild(report)

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("provider", "fallback", "identity drift"),
            ("profile_revision", "other", "identity drift"),
            ("model", "fallback/model", "identity drift"),
            ("requests", 11, "frozen cap"),
            ("prompt_tokens", 1001, "frozen cap"),
            ("completion_tokens", 1001, "frozen cap"),
            ("total_tokens", 2001, "frozen cap"),
            ("cost_usd_micros", 100_001, "frozen cap"),
        ],
    )
    def test_provider_lane_identity_and_caps_are_replayed(
        self, field: str, value: object, match: str
    ) -> None:
        envelope = longmem_envelope()
        lanes = list(envelope.evidence.provider_evidence)
        lanes[0] = lanes[0].model_copy(update={field: value})
        evidence = envelope.evidence.model_copy(update={"provider_evidence": lanes})
        report = unsigned_report().model_copy(
            update={"longmemeval": envelope.model_copy(update={"evidence": evidence})}
        )
        with pytest.raises(ConfirmationEvidenceError, match=match):
            rebuild(report)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("request_count", 4),
            ("input_tokens", 151),
            ("output_tokens", 31),
            ("provider_cost_microusd", 15_001),
        ],
    )
    def test_longmem_envelope_totals_are_not_authoritative(
        self, field: str, value: int
    ) -> None:
        envelope = longmem_envelope().model_copy(update={field: value})
        report = unsigned_report().model_copy(update={"longmemeval": envelope})
        with pytest.raises(ConfirmationEvidenceError, match="envelope accounting"):
            rebuild(report)

    def test_nested_digest_is_reconstructed(self) -> None:
        envelope = longmem_envelope().model_copy(update={"evidence_sha256": "0" * 64})
        with pytest.raises(ConfirmationEvidenceError, match="digest"):
            rebuild(unsigned_report().model_copy(update={"longmemeval": envelope}))


class TestAblationReplay:
    @pytest.mark.parametrize("intervention", ["inference", "embedding"])
    @pytest.mark.parametrize("mode", list(ConfirmationBundleMode)[1:])
    @pytest.mark.parametrize("status", ["passed", "failed"])
    def test_binary_gate_semantics_are_preserved(
        self,
        intervention: Literal["inference", "embedding"],
        mode: ConfirmationBundleMode,
        status: Literal["passed", "failed"],
    ) -> None:
        envelope = ablation_envelope(intervention, mode=mode, status=status)
        semantic = 10_000 if status == "passed" else 0
        applied = 10_000 if mode == ConfirmationBundleMode.SHADOW else semantic
        assert envelope.evidence.semantic_factor_bps == semantic
        assert envelope.evidence.applied_factor_bps == applied
        assert envelope.provider_cost_microusd == 0
        assert not hasattr(envelope.evidence, "provider")
        assert not hasattr(envelope.evidence, "model")

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("artifact_sha256", "0" * 64, "artifact"),
            ("contract_version", "v2", "profile"),
            ("profile_revision", "other", "profile"),
            ("profile_checksum", "0" * 64, "profile"),
            ("threshold_manifest_sha256", "0" * 64, "profile"),
            ("dataset_sha256", "0" * 64, "profile"),
            ("threshold_micros", 200_001, "profile"),
        ],
    )
    def test_ablation_profile_binding_is_replayed(
        self, field: str, value: object, match: str
    ) -> None:
        envelope = ablation_envelope("inference")
        evidence = envelope.evidence.model_copy(update={field: value})
        changed = envelope.model_copy(update={"evidence": evidence})
        report = unsigned_report().model_copy(update={"inference_ablation": changed})
        with pytest.raises(ConfirmationEvidenceError, match=match):
            rebuild(report)

    def test_run_specific_coordinator_provenance_is_not_globally_frozen(
        self,
    ) -> None:
        report = unsigned_report()
        coordinator_sha256 = "0" * 64

        def with_coordinator(envelope):
            evidence = envelope.evidence.model_copy(
                update={"coordinator_sha256": coordinator_sha256}
            )
            return envelope.model_copy(
                update={
                    "evidence": evidence,
                    "evidence_sha256": evidence_digest(evidence),
                }
            )

        changed = report.model_copy(
            update={
                "inference_ablation": with_coordinator(report.inference_ablation),
                "embedding_ablation": with_coordinator(report.embedding_ablation),
            }
        )
        assert rebuild(changed).ablations_complete is True

    def test_mixed_run_coordinator_provenance_is_rejected(self) -> None:
        report = unsigned_report()
        evidence = report.inference_ablation.evidence.model_copy(
            update={"coordinator_sha256": "0" * 64}
        )
        inference = report.inference_ablation.model_copy(
            update={
                "evidence": evidence,
                "evidence_sha256": evidence_digest(evidence),
            }
        )
        changed = report.model_copy(update={"inference_ablation": inference})
        with pytest.raises(ConfirmationEvidenceError, match="one run coordinator"):
            rebuild(changed)

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("chat_attempts", 0, "chat_applied|attempt accounting"),
            ("embedding_attempts", 1, "embedding attempts|embedding synthetic"),
            ("rejected_requests", 1, "attempt accounting|exhaustion"),
            ("budget_exhausted", True, "exhaustion"),
            ("upstream_requests", 1, "Input should be 0"),
            ("upstream_provider_cost_microusd", 1, "Input should be 0"),
        ],
    )
    def test_inference_synthetic_usage_cannot_escape_its_lane(
        self, field: str, value: object, match: str
    ) -> None:
        envelope = ablation_envelope("inference")
        usage = envelope.evidence.synthetic_usage.model_copy(update={field: value})
        evidence = envelope.evidence.model_copy(update={"synthetic_usage": usage})
        changed = envelope.model_copy(update={"evidence": evidence})
        report = unsigned_report().model_copy(update={"inference_ablation": changed})
        with pytest.raises((ConfirmationEvidenceError, ValueError), match=match):
            # Re-parse to exercise strict Literal[0] backstops as the real API does.
            parsed = ConfirmationCompletionReport.model_validate(
                report.model_dump(mode="json")
            )
            rebuild(parsed)

    def test_ablation_nested_digest_is_reconstructed(self) -> None:
        envelope = ablation_envelope("embedding").model_copy(
            update={"evidence_sha256": "0" * 64}
        )
        report = unsigned_report().model_copy(update={"embedding_ablation": envelope})
        with pytest.raises(ConfirmationEvidenceError, match="digest"):
            rebuild(report)

    def test_unavailable_ablation_is_signed_but_not_numerically_qualified(self) -> None:
        report = unsigned_report(embedding_status="unavailable")
        verified = rebuild(report)
        assert not verified.ablations_complete
        assert verified.ablation_semantic_factor_bps is None
        assert verified.root.embedding_ablation.status == "unavailable"
        projection = compute_subject_projection(
            mode=ConfirmationBundleMode.SHADOW,
            base_quality_micros=800_000,
            base_stderr_micros=20_000,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=verification_profile().composite,
        )
        assert projection.result_status == "provisional"
        assert projection.full_quality_micros is None
        assert projection.full_effective_micros is None

    def test_shadow_observational_drop_qualifies_and_projects_longmem(self) -> None:
        failed = ablation_envelope("inference", status="failed")
        evidence = failed.evidence.model_copy(
            update={
                "reason": "observational_drop_not_causal",
                "baseline_mean_micros": 900_000,
                "ablated_mean_micros": 400_000,
                "delta_micros": 500_000,
                "semantic_factor_bps": 0,
                "applied_factor_bps": 10_000,
            }
        )
        inference = failed.model_copy(
            update={
                "evidence": evidence,
                "evidence_sha256": evidence_digest(evidence),
            }
        )
        report = unsigned_report(embedding_status="failed").model_copy(
            update={"inference_ablation": inference}
        )
        verified = rebuild(report)
        assert verified.ablations_complete is True
        assert verified.ablation_semantic_factor_bps == 0
        assert verified.longmem_mean_micros == 500_000
        projection = compute_subject_projection(
            mode=ConfirmationBundleMode.SHADOW,
            base_quality_micros=882_550,
            base_stderr_micros=10_668,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=verification_profile().composite,
        )
        assert projection.full_quality_micros == 729_530
        assert projection.applied_factor_bps == 10_000
        assert projection.full_effective_micros == 729_530


class TestSharedRootAndSubjectProjection:
    def test_root_binds_artifact_profile_settings_generation_and_typed_children(
        self,
    ) -> None:
        verified = rebuild(settings_revision=7, generation=3)
        root = verified.root
        assert root.artifact_sha256 == ARTIFACT_SHA256
        assert root.confirmation_profile_revision == verification_profile().revision
        assert root.settings_revision == 7
        assert root.settings_checksum == _SETTINGS_SHA256
        assert root.retest_generation == 3
        assert verified.evidence_sha256 == evidence_digest(root)

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"artifact_sha256": "0" * 64}, "artifact"),
            ({"profile_revision": "other"}, "profile identity"),
            ({"profile_checksum": "0" * 64}, "profile identity"),
            ({"settings_checksum": "not-a-sha"}, "settings checksum"),
        ],
    )
    def test_root_identity_cannot_be_substituted(
        self, kwargs: dict, match: str
    ) -> None:
        if "artifact_sha256" in kwargs:
            kwargs["report"] = unsigned_report()
        with pytest.raises(ConfirmationEvidenceError, match=match):
            rebuild(**kwargs)

    def test_two_subjects_sharing_evidence_get_distinct_quality_and_stderr(
        self,
    ) -> None:
        verified = rebuild(mode=ConfirmationBundleMode.ENFORCE)
        composite = verification_profile().composite
        stronger = compute_subject_projection(
            mode=ConfirmationBundleMode.ENFORCE,
            base_quality_micros=900_000,
            base_stderr_micros=10_000,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=composite,
        )
        weaker = compute_subject_projection(
            mode=ConfirmationBundleMode.ENFORCE,
            base_quality_micros=600_000,
            base_stderr_micros=80_000,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=composite,
        )
        assert stronger.full_quality_micros == 740_000
        assert weaker.full_quality_micros == 560_000
        assert stronger.full_stderr_micros != weaker.full_stderr_micros
        assert stronger.full_effective_micros != weaker.full_effective_micros
        assert stronger.result_status == weaker.result_status == "full_confirmed"

    def test_shadow_never_becomes_full_confirmed_even_with_complete_evidence(
        self,
    ) -> None:
        verified = rebuild(mode=ConfirmationBundleMode.SHADOW)
        projection = compute_subject_projection(
            mode=ConfirmationBundleMode.SHADOW,
            base_quality_micros=800_000,
            base_stderr_micros=20_000,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=verification_profile().composite,
        )
        assert projection.result_status == "provisional"
        assert projection.full_quality_micros == 680_000
        assert projection.applied_factor_bps == 10_000
        assert projection.full_effective_micros == 680_000

    def test_failed_shadow_ablation_keeps_semantic_zero_and_applied_one(self) -> None:
        verified = rebuild(
            unsigned_report(
                mode=ConfirmationBundleMode.SHADOW,
                inference_status="failed",
            )
        )
        projection = compute_subject_projection(
            mode=ConfirmationBundleMode.SHADOW,
            base_quality_micros=800_000,
            base_stderr_micros=20_000,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=verification_profile().composite,
        )
        assert projection.semantic_factor_bps == 0
        assert projection.applied_factor_bps == 10_000
        assert projection.full_effective_micros == projection.full_quality_micros
        assert projection.result_status == "provisional"

    def test_failed_enforce_gate_is_complete_and_yields_effective_zero(self) -> None:
        verified = rebuild(
            unsigned_report(
                mode=ConfirmationBundleMode.ENFORCE,
                inference_status="failed",
            ),
            mode=ConfirmationBundleMode.ENFORCE,
        )
        projection = compute_subject_projection(
            mode=ConfirmationBundleMode.ENFORCE,
            base_quality_micros=800_000,
            base_stderr_micros=20_000,
            base_model_factor_bps=10_000,
            base_tool_factor_bps=10_000,
            verified=verified,
            composite=verification_profile().composite,
        )
        assert projection.result_status == "full_confirmed"
        assert projection.full_quality_micros == 680_000
        assert projection.semantic_factor_bps == 0
        assert projection.applied_factor_bps == 0
        assert projection.full_effective_micros == 0
        assert projection.full_stderr_micros == 0

    @pytest.mark.parametrize("field", ["model", "tool"])
    def test_base_binary_gates_are_applied_per_subject(self, field: str) -> None:
        kwargs = {
            "base_model_factor_bps": 10_000,
            "base_tool_factor_bps": 10_000,
        }
        kwargs[f"base_{field}_factor_bps"] = 0
        projection = compute_subject_projection(
            mode=ConfirmationBundleMode.ENFORCE,
            base_quality_micros=800_000,
            base_stderr_micros=20_000,
            verified=rebuild(mode=ConfirmationBundleMode.ENFORCE),
            composite=verification_profile().composite,
            **kwargs,
        )
        assert projection.semantic_factor_bps == 0
        assert projection.full_effective_micros == 0

    @pytest.mark.parametrize("factor", [-1, 1, 9999, 10001])
    def test_nonbinary_base_gate_is_rejected(self, factor: int) -> None:
        with pytest.raises(ConfirmationEvidenceError, match="binary|out of range"):
            compute_subject_projection(
                mode=ConfirmationBundleMode.ENFORCE,
                base_quality_micros=800_000,
                base_stderr_micros=20_000,
                base_model_factor_bps=factor,
                base_tool_factor_bps=10_000,
                verified=rebuild(mode=ConfirmationBundleMode.ENFORCE),
                composite=verification_profile().composite,
            )


class TestSigningDomain:
    def signing_values(self) -> SigningValues:
        return {
            "reporter_hotkey": "5Validator",
            "bundle_id": uuid4(),
            "ticket_id": uuid4(),
            "deadline": datetime(2026, 8, 8, 13, 4, 5, 6, tzinfo=UTC),
            "artifact_sha256": ARTIFACT_SHA256,
            "profile_revision": verification_profile().revision,
            "profile_checksum": verification_profile().checksum(),
            "settings_revision": 17,
            "settings_checksum": _SETTINGS_SHA256,
            "retest_generation": 0,
            "evidence_sha256": "0" * 64,
        }

    def test_exact_signing_domain_is_stable(self) -> None:
        values = self.signing_values()
        message = confirmation_signing_message(**values)
        assert message.startswith(b"validator-v9-confirmation:v1:5Validator:")
        assert b"2026-08-08T13:04:05.000006Z" in message
        assert message.endswith(("0" * 64).encode())

    @pytest.mark.parametrize(
        "field,replacement",
        [
            ("reporter_hotkey", "5Other"),
            ("bundle_id", uuid4()),
            ("ticket_id", uuid4()),
            ("deadline", datetime(2026, 8, 8, 13, 4, 6, tzinfo=UTC)),
            ("artifact_sha256", "1" * 64),
            ("profile_revision", "other"),
            ("profile_checksum", "1" * 64),
            ("settings_revision", 18),
            ("settings_checksum", "1" * 64),
            ("retest_generation", 1),
            ("evidence_sha256", "1" * 64),
        ],
    )
    def test_every_bound_field_changes_the_message(
        self, field: str, replacement: object
    ) -> None:
        values = self.signing_values()
        original = confirmation_signing_message(**values)
        changed = dict(values)
        changed[field] = replacement
        assert confirmation_signing_message(**cast(SigningValues, changed)) != original

    def test_naive_deadline_is_rejected(self) -> None:
        values = self.signing_values()
        values["deadline"] = datetime(2026, 8, 8)
        with pytest.raises(ConfirmationEvidenceError, match="timezone"):
            confirmation_signing_message(**values)

    def test_timezone_is_canonicalized_to_utc_microseconds(self) -> None:
        values = self.signing_values()
        values["deadline"] = datetime(2026, 8, 8, 14, 4, 5, 6, tzinfo=UTC) - timedelta(
            hours=1
        )
        first = confirmation_signing_message(**values)
        values["deadline"] = datetime(2026, 8, 8, 13, 4, 5, 6, tzinfo=UTC)
        assert confirmation_signing_message(**values) == first


def test_profile_dataclasses_have_no_default_calibration_values() -> None:
    with pytest.raises(TypeError):
        CompositeVerificationPolicy()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ProviderLanePolicy()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AblationVerificationPolicy()  # type: ignore[call-arg]
