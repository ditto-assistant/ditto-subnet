#!/usr/bin/env python3
"""Prove the query adapter pilot end to end through the NORMAL entry points.

For each scenario this script:

1. writes a synthetic pilot token to a private temp file,
2. spawns ``python -m query_adapter_pilot.miner`` on loopback port 0 and waits
   for its ready file (skipped for the ``unavailable`` scenario, which targets
   a held loopback port that resets every connection instead),
3. runs ``python -m query_adapter_pilot run`` against the committed workload,
4. reads the report (and the miner's ``/v1/health``), asserts the scenario's
   expectations, and SIGTERMs the miner (which must exit 0).

It prints one row per scenario and exits non-zero if any expectation fails.
Reports land under a temp dir unless ``--out-dir`` is given; nothing is
written into the source tree.

    uv run python scripts/prove.py                 # every scenario
    uv run python scripts/prove.py --only happy incorrect
    uv run python scripts/prove.py --list

Synthetic-only: loopback miner, ``syn-`` tenants, no payouts, no scoring.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from query_adapter_pilot.contract import (
    CONTRACT_VERSION,
    OUTCOMES,
    PARITY_MIN_COSINE,
    ROUTE_HEALTH,
    TOKEN_HEADER,
    Outcome,
    Source,
)
from query_adapter_pilot.workload import Workload

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD_DIR = ROOT / "testdata" / "workloads"
REPORT_KIND = "pilot-report"
# Generous per-call budget for scenarios that are not about timeouts, so a slow
# CI runner does not turn a cold first touch into a spurious timeout.
DEFAULT_TIMEOUT_MS = 2000
READY_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 15.0
CLIENT_TIMEOUT_S = 180.0

Report = dict[str, Any]


# ─── Scenario model ──────────────────────────────────────────────────────────


@dataclass
class MinerHandle:
    """A running (or deliberately absent) miner. ``exit_code`` is filled in
    after shutdown for subprocess miners; ``None`` means not applicable."""

    url: str
    exit_code: int | None = None
    log_path: Path | None = None


@dataclass(frozen=True)
class Ctx:
    scenario: Scenario
    workload: Workload
    report: Report
    health: dict[str, Any] | None


Check = Callable[[Ctx], list[str]]


@dataclass(frozen=True)
class Scenario:
    name: str
    workload: str
    checks: tuple[Check, ...]
    fault: str = "none"
    slow_ms: int = 0
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    cache_capacity: int = 64
    max_tenants: int = 1024
    no_miner: bool = False
    description: str = ""

    @property
    def run_id(self) -> str:
        return f"prove-{self.name}"[:32]

    @property
    def workload_path(self) -> Path:
        return WORKLOAD_DIR / f"{self.workload}.json"


@dataclass
class Result:
    scenario: Scenario
    failures: list[str] = field(default_factory=list)
    report: Report | None = None
    health: dict[str, Any] | None = None
    report_path: Path | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failures


# ─── Report accessors ────────────────────────────────────────────────────────


def _requests(report: Report) -> list[dict[str, Any]]:
    reqs = report.get("requests")
    return reqs if isinstance(reqs, list) else []


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _counts(report: Report, key: str) -> Counter[str]:
    return Counter(str(r.get(key)) for r in _requests(report))


def _originals(ctx: Ctx) -> set[int]:
    return {r.index for r in ctx.workload.requests if r.replay_of is None}


def _fmt(counter: Counter[str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(counter.items())) or "-"


# ─── Checks ──────────────────────────────────────────────────────────────────


def check_report_shape(ctx: Ctx) -> list[str]:
    """Applied to every scenario: the report is a v1 pilot report covering
    every workload request exactly once, with no payouts."""
    rep, out = ctx.report, []
    if rep.get("contract") != CONTRACT_VERSION:
        out.append(f"report contract {rep.get('contract')!r} != {CONTRACT_VERSION!r}")
    if rep.get("kind") != REPORT_KIND:
        out.append(f"report kind {rep.get('kind')!r} != {REPORT_KIND!r}")
    reqs = _requests(rep)
    n = len(ctx.workload.requests)
    if [r.get("index") for r in reqs] != list(range(n)):
        out.append(f"report has {len(reqs)} requests / indices out of order, workload has {n}")
    if _get(rep, "summary", "total") != n:
        out.append(f"summary.total {_get(rep, 'summary', 'total')} != {n}")
    unknown = {str(r.get("outcome")) for r in reqs} - OUTCOMES
    if unknown:
        out.append(f"unknown outcomes {sorted(unknown)}")
    if _get(rep, "ledger", "payouts") != "none":
        out.append(f"ledger.payouts {_get(rep, 'ledger', 'payouts')!r} != 'none'")
    for r in reqs:
        if r.get("user_ref") and not str(r.get("user_ref")).startswith("syn-"):
            out.append(f"request {r.get('index')}: non-synthetic user_ref")
    return out


def expect_all(outcome: str, source: str) -> Check:
    def check(ctx: Ctx) -> list[str]:
        out = []
        for r in _requests(ctx.report):
            if r.get("outcome") != outcome or r.get("source") != source:
                out.append(
                    f"request {r.get('index')}: {r.get('outcome')}/{r.get('source')}"
                    f" != {outcome}/{source} (miner_code={r.get('miner_code')})"
                )
        return out[:5] + ([f"... {len(out) - 5} more"] if len(out) > 5 else [])

    return check


def expect_accepted(units: int | None) -> Check:
    """``units=None`` means one unit per original (non-replay) request."""

    def check(ctx: Ctx) -> list[str]:
        want = len(_originals(ctx)) if units is None else units
        got = _get(ctx.report, "ledger", "accepted_units")
        return [] if got == want else [f"ledger.accepted_units {got} != {want}"]

    return check


def expect_final_parity(ctx: Ctx) -> list[str]:
    """The vector Ditto finally used matches the reference, whatever its source."""
    got = _get(ctx.report, "summary", "parity", "min_reference_cos")
    if not isinstance(got, (int, float)) or got < PARITY_MIN_COSINE:
        return [f"summary.parity.min_reference_cos {got} < {PARITY_MIN_COSINE}"]
    return []


def expect_request_parity(ctx: Ctx) -> list[str]:
    """Per request: every miner-sourced vector was verified at parity, and no
    adapter-sourced vector (miner or local fallback) reports worse parity."""
    out = []
    for r in _requests(ctx.report):
        cos = r.get("reference_cos")
        if r.get("source") == Source.MINER and (r.get("verified") is not True or cos is None):
            out.append(f"request {r.get('index')}: miner vector not verified")
        if r.get("source") in (Source.MINER, Source.FALLBACK_LOCAL) and (
            cos is not None and cos < PARITY_MIN_COSINE
        ):
            out.append(f"request {r.get('index')}: reference_cos {cos} < {PARITY_MIN_COSINE}")
    return out


def expect_verified(ctx: Ctx) -> list[str]:
    bad = [r.get("index") for r in _requests(ctx.report) if r.get("verified") is not True]
    return [f"requests not verified at verify_rate 1.0: {bad[:8]}"] if bad else []


def expect_fault(ctx: Ctx) -> list[str]:
    """The miner really ran the scenario's fault mode."""
    if ctx.health is None:
        return ["miner /v1/health unavailable"]
    got = ctx.health.get("fault")
    return [] if got == ctx.scenario.fault else [f"miner fault {got!r} != {ctx.scenario.fault!r}"]


