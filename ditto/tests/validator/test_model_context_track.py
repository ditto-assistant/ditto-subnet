"""model_context (CPM) track: shadow registration, fold, and ledger read.

Contract: ``docs/research/cpm/competition-v1.md`` section 7. The track ships
SHADOW, not weight-eligible, at 0 bps: it folds and logs, pays nothing, and
can never skip or alter the memory weight update.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest

from ditto.api_models.model_context_ledger import (
    ModelContextLedgerEntry,
    ModelContextLedgerResponse,
)
from ditto.tests import platform_model_context
from ditto.tests.validator.test_worker import _config, _platform_with_ledger
from ditto.tests.validator.test_worker import _entry as _memory_entry
from ditto.validator.config import (
    BASIS_POINT_SCALE,
    MODEL_CONTEXT_RANK_SHARES,
    ValidatorConfig,
)
from ditto.validator.errors import PlatformError
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import ledger_signing_message
from ditto.validator.tracks import (
    _SAFE_TRACK_SHARES_BPS,
    TRACK_MEMORY,
    TRACK_MODEL_CONTEXT,
    TrackFoldInputs,
    TrackState,
    build_default_registry,
    model_context_fold,
)
from ditto.validator.weights import (
    compute_model_context_weights,
    resolve_model_context_rank_shares,
)
from ditto.validator.worker import PlatformModelContextLedgerSource, ValidatorWorker

_CHAMPION = "5Champion" + "x" * 39
_CPM_A = "5CpmMinerA" + "x" * 38
_CPM_B = "5CpmMinerB" + "x" * 38


def _entry(
    hotkey: str,
    score: float,
    *,
    eligible: bool = True,
    block: int = 100,
    submission_id: UUID | None = None,
) -> ModelContextLedgerEntry:
    return ModelContextLedgerEntry(
        hotkey=hotkey,
        submission_id=submission_id or uuid4(),
        score=score,
        eligible=eligible,
        commitment_block=block,
        season=1,
    )


def _shipped(name: str) -> Any:
    return ValidatorConfig.__dataclass_fields__[name].default


# --- registry -----------------------------------------------------------------


def test_registry_includes_the_track_in_shadow_at_zero_bps() -> None:
    registry = build_default_registry(track_shares_bps={TRACK_MEMORY: 10_000})
    track = registry.get(TRACK_MODEL_CONTEXT)
    assert track is not None
    assert track.state is TrackState.SHADOW
    assert not track.weight_eligible
    assert track.emission_share_bps == 0
    assert track.unclaimed_share_to == TRACK_MEMORY
    assert track.fold is model_context_fold
    assert registry.shares_bps() == {TRACK_MEMORY: BASIS_POINT_SCALE}


def test_a_served_share_still_pays_nothing_in_shadow() -> None:
    registry = build_default_registry(
        track_shares_bps={TRACK_MEMORY: 9_000, TRACK_MODEL_CONTEXT: 1_000}
    )
    track = registry.get(TRACK_MODEL_CONTEXT)
    assert track is not None and track.emission_share_bps == 1_000
    assert track.effective_bps() == 0
    assert TRACK_MODEL_CONTEXT not in registry.shares_bps()


def test_the_validator_config_ships_the_track_shadow() -> None:
    assert _shipped("model_context_track_state") == TrackState.SHADOW.value
    assert _shipped("model_context_weight_eligible") is False
    assert _shipped("model_context_rank_shares") == (0.50, 0.20, 0.13, 0.10, 0.07)
    assert _SAFE_TRACK_SHARES_BPS[TRACK_MODEL_CONTEXT] == 0


# --- fold ---------------------------------------------------------------------


def test_fold_ranks_by_score_then_commitment_block_then_submission_id() -> None:
    low, high = UUID(int=1), UUID(int=2)
    entries = [
        _entry("hk-c", 0.40, block=10),
        # Same score: the earlier commitment block wins.
        _entry("hk-late", 0.60, block=500),
        _entry("hk-early", 0.60, block=200),
        # Same score and block: the smaller submission id wins.
        _entry("hk-tie-b", 0.30, block=50, submission_id=high),
        _entry("hk-tie-a", 0.30, block=50, submission_id=low),
    ]
    weights = compute_model_context_weights(
        entries, rank_shares=MODEL_CONTEXT_RANK_SHARES
    )
    assert list(weights) == ["hk-early", "hk-late", "hk-c", "hk-tie-a", "hk-tie-b"]
    assert weights == {
        "hk-early": 0.50,
        "hk-late": 0.20,
        "hk-c": 0.13,
        "hk-tie-a": 0.10,
        "hk-tie-b": 0.07,
    }
    assert (
        model_context_fold(
            TrackFoldInputs(
                model_context_entries=entries,
                model_context_rank_shares=MODEL_CONTEXT_RANK_SHARES,
            )
        )
        == weights
    )


def test_ineligible_and_zero_score_entries_are_excluded() -> None:
    entries = [
        _entry("hk-ineligible", 0.99, eligible=False),
        _entry("hk-zero", 0.0),
        _entry("hk-ok", 0.20),
    ]
    assert compute_model_context_weights(entries, rank_shares=(3.0, 2.0)) == {
        "hk-ok": 3.0
    }


def test_fold_keeps_one_slot_per_hotkey_and_caps_at_the_curve() -> None:
    entries = [
        _entry("hk-a", 0.9),
        _entry("hk-a", 0.8),
        _entry("hk-b", 0.7),
        _entry("hk-c", 0.6),
    ]
    assert compute_model_context_weights(entries, rank_shares=(2.0, 1.0)) == {
        "hk-a": 2.0,
        "hk-b": 1.0,
    }


def test_empty_ledger_folds_to_nothing() -> None:
    assert (
        compute_model_context_weights([], rank_shares=MODEL_CONTEXT_RANK_SHARES) == {}
    )
    assert model_context_fold(TrackFoldInputs()) == {}


@pytest.mark.parametrize("shares", [(), (float("nan"),), (-0.1, 0.5)])
def test_malformed_rank_shares_fold_to_nothing(shares: tuple[float, ...]) -> None:
    assert compute_model_context_weights([_entry("hk", 0.5)], rank_shares=shares) == {}


def test_a_zero_share_rank_gets_no_entry() -> None:
    entries = [_entry("hk-a", 0.9), _entry("hk-b", 0.8)]
    assert compute_model_context_weights(entries, rank_shares=(1.0, 0.0)) == {
        "hk-a": 1.0
    }


# --- rank shares ----------------------------------------------------------------


def test_served_rank_shares_win_over_the_default() -> None:
    assert resolve_model_context_rank_shares(
        [0.6, 0.4], default=MODEL_CONTEXT_RANK_SHARES
    ) == (0.6, 0.4)
    assert resolve_model_context_rank_shares(
        [1.0, 0.0, 0.0], default=MODEL_CONTEXT_RANK_SHARES
    ) == (1.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "served", [None, [], [0.0, 0.0], [float("inf")], [-0.5, 0.5], ["x"]]
)
def test_absent_or_unusable_rank_shares_fall_back(served: Any) -> None:
    assert (
        resolve_model_context_rank_shares(served, default=MODEL_CONTEXT_RANK_SHARES)
        == MODEL_CONTEXT_RANK_SHARES
    )


# --- wire shape -------------------------------------------------------------------


def test_a_platform_built_ledger_parses_and_folds() -> None:
    """Platform's own ``ModelContextLedgerResponse``, serialized, is what the
    validator reads; its extra fields are ignored."""
    platform = platform_model_context.load()
    sub_a, sub_b = uuid4(), uuid4()
    served = platform.ModelContextLedgerResponse(
        entries=[
            platform.ModelContextLedgerEntry(
                hotkey=_CPM_A,
                submission_id=sub_a,
                score=0.5,
                eligible=True,
                commitment_block=900,
                season=3,
            ),
            platform.ModelContextLedgerEntry(
                hotkey=_CPM_B,
                submission_id=sub_b,
                score=0.5,
                eligible=True,
                commitment_block=800,
                season=3,
            ),
        ],
        count=2,
        season=3,
        mode="shadow",
        rank_shares=[0.7, 0.3],
    ).model_dump_json()
    ledger = ModelContextLedgerResponse.model_validate_json(served)
    assert ledger.season == 3 and ledger.mode == "shadow"
    shares = resolve_model_context_rank_shares(
        ledger.rank_shares, default=MODEL_CONTEXT_RANK_SHARES
    )
    assert compute_model_context_weights(ledger.entries, rank_shares=shares) == {
        _CPM_B: 0.7,
        _CPM_A: 0.3,
    }


def test_the_platform_default_ledger_parses_empty() -> None:
    platform = platform_model_context.load()
    ledger = ModelContextLedgerResponse.model_validate_json(
        platform.ModelContextLedgerResponse().model_dump_json()
    )
    assert ledger.entries == [] and ledger.mode == "off"


def test_ledger_ignores_unknown_fields_and_modes() -> None:
    ledger = ModelContextLedgerResponse.model_validate(
        {"entries": [], "count": 0, "mode": "future-mode", "extra": {"x": 1}}
    )
    assert ledger.mode == "future-mode" and ledger.rank_shares is None


async def test_ledger_source_fails_closed_to_none() -> None:
    async def _boom() -> ModelContextLedgerResponse:
        raise RuntimeError("platform down")

    assert await PlatformModelContextLedgerSource(read=_boom).fetch() is None


# --- worker ---------------------------------------------------------------------


async def _run_epoch(
    *,
    source: MagicMock | None,
    track_shares_bps: dict[str, int] | None = None,
) -> dict[str, float] | None:
    platform = _platform_with_memory()
    chain = MagicMock()
    chain.get_recent_neurons = AsyncMock(
        return_value=[
            SimpleNamespace(hotkey=hotkey) for hotkey in (_CHAMPION, _CPM_A, _CPM_B)
        ]
    )
    chain.put_weights = AsyncMock()
    config = _config()
    if track_shares_bps is not None:
        config.track_shares_bps = track_shares_bps
    worker = ValidatorWorker(
        config=config,
        platform=platform,
        dittobench=MagicMock(),
        chain=chain,
        keypair=MagicMock(),
        model_context_ledger_source=source,
    )
    await worker.run_once()
    if not chain.put_weights.await_count:
        return None
    return dict(chain.put_weights.await_args.args[0])


def _platform_with_memory() -> MagicMock:
    return _platform_with_ledger(jobs=[], ledger=[_memory_entry(_CHAMPION, 0.9)])


def _source(result: ModelContextLedgerResponse | None) -> MagicMock:
    source = MagicMock()
    source.fetch = AsyncMock(return_value=result)
    return source


async def test_unreadable_ledger_at_zero_share_leaves_memory_weights_unchanged() -> (
    None
):
    baseline = await _run_epoch(source=None)
    assert baseline is not None and baseline.get(_CHAMPION, 0.0) > 0.0

    failing = _source(None)
    unreadable = await _run_epoch(source=failing)
    failing.fetch.assert_awaited_once()
    assert unreadable == baseline


async def test_unreadable_ledger_never_skips_the_epoch_even_with_a_served_share() -> (
    None
):
    """A share configured for the shadow track has no effect: it does not pay,
    and a failed read still never holds the memory update back."""
    shares = {TRACK_MEMORY: 9_000, TRACK_MODEL_CONTEXT: 1_000}
    baseline = await _run_epoch(
        source=_source(ModelContextLedgerResponse()), track_shares_bps=shares
    )
    unreadable = await _run_epoch(source=_source(None), track_shares_bps=shares)
    assert unreadable is not None
    assert unreadable == baseline


async def test_a_scored_shadow_ledger_changes_no_weight() -> None:
    baseline = await _run_epoch(source=None)
    scored = await _run_epoch(
        source=_source(
            ModelContextLedgerResponse(
                entries=[_entry(_CPM_A, 0.8), _entry(_CPM_B, 0.6)],
                count=2,
                season=1,
                mode="shadow",
            )
        )
    )
    assert scored == baseline
    assert scored is not None and _CPM_A not in scored and _CPM_B not in scored


# --- platform client ------------------------------------------------------------


async def test_ledger_request_is_signed_and_reads_the_model_context_route() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    seen: dict[str, str] = {}
    submission_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        message = ledger_signing_message(
            validator_hotkey=keypair.ss58_address,
            nonce=UUID(request.headers["X-Validator-Ledger-Nonce"]),
            requested_at=datetime.fromisoformat(
                request.headers["X-Validator-Ledger-Requested-At"]
            ),
        )
        assert keypair.verify(
            message, bytes.fromhex(request.headers["X-Validator-Ledger-Signature"])
        )
        return httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "hotkey": _CPM_A,
                        "submission_id": str(submission_id),
                        "score": 0.42,
                        "eligible": True,
                        "commitment_block": 1234,
                        "season": 2,
                    }
                ],
                "count": 1,
                "season": 2,
                "cpm_score_version": 1,
                "mode": "shadow",
                "rank_shares": [0.5, 0.2, 0.13, 0.1, 0.07],
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ledger = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).get_model_context_ledger()

    assert seen["path"].endswith("/scoring/model-context-ledger")
    (entry,) = ledger.entries
    assert entry.submission_id == submission_id
    assert entry.commitment_block == 1234 and entry.eligible


async def test_ledger_non_200_raises_platform_error() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match="model_context ledger rejected"):
            await PlatformClient(config, http, keypair).get_model_context_ledger()  # type: ignore[arg-type]
