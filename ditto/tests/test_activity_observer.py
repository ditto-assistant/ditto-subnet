"""Default-off observer, durable backpressure, unknown delivery and public transport."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ditto.treasury.activity_export import export_finalized_distributions
from ditto.treasury.activity_observer import (
    PUBLIC_MCP,
    ActivityObserverConfig,
    ActivityQueue,
    ObservationUnavailable,
    PublicActivityMCP,
    observer_tick,
    run_observer,
)
from ditto.treasury.collector import canonical
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin


def fixture():
    pin = EnforcingTreasuryPin.model_validate_json(
        (
            Path(__file__).resolve().parents[2]
            / "packages/ditto-screening-protocol/tests/fixtures"
            / "treasury_enforcing_pin_v2.json"
        ).read_text()
    )
    policy = pin.policy
    settings = {
        "allocation_version": 2,
        "treasury_hotkey": policy.collector_hotkey,
        "treasury_coldkey": policy.collector_coldkey,
        "service_buckets": [
            {
                **bucket.model_dump(mode="json"),
                "payee_rules": [
                    {
                        "rule_id": "vendor",
                        "asset": "TAO",
                        "recipient_coldkey": "vendor",
                        "enabled": True,
                    }
                ],
            }
            for bucket in policy.buckets
        ],
    }
    checksum = hashlib.sha256(canonical(settings).encode()).hexdigest()
    return ActivityObserverConfig(pin.approval, checksum, 120, enabled=True), settings


def h(block):
    return f"0x{block:064x}"


def selection(block=120, amount=25):
    return {
        "stage": "vendor_payment",
        "epoch_index": 9,
        "bucket_id": "gamma",
        "source_block": None,
        "block": block,
        "block_hash": h(block),
        "extrinsic_index": 0,
        "extrinsic_hash": "0x" + hashlib.blake2b(b"\xab", digest_size=32).hexdigest(),
        "amount_atomic": amount,
        "payee_rule_id": "vendor",
        "parent_receipt_id": None,
        "reason": "Observe configured vendor payment",
    }


class Chain:
    def __init__(self, config, count=1):
        self.config, self.count = config, count
        self.hashes = {}
        self.height = 120

    def block_hash(self, block):
        return self.hashes.get(block, h(block))

    def epoch_at(self, _block):
        return 9

    def finalized_height(self):
        return self.height

    def finalized_payment_block(self, block):
        holding = self.config.approval.policy.buckets[0].holding_coldkey
        return (
            h(block),
            9,
            [
                {
                    "module_id": "Balances",
                    "event_id": "Transfer",
                    "phase": "ApplyExtrinsic",
                    "extrinsic_idx": index,
                    "event": {
                        "attributes": {"from": holding, "to": "vendor", "amount": 25}
                    },
                }
                for index in range(self.count)
            ],
            ["0xab"] * self.count,
        )


class MCP:
    def __init__(self, config, settings):
        self.config, self.settings = config, settings
        self.records, self.calls = {}, []
        self.unknown_once = False
        self.bad_ack = False
        self.closed = False

    def close(self):
        self.closed = True

    def call(self, name, arguments):
        if name == "get_treasury_settings":
            assert arguments == {"revision": self.config.approval.policy.revision}
            return {
                "history": [
                    {
                        "revision": 1,
                        "checksum": self.config.settings_checksum,
                        "settings": self.settings,
                    }
                ]
            }
        assert name == "record_treasury_receipt"
        assert arguments["confirmation"] == "INGEST VERIFIED TREASURY RECEIPT"
        self.calls.append(arguments)
        identity = hashlib.sha256(canonical(arguments).encode()).hexdigest()
        self.records[identity] = arguments
        if self.unknown_once:
            self.unknown_once = False
            raise ObservationUnavailable("response lost after durable acceptance")
        return {
            **{
                field: arguments[field]
                for field in (
                    "block",
                    "block_hash",
                    "extrinsic_index",
                    "extrinsic_hash",
                )
            },
            "status": "chain_finalized",
            "provider_credit_status": "not_proven",
            "stage": arguments["stage"],
            "policy_digest": self.config.approval.policy.digest,
            "bucket_id": arguments["bucket_id"],
            "epoch_index": arguments["epoch_index"] + int(self.bad_ack),
            "source_block": arguments.get("source_block"),
            "amount_atomic": str(arguments["amount_atomic"]),
            "receipt_id": identity,
        }


def run(tmp_path, config, chain, mcp, journal=None):
    return observer_tick(
        config,
        state_path=tmp_path / "private" / "queue.sqlite",
        transfer_journal=journal,
        chain=chain,
        mcp=mcp,
    )


def test_disabled_has_no_disk_network_or_signer(tmp_path):
    config, _ = fixture()
    assert run(tmp_path, replace(config, enabled=False), None, None) == {
        "status": "disabled",
        "authority": "none",
    }
    assert not (tmp_path / "private").exists()
    output = []
    run_observer(
        replace(config, enabled=False),
        state_path=None,
        transfer_journal=None,
        chain=None,
        mcp_factory=None,
        emit=output.append,
    )
    assert output == [{"status": "disabled", "authority": "none"}]


def test_actual_cli_disabled_without_token_or_chain(tmp_path):
    config, _ = fixture()
    body = {
        "approval": config.approval.model_dump(mode="json"),
        "settings_checksum": config.settings_checksum,
        "start_block": 120,
    }
    raw = canonical(body).encode()
    path = tmp_path / "public-config.json"
    path.write_bytes(raw)
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "BACKROOM_ACTIVITY_OBSERVER_TOKEN"
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/treasury_activity_observer.py",
            "--config",
            str(path),
            "--config-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--token-file",
            str(tmp_path / "absent-credential"),
            "--once",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == {"status": "disabled", "authority": "none"}


def test_unknown_ack_restart_replays_same_selector_without_duplicate_money(tmp_path):
    config, settings = fixture()
    chain, mcp = Chain(config), MCP(config, settings)
    mcp.unknown_once = True
    with pytest.raises(ObservationUnavailable):
        run(tmp_path, config, chain, mcp)
    assert len(mcp.records) == 1
    result = run(tmp_path, config, chain, mcp)
    assert result["delivered"] == 1 and result["pending"] == 0
    assert len(mcp.records) == 1 and mcp.calls[0] == mcp.calls[1]
    assert run(tmp_path, config, chain, mcp)["delivered"] == 0


def test_near_full_batch_does_not_advance_cursor_and_drains_then_progresses(tmp_path):
    config, settings = fixture()
    path = tmp_path / "private" / "queue.sqlite"
    queue = ActivityQueue(path, config)
    for amount in range(1, 999):
        queue.enqueue(selection(block=119, amount=amount))
    queue.close()
    chain, mcp = Chain(config, count=100), MCP(config, settings)
    result = run(tmp_path, config, chain, mcp)
    assert result["delivered"] == 100 and result["pending"] == 898
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM cursor").fetchall() == []
    result = run(tmp_path, config, chain, mcp)
    assert result["pending"] == 898
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT block,hash FROM cursor").fetchall() == [(120, h(120))]
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1098
    for _ in range(9):
        run(tmp_path, config, chain, mcp)
    assert len(mcp.records) == 1098 and len(mcp.calls) == 1098


def test_legacy_over_capacity_backlog_drains_without_permanent_refusal(tmp_path):
    config, settings = fixture()
    queue = ActivityQueue(tmp_path / "private" / "queue.sqlite", config)
    for amount in range(1, 1102):
        queue.enqueue(selection(block=119, amount=amount))
    queue.close()
    result = run(tmp_path, config, Chain(config), MCP(config, settings))
    assert result["pending"] == 1001 and result["delivered"] == 100


def test_large_block_progress_is_durable_and_reorg_fenced(tmp_path):
    config, settings = fixture()
    chain, mcp = Chain(config, count=150), MCP(config, settings)
    assert run(tmp_path, config, chain, mcp)["delivered"] == 100
    state = tmp_path / "private" / "queue.sqlite"
    with sqlite3.connect(state) as db:
        assert db.execute(
            "SELECT block,event_offset FROM block_progress"
        ).fetchall() == [(120, 100)]
        assert db.execute("SELECT * FROM cursor").fetchall() == []
    chain.hashes[120] = h(121)
    with pytest.raises(ValueError, match="partial finalized block"):
        run(tmp_path, config, chain, mcp)
    chain.hashes.clear()
    assert run(tmp_path, config, chain, mcp)["delivered"] == 50
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT * FROM block_progress").fetchall() == []
        assert db.execute("SELECT block FROM cursor").fetchall() == [(120,)]
    assert len(mcp.records) == 150


@pytest.mark.parametrize("fault", ["history", "signature", "ack", "reorg"])
def test_unproved_identity_ack_history_halts_without_dropping_pending(tmp_path, fault):
    config, settings = fixture()
    chain, mcp = Chain(config), MCP(config, settings)
    if fault == "history":
        mcp.settings = {**settings, "allocation_version": 1}
    elif fault == "signature":
        config = replace(
            config,
            approval=config.approval.model_copy(update={"signature": "0x" + "00" * 64}),
        )
    elif fault == "ack":
        mcp.bad_ack = True
    elif fault == "reorg":
        run(tmp_path, config, chain, mcp)
        chain.hashes[120] = h(121)
    with pytest.raises(ValueError):
        run(tmp_path, config, chain, mcp)
    if fault == "ack":
        mcp.bad_ack = False
        assert run(tmp_path, config, chain, mcp)["delivered"] == 1


def test_queue_invalid_pin_releases_lock_and_rejects_bool_bounds(tmp_path):
    config, _ = fixture()
    path = tmp_path / "private" / "queue.sqlite"
    queue = ActivityQueue(path, config)
    queue.close()
    with pytest.raises(ValueError):
        ActivityQueue(path, replace(config, start_block=121))
    queue = ActivityQueue(path, config)
    queue.close()
    with pytest.raises(ValueError):
        replace(config, max_blocks=True)
    with pytest.raises(ValueError):
        replace(config, max_deliveries=True)


def test_public_mcp_only_bounded_transport_and_no_general_tools():
    requests = []

    def handler(request):
        assert str(request.url) == PUBLIC_MCP
        assert request.headers["authorization"] == "Bearer SYNTHETIC"
        body = json.loads(request.content)
        requests.append(body)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = {}
        if body["method"] == "tools/list":
            result = {
                "tools": [
                    {"name": "get_treasury_settings"},
                    {"name": "record_treasury_receipt"},
                ]
            }
        elif body["method"] != "initialize":
            result = {"structuredContent": {"history": []}}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"mcp-session-id": "synthetic-session"},
        )

    mcp = PublicActivityMCP("SYNTHETIC", transport=httpx.MockTransport(handler))
    assert mcp.call("get_treasury_settings", {}) == {"history": []}
    with pytest.raises(ValueError):
        mcp.call("set_treasury_settings", {})
    mcp.close()
    assert len(requests) == 4


def test_only_receipt_call_waits_for_platform_proof_budget():
    deadlines = []

    def handler(request):
        body = json.loads(request.content)
        deadlines.append(
            (body.get("params", {}).get("name"), request.extensions["timeout"]["read"])
        )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = {}
        if body["method"] == "tools/list":
            result = {
                "tools": [
                    {"name": "get_treasury_settings"},
                    {"name": "record_treasury_receipt"},
                ]
            }
        elif body["method"] == "tools/call":
            result = {"structuredContent": {"receipt_id": "synthetic"}}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    client = PublicActivityMCP("SYNTHETIC", transport=httpx.MockTransport(handler))
    client.call("get_treasury_settings", {})
    client.call("record_treasury_receipt", {})
    client.close()
    assert deadlines[-2:] == [
        ("get_treasury_settings", 60),
        ("record_treasury_receipt", 130),
    ]
    assert all(timeout == 60 for _name, timeout in deadlines[:-1])


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"name": "get_treasury_settings"}],
        [
            {"name": "get_treasury_settings"},
            {"name": "record_treasury_receipt"},
            {"name": "set_treasury_settings"},
        ],
    ],
)
def test_observer_refuses_generic_or_incomplete_token_catalog(tools):
    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"tools": tools} if body["method"] == "tools/list" else {},
            },
        )

    with pytest.raises(ValueError, match="dedicated receipt-only"):
        PublicActivityMCP("SYNTHETIC", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [401, 403, 429, 503, 302])
def test_transport_retry_only_transient_status_no_consent_or_redirect(status):
    error = ObservationUnavailable if status in (429, 503) else ValueError
    with pytest.raises(error):
        PublicActivityMCP(
            "SYNTHETIC",
            transport=httpx.MockTransport(lambda _request: httpx.Response(status)),
        )


def test_lost_http_ack_is_retryable_but_explicit_protocol_refusal_is_not():
    explicit_refusal = False

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] in {"initialize", "tools/list"}:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {}
                    if body["method"] == "initialize"
                    else {
                        "tools": [
                            {"name": "get_treasury_settings"},
                            {"name": "record_treasury_receipt"},
                        ]
                    },
                },
            )
        if explicit_refusal:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32602, "message": "PRIVATE"},
                },
            )
        return httpx.Response(202)

    mcp = PublicActivityMCP("SYNTHETIC", transport=httpx.MockTransport(handler))
    with pytest.raises(ObservationUnavailable):
        mcp.call("get_treasury_settings", {})
    explicit_refusal = True
    with pytest.raises(ValueError, match="explicitly refused") as error:
        mcp.call("get_treasury_settings", {})
    assert "PRIVATE" not in str(error.value)
    mcp.close()


def test_daemon_bounded_backoff_reconnects_but_semantic_refusal_halts(tmp_path):
    config, settings = fixture()
    mcp = MCP(config, settings)
    attempts, delays, emitted = [], [], []

    def factory():
        attempts.append(1)
        if len(attempts) <= 7:
            raise ObservationUnavailable("synthetic outage")
        return mcp

    def sleep(seconds):
        delays.append(seconds)
        if emitted[-1]["status"] == "observed":
            raise ValueError("synthetic stop after success")

    with pytest.raises(ValueError, match="synthetic stop"):
        run_observer(
            config,
            state_path=tmp_path / "private" / "queue.sqlite",
            transfer_journal=None,
            chain=Chain(config),
            mcp_factory=factory,
            sleep=sleep,
            emit=emitted.append,
        )
    assert delays == [
        15,
        30,
        60,
        120,
        240,
        300,
        300,
        60,
    ]  # stop must follow success, not outage
    assert emitted[-1]["status"] == "observed" and mcp.closed


def journal(tmp_path, config):
    directory = tmp_path / "signer"
    directory.mkdir(mode=0o700)
    path = directory / "transfer.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE pin(digest TEXT,role TEXT); CREATE TABLE operations("
            "id INTEGER PRIMARY KEY,role TEXT,state TEXT,source_block INTEGER,"
            "bucket TEXT,amount INTEGER,settlement_json TEXT,signed_json TEXT);"
        )
        db.execute(
            "INSERT INTO pin VALUES(?,'transfer')",
            (config.approval.policy.collector_policy_digest,),
        )
        settlement = {
            "status": "finalized",
            "block": 120,
            "block_hash": h(120),
            "extrinsic_index": 0,
            "extrinsic_hash": selection()["extrinsic_hash"],
        }
        db.execute(
            "INSERT INTO operations VALUES(1,'transfer','dispatching',"
            "110,'gamma',40,?,?)",
            (json.dumps(settlement), "PRIVATE-SIGNED-PAYLOAD"),
        )
        db.execute(
            "INSERT INTO operations VALUES(2,'transfer','finalized',"
            "111,'gamma',40,?,?)",
            (json.dumps(settlement), "PRIVATE-SIGNED-PAYLOAD"),
        )
    os.chmod(path, 0o600)
    return path


def test_readonly_journal_does_not_skip_unresolved_earlier_operation(tmp_path):
    config, _ = fixture()
    path = journal(tmp_path, config)
    policy = SimpleNamespace(digest=config.approval.policy.collector_policy_digest)
    before = path.read_bytes()
    assert export_finalized_distributions(path, policy, lambda _block: 9) == []
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE operations SET state='finalized' WHERE id=1")
    items = export_finalized_distributions(path, policy, lambda _block: 9)
    assert [item["journal_operation_id"] for item in items] == [1, 2]
    assert "PRIVATE" not in json.dumps(items)
    assert (
        export_finalized_distributions(path, policy, lambda _block: 9, after_id=2) == []
    )


def test_full_journal_batch_and_unknown_ack_keep_unadmitted_operation(tmp_path):
    config, settings = fixture()
    config = replace(config, max_deliveries=1)
    path = journal(tmp_path, config)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE operations SET state='finalized' WHERE id=1")
    state = tmp_path / "private" / "queue.sqlite"
    queue = ActivityQueue(state, config)
    for amount in range(1, 1000):
        queue.enqueue(selection(block=119, amount=amount))
    queue.close()
    chain, mcp = Chain(config), MCP(config, settings)
    mcp.unknown_once = True
    with pytest.raises(ObservationUnavailable):
        run(tmp_path, config, chain, mcp, path)
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT operation FROM journal_cursor").fetchall() == [(1,)]
        assert (
            db.execute(
                "SELECT count(*) FROM pending WHERE receipt_id IS NULL"
            ).fetchone()[0]
            == 1000
        )
        assert db.execute("SELECT * FROM cursor").fetchall() == []
    assert run(tmp_path, config, chain, mcp, path)["pending"] == 999
    run(tmp_path, config, chain, mcp, path)
    with sqlite3.connect(state) as db:
        assert db.execute("SELECT operation FROM journal_cursor").fetchall() == [(2,)]
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1001
    assert mcp.calls[0] == mcp.calls[1]
