from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto.api_models.coding_bounded_rollout import BoundedRolloutApproval
from ditto.api_server.coding_rollout_governor import (
    AttemptOutcome,
    RolloutError,
    RolloutState,
    govern,
)


def approval_data(count=4, parallel=2):
    now = int(time.time())
    return {
        "schema": "dittobench-coding-bounded-rollout-v2",
        "purpose": "shadow-cohort-once",
        "runtime_revision": "a" * 40,
        "runtime_archive_sha256": "a" * 64,
        "controller_sha256": "b" * 64,
        "machine_id_sha256": "c" * 64,
        "boot_id": str(uuid4()),
        "connectivity_file": "/private/network.json",
        "connectivity_sha256": "d" * 64,
        "accepted_evidence": dict.fromkeys(
            (
                "host_qualification",
                "native_matrix_acceptance",
                "canary_acceptance",
                "custody",
                "recovery",
                "rollback",
            ),
            "e" * 64,
        ),
        "issued_at_unix": now - 1,
        "expires_at_unix": now + 600,
        "max_attempts": count,
        "max_parallel": parallel,
        "max_reserved_cost_usd_micros": count * 100,
        "allow_candidate_failure": False,
        "attempts": [
            {
                "evaluation_id": str(uuid4()),
                "attempt_id": str(uuid4()),
                "worker_id": str(uuid4()),
                "config_file": f"/private/{i}.json",
                "config_sha256": "f" * 64,
                "assignment_sha256": "1" * 64,
                "policy_sha256": "2" * 64,
                "deadline_unix": now + 300,
                "cost_ceiling_usd_micros": 100,
            }
            for i in range(count)
        ],
        "shadow_only": True,
        "weight_eligible": False,
    }


def approval(count=4, parallel=2):
    return BoundedRolloutApproval.model_validate_json(
        json.dumps(approval_data(count, parallel))
    )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("max_parallel", 0),
        ("max_parallel", 5),
        ("max_attempts", 3),
        ("max_reserved_cost_usd_micros", 399),
        ("shadow_only", 1),
        ("weight_eligible", 0),
        ("allow_candidate_failure", 1),
        ("accepted_evidence", {}),
        ("runtime_revision", "0" * 40),
    ],
)
def test_approval_bounds_are_strict(field, bad):
    value = approval_data()
    value[field] = bad
    with pytest.raises(ValueError):
        BoundedRolloutApproval.model_validate_json(json.dumps(value))


def test_duplicate_assignments_and_unbounded_window_rejected():
    value = approval_data()
    value["attempts"][1]["evaluation_id"] = value["attempts"][0]["evaluation_id"]
    with pytest.raises(ValueError):
        BoundedRolloutApproval.model_validate_json(json.dumps(value))
    value = approval_data()
    value["expires_at_unix"] = value["issued_at_unix"] + 86400
    with pytest.raises(ValueError):
        BoundedRolloutApproval.model_validate_json(json.dumps(value))


async def test_concurrency_is_bounded_and_each_assignment_runs_once():
    running, peak, calls = 0, 0, []

    async def execute(index):
        nonlocal running, peak
        calls.append(index)
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return AttemptOutcome(str(index + 1) * 64, "completed")

    result = await govern(approval(), execute, asyncio.Event())
    assert sorted(calls) == [0, 1, 2, 3] and peak == 2
    assert len(result) == 4 and running == 0


async def test_failure_stops_admission_and_drains_active_work():
    calls, cancelled = [], []
    ready = asyncio.Event()

    async def execute(index):
        calls.append(index)
        if index == 0:
            await ready.wait()
            raise ValueError("synthetic failure")
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(index)
            raise

    with pytest.raises(ValueError):
        await govern(approval(), execute, asyncio.Event(), drain_seconds=1)
    assert calls == [0, 1] and cancelled == [1]


async def test_external_stop_never_replenishes_slots():
    stop = asyncio.Event()
    calls = []

    async def execute(index):
        calls.append(index)
        stop.set()
        return AttemptOutcome("1" * 64, "completed")

    with pytest.raises(RolloutError):
        await govern(approval(4, 1), execute, stop)
    assert calls == [0]


async def test_routes_are_reused_only_after_previous_runtime_cleanup():
    routes = ("a", "b", "a", "b")
    active, calls = set(), []
    release_first = asyncio.Event()

    async def execute(index):
        assert routes[index] not in active
        active.add(routes[index])
        calls.append(index)
        if index == 0:
            await release_first.wait()
        if index == 3:
            release_first.set()
        await asyncio.sleep(0)
        active.remove(routes[index])
        return AttemptOutcome("a" * 64, "completed")

    async with asyncio.timeout(2):
        await govern(approval(), execute, asyncio.Event(), resources=routes)
    assert calls == [0, 1, 3, 2] and not active


