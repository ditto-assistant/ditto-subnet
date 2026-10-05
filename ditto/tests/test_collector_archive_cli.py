"""Historical collection never substitutes a recent full-node-only view."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import treasury_collector as cli


@pytest.mark.parametrize("mode", ["watch", "tick", "export"])
def test_collector_uses_finney_archive_without_replacing_journal(
    monkeypatch, capsys, mode
):
    policy = SimpleNamespace(digest="a" * 64, enabled=True)
    calls = []
    substrate = object()

    def subtensor(**kwargs):
        calls.append(("network", kwargs))
        return nullcontext(SimpleNamespace(substrate=substrate))

    class Journal:
        def __init__(self, path, actual_policy, role):
            assert actual_policy is policy and role == "transfer"
            calls.append(("journal", str(path)))

        def close(self):
            calls.append(("close",))

    def tick(_journal, actual_policy, _chain, role):
        assert actual_policy is policy and role == "transfer"
        calls.append(("tick",))
        return "waiting"

    chain = SimpleNamespace(observe=lambda *_: SimpleNamespace())
    monkeypatch.setattr(cli, "load_policy", lambda *_: policy)
    monkeypatch.setattr(cli, "CollectorJournal", Journal)
    monkeypatch.setattr(cli, "tick", tick)
    monkeypatch.setattr(cli, "asdict", lambda _: {"finalized": True})
    monkeypatch.setattr(cli, "PublicCollectorChain", lambda *_a, **_k: chain)
    monkeypatch.setattr(
        cli,
        "export_finalized_distributions",
        lambda path, *_: calls.append(("export", str(path))) or [],
    )
    monkeypatch.setitem(sys.modules, "bittensor", SimpleNamespace(Subtensor=subtensor))
    argv = [
        "collector",
        "--role",
        "transfer",
        "--policy",
        "/signed/policy.json",
        "--policy-sha256",
        policy.digest,
        "--journal",
        "/existing/journal.db",
    ]
    if mode == "watch":
        argv.append("--watch-only")
    elif mode == "export":
        argv.append("--export-activity")
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    assert calls[0] == ("network", {"network": "archive"})
    if mode == "tick":
        assert calls[1:] == [("journal", "/existing/journal.db"), ("tick",), ("close",)]
    elif mode == "export":
        assert calls[1:] == [("export", "/existing/journal.db")]
    else:
        assert len(calls) == 1
    output = capsys.readouterr().out
    assert ("record_treasury_receipt" if mode == "export" else policy.digest) in output
