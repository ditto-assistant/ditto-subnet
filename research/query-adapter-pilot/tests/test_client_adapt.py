"""PilotClient against the loopback fake miner: validation, verification,
fallback, authorization, scoped ids and usage dedupe."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from test_client_fake_miner import TOKEN, FakeMiner, closed_port_url, user, workload_json

from query_adapter_pilot import reference
from query_adapter_pilot.client import PilotClient, RequestResult, should_verify, tenant_alias
from query_adapter_pilot.contract import (
    PARITY_MAX_ABS,
    PARITY_MIN_COSINE,
    REQUEST_ID_RE,
    ErrorCode,
    Outcome,
    Source,
)
from query_adapter_pilot.workload import Workload

SALT = "test-salt"


@pytest.fixture
def miner() -> Iterator[FakeMiner]:
    with FakeMiner() as m:
        yield m


def _workload(users: list[dict[str, Any]], requests: list[dict[str, Any]]) -> Workload:
    return Workload.from_json(workload_json(users, requests))


def _client(url: str, run_id: str = "run1", **kw: Any) -> PilotClient:
    return PilotClient(url, kw.pop("token", TOKEN), run_id, salt=SALT, **kw)


def _run(client: PilotClient, wl: Workload) -> list[RequestResult]:
    client.provision(wl)
    return [client.adapt(spec) for spec in wl.requests]


def _expected(wl: Workload, ref: str, seed: int) -> np.ndarray[Any, Any]:
    u = wl.user(ref)
    assert u.authorized_version is not None
    spec = u.adapter(u.authorized_version)
    assert spec is not None
    return reference.apply(spec.weights(), reference.synthetic_query(seed))


def _one_user(**kw: Any) -> Workload:
    return _workload([user("syn-alice", **kw)], [{"user_ref": "syn-alice", "query_seed": 7}])


def test_happy_path_all_ok_with_parity(miner: FakeMiner) -> None:
    wl = _workload(
        [user("syn-alice"), user("syn-bob", provision=[1, 2], authorized=2)],
        [{"user_ref": u, "query_seed": s} for s in range(3) for u in ("syn-alice", "syn-bob")],
    )
    client = _client(miner.url)
    entries = client.provision(wl)
    assert [(e["user_ref"], e["adapter_version"], e["status"]) for e in entries] == [
        ("syn-alice", 1, 200),
        ("syn-bob", 1, 200),
        ("syn-bob", 2, 200),
    ]
    results = [client.adapt(spec) for spec in wl.requests]
    assert {r.outcome for r in results} == {Outcome.OK}
    assert {r.source for r in results} == {Source.MINER}
    for r in results:
        assert r.verified
        assert r.reference_cos >= PARITY_MIN_COSINE
        assert r.reference_max_abs <= PARITY_MAX_ABS
        assert r.miner_compute_us == 42
        assert r.miner_code is None
        assert r.latency_ms > 0
    assert [r.cache for r in results[:2]] == ["miss", "miss"]
    assert {r.cache for r in results[2:]} == {"hit"}
    assert results[1].adapter_version == 2
    assert client.ledger.to_dict()["accepted_units"] == 6


def test_incorrect_output_falls_back_local_with_correct_vector(miner: FakeMiner) -> None:
    miner.mode = "perturb"
    wl = _one_user()
    (r,) = _run(_client(miner.url), wl)
    assert r.outcome == Outcome.REJECTED_INCORRECT
    assert r.source == Source.FALLBACK_LOCAL
    assert r.verified
    np.testing.assert_array_equal(r.vector, _expected(wl, "syn-alice", 7))
    assert r.reference_cos >= PARITY_MIN_COSINE and r.reference_max_abs == 0.0


@pytest.mark.parametrize(
    "mode",
    [
        "nan",
        "bad_norm",
        "garbage",
        "wrong_request_id",
        "wrong_tenant",
        "wrong_version",
        "wrong_digest",
    ],
)
def test_invalid_output_rejected_and_never_used(miner: FakeMiner, mode: str) -> None:
    miner.mode = mode
    wl = _one_user()
    client = _client(miner.url)
    (r,) = _run(client, wl)
    assert r.outcome == Outcome.REJECTED_INVALID, r.detail
    assert r.source == Source.FALLBACK_LOCAL
    assert not r.verified
    np.testing.assert_array_equal(r.vector, _expected(wl, "syn-alice", 7))
    assert client.ledger.accepted_units == 0


def test_deeply_nested_body_is_invalid_and_falls_back(miner: FakeMiner) -> None:
    """A recursion bomb under the size cap must not abort the run."""
    miner.mode = "deep_json"
    wl = _one_user()
    client = _client(miner.url)
    (r,) = _run(client, wl)
    assert (r.outcome, r.source) == (Outcome.REJECTED_INVALID, Source.FALLBACK_LOCAL)
    np.testing.assert_array_equal(r.vector, _expected(wl, "syn-alice", 7))
    assert client.ledger.accepted_units == 0


def test_unavailable_miner_falls_back() -> None:
    wl = _one_user()
    client = _client(closed_port_url())
    entries = client.provision(wl)
    assert entries[0]["code"] == Outcome.UNAVAILABLE and "status" not in entries[0]
    r = client.adapt(wl.requests[0])
    assert r.outcome == Outcome.UNAVAILABLE
    assert r.source == Source.FALLBACK_LOCAL
    np.testing.assert_array_equal(r.vector, _expected(wl, "syn-alice", 7))


def test_slow_miner_times_out_and_falls_back(miner: FakeMiner) -> None:
    wl = _one_user()
    client = _client(miner.url, timeout_s=0.05)
    client.provision(wl)
    miner.mode = "slow"
    r = client.adapt(wl.requests[0])
    assert r.outcome == Outcome.TIMEOUT
    assert r.source == Source.FALLBACK_LOCAL
    assert r.latency_ms < 400  # did not wait for the 500 ms miner


def test_stale_and_missing_codes_mapped(miner: FakeMiner) -> None:
    wl = _workload(
        [
            user("syn-stale", provision=[1, 2], authorized=1),
            user("syn-missing", provision=[1], authorized=2),
        ],
        [{"user_ref": "syn-stale", "query_seed": 1}, {"user_ref": "syn-missing", "query_seed": 2}],
    )
    stale, missing = _run(_client(miner.url), wl)
    assert (stale.outcome, stale.miner_code) == (Outcome.REJECTED_STALE, ErrorCode.STALE_VERSION)
    assert (missing.outcome, missing.miner_code) == (
        Outcome.REJECTED_MISSING,
        ErrorCode.MISSING_VERSION,
    )
    for r, ref, seed in ((stale, "syn-stale", 1), (missing, "syn-missing", 2)):
        assert r.source == Source.FALLBACK_LOCAL
        np.testing.assert_array_equal(r.vector, _expected(wl, ref, seed))


def test_other_miner_codes_are_miner_error(miner: FakeMiner) -> None:
    miner.mode = f"error:{ErrorCode.CAPACITY}"
    (r,) = _run(_client(miner.url), _one_user())
    assert (r.outcome, r.miner_code, r.source) == (
        Outcome.MINER_ERROR,
        ErrorCode.CAPACITY,
        Source.FALLBACK_LOCAL,
    )


def test_wrong_token_is_miner_error(miner: FakeMiner) -> None:
    client = _client(miner.url, token="not-the-token")
    wl = _one_user()
    entries = client.provision(wl)
    assert entries[0]["code"] == ErrorCode.UNAUTHORIZED
    r = client.adapt(wl.requests[0])
    assert (r.outcome, r.miner_code) == (Outcome.MINER_ERROR, ErrorCode.UNAUTHORIZED)


def test_unauthorized_makes_no_miner_call_and_uses_identity(miner: FakeMiner) -> None:
    wl = _one_user(provision=[1], authorized=None)
    client = _client(miner.url)
    client.provision(wl)
    r = client.adapt(wl.requests[0])
    assert miner.calls["adapt"] == 0
    assert r.outcome == Outcome.REJECTED_UNAUTHORIZED
    assert r.source == Source.FALLBACK_IDENTITY
    assert r.adapter_version is None and r.miner_code is None and not r.verified
    np.testing.assert_array_equal(r.vector, reference.identity(reference.synthetic_query(7)))
    assert r.reference_cos >= PARITY_MIN_COSINE
    assert client.ledger.to_dict()["by_outcome"] == {Outcome.REJECTED_UNAUTHORIZED: 1}


def test_identity_fallback_mode(miner: FakeMiner) -> None:
    miner.mode = "perturb"
    (r,) = _run(_client(miner.url, fallback="identity"), _one_user())
    assert (r.outcome, r.source) == (Outcome.REJECTED_INCORRECT, Source.FALLBACK_IDENTITY)
    np.testing.assert_array_equal(r.vector, reference.identity(reference.synthetic_query(7)))
    assert r.reference_cos < PARITY_MIN_COSINE  # report-only: identity != adapted


def test_replay_counts_one_unit(miner: FakeMiner) -> None:
    wl = _workload(
        [user("syn-alice")],
        [{"user_ref": "syn-alice", "query_seed": 7}, {"replay_of": 0}, {"replay_of": 0}],
    )
    client = _client(miner.url)
    first, again, third = _run(client, wl)
    assert first.request_id == again.request_id == third.request_id
    assert (first.outcome, again.outcome, third.outcome) == (
        Outcome.OK,
        Outcome.DUPLICATE,
        Outcome.DUPLICATE,
    )
    assert again.source == Source.MINER
    np.testing.assert_array_equal(first.vector, again.vector)
    ledger = client.ledger.to_dict()
    assert ledger["accepted_units"] == 1
    assert ledger["duplicates_suppressed"] == 2
    assert miner.calls["adapt"] == 3


def test_replay_after_failed_original_earns_its_single_unit(miner: FakeMiner) -> None:
    wl = _workload(
        [user("syn-alice")],
        [{"user_ref": "syn-alice", "query_seed": 7}, {"replay_of": 0}],
    )
    client = _client(miner.url)
    client.provision(wl)
    miner.mode = f"error:{ErrorCode.CAPACITY}"
    first = client.adapt(wl.requests[0])
    miner.mode = "honest"
    again = client.adapt(wl.requests[1])
    assert (first.outcome, again.outcome) == (Outcome.MINER_ERROR, Outcome.OK)
    assert client.ledger.to_dict()["accepted_units"] == 1
    assert client.ledger.to_dict()["duplicates_suppressed"] == 0
    assert client.ledger.history(first.request_id) == (Outcome.MINER_ERROR, Outcome.OK)


def test_verify_rate_zero_skips_verification_and_accepts_wrong_vector(miner: FakeMiner) -> None:
    """The documented cost of sampling: an unsampled plausible-but-wrong unit
    vector passes shape/echo/norm checks and is used."""
    miner.mode = "perturb"
    (r,) = _run(_client(miner.url, verify_rate=0.0), _one_user())
    assert r.outcome == Outcome.OK
    assert r.source == Source.MINER
    assert not r.verified
    assert r.reference_cos < PARITY_MIN_COSINE  # visible in the report, not the decision


def test_verify_rate_zero_still_rejects_invalid(miner: FakeMiner) -> None:
    miner.mode = "bad_norm"
    (r,) = _run(_client(miner.url, verify_rate=0.0), _one_user())
    assert r.outcome == Outcome.REJECTED_INVALID


def test_verification_sampling_is_deterministic() -> None:
    ids = [f"qa1.run1.syn-abc.{i}" for i in range(2000)]
    assert all(should_verify(i, 1.0) for i in ids)
    assert not any(should_verify(i, 0.0) for i in ids)
    half = [should_verify(i, 0.5) for i in ids]
    assert half == [should_verify(i, 0.5) for i in ids]
    assert 0.4 < sum(half) / len(ids) < 0.6


def test_request_ids_scoped_per_run_tenant_index_and_stable_across_replay() -> None:
    wl = _workload(
        [user("syn-alice"), user("syn-bob")],
        [
            {"user_ref": "syn-alice", "query_seed": 1},
            {"user_ref": "syn-bob", "query_seed": 1},
            {"user_ref": "syn-alice", "query_seed": 1},
            {"replay_of": 0},
        ],
    )
    a, b = _client(closed_port_url(), "run-a"), _client(closed_port_url(), "run-b")
    a.load_workload(wl)
    b.load_workload(wl)
    ids_a = [a.request_id(s) for s in wl.requests]
    ids_b = [b.request_id(s) for s in wl.requests]
    alice, bob = tenant_alias("syn-alice", SALT), tenant_alias("syn-bob", SALT)
    assert ids_a == [
        f"qa1.run-a.{alice}.0",
        f"qa1.run-a.{bob}.1",
        f"qa1.run-a.{alice}.2",
        f"qa1.run-a.{alice}.0",
    ]
    assert all(REQUEST_ID_RE.fullmatch(i) for i in ids_a + ids_b)
    assert set(ids_a).isdisjoint(ids_b)
    assert len(set(ids_a[:3])) == 3


def test_miner_never_sees_ditto_user_ref(miner: FakeMiner) -> None:
    wl = _workload(
        [user("syn-alice-private")], [{"user_ref": "syn-alice-private", "query_seed": 1}]
    )
    results = _run(_client(miner.url), wl)
    assert results[0].outcome == Outcome.OK
    assert miner.seen_bodies
    assert not any("syn-alice-private" in body for body in miner.seen_bodies)
    assert results[0].tenant_id == tenant_alias("syn-alice-private", SALT)


def test_proxy_environment_is_ignored(miner: FakeMiner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.1:9")
    monkeypatch.setenv("http_proxy", "http://203.0.113.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    (r,) = _run(_client(miner.url), _one_user())
    assert r.outcome == Outcome.OK


def test_constructor_rejects_bad_config(miner: FakeMiner) -> None:
    with pytest.raises(ValueError):
        _client(miner.url, fallback="remote")
    with pytest.raises(ValueError):
        _client(miner.url, verify_rate=1.5)
    with pytest.raises(ValueError):
        _client(miner.url, timeout_s=0.0)
    with pytest.raises(ValueError):
        _client(miner.url, run_id="Bad.Run")


def test_adapt_requires_a_workload(miner: FakeMiner) -> None:
    wl = _one_user()
    with pytest.raises(RuntimeError):
        _client(miner.url).adapt(wl.requests[0])