def _adapt_calls(health: dict[str, Any]) -> int | None:
    ok = _get(health, "requests", "adapt_ok")
    errors = _get(health, "requests", "errors")
    if not isinstance(ok, int) or not isinstance(errors, dict):
        return None
    return ok + sum(int(v) for v in errors.values())


def expect_miner_saw(routed: Callable[[Ctx], int]) -> Check:
    """Miner adapt traffic (adapt_ok + errors) equals exactly the requests Ditto
    routed to it, so refused requests never reached the miner."""

    def check(ctx: Ctx) -> list[str]:
        if ctx.health is None:
            return ["miner /v1/health unavailable"]
        got, want = _adapt_calls(ctx.health), routed(ctx)
        if got != want:
            return [f"miner adapt traffic {got} (adapt_ok+errors) != routed {want}"]
        return []

    return check


def _routed_all(ctx: Ctx) -> int:
    return len(ctx.workload.requests)


def _routed_authorized(ctx: Ctx) -> int:
    return sum(
        1 for r in _requests(ctx.report) if r.get("outcome") != Outcome.REJECTED_UNAUTHORIZED
    )


def check_unauthorized(ctx: Ctx) -> list[str]:
    out = []
    known = {u.user_ref: u.authorized_version is not None for u in ctx.workload.users}
    for r in _requests(ctx.report):
        if str(r.get("user_ref")) not in known:
            out.append(f"request {r.get('index')}: unknown user_ref {r.get('user_ref')!r}")
            continue
        authorized = known[str(r.get("user_ref"))]
        want = (
            (Outcome.OK, Source.MINER)
            if authorized
            else (Outcome.REJECTED_UNAUTHORIZED, Source.FALLBACK_IDENTITY)
        )
        if (r.get("outcome"), r.get("source")) != want:
            out.append(
                f"request {r.get('index')} ({r.get('user_ref')}): "
                f"{r.get('outcome')}/{r.get('source')} != {want[0]}/{want[1]}"
            )
        if not authorized and (
            r.get("miner_code") is not None or r.get("miner_compute_us") is not None
        ):
            out.append(f"request {r.get('index')}: unauthorized request carries miner fields")
    n_auth = sum(1 for r in ctx.workload.requests if known[r.user_ref])
    got = _get(ctx.report, "ledger", "accepted_units")
    if got != n_auth:
        out.append(f"ledger.accepted_units {got} != authorized requests {n_auth}")
    return out


