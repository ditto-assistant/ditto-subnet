"""Unit tests for the loopback URL guard, tenant aliasing, authorization and
the usage ledger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from test_client_fake_miner import user, workload_json

from query_adapter_pilot import pilot
from query_adapter_pilot.client import (
    Authorizer,
    PilotClient,
    UsageLedger,
    tenant_alias,
    validate_miner_url,
)
from query_adapter_pilot.contract import TENANT_RE, Outcome
from query_adapter_pilot.workload import Workload


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8080",
        "https://127.0.0.1:8080",
        "http://10.0.0.1:8080",
        "http://0.0.0.0:8080",
        "http://127.0.0.2:8080",
        "http://127.0.0.1.nip.io:8080",
        "http://localhost.example.com",
        "http://user:pw@127.0.0.1:8080",
        "http://127.0.0.1:8080/v1",
        "http://127.0.0.1:8080/?x=1",
        "ftp://localhost",
        "127.0.0.1:8080",
        "http://127.0.0.1:notaport",
        "",
    ],
)
def test_non_loopback_or_odd_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        validate_miner_url(url)
    with pytest.raises(ValueError):
        PilotClient(url, "t", "run1", salt="s")


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
        ("http://localhost:9", "http://localhost:9"),
        ("http://LOCALHOST:9", "http://localhost:9"),
        ("http://[::1]:9", "http://[::1]:9"),
    ],
)
def test_loopback_urls_are_accepted(url: str, normalized: str) -> None:
    assert validate_miner_url(url) == normalized


@pytest.mark.parametrize("token", ["caf\u00e9", "tok\u2603en", "ASCII\tbut-control"])
def test_non_ascii_token_is_refused(token: str, tmp_path: Path) -> None:
    """Latin-1 tokens can never match the miner; above U+00FF http.client would
    raise UnicodeEncodeError mid-call. Refuse both at construction."""
    with pytest.raises(ValueError, match="ASCII"):
        PilotClient("http://127.0.0.1:9", token, "run1", salt="s")
    wl = tmp_path / "wl.json"
    wl.write_text(json.dumps(workload_json([user("syn-alice")], [])))
    tok = tmp_path / "token"
    tok.write_text(token, encoding="utf-8")
    out = tmp_path / "report.json"
    args = ["run", "--workload", str(wl), "--miner-url", "http://127.0.0.1:9"]
    args += ["--token-file", str(tok), "--out", str(out)]
    assert pilot.main(args) == 2
    assert not out.exists()


def test_tenant_alias_is_salted_sha256_prefix() -> None:
    alias = tenant_alias("syn-alice", "pepper")
    assert alias == "syn-" + hashlib.sha256(b"pepper|syn-alice").hexdigest()[:16]
    assert TENANT_RE.fullmatch(alias)
    assert alias != tenant_alias("syn-alice", "other")
    assert alias != tenant_alias("syn-bob", "pepper")


def test_authorizer_uses_ditto_owned_weights() -> None:
    wl = Workload.from_json(
        workload_json(
            [
                user("syn-alice", provision=[1, 2], authorized=2),
                user("syn-nobody", authorized=None),
            ],
            [],
        )
    )
    authz = Authorizer(wl, "s")
    grant = authz.authorize("syn-alice")
    assert grant is not None
    spec = wl.user("syn-alice").adapter(2)
    assert spec is not None
    expected = spec.weights()
    np.testing.assert_array_equal(grant.weights.W1, expected.W1)
    assert grant.ref.adapter_version == 2
    assert grant.ref.adapter_digest == expected.digest()
    assert grant.ref.tenant_id == tenant_alias("syn-alice", "s")
    assert authz.authorize("syn-nobody") is None
    assert authz.authorize("syn-unknown") is None


def test_ledger_counts_one_unit_per_request_id() -> None:
    ledger = UsageLedger()
    assert ledger.accept("qa1.r.syn-a.0") == Outcome.OK
    assert ledger.accept("qa1.r.syn-a.0") == Outcome.DUPLICATE
    assert ledger.accept("qa1.r.syn-a.1") == Outcome.OK
    ledger.reject("qa1.r.syn-a.2", Outcome.TIMEOUT)
    ledger.reject("qa1.r.syn-a.3", Outcome.REJECTED_INCORRECT)
    assert ledger.to_dict() == {
        "accepted_units": 2,
        "duplicates_suppressed": 1,
        "rejected": 2,
        "by_outcome": {
            Outcome.DUPLICATE: 1,
            Outcome.OK: 2,
            Outcome.REJECTED_INCORRECT: 1,
            Outcome.TIMEOUT: 1,
        },
        "payouts": "none",
    }
    with pytest.raises(ValueError):
        ledger.reject("qa1.r.syn-a.4", Outcome.OK)
