"""Secret-safe operator smoke commands for Targon Rentals."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import asdict

from screener_capacity.targon import TargonAPIError, TargonClient, workload_summary


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


def command_rootless_probe(args: argparse.Namespace) -> int:
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 1:
        raise RuntimeError(f"{args.resource} is not currently available")

    name = f"ditto-rootless-probe-{secrets.token_hex(3)}"
    uid: str | None = None
    suspended = False
    try:
        created = client.create_rental(
            name=name,
            image=args.image,
            resource_name=args.resource,
        )
        uid = str(created["uid"])
        print(json.dumps({"phase": "registered", "uid": uid, "name": name}))
        client.deploy(uid)
        state = _wait_until_ready(
            client,
            uid,
            args.provision_timeout_seconds,
            require_ready_replica=False,
        )
        print(json.dumps({"phase": "deployed", **state}, sort_keys=True))
        if state.get("status") != "running":
            logs = client.logs(uid, tail=80)
            print(
                json.dumps(
                    {
                        "capability_gate": "NOGO",
                        "reason": "rootless Docker image did not reach running",
                        "probe_logs": logs[-8000:],
                    }
                )
            )
            return 2

        # Rental state can lead daemon readiness. Give the image entrypoint a
        # short, bounded window before probing its local control socket.
        time.sleep(10)

        output = client.exec(
            uid,
            [
                "docker",
                "info",
                "--format",
                "{{json .SecurityOptions}}",
            ],
        ).strip()
        print(json.dumps({"phase": "nested-docker", "docker_security": output}))
        if "rootless" not in output.casefold():
            print(
                json.dumps(
                    {
                        "capability_gate": "NOGO",
                        "reason": "nested Docker is not authoritatively rootless",
                    }
                )
            )
            return 3
        print(json.dumps({"capability_gate": "GO"}))
        return 0
    finally:
        if uid is not None and not args.keep:
            try:
                state = _safe_state(client, uid)
                if state.get("status") not in {"suspended", "deleted", "error"}:
                    client.suspend(uid)
                    suspended = True
            finally:
                client.delete(uid)
                print(
                    json.dumps(
                        {
                            "phase": "deleted",
                            "uid": uid,
                            "suspended_first": suspended,
                        }
                    )
                )


def _safe_probe_output(value: str, *, limit: int = 8000) -> str:
    """Bound benign probe output without ever inspecting workload env state."""
    return value.strip()[-limit:]


def _run_builder_probe(
    args: argparse.Namespace,
    *,
    backend: str,
) -> int:
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 1:
        raise RuntimeError(f"{args.resource} is not currently available")

    name = f"ditto-{backend}-probe-{secrets.token_hex(3)}"
    uid: str | None = None
    suspended = False
    try:
        created = client.create_rental(
            name=name,
            image=args.image,
            resource_name=args.resource,
        )
        uid = str(created["uid"])
        print(json.dumps({"phase": "registered", "uid": uid, "name": name}))
        client.deploy(uid)
        state = _wait_until_ready(
            client,
            uid,
            args.provision_timeout_seconds,
            require_ready_replica=False,
        )
        print(json.dumps({"phase": "deployed", **state}, sort_keys=True))
        if state.get("status") != "running":
            logs = client.logs(uid, tail=120)
            print(
                json.dumps(
                    {
                        "builder": backend,
                        "capability": "UNAVAILABLE",
                        "reason": "builder image did not reach running",
                        "probe_logs": _safe_probe_output(logs),
                    }
                )
            )
            return 2

        # Rental state can lead daemon readiness. Give the image entrypoint a
        # short, bounded window before probing its local control socket.
        time.sleep(10)

        capability_command = (
            "id; sed -n 's/^CapEff:/CapEff:/p' /proc/1/status; "
            "printf 'uid_map='; tr '\\n' ';' </proc/1/uid_map; echo; "
            "printf 'cgroup='; tr '\\n' ';' </proc/1/cgroup; echo; "
            "test -e /dev/fuse && echo dev_fuse=yes || echo dev_fuse=no; "
            "test -S /var/run/docker.sock && echo inherited_docker_socket=yes "
            "|| echo inherited_docker_socket=no"
        )
        capability = client.exec(
            uid,
            ["sh", "-c", capability_command],
        )
        print(
            json.dumps(
                {
                    "phase": "runtime-capabilities",
                    "output": _safe_probe_output(capability),
                }
            )
        )

        if backend == "buildkit":
            status = client.exec(uid, ["buildctl", "debug", "workers"])
            prepare = (
                "rm -rf /tmp/ditto-builder-probe /tmp/ditto-builder-probe.oci; "
                "mkdir -p /tmp/ditto-builder-probe; "
                "printf '%s\\n' 'FROM busybox:1.37.0' "
                "\"RUN printf 'targon-buildkit-ok\\n' >/probe.txt\" "
                '\'CMD ["cat","/probe.txt"]\' '
                ">/tmp/ditto-builder-probe/Dockerfile"
            )
            client.exec(uid, ["sh", "-c", prepare])
            build = client.exec(
                uid,
                [
                    "buildctl",
                    "build",
                    "--progress=plain",
                    "--frontend=dockerfile.v0",
                    "--local",
                    "context=/tmp/ditto-builder-probe",
                    "--local",
                    "dockerfile=/tmp/ditto-builder-probe",
                    "--output",
                    "type=oci,dest=/tmp/ditto-builder-probe.oci",
                ],
            )
            verify_command = (
                "test -s /tmp/ditto-builder-probe.oci && "
                "echo oci_output=yes || echo oci_output=no"
            )
            verify = client.exec(uid, ["sh", "-c", verify_command])
        elif backend == "dind":
            status = client.exec(
                uid,
                [
                    "docker",
                    "info",
                    "--format",
                    "{{json .SecurityOptions}}",
                ],
            )
            prepare = (
                "rm -rf /tmp/ditto-builder-probe; "
                "mkdir -p /tmp/ditto-builder-probe; "
                "printf '%s\\n' 'FROM busybox:1.37.0' "
                "\"RUN printf 'targon-dind-ok\\n' >/probe.txt\" "
                '\'CMD ["cat","/probe.txt"]\' '
                ">/tmp/ditto-builder-probe/Dockerfile"
            )
            client.exec(uid, ["sh", "-c", prepare])
            build = client.exec(
                uid,
                [
                    "docker",
                    "build",
                    "--progress=plain",
                    "--tag",
                    "ditto-targon-probe:local",
                    "/tmp/ditto-builder-probe",
                ],
            )
            verify = client.exec(
                uid,
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "ditto-targon-probe:local",
                ],
            )
        else:  # pragma: no cover - only internal callers select the backend
            raise AssertionError(f"unknown builder backend: {backend}")

        print(
            json.dumps(
                {
                    "phase": "builder-status",
                    "builder": backend,
                    "output": _safe_probe_output(status),
                }
            )
        )
        print(
            json.dumps(
                {
                    "phase": "benign-build",
                    "builder": backend,
                    "output": _safe_probe_output(build),
                }
            )
        )
        marker = "oci_output=yes" if backend == "buildkit" else "targon-dind-ok"
        available = marker in verify
        print(
            json.dumps(
                {
                    "builder": backend,
                    "capability": "AVAILABLE" if available else "INCONCLUSIVE",
                    "verification": _safe_probe_output(verify),
                }
            )
        )
        return 0 if available else 4
    except TargonAPIError as error:
        logs = ""
        if uid is not None:
            try:
                logs = client.logs(uid, tail=160)
            except TargonAPIError:
                logs = "provider logs unavailable"
        print(
            json.dumps(
                {
                    "builder": backend,
                    "capability": "UNAVAILABLE",
                    "reason": str(error),
                    "probe_logs": _safe_probe_output(logs, limit=12000),
                }
            )
        )
        return 5
    finally:
        if uid is not None and not args.keep:
            try:
                state = _safe_state(client, uid)
                if state.get("status") not in {"suspended", "deleted", "error"}:
                    client.suspend(uid)
                    suspended = True
            finally:
                client.delete(uid)
                print(
                    json.dumps(
                        {
                            "phase": "deleted",
                            "uid": uid,
                            "suspended_first": suspended,
                        }
                    )
                )


def command_buildkit_probe(args: argparse.Namespace) -> int:
    return _run_builder_probe(args, backend="buildkit")


def command_dind_probe(args: argparse.Namespace) -> int:
    return _run_builder_probe(args, backend="dind")


def command_kaniko_probe(args: argparse.Namespace) -> int:
    client = _client(args, authenticated=True)
    inventory = {row.get("name"): row for row in client.inventory()}
    selected = inventory.get(args.resource)
    if not isinstance(selected, dict) or int(selected.get("available", 0)) < 1:
        raise RuntimeError(f"{args.resource} is not currently available")

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
        print(json.dumps({"phase": "registered", "uid": uid, "name": name}))
        client.deploy(uid)

        deadline = time.monotonic() + args.provision_timeout_seconds
        last_state: dict[str, object] = {}
        logs = ""
        while time.monotonic() < deadline:
            last_state = _safe_state(client, uid)
            try:
                logs = client.logs(uid, tail=200)
            except TargonAPIError:
                logs = ""
            if "KANIKO_PROBE_AVAILABLE" in logs:
                print(json.dumps({"phase": "deployed", **last_state}, sort_keys=True))
                print(
                    json.dumps(
                        {
                            "builder": "kaniko",
                            "capability": "AVAILABLE",
                            "probe_logs": _safe_probe_output(logs, limit=12000),
                        }
                    )
                )
                break
            if last_state.get("status") == "error":
                break
            time.sleep(5)

        if "KANIKO_PROBE_AVAILABLE" not in logs:
            print(json.dumps({"phase": "deployed", **last_state}, sort_keys=True))
            print(
                json.dumps(
                    {
                        "builder": "kaniko",
                        "capability": "UNAVAILABLE",
                        "probe_logs": _safe_probe_output(logs, limit=12000),
                    }
                )
            )
            return 6

        if destination is None:
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
        runtime_state = _wait_until_ready(
            client,
            runtime_uid,
            args.provision_timeout_seconds,
            require_ready_replica=False,
        )
        time.sleep(10)
        runtime_output = client.exec(runtime_uid, ["cat", "/probe.txt"])
        runtime_logs = client.logs(runtime_uid, tail=80)
        available = "targon-kaniko-ok" in runtime_output
        print(
            json.dumps(
                {
                    "phase": "runtime-executed",
                    **runtime_state,
                    "capability": "AVAILABLE" if available else "INCONCLUSIVE",
                    "output": _safe_probe_output(runtime_output),
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
                suspended = False
                try:
                    state = _safe_state(client, cleanup_uid)
                    if state.get("status") not in {"suspended", "deleted", "error"}:
                        client.suspend(cleanup_uid)
                        suspended = True
                finally:
                    client.delete(cleanup_uid)
                    print(
                        json.dumps(
                            {
                                "phase": "deleted",
                                "uid": cleanup_uid,
                                "suspended_first": suspended,
                            }
                        )
                    )


def _add_builder_probe(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    name: str,
    image: str,
    handler: object,
) -> None:
    probe = subparsers.add_parser(name)
    probe.add_argument("--resource", default="cpu-small")
    probe.add_argument("--image", default=image)
    probe.add_argument("--provision-timeout-seconds", type=float, default=600)
    probe.add_argument("--keep", action="store_true")
    probe.set_defaults(handler=handler)


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

    probe = subparsers.add_parser("rootless-probe")
    probe.add_argument("--resource", default="cpu-small")
    probe.add_argument("--image", default="docker:27.5-dind-rootless")
    probe.add_argument("--provision-timeout-seconds", type=float, default=600)
    probe.add_argument("--keep", action="store_true")
    probe.set_defaults(handler=command_rootless_probe)
    _add_builder_probe(
        subparsers,
        name="buildkit-probe",
        image="moby/buildkit:v0.29.0",
        handler=command_buildkit_probe,
    )
    _add_builder_probe(
        subparsers,
        name="dind-probe",
        image="docker:27.5-dind",
        handler=command_dind_probe,
    )
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
    kaniko.add_argument("--keep", action="store_true")
    kaniko.set_defaults(handler=command_kaniko_probe)
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
