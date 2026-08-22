"""Secret-safe operator smoke commands for Targon Rentals."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from uuid import uuid4

from screener_capacity.oneshot import (
    DEFAULT_REGISTERED_GRACE_SECONDS,
    delete_oneshot_rental,
    sweep_oneshot_rentals,
)
from screener_capacity.targon import TargonAPIError, TargonClient, workload_summary
from screener_capacity.targon_screen_contract import (
    busybox_contract_rental_script,
    parse_starter_kit_probe_logs,
    starter_kit_rental_script,
)

_WORKLOAD_UID = re.compile(r"^wrk-[a-z0-9]{8,64}$")
_MAX_LOG_TAIL = 10000
_STARTER_KIT_ARCHIVE_URL = (
    "https://codeload.github.com/ditto-assistant/dittobench-starter-kit/"
    "tar.gz/65f9e2cb761a4db4ae71493b9a483870bf48ed7b"
)
_SOURCE_REVIEW_SECRET = "validator-openrouter-key"
_SOURCE_REVIEW_PROJECT = "ditto-app-dev"


def _client(args: argparse.Namespace, *, authenticated: bool) -> TargonClient:
    api_key = None
    if authenticated:
        if not args.api_key_stdin:
            raise SystemExit("authenticated commands require --api-key-stdin")
        api_key = sys.stdin.readline().strip()
        if not api_key:
            raise SystemExit("Secret Manager returned an empty Targon API key")
    return TargonClient(
        api_key=api_key,
        org_slug=args.org_slug,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )


def _safe_state(client: TargonClient, uid: str) -> dict[str, object]:
    state = client.state(uid)
    return {
        "uid": state.get("uid"),
        "status": state.get("status"),
        "message": state.get("message"),
        "ready_replicas": state.get("ready_replicas"),
        "total_replicas": state.get("total_replicas"),
        "updated_at": state.get("updated_at"),
    }


def _safe_logs(client: TargonClient, uid: str, *, tail: int) -> str:
    try:
        return client.logs(uid, tail=tail)
    except TargonAPIError:
        return "provider logs unavailable"


def _require_workload_uid(uid: str) -> str:
    if _WORKLOAD_UID.fullmatch(uid) is None:
        raise SystemExit("error: workload uid must match wrk-[a-z0-9]+")
    return uid


def _redact_provider_text(text: str) -> str:
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|token|secret|password)\b\s*[=:]\s*)\S+",
        r"\1[redacted]",
        text,
    )
    return re.sub(r"\bsvt_[A-Za-z0-9]+\b", "[redacted-key]", text)


def command_state(args: argparse.Namespace) -> int:
    uid = _require_workload_uid(args.uid)
    state = _safe_state(_client(args, authenticated=True), uid)
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def command_logs(args: argparse.Namespace) -> int:
    uid = _require_workload_uid(args.uid)
    if args.tail < 1 or args.tail > _MAX_LOG_TAIL:
        raise SystemExit("error: --tail must be an integer from 1 through 10000")
    client = _client(args, authenticated=True)
    if args.include_state:
        print(json.dumps(_safe_state(client, uid), indent=2, sort_keys=True))
        print("---LOGS---")
    try:
        logs = client.logs(uid, tail=args.tail)
    except TargonAPIError as error:
        if error.status == 404:
            raise TargonAPIError(
                operation=error.operation,
                status=404,
                reason="logs unavailable after replica left running",
            ) from None
        raise
    sys.stdout.write(_redact_provider_text(logs))
    return 0


def _cleanup_probe_workload(client: TargonClient, uid: str) -> dict[str, object]:
    """Delete first without letting provider cleanup hide the probe result."""
    if delete_oneshot_rental(client, uid):
        return {"phase": "deleted", "uid": uid, "deleted": True, "suspended": False}
    try:
        state = _safe_state(client, uid)
    except TargonAPIError:
        state = {}
    status = str(state.get("status", "")).casefold()
    return {
        "phase": "cleanup-required",
        "uid": uid,
        "deleted": False,
        "suspended": status == "suspended",
        "status": status or None,
    }


def command_sweep_oneshots(args: argparse.Namespace) -> int:
    result = sweep_oneshot_rentals(
        _client(args, authenticated=True),
        dry_run=not args.apply,
        registered_grace_seconds=args.registered_grace_seconds,
        max_workers=1 if not args.apply else max(1, args.workers),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if int(result["leftover"]) > 0 else 0


def _wait_until_ready(
    client: TargonClient,
    uid: str,
    timeout_seconds: float,
    *,
    require_ready_replica: bool = True,
) -> dict[str, object]:
    """Wait for a real ready replica; Targon may report running before then."""
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _safe_state(client, uid)
        if last.get("status") == "error":
            return last
        if last.get("status") == "running":
            ready = last.get("ready_replicas")
            if not require_ready_replica or (isinstance(ready, int) and ready >= 1):
                return last
        time.sleep(5)
    raise RuntimeError(
        "workload did not become ready; "
        f"last status={last.get('status')} ready_replicas={last.get('ready_replicas')}"
    )


def command_inventory(args: argparse.Namespace) -> int:
    rows = _client(args, authenticated=False).inventory()
    safe = [
        {
            "name": row.get("name"),
            "spec": row.get("spec"),
            "cost_per_hour": row.get("cost_per_hour"),
            "available": row.get("available"),
        }
        for row in rows
    ]
    print(json.dumps(safe, indent=2, sort_keys=True))
    return 0


def command_list(args: argparse.Namespace) -> int:
    client = _client(args, authenticated=True)
    rows = [asdict(workload_summary(row)) for row in client.list_workloads()]
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def _safe_probe_output(value: str, *, limit: int = 8000) -> str:
    """Bound benign probe output without ever inspecting workload env state."""
    text = value.strip()
    if len(text) <= limit:
        return text
    head = max(1024, limit // 4)
    marker = "\n...[truncated]...\n"
    tail = max(1024, limit - head - len(marker))
    return text[:head] + marker + text[-tail:]


def command_kaniko_probe(args: argparse.Namespace) -> int:
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 1:
        raise RuntimeError(f"{args.resource} is not currently available")

    starter_kit_sha = getattr(args, "starter_kit_sha", None)
    screen_contract = bool(getattr(args, "screen_contract", False))
    if starter_kit_sha:
        if args.roundtrip:
            raise SystemExit("starter-kit Kaniko probe cannot use --roundtrip")
        agent_id = str(uuid4())
        attempt_id = str(uuid4())
        script = starter_kit_rental_script(
            source_sha=starter_kit_sha,
            agent_id=agent_id,
            attempt_id=attempt_id,
        )
        success_marker = "KANIKO_STARTER_PROBE_AVAILABLE"
        probe_kind = "starter-kit"
    elif screen_contract:
        if args.roundtrip:
            raise SystemExit("screen-contract Kaniko probe cannot use --roundtrip")
        agent_id = str(uuid4())
        attempt_id = str(uuid4())
        script = busybox_contract_rental_script(
            agent_id=agent_id, attempt_id=attempt_id
        )
        success_marker = "KANIKO_PROBE_AVAILABLE"
        probe_kind = "screen-contract"
    else:
        success_marker = "KANIKO_PROBE_AVAILABLE"
        probe_kind = "busybox"
        agent_id = ""
        attempt_id = ""

    destination = (
        f"ttl.sh/ditto-targon-kaniko-{secrets.token_hex(8)}:30m"
        if args.roundtrip
        else None
    )
    output_args = (
        f"--destination={destination} --digest-file=/workspace/digest"
        if destination is not None
        else "--no-push --tar-path=/workspace/ditto-kaniko-probe.tar"
    )
    output_check = (
        "test -s /workspace/digest"
        if destination is not None
        else "test -s /workspace/ditto-kaniko-probe.tar"
    )
    image_command = (
        '\'CMD ["sh","-c","cat /probe.txt; sleep 600"]\''
        if destination is not None
        else '\'CMD ["cat","/probe.txt"]\''
    )
    if not starter_kit_sha and not screen_contract:
        script = (
            "set -eu; "
            "mkdir -p /workspace; "
            "printf '%s\\n' 'FROM busybox:1.37.0' "
            "\"RUN printf 'targon-kaniko-ok\\n' >/probe.txt\" "
            f"{image_command} >/workspace/Dockerfile; "
            "/kaniko/executor --context=dir:///workspace "
            f"--dockerfile=/workspace/Dockerfile {output_args} --verbosity=info; "
            f"{output_check}; "
            "echo KANIKO_PROBE_AVAILABLE; "
            "sleep 600"
        )
    name = f"ditto-kaniko-probe-{secrets.token_hex(3)}"
    uid: str | None = None
    runtime_uid: str | None = None
    try:
        created = client.create_rental(
            name=name,
            image=args.image,
            resource_name=args.resource,
            commands=["/busybox/sh", "-c"],
            args=[script],
        )
        uid = str(created["uid"])
        print(
            json.dumps(
                {
                    "phase": "registered",
                    "uid": uid,
                    "name": name,
                    "probe": probe_kind,
                    "agent_id": agent_id or None,
                    "attempt_id": attempt_id or None,
                }
            )
        )
        client.deploy(uid)

        deadline = time.monotonic() + args.provision_timeout_seconds
        last_state: dict[str, object] = {}
        logs = ""
        log_tail = 2000 if (starter_kit_sha or screen_contract) else 200
        while time.monotonic() < deadline:
            last_state = _safe_state(client, uid)
            try:
                logs = client.logs(uid, tail=log_tail)
            except TargonAPIError:
                logs = ""
            if success_marker in logs:
                print(json.dumps({"phase": "deployed", **last_state}, sort_keys=True))
                result: dict[str, object] = {
                    "builder": "kaniko",
                    "probe": probe_kind,
                    "capability": "AVAILABLE",
                    "probe_logs": _safe_probe_output(logs, limit=24000),
                }
                if starter_kit_sha or screen_contract:
                    try:
                        contract = parse_starter_kit_probe_logs(logs)
                    except (ValueError, json.JSONDecodeError) as error:
                        result["screen_contract"] = {
                            "ok": False,
                            "error": str(error),
                        }
                        print(json.dumps(result))
                        return 8
                    result["screen_contract"] = contract
                    if not contract["ok"]:
                        print(json.dumps(result))
                        return 8
                print(json.dumps(result))
                break
            if "KANIKO_STARTER_PROBE_FAILED" in logs:
                print(json.dumps({"phase": "deployed", **last_state}, sort_keys=True))
                print(
                    json.dumps(
                        {
                            "builder": "kaniko",
                            "probe": probe_kind,
                            "capability": "UNAVAILABLE",
                            "probe_logs": _safe_probe_output(logs, limit=24000),
                        }
                    )
                )
                return 6
            if last_state.get("status") == "error":
                # Targon restarts PID 1 on crash; keep polling so a late
                # success/failure marker in the previous instance's logs can
                # still be observed before the restart wipes them.
                time.sleep(2)
                continue
            time.sleep(2)

        if success_marker not in logs:
            print(json.dumps({"phase": "deployed", **last_state}, sort_keys=True))
            print(
                json.dumps(
                    {
                        "builder": "kaniko",
                        "probe": probe_kind,
                        "capability": "UNAVAILABLE",
                        "probe_logs": _safe_probe_output(logs, limit=24000),
                    }
                )
            )
            return 6

        if destination is None or starter_kit_sha or screen_contract:
            return 0

        runtime_name = f"ditto-kaniko-runtime-{secrets.token_hex(3)}"
        runtime = client.create_rental(
            name=runtime_name,
            image=destination,
            resource_name=args.resource,
        )
        runtime_uid = str(runtime["uid"])
        print(
            json.dumps(
                {
                    "phase": "runtime-registered",
                    "uid": runtime_uid,
                    "name": runtime_name,
                    "ephemeral_image": destination,
                }
            )
        )
        client.deploy(runtime_uid)
        runtime_deadline = time.monotonic() + args.provision_timeout_seconds
        runtime_state: dict[str, object] = {}
        runtime_logs = ""
        while time.monotonic() < runtime_deadline:
            runtime_state = _safe_state(client, runtime_uid)
            runtime_logs = _safe_logs(client, runtime_uid, tail=80)
            if (
                "targon-kaniko-ok" in runtime_logs
                or runtime_state.get("status") == "error"
            ):
                break
            time.sleep(5)
        available = "targon-kaniko-ok" in runtime_logs
        print(
            json.dumps(
                {
                    "phase": "runtime-executed",
                    **runtime_state,
                    "capability": "AVAILABLE" if available else "INCONCLUSIVE",
                    "runtime_logs": _safe_probe_output(runtime_logs),
                },
                sort_keys=True,
            )
        )
        return 0 if available else 7
    finally:
        if not args.keep:
            for cleanup_uid in (runtime_uid, uid):
                if cleanup_uid is None:
                    continue
                print(
                    json.dumps(
                        _cleanup_probe_workload(client, cleanup_uid), sort_keys=True
                    )
                )


def command_agent_probe(args: argparse.Namespace) -> int:
    """Prove ordinary Targon Rentals can host noninteractive coding agents."""
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 1:
        raise RuntimeError(f"{args.resource} is not currently available")

    script = (
        "set -eu; "
        f"npm install -g opencode-ai@{args.opencode_version} "
        f"@mariozechner/pi-coding-agent@{args.pi_version} >/tmp/npm-install.log 2>&1; "
        "printf 'opencode='; opencode --version; "
        "printf 'pi='; pi --version; "
        "echo AGENT_RUNTIME_AVAILABLE; "
        "sleep 600"
    )
    name = f"ditto-agent-probe-{secrets.token_hex(3)}"
    uid: str | None = None
    try:
        created = client.create_rental(
            name=name,
            image=args.image,
            resource_name=args.resource,
            commands=["/bin/sh", "-c"],
            args=[script],
        )
        uid = str(created["uid"])
        print(json.dumps({"phase": "registered", "uid": uid, "name": name}))
        client.deploy(uid)
        deadline = time.monotonic() + args.provision_timeout_seconds
        last_state: dict[str, object] = {}
        logs = ""
        while time.monotonic() < deadline:
            last_state = _safe_state(client, uid)
            logs = _safe_logs(client, uid, tail=80)
            if "AGENT_RUNTIME_AVAILABLE" in logs or last_state.get("status") == "error":
                break
            time.sleep(5)
        available = "AGENT_RUNTIME_AVAILABLE" in logs
        print(
            json.dumps(
                {
                    "phase": "agent-runtime",
                    **last_state,
                    "capability": "AVAILABLE" if available else "UNAVAILABLE",
                    "probe_logs": _safe_probe_output(logs),
                },
                sort_keys=True,
            )
        )
        return 0 if available else 6
    finally:
        if uid is not None and not args.keep:
            print(json.dumps(_cleanup_probe_workload(client, uid), sort_keys=True))


def command_runtime_probe(args: argparse.Namespace) -> int:
    """Prove a Rental can expose a direct-image HTTP health contract."""
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 1:
        raise RuntimeError(f"{args.resource} is not currently available")
    uid: str | None = None
    try:
        created = client.create_rental(
            name=f"ditto-runtime-probe-{secrets.token_hex(3)}",
            image=args.image,
            resource_name=args.resource,
            commands=["/bin/sh", "-c"],
            args=[
                "mkdir -p /tmp/site; printf ok >/tmp/site/health; "
                "cd /tmp/site; exec python -m http.server 8080"
            ],
            ports=[{"port": 8080, "protocol": "TCP", "routing": "PROXIED"}],
        )
        uid = str(created["uid"])
        print(json.dumps({"phase": "registered", "uid": uid}))
        client.deploy(uid)
        deadline = time.monotonic() + args.provision_timeout_seconds
        state: dict[str, object] = {}
        health_url: str | None = None
        while time.monotonic() < deadline:
            state = client.state(uid)
            urls = state.get("urls")
            if isinstance(urls, list):
                for row in urls:
                    if isinstance(row, dict) and row.get("port") == 8080:
                        base = str(row.get("url", "")).rstrip("/")
                        if base.startswith("https://"):
                            health_url = f"{base}/health"
            if health_url is not None:
                try:
                    with urllib.request.urlopen(health_url, timeout=5) as response:
                        if response.status == 200 and response.read(16) == b"ok":
                            print(
                                json.dumps(
                                    {
                                        "phase": "runtime-health",
                                        "status": state.get("status"),
                                        "ready_replicas": state.get("ready_replicas"),
                                        "capability": "AVAILABLE",
                                    },
                                    sort_keys=True,
                                )
                            )
                            return 0
                except (OSError, urllib.error.URLError):
                    pass
            if state.get("status") == "error":
                break
            time.sleep(5)
        print(
            json.dumps(
                {
                    "phase": "runtime-health",
                    "status": state.get("status"),
                    "capability": "UNAVAILABLE",
                },
                sort_keys=True,
            )
        )
        return 6
    finally:
        if uid is not None and not args.keep:
            print(json.dumps(_cleanup_probe_workload(client, uid), sort_keys=True))


def _source_review_probe_archive() -> bytes:
    """Build a tiny benign source archive without writing it to disk."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = (
            b"def answer(message: str) -> str:\n    return f'You said: {message}'\n"
        )
        member = tarfile.TarInfo("agent.py")
        member.size = len(content)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _source_review_mock_script(
    *, review_id: str, job_token: str, artifact: bytes
) -> str:
    """Return a deterministic HTTPS-proxied Platform/OpenRouter mock."""
    values = {
        "artifact_b64": base64.b64encode(artifact).decode(),
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "job_token": job_token,
        "review_id": review_id,
    }
    encoded_values = base64.b64encode(json.dumps(values).encode()).decode()
    return f"""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALUES = json.loads(base64.b64decode({encoded_values!r}))
ARTIFACT = base64.b64decode(VALUES["artifact_b64"])
BASE = "/api/v1/screener/submission-source-reviews/" + VALUES["review_id"]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def send_json(self, status, body):
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        return self.headers.get("Authorization") == "Bearer " + VALUES["job_token"]

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {{"ok": True}})
            return
        if self.path == "/artifact":
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(ARTIFACT)))
            self.end_headers()
            self.wfile.write(ARTIFACT)
            return
        if self.path == BASE + "/source" and self.authorized():
            source_url = "https://" + self.headers["Host"] + "/artifact"
            self.send_json(200, {{
                "artifact_sha256": VALUES["artifact_sha256"],
                "source_url_b64": base64.b64encode(source_url.encode()).decode(),
            }})
            return
        self.send_json(404, {{"error": "not-found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{{}}")
        if self.path == "/chat/completions":
            delivered = sum(
                1 for message in body.get("messages", [])
                if message.get("role") == "tool"
            )
            if delivered == 0:
                name, arguments = "list_files", {{"prefix": ""}}
            elif delivered == 1:
                name, arguments = "read_file", {{
                    "path": "agent.py", "start_line": 1, "end_line": 20
                }}
            else:
                name, arguments = "submit_review", {{
                    "risk_level": "low",
                    "confidence": 0.99,
                    "categories": ["none"],
                    "evidence": [],
                    "summary": "The bounded benign handler echoes its visible input.",
                }}
            self.send_json(200, {{"choices": [{{"message": {{
                "role": "assistant",
                "content": "",
                "tool_calls": [{{
                    "id": "probe-" + str(delivered),
                    "type": "function",
                    "function": {{
                        "name": name,
                        "arguments": json.dumps(arguments, separators=(",", ":")),
                    }},
                }}],
            }}}}]}})
            return
        if self.path == BASE + "/complete" and self.authorized():
            observation = body.get("observation", {{}})
            safe = {{
                "ok": observation.get("ok"),
                "risk_level": observation.get("risk_level"),
                "categories": observation.get("categories"),
                "clearance_certified": observation.get("clearance_certified"),
            }}
            expected = {{
                "ok": True,
                "risk_level": "low",
                "categories": ["none"],
                "clearance_certified": True,
            }}
            if safe != expected:
                self.send_json(422, {{"error": "unexpected-observation"}})
                return
            self.send_json(200, {{"verified": True}})
            print(
                "SOURCE_REVIEW_COMPLETE=" + json.dumps(safe, sort_keys=True),
                flush=True,
            )
            return
        self.send_json(404, {{"error": "not-found"}})

ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
""".strip()


