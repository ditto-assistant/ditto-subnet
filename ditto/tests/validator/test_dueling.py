"""Bench >= 16 (dueling): slice forwarding and the pair-masked KOTH fold.

``research/dittobench-datagen/docs/dueling-bench.md`` sections 3 and 7.7: two
v16 rows compare only inside one ``(bench_version, season_id)``, and the paired
dethrone test recomputes every shared seed's composite for both agents with the
columns either owner contributed masked out.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest

from ditto.api_models.validator import (
    ConfirmationScoreRecord,
    JobResponse,
    LedgerEntry,
    ScoreReport,
)
from ditto.tests.validator.test_worker import _config, _job, _platform_with_ledger
from ditto.validator import dittobench as dittobench_module
from ditto.validator import worker as worker_module
from ditto.validator.config import KOTH_DETHRONE_Z, KOTH_MARGIN
from ditto.validator.dittobench import (
    DittobenchClient,
    DittobenchError,
    DuelingSlice,
    DuelingSliceError,
    dueling_slice_from_lease,
    scorer_dueling_slice_bench_versions,
    verify_dueling_report,
)
from ditto.validator.errors import ValidatorInfrastructureError
from ditto.validator.weights import (
    _beats,
    _champion,
    _paired_dethrone,
    _record_masked_composite,
    _shared_seed_count,
    _tail,
    compute_weights,
    dueling_fold_dropped,
)
from ditto.validator.worker import ValidatorWorker
from ditto_screening_protocol.dueling import (
    DuelingColumnAggregate,
    DuelingReport,
    dueling_composite,
)

_VECTORS = (
    Path(__file__).resolve().parents[3]
    / "research/dittobench-datagen/challenge/testdata/dueling_composite_vectors.json"
)
_T0 = datetime(2026, 11, 3, tzinfo=UTC)
_SEASON = "s16-0001"
_OWNERS = ("coldkey:a", "coldkey:b", "coldkey:c", "coldkey:d", "")
_SEEDS = (11, 22, 33, 44)
_NOISE = (0.0, 0.01, -0.01, 0.005)


# --- fixtures ---------------------------------------------------------------


def _report(
    owner: str,
    datagen_raw: float,
    means: tuple[float, ...],
    *,
    season: str = _SEASON,
) -> DuelingReport:
    columns = [
        DuelingColumnAggregate(
            column_id=f"c{index}",
            owner_key=_OWNERS[index],
            n=35,
            sum=mean * 35,
            sum_sq=mean * mean * 35,
        )
        for index, mean in enumerate(means)
    ]
    return DuelingReport(
        season_id=season,
        season_index=1,
        slice_sha256="ab" * 32,
        owner_key=owner,
        datagen_raw=datagen_raw,
        datagen_gate=1.0,
        column_weight=0.1,
        columns=columns,
        composite_stderr=0.01,
    )


def _entry(
    index: int,
    owner: str,
    datagen_raw: float,
    means: tuple[float, ...],
    *,
    season: str | None = _SEASON,
    record_aggregates: bool = True,
    bench_version: int = 16,
) -> Any:
    """A v16 row whose four confirmation seeds share the same column means."""
    reports = [
        _report(owner, datagen_raw + noise, means, season=season or _SEASON)
        for noise in _NOISE
    ]
    history = [
        ConfirmationScoreRecord(
            seed=seed,
            composite=report.self_composite(),
            validator_hotkey="5" + "V" * 47,
            bench_version=bench_version,
            dueling=report if record_aggregates else None,
        )
        for seed, report in zip(_SEEDS, reports, strict=True)
    ]
    head = reports[0]
    return SimpleNamespace(
        miner_hotkey=f"hotkey-{index}",
        agent_id=UUID(int=index),
        composite=head.self_composite(),
        composite_stderr=0.01,
        first_seen=_T0 + timedelta(minutes=index),
        bench_version=bench_version,
        n=350,
        confirmation_history=history,
        dueling=head if season is not None else None,
        dueling_season_id=season,
    )


# A's own column c0 is the hardest on the board; B's own column c1 is easy.
# Self-masking hands A the advantage of never facing c0, so A leads on its
# reported composite. With both owners masked, B is clearly better.
_A_MEANS = (0.2, 0.6, 0.7, 0.7, 0.7)
_B_MEANS = (0.2, 0.9, 0.75, 0.75, 0.75)


def _pair() -> tuple[Any, Any]:
    return (
        _entry(1, "coldkey:a", 0.80, _A_MEANS),
        _entry(2, "coldkey:b", 0.84, _B_MEANS),
    )


# --- composite vectors -------------------------------------------------------


def _vectors() -> list[dict[str, Any]]:
    return [
        vector
        for vector in json.loads(_VECTORS.read_text())["composites"]
        if isinstance(vector.get("columns"), list) and len(vector["columns"]) == 5
    ]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda vector: vector["name"])
def test_fold_reproduces_the_shared_composite_vectors(vector: dict[str, Any]) -> None:
    """Spec phase-2 exit: pair-mask fold vectors match on Go and Python."""
    report = DuelingReport(
        season_id=_SEASON,
        season_index=1,
        slice_sha256="ab" * 32,
        owner_key="coldkey:subject",
        datagen_raw=vector["datagen_raw"],
        datagen_gate=vector["datagen_gate"],
        column_weight=0.1,
        columns=[DuelingColumnAggregate(**column) for column in vector["columns"]],
        composite_stderr=vector["composite_stderr"],
    )
    entry = SimpleNamespace(bench_version=16, dueling=report, dueling_season_id=_SEASON)
    record = SimpleNamespace(bench_version=16, dueling=report)
    folded = _record_masked_composite(
        cast(LedgerEntry, entry), record, frozenset(vector["mask"])
    )
    assert folded == vector["composite"]


# --- pair-masked dethrone ----------------------------------------------------


def test_pair_mask_decides_the_dethrone_not_the_self_masked_composite() -> None:
    champion, challenger = _pair()
    assert champion.composite > challenger.composite

    paired = _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z)
    assert paired is not None
    mean_diff, champ_ref, _se = paired
    mask = frozenset({"coldkey:a", "coldkey:b"})
    expected_diffs = []
    expected_champ = []
    for chall_record, champ_record in zip(
        challenger.confirmation_history, champion.confirmation_history, strict=True
    ):
        chall = chall_record.dueling
        champ = champ_record.dueling
        chall_value = dueling_composite(
            chall.datagen_raw, chall.datagen_gate, chall.columns, mask
        )
        champ_value = dueling_composite(
            champ.datagen_raw, champ.datagen_gate, champ.columns, mask
        )
        expected_diffs.append(chall_value - champ_value)
        expected_champ.append(champ_value)
    assert mean_diff == sum(expected_diffs) / len(expected_diffs)
    assert champ_ref == sum(expected_champ) / len(expected_champ)
    assert mean_diff > 0.04
    assert _beats(challenger, champion, KOTH_MARGIN, KOTH_DETHRONE_Z)
    assert (
        _champion(
            [champion, challenger],
            KOTH_MARGIN,
            KOTH_DETHRONE_Z,
            incumbent_agent_id=champion.agent_id,
        ).agent_id
        == challenger.agent_id
    )


def test_records_without_aggregates_carry_no_paired_evidence() -> None:
    champion = _entry(1, "coldkey:a", 0.80, _A_MEANS)
    challenger = _entry(2, "coldkey:b", 0.84, _B_MEANS, record_aggregates=False)
    assert _shared_seed_count(challenger, champion) == 0
    assert _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z) is None
    # The unpaired branch compares reported composites, where A still leads.
    assert not _beats(challenger, champion, KOTH_MARGIN, KOTH_DETHRONE_Z)


def test_pair_mask_is_deterministic_under_entry_order() -> None:
    champion, challenger = _pair()
    forward = _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z)
    challenger.confirmation_history.reverse()
    champion.confirmation_history.reverse()
    assert _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z) == forward


# --- season isolation ---------------------------------------------------------


def test_rows_from_different_seasons_never_compare() -> None:
    champion = _entry(1, "coldkey:a", 0.50, _A_MEANS, season="s16-0001")
    challenger = _entry(2, "coldkey:b", 0.95, _B_MEANS, season="s16-0002")
    assert _shared_seed_count(challenger, champion) == 0
    assert _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z) is None
    assert not _beats(challenger, champion, KOTH_MARGIN, KOTH_DETHRONE_Z)
    # The crown carries over until a same-season challenger beats it.
    assert (
        _champion(
            [champion, challenger],
            KOTH_MARGIN,
            KOTH_DETHRONE_Z,
            incumbent_agent_id=champion.agent_id,
        ).agent_id
        == champion.agent_id
    )


def test_a_row_whose_season_contradicts_its_report_has_no_season() -> None:
    entry = _entry(1, "coldkey:a", 0.80, _A_MEANS)
    entry.dueling_season_id = "s16-0009"
    assert dueling_fold_dropped([entry]) == [entry]


def test_rows_without_season_or_aggregates_never_rank_or_pay() -> None:
    paid = _entry(1, "coldkey:a", 0.80, _A_MEANS)
    unseasoned = _entry(2, "coldkey:b", 0.95, _B_MEANS, season=None)
    legacy = _entry(3, "coldkey:c", 0.70, _A_MEANS, bench_version=15)
    legacy.dueling = None
    legacy.dueling_season_id = None
    assert dueling_fold_dropped([paid, unseasoned, legacy]) == [unseasoned]
    weights = compute_weights(
        [paid, unseasoned, legacy],
        margin=KOTH_MARGIN,
        tail_size=1,
        rank_shares=[0.65, 0.35],
        dethrone_z=KOTH_DETHRONE_Z,
    )
    assert unseasoned.miner_hotkey not in weights
    assert set(weights) == {paid.miner_hotkey, legacy.miner_hotkey}


def test_pre_dueling_rows_ignore_stray_dueling_fields() -> None:
    champion, challenger = _pair()
    for entry in (champion, challenger):
        entry.bench_version = 15
        for record in entry.confirmation_history:
            record.bench_version = 15
    legacy = _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z)
    for entry in (champion, challenger):
        entry.dueling = None
        entry.dueling_season_id = None
        for record in entry.confirmation_history:
            record.dueling = None
    assert _paired_dethrone(challenger, champion, KOTH_DETHRONE_Z) == legacy
    assert legacy is not None
    # Self-masked composites: A's own-column advantage survives below v16.
    assert legacy[0] < 0.0


# --- adjacent top-five order --------------------------------------------------


def test_adjacent_tail_order_uses_the_pair_mask() -> None:
    king = _entry(9, "coldkey:d", 0.99, (0.9, 0.9, 0.9, 0.9, 0.9))
    upper, lower = _pair()
    assert upper.composite > lower.composite
    assert _tail([king, upper, lower], king, 2) == [lower, upper]
    # Membership never moves: a third row outside the tail stays out.
    outsider = _entry(3, "coldkey:c", 0.10, (0.1, 0.1, 0.1, 0.1, 0.1))
    assert _tail([king, upper, lower, outsider], king, 2) == [lower, upper]


# --- slice forwarding ---------------------------------------------------------


def test_scorer_features_advertise_dueling_versions_both_sides_run() -> None:
    features = [
        "dueling-slice-v1",
        "dueling-slice-bench-15",
        "dueling-slice-bench-16",
        "dueling-slice-bench-17",
    ]
    assert scorer_dueling_slice_bench_versions(features, (15, 16)) == (16,)
    assert scorer_dueling_slice_bench_versions(features, (15,)) is None
    assert scorer_dueling_slice_bench_versions(features[1:], (16,)) is None
    assert scorer_dueling_slice_bench_versions(None, (16,)) is None


_SLICE = json.dumps({"season_id": _SEASON, "slice": "fixture"}).encode() + b"\n"
_SLICE_SHA = hashlib.sha256(_SLICE).hexdigest()


def _dueling_job(bench_version: int = 16, **overrides: object) -> JobResponse:
    fields: dict[str, object] = {
        "dueling_slice_mode": "dueling-slice-v1",
        "dueling_slice_bytes": base64.b64encode(_SLICE).decode(),
        "expected_slice_sha256": _SLICE_SHA,
        "dueling_owner_key": "coldkey:a",
        "dueling_season_id": _SEASON,
    }
    fields.update(overrides)
    return _job("5MinerA" + "x" * 41).model_copy(
        update={"bench_version": bench_version, **fields}
    )


def test_a_complete_dueling_lease_decodes_and_verifies() -> None:
    dueling = dueling_slice_from_lease(_dueling_job())
    assert dueling == DuelingSlice(
        slice_bytes=_SLICE,
        expected_slice_sha256=_SLICE_SHA,
        owner_key="coldkey:a",
        season_id=_SEASON,
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"dueling_slice_bytes": None}, "dueling_slice_missing"),
        ({"dueling_slice_mode": None}, "dueling_slice_missing"),
        ({"expected_slice_sha256": None}, "dueling_slice_missing"),
        ({"dueling_owner_key": None}, "dueling_slice_missing"),
        ({"dueling_season_id": None}, "dueling_slice_missing"),
        ({"dueling_slice_bytes": "not base64!"}, "dueling_slice_undecodable"),
        ({"expected_slice_sha256": "0" * 64}, "dueling_slice_digest_mismatch"),
        ({"dueling_season_id": "s16-other"}, "dueling_slice_season_mismatch"),
        (
            {
                "dueling_slice_bytes": base64.b64encode(b"[]").decode(),
                "expected_slice_sha256": hashlib.sha256(b"[]").hexdigest(),
            },
            "dueling_slice_undecodable",
        ),
    ],
)
def test_an_incomplete_or_tampered_dueling_lease_is_refused(
    overrides: dict[str, Any], code: str
) -> None:
    with pytest.raises(DuelingSliceError) as refused:
        dueling_slice_from_lease(_dueling_job(**overrides))
    assert refused.value.code == code


def test_a_pre_dueling_lease_never_forwards_a_slice() -> None:
    assert dueling_slice_from_lease(_dueling_job(bench_version=15)) is None
    assert (
        dueling_slice_from_lease(_job("5MinerA" + "x" * 41)) is None
    )  # no fields at all


async def test_score_job_hands_back_a_tampered_slice_as_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_supports_bench_version", lambda _: True)
    config = _config()
    config.dittobench_mock = False
    dittobench = MagicMock()
    worker = ValidatorWorker(
        config=config,
        platform=_platform_with_ledger(jobs=[], ledger=[]),
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )
    job = _dueling_job(expected_slice_sha256="0" * 64)
    with pytest.raises(ValidatorInfrastructureError) as refused:
        await worker._score_job(job)
    assert refused.value.code == "dueling_slice_digest_mismatch"
    dittobench.score_tarball.assert_not_called()


def _submit_kwargs(bench_version: int) -> dict[str, Any]:
    return {
        "tarball_url": "https://source.test/agent.tgz",
        "bench_version": bench_version,
        "dataset_sha256": "cd" * 32,
        "screened_image_url": "https://source.test/image.tar",
        "screened_image_sha256": "a" * 64,
        "screened_image_size_bytes": 123,
        "screened_image_id": "sha256:" + "b" * 64,
        "screened_image_ref": "fixture",
    }


async def test_v2_score_forwards_the_slice_only_at_a_dueling_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dittobench_module, "SUPPORTED_BENCH_VERSIONS", (15, 16))
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "run"})

    dueling = DuelingSlice(
        slice_bytes=_SLICE,
        expected_slice_sha256=_SLICE_SHA,
        owner_key="coldkey:a",
        season_id=_SEASON,
    )
    config = SimpleNamespace(dittobench_api_url="http://scorer.test", run_size="full")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, config), http)
        assert await client._submit(**_submit_kwargs(16), dueling_slice=dueling)
        assert await client._submit(**_submit_kwargs(15))
        with pytest.raises(DittobenchError, match="requires a dueling slice"):
            await client._submit(**_submit_kwargs(16))
        with pytest.raises(DittobenchError, match="does not take a dueling slice"):
            await client._submit(**_submit_kwargs(15), dueling_slice=dueling)
        tampered = DuelingSlice(
            slice_bytes=b"other",
            expected_slice_sha256=_SLICE_SHA,
            owner_key="coldkey:a",
            season_id=_SEASON,
        )
        with pytest.raises(DittobenchError, match="digest mismatch"):
            await client._submit(**_submit_kwargs(16), dueling_slice=tampered)
        empty = DuelingSlice(
            slice_bytes=b"",
            expected_slice_sha256=_SLICE_SHA,
            owner_key="coldkey:a",
            season_id=_SEASON,
        )
        with pytest.raises(DittobenchError, match="empty or oversized"):
            await client._submit(**_submit_kwargs(16), dueling_slice=empty)
        wrong_mode = DuelingSlice(
            slice_bytes=_SLICE,
            expected_slice_sha256=_SLICE_SHA,
            owner_key="coldkey:a",
            season_id=_SEASON,
            mode="dueling-slice-v2",
        )
        with pytest.raises(DittobenchError, match="mode mismatch"):
            await client._submit(**_submit_kwargs(16), dueling_slice=wrong_mode)
    assert len(requests) == 2
    dueling_body, legacy_body = requests
    assert dueling_body["dueling_slice_mode"] == "dueling-slice-v1"
    assert base64.b64decode(cast(str, dueling_body["dueling_slice_bytes"])) == _SLICE
    assert dueling_body["expected_slice_sha256"] == _SLICE_SHA
    assert dueling_body["dueling_owner_key"] == "coldkey:a"
    assert "dueling_season_id" not in dueling_body
    assert not {key for key in legacy_body if key.startswith("dueling")}
    assert "expected_slice_sha256" not in legacy_body


def _score_report(dueling: object) -> ScoreReport:
    return ScoreReport(
        run_id="run",
        seed=1,
        composite=0.7,
        tool_mean=0.7,
        memory_mean=0.7,
        median_ms=100,
        n=350,
        generated_at=_T0,
        per_case=[],
        structural_fingerprint=None,
        details={"dueling": dueling} if dueling is not None else {},
    )


def test_a_report_must_echo_the_forwarded_slice() -> None:
    dueling = DuelingSlice(
        slice_bytes=_SLICE,
        expected_slice_sha256=_SLICE_SHA,
        owner_key="coldkey:a",
        season_id=_SEASON,
    )
    echoed = _report("coldkey:a", 0.8, _A_MEANS).model_copy(
        update={"slice_sha256": _SLICE_SHA}
    )
    verify_dueling_report(_score_report(echoed.model_dump(mode="json")), dueling)
    with pytest.raises(ValidatorInfrastructureError) as missing:
        verify_dueling_report(_score_report(None), dueling)
    assert missing.value.code == "dueling_report_invalid"
    for wrong in (
        {"slice_sha256": "0" * 64},
        {"owner_key": "coldkey:b"},
        {"season_id": "s16-0002"},
    ):
        report = echoed.model_copy(update=wrong).model_dump(mode="json")
        with pytest.raises(ValidatorInfrastructureError) as mismatch:
            verify_dueling_report(_score_report(report), dueling)
        assert mismatch.value.code == "dueling_report_mismatch"


async def test_unleased_crn_rescore_never_runs_a_dueling_version() -> None:
    """The P4/contested lanes carry no season slice, so they skip v16."""
    config = _config()
    config.dittobench_mock = False
    dittobench = MagicMock()
    platform = _platform_with_ledger(jobs=[], ledger=[])
    worker = ValidatorWorker(
        config=config,
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )
    submitted = await worker._confirm_and_submit(
        UUID(int=1), "ab" * 32, "5MinerA" + "x" * 41, bench_version=16, seeds=[1, 2]
    )
    assert submitted is None
    dittobench.score_tarball.assert_not_called()
    platform.get_artifact.assert_not_called()
