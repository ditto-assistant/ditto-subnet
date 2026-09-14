"""Validator side of the bench v13+ finalized-block CRN binding.

Three things must hold on this side of the wire:

* a Platform-issued confirmation pin that claims a block binding must re-derive
  from that block, or the validator refuses to lend it a signature (the
  confirmation-lane twin of the P2 ``derive_validator_seed`` check);
* the legacy validator-derived lanes (version-bump re-score sweep, contested
  dethrone) hash the ledger's pinned anchor when it carries one; without one
  the posture decides -- ``enforce`` draws no fresh seed until a pin lands,
  ``observe`` (the v13.0 default) logs and falls back to the legacy family;
* the fold's planning mirror derives the same family Platform issues.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.api_models.validator import (
    ArtifactResponse,
    ConfirmationDatasetPin,
    ConfirmationSeedAnchorPin,
    LedgerResponse,
    ScoreReport,
)
from ditto.tests.validator.test_worker import (
    _T0,
    _config,
    _entry,
    _job,
    _platform_with_ledger,
    _report,
)
from ditto.validator import crn as crn_mod
from ditto.validator.crn import confirmation_seeds, crn_seed
from ditto.validator.weights import (
    reign_seed_planning,
    top5_confirmation_set,
    version_seed_planning,
)
from ditto.validator.worker import (
    ValidatorWorker,
    _confirmation_pin_binding_mismatch,
)

_HASH = "0x" + "ab" * 32


def _worker(
    platform: MagicMock, dittobench: MagicMock | None = None
) -> ValidatorWorker:
    return ValidatorWorker(
        config=_config(),
        platform=platform,
        dittobench=dittobench or MagicMock(),
        chain=MagicMock(),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )


def _bound_pin(
    anchor: Any, *, version: int, k: int, block: int = 110
) -> ConfirmationDatasetPin:
    seed = crn_seed([str(anchor)], version=version, k=k, block_hash=_HASH)
    return ConfirmationDatasetPin(
        seed=seed,
        dataset_sha256="cd" * 32,
        run_size="full",
        anchor_agent_id=anchor,
        seed_index=k,
        seed_block=block,
        seed_block_hash=_HASH,
    )


class TestPinBindingCheck:
    def test_legacy_pins_are_not_checked(self) -> None:
        pin = ConfirmationDatasetPin(seed=5, dataset_sha256="cd" * 32, run_size="full")
        assert _confirmation_pin_binding_mismatch([pin], bench_version=13) is None

    def test_bound_pin_that_rederives_passes(self) -> None:
        anchor = uuid4()
        assert (
            _confirmation_pin_binding_mismatch(
                [_bound_pin(anchor, version=13, k=2)], bench_version=13
            )
            is None
        )

    def test_bound_pin_that_does_not_rederive_is_named(self) -> None:
        anchor = uuid4()
        pin = _bound_pin(anchor, version=13, k=2).model_copy(update={"seed": 12345})
        assert _confirmation_pin_binding_mismatch([pin], bench_version=13) is pin
        # A binding at the wrong version is a mismatch too.
        wrong_version = _bound_pin(anchor, version=12, k=2)
        assert (
            _confirmation_pin_binding_mismatch([wrong_version], bench_version=13)
            is wrong_version
        )

    def test_partial_binding_is_a_mismatch(self) -> None:
        anchor = uuid4()
        partial = _bound_pin(anchor, version=13, k=0).model_copy(
            update={"seed_block": None}
        )
        assert (
            _confirmation_pin_binding_mismatch([partial], bench_version=13) is partial
        )
        assert (
            _confirmation_pin_binding_mismatch(
                [_bound_pin(anchor, version=13, k=0)], bench_version=None
            )
            is not None
        )


class TestTop5LaneRefusesGrindableSeeds:
    # The binding check is version-independent; lease at the newest version the
    # fixture validator supports so the lane reaches it (v13 is the scorer PR).
    async def test_mismatched_binding_is_refused_before_any_run(self) -> None:
        anchor = uuid4()
        job = _job("5Miner" + "x" * 42, slot_id="slot-0").model_copy(
            update={
                "bench_version": 12,
                "deadline": datetime.now(UTC) + timedelta(hours=3),
                "confirmation_datasets": [
                    _bound_pin(anchor, version=12, k=0).model_copy(
                        update={"seed": 4242}
                    )
                ],
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = _worker(platform)
        worker._evaluate_confirmation_report = AsyncMock()  # type: ignore[method-assign]
        worker._report_ticket_failed = AsyncMock()  # type: ignore[method-assign]

        accepted = await worker._run_top5_confirmation_lane()

        assert accepted is False
        worker._evaluate_confirmation_report.assert_not_awaited()
        worker._report_ticket_failed.assert_awaited_once_with(
            job, "infrastructure", "confirmation_seed_binding_mismatch"
        )
        platform.submit_top5_confirmation_score.assert_not_awaited()

    async def test_bound_pin_that_rederives_is_scored(self) -> None:
        anchor = uuid4()
        pin = _bound_pin(anchor, version=12, k=1)
        job = _job("5Miner" + "x" * 42, slot_id="slot-0").model_copy(
            update={
                "bench_version": 12,
                "deadline": datetime.now(UTC) + timedelta(hours=3),
                "confirmation_datasets": [pin],
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = _worker(platform)
        report = _report("bound-run", 0.8).model_copy(update={"seed": pin.seed})
        worker._evaluate_confirmation_report = AsyncMock(return_value=report)  # type: ignore[method-assign]
        worker._activate_ticket_inference = AsyncMock(return_value=None)  # type: ignore[method-assign]
        worker._report_ticket_failed = AsyncMock()  # type: ignore[method-assign]

        accepted = await worker._run_top5_confirmation_lane()

        assert accepted is True
        worker._report_ticket_failed.assert_not_awaited()
        evaluated = worker._evaluate_confirmation_report.await_args
        assert evaluated is not None
        assert evaluated.kwargs["datasets"] == [pin]
        platform.submit_top5_confirmation_score.assert_awaited_once()


class TestLegacyLanesWaitForTheLedgerAnchor:
    @pytest.fixture(autouse=True)
    def _floor_at_fixture_era(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The fixtures score at v8/v9; read the floor at call time and lower it
        # to them so the binding path is exercised without a v13 fixture stack.
        monkeypatch.setattr(crn_mod, "CRN_BLOCK_BINDING_MIN_BENCH_VERSION", 8)

    def _anchor(self, champion: Any, *, version: int) -> ConfirmationSeedAnchorPin:
        return ConfirmationSeedAnchorPin(
            champion_agent_id=champion,
            bench_version=version,
            anchor_block=110,
            anchor_block_hash=_HASH,
        )

    @staticmethod
    def _stale_ledger(champ: Any, r1: Any) -> LedgerResponse:
        return LedgerResponse(
            entries=[
                _entry("5A" + "a" * 44, 0.90, agent_id=champ, bench_version=8),
                _entry(
                    "5B" + "b" * 44,
                    0.70,
                    agent_id=r1,
                    bench_version=8,
                    first_seen=_T0 + timedelta(minutes=1),
                ),
            ],
            active_bench_version=9,
            count=2,
        )

    async def test_rescore_sweep_defers_without_a_pinned_anchor_under_enforce(
        self,
    ) -> None:
        w = _worker(_platform_with_ledger(jobs=[], ledger=[]))
        w._config.crn_block_binding_posture = "enforce"  # type: ignore[misc]
        w._confirm_and_submit = AsyncMock()  # type: ignore[method-assign]
        stale = self._stale_ledger(uuid4(), uuid4())

        folded = await w._rescore_stale_champion_and_tail(stale)

        assert folded is stale
        w._confirm_and_submit.assert_not_awaited()

    async def test_rescore_sweep_observe_posture_falls_back_to_the_legacy_family(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The v13.0 default: a Platform pin gap is logged, never a stall. The
        sweep runs the exact unbound family every version below the floor runs."""
        w = _worker(_platform_with_ledger(jobs=[], ledger=[]))
        assert w._config.crn_block_binding_posture == "observe"
        w._confirm_and_submit = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        w._platform.get_ledger = AsyncMock(  # type: ignore[method-assign]
            return_value=LedgerResponse(entries=[], active_bench_version=9, count=0)
        )
        champ, r1 = uuid4(), uuid4()

        with caplog.at_level("WARNING"):
            await w._rescore_stale_champion_and_tail(self._stale_ledger(champ, r1))

        seed_sets = {
            tuple(c.kwargs["seeds"]) for c in w._confirm_and_submit.await_args_list
        }
        assert seed_sets == {
            tuple(confirmation_seeds([str(champ), str(r1)], version=9, count=3))
        }
        assert "observe posture" in caplog.text
        assert "no pinned confirmation seed anchor" in caplog.text

    async def test_rescore_sweep_binds_to_the_versions_oldest_anchor(self) -> None:
        w = _worker(_platform_with_ledger(jobs=[], ledger=[]))
        w._confirm_and_submit = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        w._platform.get_ledger = AsyncMock(  # type: ignore[method-assign]
            return_value=LedgerResponse(entries=[], active_bench_version=9, count=0)
        )
        champ, r1, other = uuid4(), uuid4(), uuid4()
        stale = LedgerResponse(
            entries=[
                _entry("5A" + "a" * 44, 0.90, agent_id=champ, bench_version=8),
                _entry(
                    "5B" + "b" * 44,
                    0.70,
                    agent_id=r1,
                    bench_version=8,
                    first_seen=_T0 + timedelta(minutes=1),
                ),
            ],
            active_bench_version=9,
            count=2,
            # Anchored on an agent that is not in the stale set: the sweep binds
            # to the version's oldest pin, not to a champion it may not have.
            confirmation_seed_anchors=[self._anchor(other, version=9)],
        )

        await w._rescore_stale_champion_and_tail(stale)

        seed_sets = {
            tuple(c.kwargs["seeds"]) for c in w._confirm_and_submit.await_args_list
        }
        assert seed_sets == {
            tuple(
                confirmation_seeds(
                    [str(champ), str(r1)], version=9, count=3, block_hash=_HASH
                )
            )
        }
        assert seed_sets != {
            tuple(confirmation_seeds([str(champ), str(r1)], version=9, count=3))
        }

    async def test_contested_dethrone_defers_then_binds(self) -> None:
        champ = _entry("5A" + "a" * 44, 0.80, bench_version=8)
        chall = _entry(
            "5B" + "b" * 44,
            0.795,
            bench_version=8,
            first_seen=_T0 + timedelta(minutes=1),
        )

        async def _score(**kw: Any) -> ScoreReport:
            return _report(f"run-{kw['seed']}", 0.8).model_copy(
                update={"seed": kw["seed"]}
            )

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.last_details = {}
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=champ.agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
                bench_version=8,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = _worker(platform, dittobench)

        # Under ``enforce`` a reign without a pin draws no fresh seed at all.
        w._config.crn_block_binding_posture = "enforce"  # type: ignore[misc]
        await w._confirm_contested_dethrone(
            LedgerResponse(entries=[champ, chall], active_bench_version=8, count=2)
        )
        dittobench.score_tarball.assert_not_awaited()
        platform.submit_score.assert_not_awaited()

        await w._confirm_contested_dethrone(
            LedgerResponse(
                entries=[champ, chall],
                active_bench_version=8,
                count=2,
                confirmation_seed_anchors=[self._anchor(champ.agent_id, version=8)],
            )
        )
        assert dittobench.score_tarball.await_count == 6
        reports = [c.kwargs["report"] for c in platform.submit_score.await_args_list]
        assert len(reports) == 2
        expected = confirmation_seeds(
            [str(champ.agent_id)], version=8, count=3, block_hash=_HASH
        )
        assert set(reports[0].confirmation_seeds) == set(expected)
        assert set(reports[1].confirmation_seeds) == set(expected)

    async def test_contested_dethrone_observe_posture_derives_the_legacy_family(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        champ = _entry("5A" + "a" * 44, 0.80, bench_version=8)
        chall = _entry(
            "5B" + "b" * 44,
            0.795,
            bench_version=8,
            first_seen=_T0 + timedelta(minutes=1),
        )

        async def _score(**kw: Any) -> ScoreReport:
            return _report(f"run-{kw['seed']}", 0.8).model_copy(
                update={"seed": kw["seed"]}
            )

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.last_details = {}
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=champ.agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
                bench_version=8,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = _worker(platform, dittobench)
        assert w._config.crn_block_binding_posture == "observe"

        with caplog.at_level("WARNING"):
            await w._confirm_contested_dethrone(
                LedgerResponse(entries=[champ, chall], active_bench_version=8, count=2)
            )

        assert dittobench.score_tarball.await_count == 6
        reports = [c.kwargs["report"] for c in platform.submit_score.await_args_list]
        legacy = confirmation_seeds([str(champ.agent_id)], version=8, count=3)
        assert len(reports) == 2
        assert set(reports[0].confirmation_seeds) == set(legacy)
        assert "observe posture" in caplog.text


class TestFoldPlanningMirror:
    def test_reign_planning_reads_the_ledger_pins(self) -> None:
        champion = uuid4()
        anchor = ConfirmationSeedAnchorPin(
            champion_agent_id=champion,
            bench_version=13,
            anchor_block=110,
            anchor_block_hash=_HASH,
        )
        assert reign_seed_planning(
            [anchor], champion_agent_id=champion, version=13
        ) == (
            _HASH,
            True,
        )
        assert reign_seed_planning([], champion_agent_id=champion, version=13) == (
            None,
            False,
        )
        assert reign_seed_planning(None, champion_agent_id=champion, version=13) == (
            None,
            False,
        )
        # Another reign's pin does not bind this champion.
        assert reign_seed_planning([anchor], champion_agent_id=uuid4(), version=13) == (
            None,
            False,
        )
        # Below the floor: legacy, whatever the ledger carries.
        assert reign_seed_planning(
            [anchor], champion_agent_id=champion, version=12
        ) == (
            None,
            True,
        )
        assert version_seed_planning([anchor], version=13) == (_HASH, True)
        assert version_seed_planning([], version=13) == (None, False)
        assert version_seed_planning([anchor], version=12) == (None, True)

    def test_top5_plan_uses_the_bound_family(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crn_mod, "CRN_BLOCK_BINDING_MIN_BENCH_VERSION", 8)
        champ = _entry("5A" + "a" * 44, 0.90, bench_version=8, composite_stderr=0.03)
        tail = [
            _entry(
                f"5{chr(66 + index)}" + chr(98 + index) * 44,
                0.85 - index * 0.01,
                bench_version=8,
                composite_stderr=0.03,
                first_seen=_T0 + timedelta(minutes=index + 1),
            )
            for index in range(4)
        ]
        kwargs: dict[str, Any] = {
            "current_version": 8,
            "margin": 0.01,
            "dethrone_z": 1.64,
            "tail_size": 4,
            "baseline_seeds": 8,
            "max_seeds": 15,
        }
        waiting = top5_confirmation_set([champ, *tail], **kwargs)
        assert waiting is None  # no pinned anchor: nothing fresh to owe

        plan = top5_confirmation_set(
            [champ, *tail],
            confirmation_seed_anchors=[
                ConfirmationSeedAnchorPin(
                    champion_agent_id=champ.agent_id,
                    bench_version=8,
                    anchor_block=110,
                    anchor_block_hash=_HASH,
                )
            ],
            **kwargs,
        )
        assert plan is not None
        bound = confirmation_seeds(
            [str(champ.agent_id)], version=8, count=15, block_hash=_HASH
        )
        assert set(plan.anchor_seeds) <= set(bound)
        assert set(plan.anchor_seeds).isdisjoint(
            confirmation_seeds([str(champ.agent_id)], version=8, count=15)
        )