def check_isolation(ctx: Ctx) -> list[str]:
    """Each user maps to one tenant id, distinct users to distinct ids, and
    every tenant individually holds parity."""
    out = []
    tenant_of: dict[str, set[str]] = {}
    worst: dict[str, float] = {}
    for r in _requests(ctx.report):
        user = str(r.get("user_ref"))
        tenant_of.setdefault(user, set()).add(str(r.get("tenant_id")))
        cos = r.get("reference_cos")
        if isinstance(cos, (int, float)):
            worst[user] = min(worst.get(user, 1.0), float(cos))
    for user, tenants in tenant_of.items():
        if len(tenants) != 1:
            out.append(f"{user} mapped to several tenant ids {sorted(tenants)}")
    flat = [next(iter(t)) for t in tenant_of.values() if len(t) == 1]
    if len(set(flat)) != len(flat):
        out.append("two users share a tenant id")
    for u in ctx.workload.users:
        if u.user_ref not in worst:
            out.append(f"{u.user_ref}: no per-tenant parity recorded")
        elif worst[u.user_ref] < PARITY_MIN_COSINE:
            out.append(f"{u.user_ref}: per-tenant min cos {worst[u.user_ref]} < threshold")
    return out


def check_replay(ctx: Ctx) -> list[str]:
    out = []
    originals = _originals(ctx)
    replays = len(ctx.workload.requests) - len(originals)
    ledger = ctx.report.get("ledger") or {}
    if ledger.get("accepted_units") != len(originals):
        out.append(f"ledger.accepted_units {ledger.get('accepted_units')} != {len(originals)}")
    if ledger.get("duplicates_suppressed") != replays:
        out.append(
            f"ledger.duplicates_suppressed {ledger.get('duplicates_suppressed')} != {replays}"
        )
    by_index = {r.get("index"): r for r in _requests(ctx.report)}
    for spec in ctx.workload.requests:
        r = by_index.get(spec.index) or {}
        if spec.replay_of is None:
            if r.get("outcome") != Outcome.OK:
                out.append(f"original {spec.index}: outcome {r.get('outcome')} != ok")
        else:
            if r.get("outcome") not in (Outcome.OK, Outcome.DUPLICATE):
                out.append(f"replay {spec.index}: outcome {r.get('outcome')} not ok/duplicate")
            orig = by_index.get(spec.replay_of) or {}
            if r.get("request_id") != orig.get("request_id"):
                out.append(f"replay {spec.index}: request_id differs from original")
    return out


