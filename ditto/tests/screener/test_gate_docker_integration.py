"""Real-Docker coverage for the screener model-call canary.

Run with a current checkout of the canonical starter kit::

    DITTO_STARTER_KIT_DIR=/path/to/dittobench-starter-kit \
      uv run pytest -m integration \
      ditto/tests/screener/test_gate_docker_integration.py -s

For a previously submitted archive, set ``DITTO_STARTER_KIT_ARCHIVE`` instead.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ditto.screener.config import ScreenerConfig
from ditto.screener.gate import BuildGate, GateResult


@pytest.mark.integration
async def test_current_starter_kit_uses_hidden_model_response(
    make_config: Any, tmp_path: Path
) -> None:
    """Build and run the real starter kit through the isolated fake gateway."""
    archive_raw = os.environ.get("DITTO_STARTER_KIT_ARCHIVE")
    starter_dir_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    if archive_raw:
        tarball = Path(archive_raw).resolve().read_bytes()
        revision = hashlib.sha256(tarball).hexdigest()
    else:
        if not starter_dir_raw:
            pytest.skip("set DITTO_STARTER_KIT_DIR to a current canonical checkout")
        starter_dir = Path(starter_dir_raw).resolve()
        if not (starter_dir / "Dockerfile").is_file():
            pytest.fail(f"no Dockerfile in DITTO_STARTER_KIT_DIR={starter_dir}")

        revision = subprocess.run(
            ["git", "-C", str(starter_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        archive = tmp_path / f"dittobench-starter-kit-{revision}.tar.gz"
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(starter_dir), "archive", "--format=tar.gz", "HEAD"],
                check=True,
                stdout=output,
            )
        tarball = archive.read_bytes()

    def artifact_response(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://artifact.test/starter-kit.tar.gz")
        return httpx.Response(200, content=tarball)

    config: ScreenerConfig = make_config(
        build_timeout_seconds=1200.0,
        run_timeout_seconds=120.0,
        max_tarball_bytes=20 * 1024 * 1024,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact_response))
    gate = BuildGate(config, client)
    canary_results: list[GateResult] = []
    real_run_model_canary = gate._run_model_canary

    async def observed_run_model_canary(*args: Any, **kwargs: Any) -> GateResult:
        result = await real_run_model_canary(*args, **kwargs)
        canary_results.append(result)
        return result

    gate._run_model_canary = observed_run_model_canary  # type: ignore[method-assign]
    async with client:
        result = await gate.screen(
            agent_id=uuid4(),
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url="https://artifact.test/starter-kit.tar.gz",
        )

    assert result.passed, f"starter kit {revision}: {result.detail}"
    assert canary_results == [GateResult(True, "")]
