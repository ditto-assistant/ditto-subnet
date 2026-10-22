"""Hostile-runtime probe sandbox for one submitted harness image.

The sandbox reuses the screener's runtime contract from ``ditto_screener.gate``:
the same non-root read-only container, tmpfs, resource limits, dropped
capabilities, ``--internal`` Docker network, scorer-equivalent inference
environment, and openrouter.ai TLS shim. The only difference is the sidecar:
``scripted_gateway.py`` lets a reviewer dictate model and tool replies and
records the full transcript. A session is persisted as JSON so separate CLI
calls (one per reviewer tool call) reach the same running containers.

This module is report-only. It never contacts Platform and cannot change a
screening outcome.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ditto_screener.gate import (
    _CANARY_IMAGE,
    _CHAT_GATEWAY_PORT,
    _GATEWAY_ALIAS,
    _HARNESS_ALIAS,
    _OPENROUTER_SHIM_CA_BUNDLE_PATH,
    _OPENROUTER_SHIM_HOST,
    _VALIDATOR_SANDBOX_CPUS,
    _VALIDATOR_SANDBOX_MEMORY,
    _VALIDATOR_SANDBOX_PIDS,
    _VALIDATOR_SANDBOX_TMPFS,
    _VALIDATOR_SANDBOX_USER,
    _dockerfile_instructions,
    _gateway_runtime_env,
    _normalized_build_context,
    _write_openrouter_shim_certs,
)

_GATEWAY_SCRIPT = Path(__file__).with_name("scripted_gateway.py")
_CONTAINER_PORT = 8080
# Production screeners and validators are x86_64; miner sources may use
# arch-specific syscalls (e.g. ``libc::SYS_fork``), so probe the same target.
PLATFORM = "linux/amd64"
_MAX_RESPONSE_BYTES = 1024 * 1024


class SandboxError(RuntimeError):
    """Infrastructure failure: never evidence about the submission."""


def _docker_env() -> dict[str, str] | None:
    """One daemon builds and runs every probe image: PROBE_REVIEW_DOCKER_HOST."""
    host = os.environ.get("PROBE_REVIEW_DOCKER_HOST")
    if not host:
        return None
    if not host.startswith("unix://"):
        # Sessions bind-mount host-staged state (gateway script, shim certs,
        # transcript), which only a same-host daemon can see.
        raise SandboxError(
            "PROBE_REVIEW_DOCKER_HOST must be a same-host unix:// socket"
        )
    # A context would take precedence over DOCKER_HOST and could select
    # another daemon, so the isolated host is the only daemon selector left.
    env = {k: v for k, v in os.environ.items() if k != "DOCKER_CONTEXT"}
    env["DOCKER_HOST"] = host
    return env


_MAX_DOCKER_OUTPUT = 1024 * 1024


def _tail(handle: Any) -> str:
    """The last ``_MAX_DOCKER_OUTPUT`` bytes of a spooled stream."""
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - _MAX_DOCKER_OUTPUT))
    data: bytes = handle.read()
    return data.decode("utf-8", "replace")


def _docker(args: list[str], *, timeout: float, stdin: Any = None) -> tuple[int, str]:
    # Untrusted RUN steps can print without bound, so output spools to disk
    # and only its tail is read back into memory.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(
                ["docker", *args],
                env=_docker_env(),
                stdin=stdin,
                stdout=out,
                stderr=err,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, f"[timeout after {timeout:g}s]"
        output = _tail(out)
        if completed.returncode != 0:
            output += _tail(err)
        return completed.returncode, output


_ARCHIVE_LABEL = "probe-review.archive-sha256"


def file_sha256(path: Path) -> str:
    """Stream the digest so a large archive is never held in memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@contextlib.contextmanager
