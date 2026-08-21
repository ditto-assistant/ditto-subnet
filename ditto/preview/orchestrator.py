"""Start and stop a preview using the same plan locally or in CI."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ditto.preview.composition import PreviewPlan, compose
from ditto.preview.engine import PreviewEngine
from ditto.preview.identity import preview_id
from ditto.preview.proxy import FaultProxy
from ditto.preview.server import PreviewServer

ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = ROOT / "preview" / ".run"
COMPOSE_FILE = ROOT / "preview" / "compose.yml"


@dataclass
class PreviewHandle:
    """Live preview (in-process servers plus optional compose)."""

    identity: str
    plan: PreviewPlan
    engine: PreviewEngine
    control: PreviewServer
    proxy: FaultProxy | None
    urls: dict[str, str]
    control_token: str
    compose_project: str | None = None

    def down(self) -> None:
        if self.proxy is not None:
            self.proxy.stop()
        self.control.stop()
        if self.compose_project and COMPOSE_FILE.exists():
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE_FILE),
                    "-p",
                    self.compose_project,
                    "down",
                    "-v",
                ],
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "docker compose failed to tear down the preview; "
                    "state was preserved for retry"
                )
        state_path = RUN_ROOT / self.identity / "state.json"
        if state_path.exists():
            state_path.unlink()
        latest = RUN_ROOT / "latest"
        if latest.is_symlink() and latest.resolve() == state_path.parent.resolve():
            latest.unlink()


def up(
    profiles: Sequence[str],
    *,
    ref: str = "local",
    sha: str = "0" * 40,
    attach_prod_api: bool = False,
    network: str = "local",
    endpoint: str = "ws://127.0.0.1:9944",
    netuid: int = 3,
    start_postgres: bool = False,
    upstream: str = "http://127.0.0.1:11434",
) -> PreviewHandle:
    """Bring up preview-control (and optional postgres/fault-proxy) for ``profiles``."""
    plan = compose(profiles, attach_prod_api=attach_prod_api)
    identity = preview_id(ref, sha)
    engine = PreviewEngine(network=network, endpoint=endpoint, netuid=netuid)
    control = PreviewServer(engine, host="127.0.0.1", port=0)
    proxy: FaultProxy | None = None
    compose_project = None
    try:
        control.start()
        if plan.stack:
            proxy = FaultProxy(
                control.url,
                upstream=upstream,
                host="127.0.0.1",
                port=0,
                control_token=control.token,
            )
            proxy.start()
        if plan.stack and start_postgres and COMPOSE_FILE.exists():
            compose_project = f"preview-{identity}"
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE_FILE),
                    "-p",
                    compose_project,
                    "up",
                    "-d",
                    "postgres",
                ],
                check=True,
            )
        urls = {"id": identity, "control": control.url}
        if proxy is not None:
            urls["fault_proxy"] = proxy.url
        handle = PreviewHandle(
            identity=identity,
            plan=plan,
            engine=engine,
            control=control,
            proxy=proxy,
            urls=urls,
            control_token=control.token,
            compose_project=compose_project,
        )
        _write_state(handle)
        return handle
    except BaseException:
        if proxy is not None:
            proxy.stop()
        control.stop()
        if compose_project:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(COMPOSE_FILE),
                    "-p",
                    compose_project,
                    "down",
                    "-v",
                ],
                check=False,
                capture_output=True,
            )
        raise


def _write_state(handle: PreviewHandle) -> None:
    directory = RUN_ROOT / handle.identity
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": handle.identity,
        "profiles": sorted(handle.plan.profiles),
        "stack": handle.plan.stack,
        "copy_database": handle.plan.copy_database,
        "attach_prod_api": handle.plan.attach_prod_api,
        "localnet_validator": handle.plan.localnet_validator,
        "urls": handle.urls,
        "control_token": handle.control_token,
        "control_port": handle.control.port,
        "pid": os.getpid(),
    }
    state_path = directory / "state.json"
    state_path.write_text(json.dumps(payload, indent=2) + "\n")
    state_path.chmod(0o600)
    latest = RUN_ROOT / "latest"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    try:
        latest.symlink_to(directory)
    except OSError:
        latest.write_text(handle.identity)


def load_latest_state() -> dict[str, Any]:
    """Read ``preview/.run/latest/state.json``."""
    latest = RUN_ROOT / "latest" / "state.json"
    if not latest.exists():
        direct = RUN_ROOT / "latest"
        if direct.is_file():
            identity = direct.read_text().strip()
            latest = RUN_ROOT / identity / "state.json"
    if not latest.exists():
        raise FileNotFoundError("no preview is running (preview/.run/latest missing)")
    return json.loads(latest.read_text())


def plan_as_dict(plan: PreviewPlan) -> dict[str, Any]:
    return asdict(plan) | {"profiles": sorted(plan.profiles)}
