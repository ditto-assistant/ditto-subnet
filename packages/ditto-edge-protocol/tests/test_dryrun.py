from __future__ import annotations

from typing import Any

import pytest

from ditto_edge_protocol.dryrun import DryRunConfig, run_dry_run
from ditto_edge_protocol.settings import LaneSettings
from ditto_edge_protocol.sources import PublicState, SourceObject, synthetic_objects


def test_default_dry_run_catches_every_adversary_and_fails_no_honest_node() -> None:
    report = run_dry_run(DryRunConfig(), synthetic_objects(40, seed=0))
    verdict = report["verdict"]
    assert verdict["pass"] is True
    assert verdict["honest_false_fail_rate"] == 0.0
    assert verdict["adversaries_caught"] == {
        "lazy": True,
        "keeps_revoked": True,
        "tamper": True,
    }
    assert verdict["part_time_paid_less"] is True
    assert verdict["revocation_within_bound"] is True
    assert report["payouts"] == "none"
    assert report["weight_eligible"] is False
    assert all(
        window["weight_eligible"] is False
        for node in report["nodes"]
        for window in node["windows"]
    )
    by_behavior = {node["behavior"]: node for node in report["nodes"]}
    assert "integrity_failure" in by_behavior["tamper"]["reasons"]
    assert "revocation_violation" in by_behavior["keeps_revoked"]["reasons"]
    lazy_rows = by_behavior["lazy"]["windows"]
    assert sum(row["refilled"] for row in lazy_rows) > 0
    assert by_behavior["lazy"]["verified_units"] == 0.0


def test_dry_run_is_deterministic_for_a_seed() -> None:
    objects = synthetic_objects(12, seed=5)
    config = DryRunConfig(honest_nodes=3, windows=2, seed=9)
    assert run_dry_run(config, objects) == run_dry_run(config, objects)


class _Unpublishing:
    """A public source where one share is unpublished from the second window."""

    def __init__(self, share_id: str) -> None:
        self.share_id = share_id
        self.checks = 0

    def state(self, share_id: str) -> PublicState:
        self.checks += 1
        if share_id == self.share_id and self.checks > 1:
            return "unpublished"
        return "public"


def test_an_unpublished_share_is_revoked_and_released_within_the_bound() -> None:
    objects = synthetic_objects(10, seed=1) + [
        SourceObject(
            b"a public memory, published then unpublished",
            "text/markdown",
            "share:abc123",
        )
    ]
    source = _Unpublishing("abc123")
    report = run_dry_run(
        DryRunConfig(
            honest_nodes=3,
            part_time_nodes=0,
            adversaries=(),
            windows=3,
            revoke_fraction=0,
        ),
        objects,
        public_source=source,
    )
    assert report["objects"]["public_shares"] == 1
    assert report["objects"]["revoked_unpublished"] == 1
    revocation = report["revocation"]
    assert revocation["honest_not_released"] == 0
    assert revocation["max_release_seconds"] is not None
    assert revocation["max_release_seconds"] <= revocation["bound_seconds"]


def test_part_time_comparison_is_none_without_both_groups() -> None:
    report = run_dry_run(
        DryRunConfig(honest_nodes=2, part_time_nodes=0, adversaries=(), windows=2),
        synthetic_objects(8, seed=2),
    )
    assert report["verdict"]["part_time_paid_less"] is None


def test_every_instance_of_a_repeated_adversary_must_be_caught() -> None:
    report = run_dry_run(
        DryRunConfig(honest_nodes=3, adversaries=("tamper", "tamper"), windows=3),
        synthetic_objects(16, seed=4),
    )
    tamper_nodes = [n for n in report["nodes"] if n["behavior"] == "tamper"]
    assert len(tamper_nodes) == 2
    caught_all = all(n["first_flagged_window"] is not None for n in tamper_nodes)
    assert report["verdict"]["adversaries_caught"]["tamper"] is caught_all


def test_a_window_shorter_than_one_poll_is_refused() -> None:
    settings = LaneSettings(
        mode="shadow", window_seconds=300, poll_interval_seconds=600, lease_seconds=1200
    )
    with pytest.raises(ValueError, match="poll interval"):
        run_dry_run(DryRunConfig(settings=settings), synthetic_objects(4))


class _States:
    """A public source with fixed states that records every check."""

    def __init__(self, states: dict[str, PublicState]) -> None:
        self.states = states
        self.checked: set[str] = set()

    def state(self, share_id: str) -> PublicState:
        self.checked.add(share_id)
        return self.states.get(share_id, "public")


def _two_shares_one_digest(source: _States) -> dict[str, Any]:
    same = b"the same public note, shared twice"
    objects = synthetic_objects(6, seed=1) + [
        SourceObject(same, "text/markdown", "share:first"),
        SourceObject(same, "text/markdown", "share:second"),
    ]
    return run_dry_run(
        DryRunConfig(
            honest_nodes=2,
            part_time_nodes=0,
            adversaries=(),
            windows=2,
            revoke_fraction=0,
        ),
        objects,
        public_source=source,
    )


def test_identical_shares_keep_every_label_and_stay_while_one_is_public() -> None:
    source = _States({"first": "unpublished"})
    report = _two_shares_one_digest(source)
    assert report["objects"]["public_shares"] == 2
    assert report["objects"]["revoked_unpublished"] == 0
    assert source.checked == {"first", "second"}


def test_identical_shares_are_revoked_once_every_one_is_unpublished() -> None:
    source = _States({"first": "unpublished", "second": "unpublished"})
    report = _two_shares_one_digest(source)
    assert report["objects"]["public_shares"] == 2
    assert report["objects"]["revoked_unpublished"] == 1
    assert source.checked == {"first", "second"}


def test_a_run_that_exercised_no_check_cannot_pass() -> None:
    objects = synthetic_objects(12, seed=2)
    unchallenged = run_dry_run(
        DryRunConfig(honest_nodes=3, part_time_nodes=0, adversaries=(), windows=3),
        objects,
    )
    assert unchallenged["verdict"]["adversaries_caught"] == {}
    assert unchallenged["verdict"]["pass"] is False
    unrevoked = run_dry_run(
        DryRunConfig(honest_nodes=3, windows=3, revoke_fraction=0), objects
    )
    assert unrevoked["verdict"]["revocation_within_bound"] is None
    assert unrevoked["verdict"]["pass"] is False
