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
    ) -> None:
        self.rows = rows
        self.delete_fails = delete_fails
        self.delete_fails_until_suspended = delete_fails_until_suspended
        self.deleted: list[str] = []
        self.suspended: list[str] = []
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

    def delete(self, uid: str) -> None:
        self.deleted.append(uid)
        if self.delete_fails:
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
        created_at="2026-08-16T00:00:00Z",
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


def test_delete_retries_after_suspend() -> None:
    client = _Targon([], delete_fails_until_suspended=True)
    client.status_by_uid["wrk-1"] = "running"

    assert delete_oneshot_rental(client, "wrk-1")
    assert client.deleted == ["wrk-1", "wrk-1"]
    assert client.suspended == ["wrk-1"]


def test_sweep_deletes_terminal_oneshots_and_skips_inflight() -> None:
    client = _Targon(
        [
            _row(uid="wrk-slot", name="ditto-screener-prod-slot-01", status="running"),
            _row(uid="wrk-run", name="ditto-miner-build-aaa", status="running"),
            _row(uid="wrk-hold", name="ditto-miner-build-bbb", status="suspended"),
            _row(uid="wrk-err", name="ditto-rootless-probe-ccc", status="error"),
        ]
    )

    result = sweep_oneshot_rentals(client)

    assert result["oneshot"] == 3
    assert result["skipped_inflight"] == 1
    assert result["deleted"] == 2
    assert result["leftover"] == 0
    assert client.deleted == ["wrk-hold", "wrk-err"]
    assert {item["uid"] for item in result["items"] if item["action"] == "deleted"} == {
        "wrk-hold",
        "wrk-err",
    }


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
        command_sweep_oneshots(Namespace(apply=False, registered_grace_seconds=1200))
        == 0
    )

    output = capsys.readouterr().out
    assert '"dry_run": true' in output
    assert '"would-delete"' in output
    assert client.deleted == []
