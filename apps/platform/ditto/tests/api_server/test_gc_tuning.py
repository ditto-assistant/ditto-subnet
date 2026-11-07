from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest

from ditto.api_server import gc_tuning
from ditto.api_server.gc_tuning import (
    DEFAULT_GC_THRESHOLDS,
    GcTuning,
    GcTuningConfigError,
    apply_gc_tuning,
    parse_gc_tuning_from_env,
)


@pytest.fixture
def restore_gc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    thresholds = gc.get_threshold()
    monkeypatch.setattr(gc_tuning, "_applied", False)
    try:
        yield
    finally:
        gc.unfreeze()
        gc.set_threshold(*thresholds)


def test_unset_uses_the_server_defaults() -> None:
    assert parse_gc_tuning_from_env({}) == GcTuning(thresholds=DEFAULT_GC_THRESHOLDS)


def test_off_leaves_the_interpreter_alone() -> None:
    assert parse_gc_tuning_from_env({"DITTO_API_GC_THRESHOLDS": " OFF "}) == GcTuning(
        thresholds=None
    )


def test_explicit_thresholds_are_parsed() -> None:
    tuning = parse_gc_tuning_from_env({"DITTO_API_GC_THRESHOLDS": "1000, 5, 7"})
    assert tuning == GcTuning(thresholds=(1000, 5, 7))


@pytest.mark.parametrize("raw", ["1000,5", "a,b,c", "1000,0,10", "1,2,3,4"])
def test_malformed_thresholds_fail_boot(raw: str) -> None:
    with pytest.raises(GcTuningConfigError):
        parse_gc_tuning_from_env({"DITTO_API_GC_THRESHOLDS": raw})


@pytest.mark.usefixtures("restore_gc")
def test_apply_freezes_once_and_sets_thresholds() -> None:
    assert apply_gc_tuning(GcTuning(thresholds=(12_345, 6, 7))) is True
    assert gc.get_threshold() == (12_345, 6, 7)
    assert gc.get_freeze_count() > 0
    # A second app in the same process does not re-freeze its request garbage.
    assert apply_gc_tuning(GcTuning(thresholds=(1, 1, 1))) is False
    assert gc.get_threshold() == (12_345, 6, 7)


@pytest.mark.usefixtures("restore_gc")
def test_apply_off_changes_nothing() -> None:
    before = (gc.get_threshold(), gc.get_freeze_count())
    assert apply_gc_tuning(GcTuning(thresholds=None)) is False
    assert (gc.get_threshold(), gc.get_freeze_count()) == before