def _source_review_starter_kit_mock_script(
    *, review_id: str, job_token: str, archive_url: str
) -> str:
    """Platform mock that fetches a public miner tarball and records L1 complete."""
    values = {
        "archive_url": archive_url,
        "job_token": job_token,
        "review_id": review_id,
    }
    encoded_values = base64.b64encode(json.dumps(values).encode()).decode()
    return f"""
import base64, hashlib, io, json, tarfile, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALUES = json.loads(base64.b64decode({encoded_values!r}))
BASE = "/api/v1/screener/submission-source-reviews/" + VALUES["review_id"]
raw = urllib.request.urlopen(VALUES["archive_url"], timeout=120).read()
src = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as dst:
    for member in src.getmembers():
        if not member.isfile():
            continue
        parts = member.name.split("/", 1)
        if len(parts) < 2:
            continue
        rel = parts[1]
        if any(part in (".git", "target") for part in rel.split("/")):
            continue
        info = tarfile.TarInfo(rel)
        info.size = member.size
        info.mode = 0o644
        handle = src.extractfile(member)
        if handle is None:
            continue
        dst.addfile(info, handle)
ARTIFACT = buf.getvalue()
SHA = hashlib.sha256(ARTIFACT).hexdigest()
print("SOURCE_REVIEW_ARTIFACT_SHA256=" + SHA, flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def send_json(self, status, body):
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        return self.headers.get("Authorization") == "Bearer " + VALUES["job_token"]

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {{"ok": True, "artifact_sha256": SHA}})
            return
        if self.path == "/artifact":
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(ARTIFACT)))
            self.end_headers()
            self.wfile.write(ARTIFACT)
            return
        if self.path == BASE + "/source" and self.authorized():
            source_url = "https://" + self.headers["Host"] + "/artifact"
            self.send_json(200, {{
                "artifact_sha256": SHA,
                "source_url_b64": base64.b64encode(source_url.encode()).decode(),
            }})
            return
        self.send_json(404, {{"error": "not-found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{{}}")
        if self.path == BASE + "/complete" and self.authorized():
            observation = body.get("observation", {{}})
            safe = {{
                "ok": observation.get("ok"),
                "risk_level": observation.get("risk_level"),
                "categories": observation.get("categories"),
                "clearance_certified": observation.get("clearance_certified"),
                "error_code": observation.get("error_code"),
                "finding_digest": observation.get("finding_digest"),
            }}
            self.send_json(200, {{"verified": True}})
            print(
                "SOURCE_REVIEW_COMPLETE=" + json.dumps(safe, sort_keys=True),
                flush=True,
            )
            return
        self.send_json(404, {{"error": "not-found"}})

ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
""".strip()