def _session_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "probe.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _refused_build_entitlement(archive: Path) -> str | None:
    """The gate's contract check: no RUN --network=host / --security=insecure."""
    with tarfile.open(archive) as tar:
        member = next(
            (m for m in tar.getmembers() if m.name in {"Dockerfile", "./Dockerfile"}),
            None,
        )
        handle = tar.extractfile(member) if member is not None else None
        if handle is None:
            return "archive has no readable Dockerfile"
        text = handle.read().decode("utf-8", "replace")
    for instruction, remainder in _dockerfile_instructions(text):
        lowered = remainder.casefold()
        if instruction == "RUN" and (
            "--security=insecure" in lowered or "--network=host" in lowered
        ):
            return "Dockerfile requests an insecure build entitlement"
    return None


def build_image(
    archive: Path,
    tag: str,
    *,
    timeout: float = 5400.0,
    allow_host_daemon: bool = False,
) -> tuple[str, float]:
    """Build the exact archive with the screener's build-context normalizer.

    Submitted Dockerfiles run ``RUN`` steps with network access, so the trust
    boundary is the daemon: like the gate's dedicated rootless builder behind
    a host egress guard, builds and the sessions that run them all go to
    ``PROBE_REVIEW_DOCKER_HOST``. The workstation's default daemon is
    used only with an explicit ``allow_host_daemon`` (research on artifacts
    whose build steps the operator accepts, or trusted test fixtures).

    A cached image under ``tag`` is reused only when its label names this
    archive's digest, so a reused tag can never serve another archive's code.
    """
    if not os.environ.get("PROBE_REVIEW_DOCKER_HOST") and not allow_host_daemon:
        raise SandboxError(
            "refusing to build an untrusted archive on the default Docker daemon: "
            "set PROBE_REVIEW_DOCKER_HOST to an isolated builder or pass "
            "allow_host_daemon (--allow-host-daemon) to accept the risk"
        )
    refused = _refused_build_entitlement(archive)
    if refused is not None:
        raise SandboxError(refused)
    started = time.monotonic()
    digest = file_sha256(archive)
    code, out = _docker(
        [
            "image",
            "inspect",
            "--format",
            f'{{{{.Id}}}} {{{{index .Config.Labels "{_ARCHIVE_LABEL}"}}}}',
            tag,
        ],
        timeout=30,
    )
    if code == 0:
        image_id, _, label = out.strip().partition(" ")
        if label == digest:
            return image_id, 0.0
    with _normalized_build_context(str(archive)) as context:
        code, out = _docker(
            [
                "build",
                "--platform",
                PLATFORM,
                "--load",
                "--pull=false",
                "--provenance=false",
                "--sbom=false",
                # The gate's per-build limits, plus a process ceiling.
                "--memory",
                "8g",
                "--memory-swap",
                "8g",
                "--cpu-period",
                "100000",
                "--cpu-quota",
                "200000",
                "--shm-size",
                "64m",
                "--ulimit",
                "nofile=1024:1024",
                "--ulimit",
                "nproc=4096:4096",
                "--network",
                "default",
                "--label",
                f"{_ARCHIVE_LABEL}={digest}",
                "-t",
                tag,
                "-f",
                "Dockerfile",
                "-",
            ],
            timeout=timeout,
            stdin=context,
        )
    if code != 0:
        raise SandboxError(f"build failed: {out[-1500:]}")
    code, out = _docker(
        ["image", "inspect", "--format", "{{.Id}}", tag],
        timeout=30,
    )
    if code != 0:
        raise SandboxError("built image is not inspectable")
    return out.strip(), time.monotonic() - started


