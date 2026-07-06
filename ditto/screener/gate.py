"""The screener build gate: does the submitted crate build and serve?

The gate is deliberately cheaper than a full DittoBench run — it answers only
"does this compile into an image that comes up and serves ``/health``?" so a
broken submission is rejected before it costs a scoring run.

Flow for one agent:

1. **Download + verify.** Stream the presigned tarball to a temp file, bounded by
   ``max_tarball_bytes``, and re-check its SHA-256 against the queue value (the
   URL is presigned but the bytes are still attacker-controlled).
2. **Contract check.** The submission contract fixes a ``Dockerfile`` at the
   tarball root; a tar without one fails fast (no build attempted).
3. **Build.** ``docker build`` reads the *tarball itself* as the build context on
   stdin — Docker unpacks it inside its own build sandbox, so the screener never
   re-implements safe tar extraction. BuildKit is used with an optional
   ``gh_token`` secret (the same private-dep token dittobench uses) when
   configured. Bounded by ``build_timeout_seconds``.
4. **Serve smoke.** Run the image detached with a memory + pids cap on a
   loopback-published port, and poll ``GET /health`` until it returns 2xx or
   ``run_timeout_seconds`` elapses. No LLM key is needed — the gate never calls
   ``/run``.
5. **Teardown.** The container + image are always removed.

A pass is "built AND served"; anything else is a fail with a short ``detail``
(build-log tail or the failing stage) that rides the verdict for the miner's
benefit. Every stage is best-effort and never raises into the worker loop: an
infrastructure error (Docker down) is reported as a non-pass with detail, so a
flaky host does not silently promote or wrongly reject.

Trust posture: the build runs on the host Docker daemon, same as dittobench's;
wall-time is bounded by the timeout. Deeper isolation (rootless/gVisor) and an
egress allowlist are tracked separately (roadmap C3) and are not this gate's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import tarfile
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

if TYPE_CHECKING:
    from ditto.screener.config import ScreenerConfig

logger = logging.getLogger(__name__)

# Bytes of a failing build log to attach to the verdict detail.
_LOG_TAIL_BYTES = 2000
# How long to wait between /health probes while the container boots.
_PROBE_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class GateResult:
    """Outcome of screening one agent."""

    passed: bool
    detail: str


def dockerfile_at_root(member_names: list[str]) -> bool:
    """Whether the tar has a ``Dockerfile`` at its root.

    Accepts the bare ``Dockerfile`` and a leading ``./`` (tar writers differ).
    The submission contract fixes the Dockerfile at the tarball root, so a
    Dockerfile only in a subdirectory does not satisfy the gate.
    """
    return any(name in ("Dockerfile", "./Dockerfile") for name in member_names)


def _log_tail(text: str) -> str:
    """Last chunk of a build log, trimmed for the verdict detail field."""
    trimmed = text.strip()
    if len(trimmed) <= _LOG_TAIL_BYTES:
        return trimmed
    return "…" + trimmed[-_LOG_TAIL_BYTES:]


class BuildGate:
    """Runs the build+serve check for one agent at a time.

    Docker CLI calls are funnelled through :meth:`_run` so tests can stub the
    subprocess layer; HTTP (download + health probe) uses the injected client.
    """

    def __init__(self, config: ScreenerConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    async def screen(
        self, *, agent_id: UUID, sha256: str, download_url: str
    ) -> GateResult:
        """Screen one agent end-to-end; never raises."""
        tag = f"ditto-screen/{agent_id}:latest"
        container = f"ditto-screen-{agent_id}"
        tmp_path: str | None = None
        try:
            tmp_path, dl_detail = await self._download_verified(download_url, sha256)
            if tmp_path is None:
                return GateResult(False, dl_detail)
            if not self._has_root_dockerfile(tmp_path):
                return GateResult(False, "no Dockerfile at tarball root")

            built, build_detail = await self._build(tmp_path, tag)
            if not built:
                return GateResult(False, f"build failed: {build_detail}")

            served, serve_detail = await self._run_and_probe(tag, container)
            if not served:
                return GateResult(False, f"serve check failed: {serve_detail}")
            return GateResult(True, "")
        except Exception as e:  # noqa: BLE001 - the loop must never die on one agent
            logger.exception("gate error for agent_id=%s", agent_id)
            return GateResult(False, f"screener error: {type(e).__name__}: {e}")
        finally:
            await self._teardown(container, tag)
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    # --- stages -----------------------------------------------------------

    async def _download_verified(
        self, url: str, expected_sha256: str
    ) -> tuple[str | None, str]:
        """Stream the tarball to a temp file, size-bounded + sha256-checked.

        Returns ``(path, "")`` on success or ``(None, reason)`` on a cap breach,
        digest mismatch, or transport error.
        """
        cap = self._config.max_tarball_bytes
        hasher = hashlib.sha256()
        total = 0
        fd, path = tempfile.mkstemp(prefix="ditto-screen-", suffix=".tar.gz")
        try:
            with os.fdopen(fd, "wb") as fh:
                async with self._client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        return None, f"artifact download HTTP {resp.status_code}"
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > cap:
                            return None, f"tarball exceeds {cap} byte cap"
                        hasher.update(chunk)
                        fh.write(chunk)
        except httpx.HTTPError as e:
            return None, f"artifact download failed: {e}"
        digest = hasher.hexdigest()
        if digest != expected_sha256.lower():
            return None, f"sha256 mismatch (got {digest[:12]}…)"
        return path, ""

    def _has_root_dockerfile(self, tar_path: str) -> bool:
        """List the tar (no extraction) and check for a root Dockerfile."""
        try:
            with tarfile.open(tar_path, mode="r:gz") as tar:
                return dockerfile_at_root(tar.getnames())
        except (tarfile.TarError, OSError) as e:
            logger.warning("could not read tar %s: %s", tar_path, e)
            return False

    async def _build(self, tar_path: str, tag: str) -> tuple[bool, str]:
        """``docker build`` from the tarball-on-stdin; returns (ok, log_tail)."""
        args = ["build", "-t", tag, "-f", "Dockerfile"]
        env = dict(os.environ)
        env["DOCKER_BUILDKIT"] = "1"
        gh_file = self._config.gh_token_file
        if gh_file and os.path.exists(gh_file):
            args += ["--secret", f"id=gh_token,src={gh_file}"]
        args.append("-")  # build context comes from stdin
        with open(tar_path, "rb") as stdin_f:
            code, out = await self._run(
                args, stdin=stdin_f, timeout=self._config.build_timeout_seconds, env=env
            )
        if code == 0:
            return True, ""
        return False, _log_tail(out)

    async def _run_and_probe(self, tag: str, container: str) -> tuple[bool, str]:
        """Run the image detached and poll ``/health`` until it serves."""
        port = self._config.container_port
        run_args = [
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "--memory",
            self._config.build_memory,
            "--pids-limit",
            str(self._config.pids_limit),
            "--publish",
            f"127.0.0.1::{port}",
            tag,
        ]
        code, out = await self._run(run_args, timeout=self._config.run_timeout_seconds)
        if code != 0:
            return False, f"container did not start: {_log_tail(out)}"

        mapped = await self._published_port(container, port)
        if mapped is None:
            return False, "could not resolve published port"

        base = f"http://127.0.0.1:{mapped}{self._config.health_path}"
        deadline = self._config.run_timeout_seconds
        waited = 0.0
        last = "no response"
        while waited < deadline:
            try:
                resp = await self._client.get(base, timeout=5.0)
                if 200 <= resp.status_code < 300:
                    return True, ""
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}"
            await asyncio.sleep(_PROBE_INTERVAL_SECONDS)
            waited += _PROBE_INTERVAL_SECONDS
        return False, f"/health never healthy within {deadline:g}s ({last})"

    async def _published_port(self, container: str, container_port: int) -> str | None:
        """Resolve the ephemeral host port Docker mapped for ``container_port``."""
        code, out = await self._run(
            ["port", container, str(container_port)], timeout=15.0
        )
        if code != 0 or not out.strip():
            return None
        # e.g. "127.0.0.1:49153" (last line, last colon-field).
        line = out.strip().splitlines()[-1]
        return line.rsplit(":", 1)[-1].strip() or None

    async def _teardown(self, container: str, tag: str) -> None:
        """Best-effort removal of the container + image; never raises."""
        try:
            await self._run(["rm", "-f", container], timeout=30.0)
            await self._run(["rmi", "-f", tag], timeout=30.0)
        except Exception:  # noqa: BLE001 - teardown must never mask a result
            logger.warning("teardown issue for %s / %s", container, tag, exc_info=True)

    # --- subprocess -------------------------------------------------------

    async def _run(
        self,
        args: list[str],
        *,
        stdin: io.BufferedReader | None = None,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run ``docker <args>`` with a hard timeout; return (returncode, output).

        stdout+stderr are merged. On timeout the process is killed and a
        non-zero code with a ``[timeout]`` marker is returned.
        """
        proc = await asyncio.create_subprocess_exec(
            self._config.docker_bin,
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return 124, f"[timeout after {timeout:g}s]"
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
