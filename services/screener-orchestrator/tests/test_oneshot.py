from argparse import Namespace
from datetime import UTC, datetime

from screener_capacity.oneshot import (
    delete_oneshot_rental,
    is_oneshot_name,
    should_sweep,
    sweep_oneshot_rentals,
)
from screener_capacity.targon import TargonAPIError
from screener_capacity.targon_cli import command_sweep_oneshots


class _Targon:
    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        delete_fails: bool = False,
        delete_fails_until_suspended: bool = False,
        deploy_status: int | None = None,
    ) -> None:
        self.rows = rows
        self.delete_fails = delete_fails
        self.delete_fails_until_suspended = delete_fails_until_suspended
        self.deploy_status = deploy_status
        self.deleted: list[str] = []
        self.suspended: list[str] = []
        self.deployed: list[str] = []
        self.status_by_uid = {
            str(row["uid"]): str((row.get("state") or {}).get("status") or "")
            for row in rows
        }

    def list_workloads(self, *, name: str | None = None) -> list[dict[str, object]]:
        del name
        return self.rows

    def state(self, uid: str) -> dict[str, object]:
        return {"status": self.status_by_uid.get(uid, "")}

    def suspend(self, uid: str) -> dict[str, object]:
        self.suspended.append(uid)
        self.status_by_uid[uid] = "suspended"
        return {}

    def deploy(self, uid: str) -> dict[str, object]:
        if self.deploy_status is not None:
            raise TargonAPIError(
                operation="POST deploy",
                status=self.deploy_status,
                reason="HTTP error",
            )
        self.deployed.append(uid)
        self.status_by_uid[uid] = "running"
        return {}

    def delete(self, uid: str) -> None:
        self.deleted.append(uid)
        if self.delete_fails:
            raise TargonAPIError(operation="DELETE", status=500, reason="HTTP error")
        if (self.status_by_uid.get(uid) or "").casefold() in {
            "suspended",
            "error",
            "registered",
        }:
            raise TargonAPIError(operation="DELETE", status=500, reason="HTTP error")
        if self.delete_fails_until_suspended and uid not in self.suspended:
            raise TargonAPIError(operation="DELETE", status=500, reason="HTTP error")
        self.status_by_uid[uid] = "deleted"


def _row(
    *,
    uid: str,
    name: str,
    status: str,
    created_at: str | None = "2026-08-16T00:00:00Z",
) -> dict[str, object]:
    return {
        "uid": uid,
        "name": name,
        "created_at": created_at,
        "state": {"status": status},
    }


def test_oneshot_names_exclude_screener_slots() -> None:
    assert is_oneshot_name("ditto-miner-build-550e8400e29b")
    assert is_oneshot_name("ditto-buildkit-probe-abc")
    assert is_oneshot_name("ditto-screener-vm-probe-abc")
    assert is_oneshot_name("ditto-kaniko-runtime-0f327e")
    assert is_oneshot_name("ditto-sandbox-probe-494d9f")
    assert not is_oneshot_name("ditto-screener-prod-slot-01")
    assert not is_oneshot_name("manual-debug")


def test_should_sweep_skips_inflight_and_fresh_registered() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert should_sweep(
        name="ditto-miner-build-abc",
        status="suspended",
        created_at="2026-08-16T11:59:00Z",
        now=now,
        registered_grace_seconds=1200,
    )
    assert not should_sweep(
        name="ditto-miner-build-abc",
        status="running",
        created_at="2026-08-16T11:50:00Z",
        now=now,
        registered_grace_seconds=1200,
    )
    assert should_sweep(
        name="ditto-miner-build-abc",
        status="provisioning",
        created_at="2026-08-11T00:00:00Z",
        now=now,
        registered_grace_seconds=1200,
    )
    assert not should_sweep(
        name="ditto-miner-build-abc",
        status="registered",
        created_at="2026-08-16T11:50:00Z",
        now=now,
        registered_grace_seconds=1200,
    )
    assert should_sweep(
        name="ditto-miner-build-abc",
        status="registered",
        created_at="2026-08-16T11:00:00Z",
        now=now,
        registered_grace_seconds=1200,
    )
    assert not should_sweep(
        name="ditto-screener-prod-slot-01",
        status="suspended",
        created_at="2026-08-16T00:00:00Z",
        now=now,
        registered_grace_seconds=1200,
    )


