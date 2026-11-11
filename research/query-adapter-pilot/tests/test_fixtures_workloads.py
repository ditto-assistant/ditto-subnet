"""Committed synthetic workloads are deterministic, clean, and shaped for
every proof scenario."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from query_adapter_pilot import fixtures
from query_adapter_pilot.contract import CONTRACT_VERSION, TENANT_RE
from query_adapter_pilot.workload import Workload

WORKLOADS = Path(__file__).resolve().parents[1] / "testdata" / "workloads"


def _load(name: str) -> Workload:
    return Workload.load(WORKLOADS / f"{name}.json")


def test_builders_are_deterministic() -> None:
    first, second = fixtures.build_all(), fixtures.build_all()
    assert first == second
    assert [fixtures.render(d) for d in first.values()] == [
        fixtures.render(d) for d in second.values()
    ]
    assert tuple(first) == fixtures.WORKLOAD_NAMES


def test_committed_workloads_have_no_drift() -> None:
    assert fixtures.DEFAULT_DIR == WORKLOADS
    assert fixtures.check(WORKLOADS) == []


def test_check_cli_clean_and_detects_drift(tmp_path: Path) -> None:
    cli = [sys.executable, "-m", "query_adapter_pilot.fixtures"]
    clean = subprocess.run([*cli, "--check"], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr

    assert subprocess.run([*cli, "--write", str(tmp_path)], capture_output=True).returncode == 0
    assert subprocess.run([*cli, "--check", str(tmp_path)], capture_output=True).returncode == 0

    smoke = tmp_path / "smoke.json"
    doc = json.loads(smoke.read_text())
    doc["requests"][0]["query_seed"] += 1
    smoke.write_text(json.dumps(doc))
    (tmp_path / "stray.json").write_text("{}")
    (tmp_path / "happy.json").unlink()
    drift = subprocess.run([*cli, "--check", str(tmp_path)], capture_output=True, text=True)
    assert drift.returncode == 1
    assert "drift" in drift.stderr and "missing" in drift.stderr and "unexpected" in drift.stderr


@pytest.mark.parametrize("name", fixtures.WORKLOAD_NAMES)
def test_every_workload_loads_and_is_synthetic(name: str) -> None:
    raw = json.loads((WORKLOADS / f"{name}.json").read_text())
    assert raw["contract"] == CONTRACT_VERSION and raw["name"] == name
    wl = Workload.from_json(raw)
    assert wl.name == name and wl.requests
    for u in wl.users:
        assert TENANT_RE.fullmatch(u.user_ref) and u.user_ref.startswith("syn-")
    assert all(r.index == i for i, r in enumerate(wl.requests))


def test_seeds_are_unique_across_workloads() -> None:
    adapter_seeds: list[int] = []
    query_seeds: set[int] = set()
    for name in fixtures.WORKLOAD_NAMES:
        wl = _load(name)
        adapter_seeds += [a.seed for u in wl.users for a in u.adapters]
        query_seeds |= {r.query_seed for r in wl.requests if r.replay_of is None}
    assert len(adapter_seeds) == len(set(adapter_seeds))
    total_originals = sum(
        1 for n in fixtures.WORKLOAD_NAMES for r in _load(n).requests if r.replay_of is None
    )
    assert len(query_seeds) == total_originals - _shared_isolation_queries()


def _shared_isolation_queries() -> int:
    """isolation deliberately sends each round's query to every tenant."""
    return fixtures.ISOLATION_ROUNDS * (fixtures.ISOLATION_USERS - 1)


def test_write_removes_only_unexpected_top_level_json(tmp_path: Path) -> None:
    (tmp_path / "stray.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.json").write_text("{}")
    fixtures.write(tmp_path)
    assert not (tmp_path / "stray.json").exists()
    assert (tmp_path / "notes.txt").read_text() == "keep me"
    assert (tmp_path / "sub" / "nested.json").is_file()
    assert fixtures.check(tmp_path) == []


def test_builder_order_invariant_survives_optimize() -> None:
    with pytest.raises(RuntimeError):
        fixtures.verify_builder_order({"happy": None, "smoke": None}, ("smoke", "happy"))
    fixtures.verify_builder_order(fixtures.BUILDERS, fixtures.WORKLOAD_NAMES)
    code = (
        "import query_adapter_pilot.fixtures as f\n"
        "try:\n"
        "    f.verify_builder_order({'b': 1}, ('a',))\n"
        "except RuntimeError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    proc = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_smoke_and_happy_shapes() -> None:
    smoke, happy = _load("smoke"), _load("happy")
    assert len(smoke.users) == 1 and len(smoke.requests) == 3
    assert len(happy.users) == 3 and len(happy.requests) == 30
    for wl in (smoke, happy):
        for u in wl.users:
            assert u.provision == (1,) and u.authorized_version == 1


def test_multi_tenant_overflows_a_small_cache_then_repeats() -> None:
    wl = _load("multi_tenant")
    assert len(wl.users) >= 32
    for u in wl.users:
        assert [a.version for a in u.adapters] == [1, 2]
        assert u.provision == (1, 2) and u.authorized_version == 2
    refs = [u.user_ref for u in wl.users]
    first_pass = [r.user_ref for r in wl.requests[: len(refs)]]
    assert first_pass == refs  # round-robin: every tenant touched before any repeat
    assert len(set(first_pass)) > 8  # exceeds the proof's --cache-capacity 8
    tail = [r.user_ref for r in wl.requests[-fixtures.MULTI_TENANT_HOT_REPEATS :]]
    assert len(set(tail)) == 1  # back-to-back repeats on one tenant -> warm hits


def test_negative_case_divergence() -> None:
    (stale,) = _load("stale").users
    assert stale.provision == (1, 2) and stale.authorized_version == 1
    (missing,) = _load("missing").users
    assert missing.provision == (1,) and missing.authorized_version == 2
    assert missing.adapter(2) is not None  # Ditto owns v2; the miner never got it
    unauth = _load("unauthorized")
    assert sorted(u.authorized_version is None for u in unauth.users) == [False, True]
    assert {r.user_ref for r in unauth.requests} == {u.user_ref for u in unauth.users}


def test_replay_layout() -> None:
    wl = _load("replay")
    replays = [r for r in wl.requests if r.replay_of is not None]
    originals = [r for r in wl.requests if r.replay_of is None]
    assert len(originals) == 6 and len(replays) == 4
    for r in replays:
        orig = wl.requests[r.replay_of or 0]
        assert orig.replay_of is None and r.index > orig.index
        assert (r.user_ref, r.query_seed) == (orig.user_ref, orig.query_seed)


def test_isolation_shares_versions_and_queries_but_not_seeds() -> None:
    wl = _load("isolation")
    assert len(wl.users) >= 2
    versions = {tuple(a.version for a in u.adapters) for u in wl.users}
    assert len(versions) == 1  # same version numbers for every tenant
    for v in next(iter(versions)):
        seeds = [u.adapter(v).seed for u in wl.users]  # type: ignore[union-attr]
        assert len(set(seeds)) == len(seeds)
    users = [r.user_ref for r in wl.requests]
    assert all(a != b for a, b in zip(users, users[1:], strict=False))  # interleaved
    n = len(wl.users)
    for k in range(0, len(wl.requests), n):
        assert len({r.query_seed for r in wl.requests[k : k + n]}) == 1