async def test_candidate_failures_require_explicit_policy():
    async def execute(_index):
        return AttemptOutcome("1" * 64, "candidate_failure")

    with pytest.raises(RolloutError):
        await govern(approval(), execute, asyncio.Event())
    value = approval_data()
    value["allow_candidate_failure"] = True
    result = await govern(
        BoundedRolloutApproval.model_validate_json(json.dumps(value)),
        execute,
        asyncio.Event(),
    )
    assert len(result) == 4


async def test_cancel_resistant_work_is_unconfirmed_not_success():
    stop, finish = asyncio.Event(), asyncio.Event()
    tasks = []

    async def execute(_index):
        tasks.append(asyncio.current_task())
        stop.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await finish.wait()
        return AttemptOutcome("1" * 64, "completed")

    try:
        with pytest.raises(RolloutError, match="drain unconfirmed"):
            await govern(approval(1, 1), execute, stop, drain_seconds=0.01)
    finally:
        finish.set()
        await asyncio.gather(*tasks)


def test_persistent_active_marker_blocks_other_approvals_and_replay(tmp_path):
    tmp_path.chmod(0o700)
    first = RolloutState(tmp_path, "a" * 64)
    try:
        first.consume()
        with pytest.raises(BlockingIOError):
            RolloutState(tmp_path, "b" * 64)
    finally:
        first.close()
    with pytest.raises(RolloutError, match="reconciliation"):
        RolloutState(tmp_path, "b" * 64)
    assert (tmp_path / "active").read_bytes() == b"a" * 64 + b"\n"


def test_success_keeps_receipt_and_consumed_marker(tmp_path):
    tmp_path.chmod(0o700)
    first = RolloutState(tmp_path, "a" * 64)
    try:
        first.consume()
        first.finish(b'{"synthetic":true}\n')
    finally:
        first.close()
    assert not (tmp_path / "active").exists()
    assert (tmp_path / ("a" * 64 + ".result.json")).exists()
    second = RolloutState(tmp_path, "a" * 64)
    try:
        with pytest.raises(FileExistsError):
            second.consume()
    finally:
        second.close()


def test_runtime_config_pin_rejects_before_private_dependencies(tmp_path):
    from ditto.api_server import coding_hosted_runtime_config as config

    path = tmp_path / "config.json"
    path.write_bytes(b"{}")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="commitment"):
        config.load_runtime_config(path, expected_sha256="a" * 64)