def test_delete_redeploys_suspended_rental_then_deletes() -> None:
    client = _Targon([])
    client.status_by_uid["wrk-1"] = "suspended"

    assert delete_oneshot_rental(client, "wrk-1")
    assert client.deleted == ["wrk-1", "wrk-1"]
    assert client.deployed == ["wrk-1"]
    assert client.suspended == []


def test_sweep_deletes_terminal_oneshots_and_skips_inflight() -> None:
    client = _Targon(
        [
            _row(uid="wrk-slot", name="ditto-screener-prod-slot-01", status="running"),
            _row(
                uid="wrk-run",
                name="ditto-miner-build-aaa",
                status="running",
                created_at="2026-08-16T11:50:00Z",
            ),
            _row(uid="wrk-hold", name="ditto-miner-build-bbb", status="suspended"),
            _row(uid="wrk-err", name="ditto-rootless-probe-ccc", status="error"),
        ]
    )

    result = sweep_oneshot_rentals(client, now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC))

    assert result["oneshot"] == 3
    assert result["skipped_inflight"] == 1
    assert result["deleted"] == 2
    assert result["leftover"] == 0
    assert sorted(client.deleted) == ["wrk-err", "wrk-err", "wrk-hold", "wrk-hold"]
    assert sorted(client.deployed) == ["wrk-err", "wrk-hold"]
    assert {item["uid"] for item in result["items"] if item["action"] == "deleted"} == {
        "wrk-hold",
        "wrk-err",
    }


def test_sweep_can_delete_in_parallel() -> None:
    client = _Targon(
        [
            _row(uid="wrk-a", name="ditto-miner-build-aaa", status="suspended"),
            _row(uid="wrk-b", name="ditto-miner-build-bbb", status="error"),
        ]
    )

    result = sweep_oneshot_rentals(client, max_workers=2)

    assert result["deleted"] == 2
    assert sorted(client.deleted) == ["wrk-a", "wrk-a", "wrk-b", "wrk-b"]
    assert sorted(client.deployed) == ["wrk-a", "wrk-b"]


def test_sweep_stops_redeploy_after_payment_required() -> None:
    client = _Targon(
        [
            _row(uid="wrk-a", name="ditto-miner-build-aaa", status="suspended"),
            _row(uid="wrk-b", name="ditto-miner-build-bbb", status="suspended"),
        ],
        deploy_status=402,
    )

    result = sweep_oneshot_rentals(client)

    assert result["deleted"] == 0
    assert result["leftover"] == 2
    assert {item["action"] for item in result["items"]} == {"payment-required"}
    assert client.deployed == []


def test_sweep_dry_run_does_not_mutate() -> None:
    client = _Targon(
        [_row(uid="wrk-hold", name="ditto-miner-build-bbb", status="suspended")]
    )

    result = sweep_oneshot_rentals(client, dry_run=True)

    assert result["dry_run"] is True
    assert result["deleted"] == 1
    assert result["items"][0]["action"] == "would-delete"
    assert client.deleted == []


def test_sweep_command_defaults_to_dry_run(monkeypatch, capsys) -> None:
    client = _Targon(
        [_row(uid="wrk-hold", name="ditto-miner-build-bbb", status="suspended")]
    )
    monkeypatch.setattr(
        "screener_capacity.targon_cli._client", lambda *_args, **_kwargs: client
    )

    assert (
        command_sweep_oneshots(
            Namespace(apply=False, registered_grace_seconds=1200, workers=8)
        )
        == 0
    )

    output = capsys.readouterr().out
    assert '"dry_run": true' in output
    assert '"would-delete"' in output
    assert client.deleted == []