def check_cache_churn(ctx: Ctx) -> list[str]:
    """Tenants exceed cache capacity: the miner must evict, miss, and hit."""
    if ctx.health is None:
        return ["miner /v1/health unavailable"]
    cache = ctx.health.get("cache") or {}
    out = []
    if cache.get("capacity") != ctx.scenario.cache_capacity:
        out.append(f"cache.capacity {cache.get('capacity')} != {ctx.scenario.cache_capacity}")
    for key in ("evictions", "misses", "hits"):
        value = cache.get(key)
        if not isinstance(value, int) or value <= 0:
            out.append(f"miner cache.{key} {value} is not > 0")
    seen = _counts(ctx.report, "cache")
    if seen.get("hit", 0) == 0 or seen.get("miss", 0) == 0:
        out.append(f"report cache markers {_fmt(seen)} lack hit and miss")
    if len(ctx.workload.users) <= ctx.scenario.cache_capacity:
        out.append("workload tenants do not exceed cache capacity")
    return out


# ─── Scenarios ───────────────────────────────────────────────────────────────

_LOCAL = Source.FALLBACK_LOCAL


def _fallback(outcome: str) -> tuple[Check, ...]:
    return (expect_all(outcome, _LOCAL), expect_accepted(0), expect_final_parity)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "happy",
        "happy",
        (
            expect_all(Outcome.OK, Source.MINER),
            expect_accepted(None),
            expect_final_parity,
            expect_request_parity,
            expect_verified,
            expect_fault,
            expect_miner_saw(_routed_all),
        ),
        description="honest miner: every vector accepted at parity",
    ),
    Scenario(
        "incorrect",
        "happy",
        (*_fallback(Outcome.REJECTED_INCORRECT), expect_verified, expect_fault),
        fault="perturb",
        description="well-formed wrong vectors are caught by parity verification",
    ),
    *(
        Scenario(
            f"invalid-{fault}",
            "smoke",
            (*_fallback(Outcome.REJECTED_INVALID), expect_fault),
            fault=fault,
            description=f"{fault} output is rejected before parity",
        )
        for fault in ("nan", "bad-norm", "wrong-echo")
    ),
    Scenario(
        "unavailable",
        "smoke",
        _fallback(Outcome.UNAVAILABLE),
        no_miner=True,
        description="held loopback port resets every connection: Ditto recomputes locally",
    ),
    Scenario(
        "timeout",
        "smoke",
        (*_fallback(Outcome.TIMEOUT), expect_fault),
        fault="slow",
        slow_ms=1000,
        timeout_ms=200,
        description="miner slower than the per-call budget",
    ),
    Scenario(
        "stale",
        "stale",
        _fallback(Outcome.REJECTED_STALE),
        description="authorized version was retired by a newer provision",
    ),
    Scenario(
        "missing",
        "missing",
        _fallback(Outcome.REJECTED_MISSING),
        description="authorized version was never provisioned",
    ),
    Scenario(
        "unauthorized",
        "unauthorized",
        (check_unauthorized, expect_request_parity, expect_miner_saw(_routed_authorized)),
        description="revoked user is refused locally with no miner call",
    ),
    Scenario(
        "isolation",
        "isolation",
        (
            expect_all(Outcome.OK, Source.MINER),
            expect_accepted(None),
            expect_final_parity,
            check_isolation,
        ),
        description="same version numbers and queries across tenants stay separate",
    ),
    Scenario(
        "isolation-swap",
        "isolation",
        (*_fallback(Outcome.REJECTED_INCORRECT), expect_fault),
        fault="swap-tenant",
        description="cross-tenant output is caught by verification",
    ),
    Scenario(
        "replay",
        "replay",
        (check_replay, expect_final_parity),
        description="replayed request ids settle one usage unit",
    ),
    Scenario(
        "multi-tenant",
        "multi_tenant",
        (
            expect_all(Outcome.OK, Source.MINER),
            expect_accepted(None),
            expect_final_parity,
            check_cache_churn,
        ),
        cache_capacity=8,
        description="32 tenants over an 8-entry cache: evictions, misses, warm hits",
    ),
)


def scenario(name: str) -> Scenario:
    for s in SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(name)


def evaluate(scn: Scenario, report: Report, health: dict[str, Any] | None) -> list[str]:
    """Run the shape check and every scenario check against a report."""
    ctx = Ctx(scn, Workload.load(scn.workload_path), report, health)
    failures: list[str] = []
    for check in (check_report_shape, *scn.checks):
        failures.extend(check(ctx))
    return failures


# ─── Processes ───────────────────────────────────────────────────────────────


def write_token(workdir: Path) -> tuple[Path, str]:
    """A fresh synthetic pilot token in a 0600 file. Never a real credential."""
    token = "syn-pilot-" + os.urandom(12).hex()
    path = workdir / "pilot-token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return path, token