def _load_source_review_api_key() -> str:
    """Read the OpenRouter key from Secret Manager without printing it."""
    raw = subprocess.check_output(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--project={_SOURCE_REVIEW_PROJECT}",
            f"--secret={_SOURCE_REVIEW_SECRET}",
        ],
        stderr=subprocess.DEVNULL,
    )
    key = raw.decode().strip()
    if len(key) < 16:
        raise RuntimeError("source-review secret was empty")
    return key


def command_source_review_probe(args: argparse.Namespace) -> int:
    """Run the exact one-shot source-review code against deterministic mocks."""
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 2:
        raise RuntimeError(f"{args.resource} needs two currently available slots")

    review_id = str(uuid4())
    attempt_id = str(uuid4())
    job_token = secrets.token_urlsafe(48)
    starter_kit = bool(getattr(args, "starter_kit", False))
    live_model = bool(getattr(args, "live_model", False))
    if live_model and not starter_kit:
        raise SystemExit("--live-model requires --starter-kit")
    artifact = b"" if starter_kit else _source_review_probe_archive()
    artifact_sha256 = "" if starter_kit else hashlib.sha256(artifact).hexdigest()
    mock_script = (
        _source_review_starter_kit_mock_script(
            review_id=review_id,
            job_token=job_token,
            archive_url=_STARTER_KIT_ARCHIVE_URL,
        )
        if starter_kit
        else _source_review_mock_script(
            review_id=review_id,
            job_token=job_token,
            artifact=artifact,
        )
    )
    mock_uid: str | None = None
    review_uid: str | None = None
    try:
        mock = client.create_rental(
            name=f"ditto-source-mock-{secrets.token_hex(3)}",
            image="python:3.12-alpine",
            resource_name=args.resource,
            commands=["python", "-c"],
            args=[mock_script],
            ports=[{"port": 8080, "protocol": "TCP", "routing": "PROXIED"}],
        )
        mock_uid = str(mock["uid"])
        print(json.dumps({"phase": "mock-registered", "uid": mock_uid}))
        client.deploy(mock_uid)

        platform_url: str | None = None
        deadline = time.monotonic() + args.provision_timeout_seconds
        while time.monotonic() < deadline:
            state = client.state(mock_uid)
            urls = state.get("urls")
            if isinstance(urls, list):
                for row in urls:
                    if isinstance(row, dict) and row.get("port") == 8080:
                        candidate = str(row.get("url", "")).rstrip("/")
                        if candidate.startswith("https://"):
                            try:
                                with urllib.request.urlopen(
                                    f"{candidate}/health", timeout=5
                                ) as response:
                                    if response.status == 200:
                                        platform_url = candidate
                                        break
                            except (OSError, urllib.error.URLError):
                                pass
            if platform_url is not None:
                break
            if state.get("status") == "error":
                break
            time.sleep(5)
        if platform_url is None:
            print(json.dumps({"phase": "source-review", "capability": "UNAVAILABLE"}))
            return 6

        if starter_kit:
            try:
                with urllib.request.urlopen(
                    f"{platform_url}/health", timeout=10
                ) as response:
                    health = json.loads(response.read().decode())
            except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError):
                health = {}
            artifact_sha256 = str(health.get("artifact_sha256") or "")
            if len(artifact_sha256) != 64:
                print(
                    json.dumps(
                        {
                            "phase": "source-review",
                            "capability": "UNAVAILABLE",
                            "reason": "starter-kit mock missing artifact digest",
                        }
                    )
                )
                return 6

        model_key = (
            _load_source_review_api_key()
            if live_model
            else "probe-openrouter-key-not-a-secret"
        )
        review_env = [
            {"name": "DITTO_PLATFORM_URL", "value": platform_url},
            {"name": "DITTO_SOURCE_REVIEW_ID", "value": review_id},
            {"name": "DITTO_SOURCE_REVIEW_ATTEMPT_ID", "value": attempt_id},
            {"name": "DITTO_SOURCE_REVIEW_JOB", "value": "1"},
            {
                "name": "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256",
                "value": artifact_sha256,
            },
            {"name": "DITTO_SOURCE_REVIEW_JOB_TOKEN", "value": job_token},
            {
                "name": "SCREENER_NODE_CREDENTIAL_FILE",
                "value": "/tmp/ditto-source-review/node.json",
            },
            {
                "name": "SCREENER_SOURCE_REVIEW_API_KEY",
                "value": model_key,
            },
            {
                "name": "SCREENER_SOURCE_REVIEW_BASE_URL",
                "value": (
                    "https://openrouter.ai/api/v1" if live_model else platform_url
                ),
            },
            {
                "name": "SCREENER_SOURCE_REVIEW_MODEL",
                "value": "openai/gpt-5.6-luna" if live_model else "probe-model",
            },
            {
                "name": "SCREENER_SOURCE_REVIEW_MAX_STEPS",
                "value": "40" if live_model else "4",
            },
            {
                "name": "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS",
                "value": str(args.review_timeout_seconds),
            },
            {"name": "SCREENER_STATIC_PREFLIGHT_V2_MODE", "value": "off"},
        ]
        review = client.create_rental(
            name=f"ditto-source-probe-{secrets.token_hex(3)}",
            image=args.image,
            resource_name=args.resource,
            envs=review_env,
            commands=["/app/workers/screener/.venv/bin/python", "-m"],
            args=["ditto_screener.source_review_job"],
        )
        review_uid = str(review["uid"])
        print(json.dumps({"phase": "review-registered", "uid": review_uid}))
        client.deploy(review_uid)

        deadline = time.monotonic() + args.provision_timeout_seconds + (
            float(args.review_timeout_seconds) if live_model else 0
        )
        while time.monotonic() < deadline:
            mock_logs = _safe_logs(client, mock_uid, tail=80)
            marker = next(
                (
                    line.removeprefix("SOURCE_REVIEW_COMPLETE=")
                    for line in mock_logs.splitlines()
                    if line.startswith("SOURCE_REVIEW_COMPLETE=")
                ),
                None,
            )
            if marker is not None:
                # Match the controller's post-completion grace so the worker
                # receives the HTTP response and reaches its persistent hold
                # before the DELETE-first cleanup.
                time.sleep(5)
                print(
                    json.dumps(
                        {
                            "phase": "source-review",
                            "capability": "AVAILABLE",
                            "observation": json.loads(marker),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            review_state = _safe_state(client, review_uid)
            if review_state.get("status") == "error":
                print(
                    json.dumps(
                        {
                            "phase": "source-review",
                            "capability": "UNAVAILABLE",
                            "review_logs": _safe_probe_output(
                                _safe_logs(client, review_uid, tail=120)
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 7
            time.sleep(5)
        print(json.dumps({"phase": "source-review", "capability": "UNAVAILABLE"}))
        return 8
    finally:
        if not args.keep:
            for cleanup_uid in (review_uid, mock_uid):
                if cleanup_uid is not None:
                    print(
                        json.dumps(
                            _cleanup_probe_workload(client, cleanup_uid), sort_keys=True
                        )
                    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://api.targon.com/tha/v3")
    parser.add_argument("--org-slug", default=os.environ.get("TARGON_ORG_SLUG"))
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--api-key-stdin", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory")
    inventory.set_defaults(handler=command_inventory)

    list_command = subparsers.add_parser("list")
    list_command.set_defaults(handler=command_list)

    logs_command = subparsers.add_parser("logs")
    logs_command.add_argument("uid")
    logs_command.add_argument("--tail", type=int, default=400)
    logs_command.add_argument("--include-state", action="store_true")
    logs_command.set_defaults(handler=command_logs)

    state_command = subparsers.add_parser("state")
    state_command.add_argument("uid")
    state_command.set_defaults(handler=command_state)

    sweep = subparsers.add_parser("sweep-oneshots")
    sweep.add_argument(
        "--apply",
        action="store_true",
        help="Delete matching leftover rentals. Default is a dry run.",
    )
    sweep.add_argument(
        "--registered-grace-seconds",
        type=int,
        default=DEFAULT_REGISTERED_GRACE_SECONDS,
    )
    sweep.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel DELETE workers used with --apply.",
    )
    sweep.set_defaults(handler=command_sweep_oneshots)

    kaniko = subparsers.add_parser("kaniko-probe")
    kaniko.add_argument("--resource", default="cpu-small")
    kaniko.add_argument(
        "--image",
        default=(
            "us-central1-docker.pkg.dev/ditto-app-dev/"
            "ditto-public-builders/kaniko-executor:v1.25.16"
        ),
    )
    kaniko.add_argument("--provision-timeout-seconds", type=float, default=600)
    kaniko.add_argument("--roundtrip", action="store_true")
    kaniko.add_argument(
        "--starter-kit-sha",
        default=None,
        help="40-char SHA of ditto-assistant/dittobench-starter-kit",
    )
    kaniko.add_argument(
        "--screen-contract",
        action="store_true",
        help="Live busybox build with production destination and tar inspect",
    )
    kaniko.add_argument("--keep", action="store_true")
    kaniko.set_defaults(handler=command_kaniko_probe)
    agent = subparsers.add_parser("agent-probe")
    agent.add_argument("--resource", default="cpu-small")
    agent.add_argument("--image", default="node:22-bookworm-slim")
    agent.add_argument("--opencode-version", default="1.18.18")
    agent.add_argument("--pi-version", default="0.73.1")
    agent.add_argument("--provision-timeout-seconds", type=float, default=600)
    agent.add_argument("--keep", action="store_true")
    agent.set_defaults(handler=command_agent_probe)
    runtime = subparsers.add_parser("runtime-probe")
    runtime.add_argument("--resource", default="cpu-small")
    runtime.add_argument("--image", default="python:3.12-alpine")
    runtime.add_argument("--provision-timeout-seconds", type=float, default=600)
    runtime.add_argument("--keep", action="store_true")
    runtime.set_defaults(handler=command_runtime_probe)
    source_review = subparsers.add_parser("source-review-probe")
    source_review.add_argument("--resource", default="cpu-small")
    source_review.add_argument("--image", required=True)
    source_review.add_argument("--provision-timeout-seconds", type=float, default=600)
    source_review.add_argument("--review-timeout-seconds", type=float, default=90)
    source_review.add_argument(
        "--starter-kit",
        action="store_true",
        help="Serve dittobench-starter-kit as the extracted source archive",
    )
    source_review.add_argument(
        "--live-model",
        action="store_true",
        help="Call real OpenRouter L1 instead of the scripted mock",
    )
    source_review.add_argument("--keep", action="store_true")
    source_review.set_defaults(handler=command_source_review_probe)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (TargonAPIError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