@dataclass
class ProbeRecord:
    """One executed probe with the complete gateway transcript."""

    probe_id: str
    path: str
    request: dict[str, Any]
    script: dict[str, Any]
    http_status: int | None
    response: Any
    elapsed_ms: int
    transcript: list[dict[str, Any]]
    label: str = ""
    error: str = ""
    # Stimuli that must precede this one on a fresh harness (e.g. /seed).
    preconditions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def model_requests(self) -> list[dict[str, Any]]:
        return [e for e in self.transcript if e.get("surface") in {"chat", "responses"}]

    @property
    def tool_executions(self) -> list[dict[str, Any]]:
        return [e for e in self.transcript if e.get("surface") == "tool"]

    def digest(self) -> str:
        """Content digest of the stimulus only (request + script), for replay."""
        body = json.dumps(
            {
                "path": self.path,
                "request": self.request,
                "script": self.script,
                "preconditions": self.preconditions,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["stimulus_sha256"] = self.digest()
        return value

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> ProbeRecord:
        value = {k: v for k, v in value.items() if k != "stimulus_sha256"}
        return cls(**value)


@dataclass
class Session:
    """Persistent identity of one running probe sandbox."""

    root: str
    image: str
    artifact_sha256: str
    bench_version: int
    token: str = field(default_factory=lambda: secrets.token_hex(6))
    tool_route: str = field(default_factory=lambda: secrets.token_hex(8))
    started: bool = False
    probe_count: int = 0

    @property
    def network(self) -> str:
        return f"probe-net-{self.token}"

    @property
    def gateway(self) -> str:
        return f"probe-gw-{self.token}"

    @property
    def harness(self) -> str:
        return f"probe-harness-{self.token}"

    @property
    def state_dir(self) -> Path:
        return Path(self.root) / "state"

    @property
    def probes_dir(self) -> Path:
        return Path(self.root) / "probes"

    def save(self) -> None:
        (Path(self.root) / "session.json").write_text(
            json.dumps(asdict(self), indent=2)
        )

    @classmethod
    def load(cls, root: str | Path) -> Session:
        return cls(**json.loads((Path(root) / "session.json").read_text()))

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, health_timeout: float = 300.0) -> None:
        # Same scheme as the gate's gateway state: the session root is private
        # to the owner; the sidecar (a different uid, no DAC override) can
        # traverse the state dir, read the script and throwaway shim certs,
        # and append to the transcript without reading it back.
        Path(self.root).mkdir(parents=True, exist_ok=True)
        Path(self.root).chmod(0o700)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.probes_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o711)
        # A restarted session re-stages read-only files; probes already hold
        # their own transcript slices, so the previous state is disposable.
        for stale in self.state_dir.iterdir():
            if stale.is_file():
                stale.unlink()
        _write_openrouter_shim_certs(str(self.state_dir))
        gateway_script = self.state_dir / "scripted_gateway.py"
        shutil.copyfile(_GATEWAY_SCRIPT, gateway_script)
        gateway_script.chmod(0o444)
        script = self.state_dir / "script.json"
        script.write_text("{}")
        script.chmod(0o644)
        transcript = self.state_dir / "transcript.jsonl"
        transcript.touch()
        transcript.chmod(0o622)
        code, out = _docker(
            ["network", "create", "--internal", self.network], timeout=120
        )
        if code != 0:
            raise SandboxError(f"network create failed: {out[-500:]}")
        code, out = _docker(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                self.gateway,
                "--network",
                self.network,
                "--network-alias",
                _GATEWAY_ALIAS,
                "--network-alias",
                _OPENROUTER_SHIM_HOST,
                "--read-only",
                "--ipc",
                "none",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_BIND_SERVICE",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "256m",
                "--pids-limit",
                "64",
                "-e",
                "PROBE_GATEWAY_STATE_DIR=/state",
                "-e",
                "PROBE_GATEWAY_TLS_CERT=/state/leaf.crt",
                "-e",
                "PROBE_GATEWAY_TLS_KEY=/state/leaf.key",
                "-v",
                f"{self.state_dir}:/state",
                _CANARY_IMAGE,
                "python",
                "/state/scripted_gateway.py",
            ],
            timeout=300,
        )
        if code != 0:
            self.stop()
            raise SandboxError(f"gateway start failed: {out[-500:]}")
        try:
            self._start_harness(health_timeout)
        except SandboxError:
            self.stop()
            raise
        self.started = True
        self.save()

    def _start_harness(self, health_timeout: float) -> None:
        chat = f"http://{_GATEWAY_ALIAS}:11435"
        embed = f"http://{_GATEWAY_ALIAS}:11434"
        env = _gateway_runtime_env(
            provider="platform", chat_gateway=chat, embed_gateway=embed
        )
        args = [
            "run",
            "-d",
            "--init",
            "--platform",
            PLATFORM,
            "--name",
            self.harness,
            "--user",
            _VALIDATOR_SANDBOX_USER,
            "--read-only",
            "--ipc",
            "none",
            "--tmpfs",
            _VALIDATOR_SANDBOX_TMPFS,
            "--network",
            self.network,
            "--network-alias",
            _HARNESS_ALIAS,
            "--memory",
            _VALIDATOR_SANDBOX_MEMORY,
            "--cpus",
            _VALIDATOR_SANDBOX_CPUS,
            "--pids-limit",
            _VALIDATOR_SANDBOX_PIDS,
            "--ulimit",
            "nofile=1024:1024",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,src={self.state_dir}/ca-bundle.pem,"
            f"dst={_OPENROUTER_SHIM_CA_BUNDLE_PATH},readonly",
        ]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args.append(self.image)
        code, out = _docker(args, timeout=300)
        if code != 0:
            raise SandboxError(f"harness start failed: {out[-500:]}")
        deadline = time.monotonic() + health_timeout
        while time.monotonic() < deadline:
            status, _, _ = self._http("GET", "/health", None, timeout=5)
            if status is not None and 200 <= status < 300:
                return
            code, state = _docker(
                ["container", "inspect", "--format", "{{.State.Status}}", self.harness],
                timeout=10,
            )
            if state.strip() in {"exited", "dead"}:
                _, logs = _docker(["logs", "--tail", "40", self.harness], timeout=10)
                raise SandboxError(f"harness exited before health: {logs[-1500:]}")
            time.sleep(1)
        raise SandboxError("harness never became healthy")

    def restart_harness(self, *, health_timeout: float = 300.0) -> None:
        """Fresh process and tmpfs: clears any seeded memory or run state."""
        _docker(["rm", "-f", self.harness], timeout=60)
        self._start_harness(health_timeout)

    def stop(self) -> None:
        _docker(["rm", "-f", self.harness, self.gateway], timeout=180)
        _docker(["network", "rm", self.network], timeout=120)
        self.started = False
        self.save()

    # -- probing -------------------------------------------------------------

    def tool_endpoint(self, case_id: str, user_id: str) -> str:
        # Scored relays serve inference and tools on ONE origin, and some
        # harnesses enforce that (tool origin must equal the inference origin),
        # so the tool route rides the chat port rather than a separate one.
        query = urlencode(
            {"cap": secrets.token_urlsafe(16), "case_id": case_id, "user_id": user_id}
        )
        return (
            f"http://{_GATEWAY_ALIAS}:{_CHAT_GATEWAY_PORT}/v1/tools/{self.tool_route}"
            f"/tool?{query}"
        )

    def _http(
        self, method: str, path: str, payload: Any, *, timeout: float
    ) -> tuple[int | None, bytes, bool]:
        """(status, body, truncated) for one request, via the gateway container."""
        encoded = (
            base64.b64encode(json.dumps(payload).encode()).decode()
            if payload is not None
            else ""
        )
        script = (
            "import base64,sys,urllib.request,urllib.error\n"
            "url,method,data,t=sys.argv[1:5]\n"
            "body=base64.b64decode(data) if data else None\n"
            "req=urllib.request.Request(url,data=body,"
            "method=method,headers={'Content-Type':'application/json'})\n"
            "try:\n r=urllib.request.urlopen(req,timeout=float(t))\n"
            "except urllib.error.HTTPError as e:\n r=e\n"
            "except Exception as e:\n"
            " sys.stdout.write('ERR '+type(e).__name__);sys.exit(24)\n"
            f"out=r.read({_MAX_RESPONSE_BYTES + 1})\n"
            f"cut=len(out)>{_MAX_RESPONSE_BYTES}\n"
            f"out=out[:{_MAX_RESPONSE_BYTES}]\n"
            "sys.stdout.write(str(r.status)+(' TRUNCATED' if cut else '')+'\\n')\n"
            "sys.stdout.flush();sys.stdout.buffer.write(out)\n"
        )
        try:
            completed = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.gateway,
                    "python",
                    "-c",
                    script,
                    f"http://{_HARNESS_ALIAS}:{_CONTAINER_PORT}{path}",
                    method,
                    encoded,
                    str(timeout),
                ],
                capture_output=True,
                timeout=timeout + 15,
                check=False,
                env=_docker_env(),
            )
        except subprocess.TimeoutExpired:
            return None, b"probe transport timeout", False
        if completed.returncode != 0:
            return None, completed.stdout + completed.stderr, False
        head, _, body = completed.stdout.partition(b"\n")
        status, _, flag = head.partition(b" ")
        try:
            return int(status), body, flag == b"TRUNCATED"
        except ValueError:
            return None, completed.stdout, False

    def _transcript_offset(self) -> int:
        return (self.state_dir / "transcript.jsonl").stat().st_size

    def _transcript_since(self, offset: int) -> list[dict[str, Any]]:
        with (self.state_dir / "transcript.jsonl").open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        entries = []
        for line in data.splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def probe(
        self,
        request: dict[str, Any],
        *,
        script: dict[str, Any] | None = None,
        path: str = "/run",
        timeout: float = 120.0,
        label: str = "",
        preconditions: list[dict[str, Any]] | None = None,
    ) -> ProbeRecord:
        """POST one request with a dictated model script; return the transcript.

        Reviewer tools are separate processes sharing one session, so a
        cross-process lock covers the shared script, transcript window and
        probe counter.
        """
        with _session_lock(Path(self.root)):
            saved = Path(self.root) / "session.json"
            if saved.exists():
                self.probe_count = max(
                    self.probe_count, json.loads(saved.read_text())["probe_count"]
                )
            return self._probe_locked(
                request,
                script=script,
                path=path,
                timeout=timeout,
                label=label,
                preconditions=preconditions,
            )

    def _probe_locked(
        self,
        request: dict[str, Any],
        *,
        script: dict[str, Any] | None,
        path: str,
        timeout: float,
        label: str,
        preconditions: list[dict[str, Any]] | None,
    ) -> ProbeRecord:
        script = dict(script or {})
        self.probe_count += 1
        probe_id = f"p{self.probe_count:03d}-{secrets.token_hex(3)}"
        # The epoch key forces the gateway to reset its cursor even when the
        # reviewer reuses an identical script for a replay.
        (self.state_dir / "script.json").write_text(
            json.dumps({**script, "_epoch": probe_id})
        )
        offset = self._transcript_offset()
        started = time.monotonic()
        status, body, truncated = self._http("POST", path, request, timeout=timeout)
        elapsed = round((time.monotonic() - started) * 1000)
        time.sleep(0.05)
        transcript = self._transcript_since(offset)
        try:
            response: Any = json.loads(body) if body else None
        except (ValueError, UnicodeError):
            response = body[:4096].decode("utf-8", "replace")
        record = ProbeRecord(
            probe_id=probe_id,
            path=path,
            request=request,
            script=script,
            http_status=status,
            response=response,
            elapsed_ms=elapsed,
            transcript=transcript,
            label=label,
            preconditions=list(preconditions or []),
            error=(
                body[:500].decode("utf-8", "replace")
                if status is None
                else f"response truncated at {_MAX_RESPONSE_BYTES} bytes"
                if truncated
                else ""
            ),
        )
        (self.probes_dir / f"{probe_id}.json").write_text(
            json.dumps(record.to_json(), indent=1)
        )
        self.save()
        return record
