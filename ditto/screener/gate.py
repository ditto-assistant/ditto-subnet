"""The screener build gate: does the crate build, serve, and use the model?

The gate is deliberately cheaper than a full DittoBench run. It verifies the
image, service contract, and one synthetic model-call canary before a submission
can consume a scoring run.

Flow for one agent:

1. **Download + verify.** Stream the presigned tarball to a temp file, bounded by
   ``max_tarball_bytes``, and re-check its SHA-256 against the queue value (the
   URL is presigned but the bytes are still attacker-controlled).
2. **Contract check.** Reject unsafe archive entries and require a root Rust
   crate before any build is attempted. The crate may use, fork, or replace
   ``ditto-harness``.
3. **Build.** ``docker build`` reads the *tarball itself* as the build context on
   stdin: Docker unpacks it inside its own build sandbox, so the screener never
   re-implements safe tar extraction. BuildKit is used with an optional
   ``gh_token`` secret for a private build dependency, when configured. Bounded by
   ``build_timeout_seconds``.
4. **Serve smoke.** Run the image detached with a memory + pids cap and poll
   ``GET /health`` until it returns 2xx.
5. **Model canary.** Put the harness and a locked-down fake OpenAI-compatible
   sidecar on a private Docker network, call ``POST /run``, and require the
   harness to return a random token known only to the fake model response.
6. **Teardown.** The container + image are always removed.

A pass is "built, served, and consumed a model response"; anything else fails
with a short ``detail`` (build-log tail or the failing stage) that rides the
verdict for the miner's benefit. Every stage is best-effort and never raises into
the worker loop: an
infrastructure error (Docker down) is reported as a non-pass with detail, so a
flaky host does not silently promote or wrongly reject.

Trust posture: the build runs on the host Docker daemon, same as dittobench's;
wall-time is bounded by the timeout. Deeper isolation (rootless/gVisor) and an
egress allowlist are out of scope for this gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from ditto.screener.model_canary import LOCKED_HARNESS_MODEL

if TYPE_CHECKING:
    from ditto.screener.config import ScreenerConfig

logger = logging.getLogger(__name__)

# Bytes of a failing build log to attach to the verdict detail.
_LOG_TAIL_BYTES = 2000
# How long to wait between /health probes while the container boots.
_PROBE_INTERVAL_SECONDS = 1.0
_MAX_UNPACKED_BYTES = 64 * 1024 * 1024
_MAX_CANARY_RESPONSE_BYTES = 64 * 1024
_CANARY_IMAGE = (
    "python:3.12-alpine@sha256:"
    "6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
)
_CANARY_ALIAS = "model-canary"


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
    """Runs the build, serve, and model-call checks for one agent at a time.

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
        canary_container = f"ditto-canary-{agent_id}"
        network = f"ditto-screen-{agent_id}"
        canary_state_dir = tempfile.mkdtemp(prefix="ditto-canary-state-")
        os.chmod(canary_state_dir, 0o755)
        tmp_path: str | None = None
        try:
            tmp_path, dl_detail = await self._download_verified(download_url, sha256)
            if tmp_path is None:
                return GateResult(False, dl_detail)
            contract_error = self._contract_error(tmp_path)
            if contract_error is not None:
                return GateResult(False, contract_error)

            built, build_detail = await self._build(tmp_path, tag)
            if not built:
                return GateResult(False, f"build failed: {build_detail}")

            served, serve_detail = await self._run_and_probe(
                tag,
                container,
                canary_container=canary_container,
                network=network,
                canary_state_dir=canary_state_dir,
            )
            if not served:
                return GateResult(False, f"serve check failed: {serve_detail}")
            return GateResult(True, "")
        except Exception as e:  # noqa: BLE001 - the loop must never die on one agent
            logger.exception("gate error for agent_id=%s", agent_id)
            return GateResult(False, f"screener error: {type(e).__name__}: {e}")
        finally:
            await self._teardown(
                container,
                tag,
                canary_container=canary_container,
                network=network,
            )
            shutil.rmtree(canary_state_dir, ignore_errors=True)
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

    def _contract_error(self, tar_path: str) -> str | None:
        """Validate the archive and Rust harness contract without extracting it."""
        try:
            with tarfile.open(tar_path, mode="r:gz") as tar:
                members: dict[str, tarfile.TarInfo] = {}
                unpacked = 0
                for member in tar.getmembers():
                    name = member.name.removeprefix("./")
                    if not name and member.isdir():
                        continue
                    path = PurePosixPath(name)
                    if (
                        not name
                        or name.startswith("/")
                        or "\\" in name
                        or (path.parts and path.parts[0].endswith(":"))
                        or ".." in path.parts
                    ):
                        return "contract failed: unsafe archive path"
                    if name in members:
                        return "contract failed: duplicate archive path"
                    if not (member.isfile() or member.isdir()):
                        return "contract failed: links and special files are forbidden"
                    unpacked += member.size
                    if unpacked > _MAX_UNPACKED_BYTES:
                        return "contract failed: archive expands beyond the safety cap"
                    members[name] = member

                if "Dockerfile" not in members or not members["Dockerfile"].isfile():
                    return "contract failed: no Dockerfile at tarball root"
                manifest_member = members.get("Cargo.toml")
                if manifest_member is None or not manifest_member.isfile():
                    return "contract failed: no Cargo.toml at tarball root"
                if not any(
                    name.startswith("src/") and name.endswith(".rs") and member.isfile()
                    for name, member in members.items()
                ):
                    return "contract failed: no Rust source under src/"

                manifest_file = tar.extractfile(manifest_member)
                if manifest_file is None:
                    return "contract failed: Cargo.toml is unreadable"
                try:
                    manifest = tomllib.loads(manifest_file.read().decode("utf-8"))
                except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                    return "contract failed: Cargo.toml is invalid"
                if not isinstance(manifest.get("package"), dict):
                    return "contract failed: Cargo.toml has no package"
                return None
        except (tarfile.TarError, OSError) as e:
            logger.warning("could not read tar %s: %s", tar_path, e)
            return "contract failed: archive is unreadable"

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

    async def _run_and_probe(
        self,
        tag: str,
        container: str,
        *,
        canary_container: str,
        network: str,
        canary_state_dir: str,
    ) -> tuple[bool, str]:
        """Run the image, await health, then prove it consumes a model response."""
        port = self._config.container_port
        token = f"ditto-canary-{secrets.token_hex(16)}"
        model_called_path = os.path.join(canary_state_dir, "model-called")
        started, detail = await self._start_canary_sidecar(
            canary_container=canary_container,
            network=network,
            token=token,
            state_dir=canary_state_dir,
        )
        if not started:
            return False, detail

        gateway = f"http://{_CANARY_ALIAS}:8080"
        run_args = [
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "--network",
            network,
            "--memory",
            self._config.build_memory,
            "--pids-limit",
            str(self._config.pids_limit),
            "--publish",
            f"127.0.0.1::{port}",
        ]
        for key, value in self._config.smoke_env:
            run_args += ["-e", f"{key}={value}"]
        # Mirror the production scorer's locked provider contract. These are
        # appended last so an operator's legacy smoke env cannot bypass the
        # fake gateway.
        canary_env = {
            "DITTOBENCH_PROVIDER": "chutes",
            "DITTOBENCH_MODEL": LOCKED_HARNESS_MODEL,
            "CHUTES_BASE_URL": f"{gateway}/v1",
            "CHUTES_API_KEY": "relay",
            "OPENAI_BASE_URL": f"{gateway}/v1",
            "OPENAI_API_KEY": "relay",
            "OLLAMA_BASE_URL": gateway,
        }
        for key, value in canary_env.items():
            run_args += ["-e", f"{key}={value}"]
        run_args.append(tag)
        code, out = await self._run(run_args, timeout=self._config.run_timeout_seconds)
        if code != 0:
            return False, f"container did not start: {_log_tail(out)}"

        mapped = await self._published_port(container, port)
        if mapped is None:
            return False, "could not resolve published port"

        harness_base = f"http://127.0.0.1:{mapped}"
        healthy, detail = await self._wait_healthy(harness_base)
        if not healthy:
            return False, detail
        return await self._run_model_canary(
            harness_base,
            token=token,
            model_called_path=model_called_path,
        )

    async def _start_canary_sidecar(
        self,
        *,
        canary_container: str,
        network: str,
        token: str,
        state_dir: str,
    ) -> tuple[bool, str]:
        """Start the fake gateway beside the harness on an internal network."""
        code, out = await self._run(
            ["network", "create", "--internal", network], timeout=30.0
        )
        if code != 0:
            return False, f"could not create canary network: {_log_tail(out)}"

        script = str(Path(__file__).with_name("model_canary.py").resolve())
        code, out = await self._run(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                canary_container,
                "--network",
                network,
                "--network-alias",
                _CANARY_ALIAS,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "64m",
                "--pids-limit",
                "32",
                "-e",
                f"DITTO_CANARY_TOKEN={token}",
                "-e",
                "DITTO_CANARY_STATE_FILE=/state/model-called",
                "-v",
                f"{script}:/app/model_canary.py:ro",
                "-v",
                f"{state_dir}:/state",
                _CANARY_IMAGE,
                "python",
                "/app/model_canary.py",
            ],
            timeout=self._config.run_timeout_seconds,
        )
        if code != 0:
            return False, f"model canary did not start: {_log_tail(out)}"

        probe = (
            "import socket; socket.create_connection(('127.0.0.1', 8080), 2).close()"
        )
        for _ in range(20):
            code, _ = await self._run(
                ["exec", canary_container, "python", "-c", probe], timeout=5.0
            )
            if code == 0:
                return True, ""
            await asyncio.sleep(0.1)
        return False, "model canary did not become ready"

    async def _wait_healthy(self, harness_base: str) -> tuple[bool, str]:
        """Poll the submitted container's health endpoint until the deadline."""
        url = f"{harness_base}{self._config.health_path}"
        deadline = self._config.run_timeout_seconds
        waited = 0.0
        last = "no response"
        while waited < deadline:
            try:
                resp = await self._client.get(url, timeout=5.0)
                if 200 <= resp.status_code < 300:
                    return True, ""
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                last = type(e).__name__
            await asyncio.sleep(_PROBE_INTERVAL_SECONDS)
            waited += _PROBE_INTERVAL_SECONDS
        return False, f"/health never healthy within {deadline:g}s ({last})"

    async def _run_model_canary(
        self,
        harness_base: str,
        *,
        token: str,
        model_called_path: str | None,
    ) -> tuple[bool, str]:
        """Run one hidden-response case and verify the gateway response was used."""
        request = {
            "case_id": secrets.token_hex(16),
            "system_prompt": "You are a concise assistant.",
            "user_input": "Give a brief acknowledgement.",
            "tools": [],
            "tool_endpoint": "",
            "user_id": secrets.token_hex(16),
        }
        try:
            async with self._client.stream(
                "POST",
                f"{harness_base}/run",
                json=request,
                timeout=min(self._config.run_timeout_seconds, 30.0),
            ) as response:
                if not 200 <= response.status_code < 300:
                    return (
                        False,
                        f"model canary /run returned HTTP {response.status_code}",
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_CANARY_RESPONSE_BYTES:
                        return False, "model canary /run response exceeded safety cap"
        except httpx.HTTPError as e:
            return False, f"model canary /run failed: {type(e).__name__}"

        if model_called_path is not None and not os.path.exists(model_called_path):
            return False, "model canary observed no model call"
        try:
            payload = json.loads(body)
            final_text = payload.get("final_text", "")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return False, "model canary /run returned an invalid response"
        if not isinstance(final_text, str) or token not in final_text:
            return False, "model canary response was not used by the harness"
        return True, ""

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

    async def _teardown(
        self,
        container: str,
        tag: str,
        *,
        canary_container: str,
        network: str,
    ) -> None:
        """Best-effort removal of the container + image; never raises."""
        try:
            await self._run(["rm", "-f", container], timeout=30.0)
            await self._run(["rm", "-f", canary_container], timeout=30.0)
            await self._run(["network", "rm", network], timeout=30.0)
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