_RST_ON_CLOSE = struct.pack("ii", 1, 0)  # SO_LINGER on, 0 s: close() sends RST


@contextlib.contextmanager
def refusing_loopback_url() -> Iterator[str]:
    """A loopback URL that no miner serves. The port stays held by this
    process for the whole ``with`` block, so no other process can take it (no
    bind-then-release race), and every connection is reset at once.

    A socket that is bound but not listening is not enough: Linux answers its
    SYNs with RST, but macOS silently drops them, so the client would see a
    timeout instead of a refusal. Accept-then-RST fails every connection
    immediately on both platforms, and the client classifies a reset as
    ``unavailable``."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stop = threading.Event()

    def reset_everything() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with contextlib.suppress(OSError):
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _RST_ON_CLOSE)
            conn.close()

    with srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        srv.settimeout(0.05)
        thread = threading.Thread(target=reset_everything, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{srv.getsockname()[1]}"
        finally:
            stop.set()
            thread.join(timeout=2)


# Per-scenario files the harness, miner, or client write. They are removed
# before each run so a reused --out-dir can never feed a stale ready file,
# report, or health snapshot into a new run.
SCENARIO_ARTIFACTS = (
    "miner-ready.json",
    "report.json",
    "health.json",
    "client.log",
    "miner.log",
    "pilot-token",
)


def clear_artifacts(workdir: Path) -> None:
    for name in SCENARIO_ARTIFACTS:
        (workdir / name).unlink(missing_ok=True)


def _wait_ready(proc: subprocess.Popen[bytes], ready: Path, log: Path) -> dict[str, Any]:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"miner exited {proc.returncode} before ready:\n{_tail(log)}")
        if ready.is_file():
            try:
                info = json.loads(ready.read_text())
            except json.JSONDecodeError:
                info = None  # partially written; retry
            if isinstance(info, dict) and info.get("url"):
                if info.get("contract") != CONTRACT_VERSION:
                    raise RuntimeError(f"miner ready file contract {info.get('contract')!r}")
                if not str(info["url"]).startswith("http://127.0.0.1:"):
                    raise RuntimeError(f"miner is not on loopback: {info['url']!r}")
                return info
        time.sleep(0.05)
    raise RuntimeError(f"miner not ready after {READY_TIMEOUT_S}s:\n{_tail(log)}")


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


@contextlib.contextmanager
def subprocess_miner(scn: Scenario, workdir: Path, token_file: Path) -> Iterator[MinerHandle]:
    """Spawn the reference miner CLI on loopback port 0 and SIGTERM it after."""
    ready = workdir / "miner-ready.json"
    log = workdir / "miner.log"
    ready.unlink(missing_ok=True)  # never accept a ready file from an earlier run
    cmd = [
        sys.executable,
        "-m",
        "query_adapter_pilot.miner",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--token-file",
        str(token_file),
        "--cache-capacity",
        str(scn.cache_capacity),
        "--max-tenants",
        str(scn.max_tenants),
        "--fault",
        scn.fault,
        "--ready-file",
        str(ready),
    ]
    if scn.slow_ms:
        cmd += ["--slow-ms", str(scn.slow_ms)]
    with log.open("wb") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=workdir)
        handle = MinerHandle(url="", log_path=log)
        try:
            handle.url = str(_wait_ready(proc, ready, log)["url"])
            yield handle
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            handle.exit_code = proc.returncode


Launcher = Callable[[Scenario, Path, Path], contextlib.AbstractContextManager[MinerHandle]]


def fetch_health(url: str, token: str) -> dict[str, Any] | None:
    req = urllib.request.Request(url.rstrip("/") + ROUTE_HEALTH, headers={TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def run_client(
    scn: Scenario, url: str, token_file: Path, out: Path
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "query_adapter_pilot",
        "run",
        "--workload",
        str(scn.workload_path),
        "--miner-url",
        url,
        "--token-file",
        str(token_file),
        "--out",
        str(out),
        "--run-id",
        scn.run_id,
        "--verify-rate",
        "1.0",
        "--fallback",
        "local",
        "--timeout-ms",
        str(scn.timeout_ms),
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=CLIENT_TIMEOUT_S, cwd=out.parent
    )


def run_scenario(scn: Scenario, out_dir: Path, launcher: Launcher = subprocess_miner) -> Result:
    started = time.monotonic()
    result = Result(scn)
    # Absolute before deriving any child path: the miner and client run with a
    # different cwd and would otherwise resolve a relative path a second time.
    workdir = out_dir.resolve() / scn.name
    workdir.mkdir(parents=True, exist_ok=True)
    clear_artifacts(workdir)
    token_file, token = write_token(workdir)
    report_path = workdir / "report.json"
    result.report_path = report_path
    handle: MinerHandle | None = None
    try:
        with contextlib.ExitStack() as stack:
            if scn.no_miner:
                # port held (resetting connections) until the client has finished
                handle = MinerHandle(url=stack.enter_context(refusing_loopback_url()))
            else:
                handle = stack.enter_context(launcher(scn, workdir, token_file))
            proc = run_client(scn, handle.url, token_file, report_path)
            (workdir / "client.log").write_text(proc.stdout + proc.stderr)
            if proc.returncode != 0:
                result.failures.append(
                    f"client exited {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}"
                )
            if not scn.no_miner:
                result.health = fetch_health(handle.url, token)
                if result.health is not None:
                    (workdir / "health.json").write_text(json.dumps(result.health, indent=2))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        result.failures.append(f"harness: {exc}")
    if handle is not None and handle.exit_code not in (None, 0):
        result.failures.append(f"miner exited {handle.exit_code} on SIGTERM (want 0)")
    if report_path.is_file():
        try:
            result.report = json.loads(report_path.read_text())
        except json.JSONDecodeError as exc:
            result.failures.append(f"report is not JSON: {exc}")
    elif not result.failures:
        result.failures.append("client wrote no report")
    if isinstance(result.report, dict):
        result.failures.extend(evaluate(scn, result.report, result.health))
    result.seconds = time.monotonic() - started
    return result


# ─── Output ──────────────────────────────────────────────────────────────────

COLUMNS = ("scenario", "outcomes", "sources", "accepted", "dupes", "min_cos", "p50/p95 ms", "")


def _num(value: Any, fmt: str) -> str:
    return format(value, fmt) if isinstance(value, (int, float)) else "-"


def row(result: Result) -> tuple[str, ...]:
    rep = result.report or {}
    lat = _get(rep, "summary", "latency_ms") or {}
    return (
        result.scenario.name,
        _fmt(_counts(rep, "outcome")),
        _fmt(_counts(rep, "source")),
        _num(_get(rep, "ledger", "accepted_units"), "d"),
        _num(_get(rep, "ledger", "duplicates_suppressed"), "d"),
        _num(_get(rep, "summary", "parity", "min_reference_cos"), ".7f"),
        f"{_num(lat.get('end_to_end_p50'), '.2f')}/{_num(lat.get('end_to_end_p95'), '.2f')}",
        "PASS" if result.ok else "FAIL",
    )


def table(results: Sequence[Result]) -> str:
    rows = [COLUMNS, *(row(r) for r in results)]
    widths = [max(len(r[i]) for r in rows) for i in range(len(COLUMNS))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)) for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(line.rstrip() for line in lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--out-dir", type=Path, help="where reports go (default: a new temp dir)")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="run only these scenarios")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args(argv)
    if args.list:
        for s in SCENARIOS:
            print(f"{s.name:18} {s.workload:13} fault={s.fault:11} {s.description}")
        return 0
    try:
        chosen = [scenario(n) for n in args.only] if args.only else list(SCENARIOS)
    except KeyError as exc:
        parser.error(f"unknown scenario {exc}")
    out_dir = (args.out_dir or Path(tempfile.mkdtemp(prefix="qa-pilot-prove-"))).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for scn in chosen:
        res = run_scenario(scn, out_dir)
        results.append(res)
        print(f"[{'PASS' if res.ok else 'FAIL'}] {scn.name} ({res.seconds:.1f}s)", flush=True)
    print()
    print(table(results))
    failed = [r for r in results if not r.ok]
    for r in failed:
        print(f"\n{r.scenario.name}: {len(r.failures)} failed expectation(s)")
        for f in r.failures:
            print(f"  - {f}")
    print(f"\nreports: {out_dir}")
    print(f"{len(results) - len(failed)}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