def test_cli_default_does_not_start_work(monkeypatch):
    from ditto import coding_bounded_rollout as cli

    monkeypatch.setattr("sys.argv", ["rollout", "--config", "/unread/config"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_progress_status_is_read_only_and_does_not_claim_liveness(tmp_path):
    from ditto.api_server.coding_rollout_governor import inspect_state

    tmp_path.chmod(0o700)
    state = RolloutState(tmp_path, "a" * 64)
    try:
        state.consume(
            json.dumps(
                {
                    "max_attempts": 1,
                    "max_parallel": 1,
                    "reserved_cost_ceiling_usd_micros": 100,
                    "expires_at_unix": 2000000000,
                }
            ).encode()
        )
        state.record(
            0,
            "launch",
            json.dumps(
                {"evaluation_id": str(uuid4()), "private_extension": "must-not-appear"}
            ).encode(),
        )
        before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        report = inspect_state(tmp_path, "a" * 64)
        assert report["consumed"] and report["active_or_reconciliation_required"]
        assert report["process_liveness_verified"] is False
        assert report["plan"]["reserved_cost_ceiling_usd_micros"] == 100
        assert "must-not-appear" not in json.dumps(report)
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    finally:
        state.close()


def test_configuration_budget_and_routes_must_match_approval(monkeypatch):
    from ditto.api_server import coding_bounded_rollout as runtime

    data = approval_data(2, 2)
    network = {
        "schema": "dittobench-coding-hosted-connectivity-v3",
        "expires_at_unix": data["expires_at_unix"] + 3600,
        "candidate_tcp": [
            {"address": "10.30.0.4", "port": port} for port in (18080, 18081, 18090)
        ],
    }
    data["connectivity_sha256"] = hashlib.sha256(
        json.dumps(network, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    chosen = BoundedRolloutApproval.model_validate_json(json.dumps(data))
    configs = []
    for i, item in enumerate(chosen.attempts):
        prefix = "/opt/ditto-coding-hosted/" + chosen.runtime_revision
        configs.append(
            SimpleNamespace(
                wire=SimpleNamespace(
                    evaluation_id=item.evaluation_id,
                    attempt_id=item.attempt_id,
                    worker_id=item.worker_id,
                    assignment_sha256=item.assignment_sha256,
                    worker_executable=prefix + "/bin/dittobench-coding-hosted-worker",
                    python_executable=prefix + "/apps/platform/.venv/bin/python",
                    runtime_root=f"/private/run-{i}",
                    host=SimpleNamespace(
                        docker_executable="/usr/bin/docker",
                        docker_socket=str(runtime.SOCKET),
                        router_listen=f"10.30.0.4:{18080 + i}",
                        egress_proxy="http://10.30.0.4:18090",
                    ),
                ),
                policy=SimpleNamespace(
                    digest=lambda: "2" * 64, max_cost_usd_micros=100
                ),
                postgres="synthetic-db",
            )
        )
    monkeypatch.setattr(runtime, "read_json", lambda *_args: network)

    def load(path, *, expected_sha256):
        index = next(
            i
            for i, item in enumerate(chosen.attempts)
            if Path(item.config_file) == path
        )
        assert expected_sha256 == chosen.attempts[index].config_sha256
        return configs[index]

    monkeypatch.setattr(runtime, "load_runtime_config", load)
    assert runtime.configurations(chosen, chosen.connectivity_sha256) == configs
    configs[0].policy.max_cost_usd_micros = 101
    with pytest.raises(RolloutError):
        runtime.configurations(chosen, chosen.connectivity_sha256)
    configs[0].policy.max_cost_usd_micros = 100
    configs[1].wire.runtime_root = configs[0].wire.runtime_root + "/"
    with pytest.raises(RolloutError):
        runtime.configurations(chosen, chosen.connectivity_sha256)


async def test_loaded_runtime_wrapper_preserves_the_original_entrypoint(monkeypatch):
    from ditto.api_server import coding_hosted_runtime as runtime

    config = object()
    calls = []
    monkeypatch.setattr(runtime, "load_runtime_config", lambda _path: config)

    async def run(value):
        calls.append(value)
        return "a" * 64

    monkeypatch.setattr(runtime, "run_loaded_runtime", run)
    assert await runtime.run_runtime(Path("/private/config")) == "a" * 64
    assert calls == [config]


@pytest.mark.parametrize("failure", [False, True])
async def test_controller_composition_retains_failures_and_only_accepts_drained_success(
    tmp_path, monkeypatch, failure
):
    from contextlib import asynccontextmanager

    from ditto.api_server import coding_bounded_rollout as runtime
    from ditto.api_server.coding_rollout_governor import inspect_state

    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    data = approval_data(2, 2)
    raw = json.dumps(data).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    config_path = tmp_path / "approval.json"
    config_path.write_bytes(raw)
    config_path.chmod(0o600)
    chosen = BoundedRolloutApproval.model_validate_json(raw)
    configs = [
        SimpleNamespace(
            postgres="synthetic",
            wire=SimpleNamespace(
                evaluation_id=item.evaluation_id,
                attempt_id=item.attempt_id,
                host=SimpleNamespace(router_listen=str(item.evaluation_id)),
            ),
        )
        for item in chosen.attempts
    ]
    monkeypatch.setattr(runtime, "STATE", state)
    monkeypatch.setattr(runtime, "host", lambda _approval: None)
    monkeypatch.setattr(runtime, "configurations", lambda *_args: configs)
    disposed, inspected, calls = [], [], []

    async def dispose():
        disposed.append(True)

    monkeypatch.setattr(
        runtime, "create_db_engine", lambda _config: SimpleNamespace(dispose=dispose)
    )

    class Session:
        async def get(self, *_args):
            return SimpleNamespace(identity={}, identity_sha256="a" * 64)

    @asynccontextmanager
    async def sessions():
        yield Session()

    monkeypatch.setattr(runtime, "create_session_maker", lambda _engine: sessions)

    async def projection(config, _sessions):
        index = configs.index(config)
        return SimpleNamespace(
            authority=SimpleNamespace(
                deadline_unix=chosen.attempts[index].deadline_unix
            )
        )

    monkeypatch.setattr(runtime, "_projection", projection)

    async def empty():
        inspected.append(True)

    monkeypatch.setattr(runtime, "empty_daemon", empty)

    async def execute(config):
        calls.append(config.wire.evaluation_id)
        if failure:
            raise ValueError("synthetic runtime failure")
        await asyncio.sleep(0)
        return "a" * 64

    monkeypatch.setattr(runtime, "run_loaded_runtime", execute)
    monkeypatch.setattr(
        runtime.HostedTerminalIdentity,
        "model_validate_json",
        lambda _body: SimpleNamespace(digest=lambda: "a" * 64, outcome="completed"),
    )
    if failure:
        with pytest.raises(ValueError):
            await runtime.run(
                config_path, checksum, chosen.connectivity_sha256, asyncio.Event()
            )
        assert (state / "active").exists()
        assert not (state / (checksum + ".result.json")).exists()
    else:
        result = await runtime.run(
            config_path, checksum, chosen.connectivity_sha256, asyncio.Event()
        )
        assert result["reserved_cost_ceiling_usd_micros"] == 200
        assert result["weight_eligible"] is False and len(result["attempts"]) == 2
        assert len(inspected) == 2 and not (state / "active").exists()
        assert len(calls) == 2
    assert disposed == [True]
    status = inspect_state(state, checksum)
    assert status["consumed"] and not status["process_liveness_verified"]
