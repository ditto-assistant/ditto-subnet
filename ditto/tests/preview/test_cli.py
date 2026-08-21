from __future__ import annotations

import json

from ditto.preview.cli import main

HOTKEY = "5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY"


def test_compose_cli_json(capsys) -> None:
    assert main(["compose", "dashboard"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["attach_prod_api"] is True
    assert payload["stack"] is False
    assert payload["id"]


def test_compose_cli_rejects_prod_attach_on_stack() -> None:
    assert main(["compose", "stack", "--attach-prod-api"]) == 2


def test_cli_bounds_down_and_transport_failures(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PREVIEW_CONTROL_TOKEN", "test-token")
    assert main(["ctl", "warp_block", "--url", "http://127.0.0.1:1"]) == 2
    assert "request failed" in capsys.readouterr().err
    assert main(["down"]) == 2
    assert "foreground" in capsys.readouterr().err


def test_ctl_against_live_server(monkeypatch) -> None:
    from ditto.preview.engine import PreviewEngine
    from ditto.preview.server import PreviewServer

    engine = PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944")
    server = PreviewServer(engine, host="127.0.0.1", port=0)
    server.start()
    monkeypatch.setenv("PREVIEW_CONTROL_TOKEN", server.token)
    try:
        assert (
            main(
                ["ctl", "register", "--url", server.url, "--hotkey", HOTKEY, "--permit"]
            )
            == 0
        )
        assert main(["ctl", "warp_block", "--url", server.url, "--n", "4"]) == 0
        assert (
            main(["ctl", "inject_provider", "--url", server.url, "--status", "429"])
            == 0
        )
        assert engine.provider_status == 429
        assert main(["ctl", "inject_provider", "--url", server.url, "--clear"]) == 0
        assert engine.provider_status is None
    finally:
        server.stop()
