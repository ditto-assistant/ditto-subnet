"""Drive a miner harness OCI image over stdio.

The validator runs each miner submission inside a sandboxed Docker container
with the network disabled (or restricted to a single OpenAI-compatible LLM
endpoint passed via env). The harness reads ``ChallengeRequest`` JSON lines
from stdin and writes ``MinerResponse`` JSON lines to stdout. This module
spawns the container, streams requests, and reads back responses with a
hard per-case wall-clock budget.

This is the contributor-facing reference driver, not the production
validator: the on-chain validator is expected to run the same protocol with
stricter cgroup limits and audited image-digest pinning (see
``docs/anti_gaming.md``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HarnessConfig:
    """How to launch the miner's harness container.

    ``image`` is the fully-qualified image reference (preferably pinned by
    digest, e.g. ``my-harness@sha256:...``). ``network`` is passed through to
    ``docker run --network``; the default ``none`` matches the subnet's
    sandboxing requirement.

    ``command`` is an escape hatch that bypasses Docker entirely: when set,
    :class:`HarnessDriver` runs the command as a plain subprocess. Use this
    only for local development of a harness binary or for testing the
    runner stub itself; production validators MUST leave it as ``None`` so
    every miner submission runs inside the sandbox.
    """

    image: str = ""
    network: str = "none"
    cpu_limit: str = "2"
    memory_limit: str = "4g"
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    docker_bin: str = "docker"
    command: list[str] | None = None


class HarnessError(RuntimeError):
    """Raised when the harness container fails to launch or violates the protocol."""


class HarnessTimeoutError(HarnessError):
    """Raised when a single challenge exceeds its wall-clock budget."""


class HarnessDriver:
    """Async-free stdio driver for a single harness container.

    Usage::

        with HarnessDriver(HarnessConfig(image="my-harness:dev")) as harness:
            response = harness.send(challenge_request, deadline_ms=8000)

    The container is started lazily on first use and torn down on context
    exit. Containers are short-lived by design: validators should rotate
    them between cases to defeat in-process caches.
    """

    def __init__(self, config: HarnessConfig) -> None:
        """Store the launch config; the container is created in ``__enter__``."""
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> HarnessDriver:
        """Launch the container and prepare stdio pipes."""
        cmd = self._build_cmd()
        if (
            self._config.command is None
            and shutil.which(self._config.docker_bin) is None
        ):
            raise HarnessError(
                f"docker binary {self._config.docker_bin!r} not found on PATH"
            )
        logger.info("launching harness: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - docker is trusted
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as e:
            raise HarnessError(
                f"failed to launch harness {self._config.image}: {e}"
            ) from e
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Tear down the container, killing it if it has not exited."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                logger.warning("harness %s did not exit after kill", self._config.image)

    def send(self, request: dict[str, Any], deadline_ms: int) -> dict[str, Any]:
        """Send one challenge and read back the miner response JSON.

        Args:
            request: A ``ChallengeRequest`` dict matching
                ``schemas/challenge_request.schema.json``.
            deadline_ms: Wall-clock budget for this case. Exceeding this
                raises :class:`HarnessTimeoutError` and the validator will
                score the case as zero.

        Returns:
            The parsed ``MinerResponse`` dict.

        Raises:
            HarnessError: If the harness writes invalid JSON or stdout closes.
            HarnessTimeoutError: If the harness does not respond within
                ``deadline_ms``.
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise HarnessError("harness driver used outside its context manager")

        try:
            payload = (json.dumps(request, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise HarnessError(f"failed to write challenge to harness: {e}") from e

        line = _read_line_with_timeout(proc, deadline_ms)
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise HarnessError(
                f"harness emitted invalid JSON for response: {e}; raw={line!r}"
            ) from e

    def _build_cmd(self) -> list[str]:
        if self._config.command is not None:
            return list(self._config.command)
        if not self._config.image:
            raise HarnessError("HarnessConfig.image is required when command is unset")
        cmd: list[str] = [
            self._config.docker_bin,
            "run",
            "--rm",
            "-i",
            f"--network={self._config.network}",
            f"--cpus={self._config.cpu_limit}",
            f"--memory={self._config.memory_limit}",
        ]
        for k, v in sorted(self._config.env.items()):
            cmd.extend(["-e", f"{k}={v}"])
        cmd.extend(self._config.extra_args)
        cmd.append(self._config.image)
        return cmd


def _read_line_with_timeout(proc: subprocess.Popen[bytes], deadline_ms: int) -> bytes:
    """Read one newline-terminated record from ``proc.stdout`` with a budget.

    The harness contract requires each response to be a single JSON object
    on one line. We read with a select-based timeout so a misbehaving
    harness cannot block the validator indefinitely.
    """
    import select
    import time

    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + (deadline_ms / 1000.0)
    buf = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessTimeoutError(f"harness did not respond within {deadline_ms}ms")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise HarnessTimeoutError(f"harness did not respond within {deadline_ms}ms")
        chunk = (
            proc.stdout.read1(8192)
            if hasattr(proc.stdout, "read1")
            else proc.stdout.read(8192)
        )
        if not chunk:
            raise HarnessError("harness stdout closed before a complete response")
        buf.extend(chunk)
        nl = buf.find(b"\n")
        if nl >= 0:
            return bytes(buf[:nl])


def stream_challenges(
    driver: HarnessDriver,
    requests: list[dict[str, Any]],
    deadline_ms: int,
) -> Iterator[dict[str, Any]]:
    """Yield ``(response_dict)`` for each request in order.

    Helper for callers that prefer iterator semantics over manual loops.
    Exceptions from :meth:`HarnessDriver.send` propagate; the caller decides
    whether a single timeout aborts the run or scores zero for the case.
    """
    for req in requests:
        yield driver.send(req, deadline_ms=deadline_ms)
