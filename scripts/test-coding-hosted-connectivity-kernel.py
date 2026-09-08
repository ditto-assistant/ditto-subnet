#!/usr/bin/env python3
"""Root-only synthetic kernel test; refuses the host network namespace."""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = (
    ROOT / "infra/ansible/roles/coding_hosted_connectivity/files/connectivity-policy.py"
)
TEST_UID = 60001


def load(path):
    spec = importlib.util.spec_from_file_location("kernel_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def nft(body):
    subprocess.run(["/usr/sbin/nft", "--file", "-"], input=body.encode(), check=True)


def echo(address, port, *, udp=False):
    listener = socket.socket(
        socket.AF_INET6 if ":" in address else socket.AF_INET,
        socket.SOCK_DGRAM if udp else socket.SOCK_STREAM,
    )
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((address, port))
    if not udp:
        listener.listen()

    pid = os.fork()
    if pid == 0:
        while True:
            if udp:
                body, peer = listener.recvfrom(32)
                listener.sendto(body, peer)
                continue
            try:
                connection, _ = listener.accept()
            except OSError:
                os._exit(0)
            with connection:
                connection.settimeout(10)
                try:
                    while body := connection.recv(32):
                        connection.sendall(body)
                except OSError:
                    pass
    listener.close()
    return pid


def isolated(cgroup, function):
    """Move only this forked test child, then drop every identity/group privilege."""
    pid = os.fork()
    if pid == 0:
        try:
            if cgroup is not None:
                (cgroup / "cgroup.procs").write_text(str(os.getpid()))
            os.setgroups([])
            os.setgid(TEST_UID)
            os.setuid(TEST_UID)
            signal.alarm(12)
            function()
        except BaseException as error:
            print(f"synthetic child failure: {error}", file=sys.stderr, flush=True)
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "synthetic child check failed"


def reaches(address, port, allowed):
    try:
        with socket.create_connection((address, port), timeout=1) as connection:
            connection.sendall(b"synthetic")
            actual = connection.recv(32) == b"synthetic"
    except OSError:
        actual = False
    assert actual is allowed, f"TCP {address}:{port} expected allowed={allowed}"


def reaches_dns(allowed):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(1)
            client.connect(("127.0.0.53", 53))
            client.send(b"synthetic")
            actual = client.recv(32) == b"synthetic"
    except OSError:
        actual = False
    assert actual is allowed


def reply(cgroup, address, port):
    client = os.fork()
    if client == 0:
        try:
            signal.alarm(6)
            for _ in range(100):
                try:
                    connection = socket.create_connection((address, port), timeout=1)
                    break
                except OSError:
                    time.sleep(0.02)
            else:
                os._exit(1)
            with connection:
                connection.sendall(b"synthetic")
                assert connection.recv(32) == b"synthetic"
        except BaseException:
            os._exit(1)
        os._exit(0)

    def serve_once():
        with socket.socket() as listener:
            listener.settimeout(3)
            listener.bind((address, port))
            listener.listen()
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                connection.sendall(connection.recv(32))

    try:
        isolated(cgroup, serve_once)
    finally:
        _, status = os.waitpid(client, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "scoped reply failed"


def main():
    assert os.geteuid() == 0, "requires isolated test root"
    assert os.readlink("/proc/self/ns/net") != os.readlink("/proc/1/ns/net"), (
        "host network namespace forbidden"
    )
    assert Path("/sys/fs/cgroup/cgroup.controllers").is_file(), "requires cgroup v2"
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    addresses = ["10.20.0.7", "10.30.0.4", "1.1.1.1", "169.254.169.254", "203.0.113.7"]
    for address in addresses:
        subprocess.run(
            ["ip", "address", "add", address + "/32", "dev", "lo"], check=True
        )
    policy = load(POLICY_PATH)
    host = load(ROOT / "infra/ansible/roles/coding_hosted/files/host-policy.py")
    parent = Path("/sys/fs/cgroup") / ("ditto-connectivity-test-" + uuid.uuid4().hex)
    worker = parent / "worker"
    daemon_parent = parent / "daemon"
    daemon = daemon_parent / "children"
    made = []
    listeners = []
    try:
        for path in (parent, worker, daemon_parent, daemon):
            path.mkdir()  # Fresh exact test-owned cgroups, never existing services.
            made.append(path)
        for address, port in [
            ("10.20.0.7", 5432),
            ("1.1.1.1", 443),
            ("10.30.0.4", 18080),
            ("10.30.0.4", 18087),
            ("169.254.169.254", 80),
            ("203.0.113.7", 443),
            ("127.0.0.1", 18081),
        ]:
            listeners.append(echo(address, port))
        listeners.append(echo("::1", 18081))
        listeners.append(echo("127.0.0.53", 53))
        listeners.append(echo("127.0.0.53", 53, udp=True))

        def compiled(seconds, rollout=False):
            now = int(time.time())
            profile = {
                "schema": "dittobench-coding-hosted-connectivity-v2",
                "shadow_only": True,
                "weight_eligible": False,
                "issued_at_unix": now,
                "expires_at_unix": now + seconds,
                "trusted_tcp": [
                    {"address": "10.20.0.7", "port": 5432},
                    {"address": "1.1.1.1", "port": 443},
                ],
                "trusted_dns": [{"address": "127.0.0.53", "port": 53}],
                "trusted_loopback_tcp": True,
                "candidate_tcp": [
                    {"address": "10.30.0.4", "port": 18080},
                    {"address": "10.30.0.4", "port": 18082},
                ],
            }
            if rollout:
                profile["schema"] = "dittobench-coding-hosted-connectivity-v3"
                profile["candidate_tcp"] = [
                    {"address": "10.30.0.4", "port": 18080 + i} for i in range(8)
                ]
            body = policy.policy(profile, TEST_UID, now)
            # Substitute only the test-owned cgroup names, retaining the exact
            # production ancestor depths, syntax and all policy predicates.
            body = body.replace(
                policy.WORKER, str(worker.relative_to("/sys/fs/cgroup"))
            )
            return body.replace(
                f"user.slice/user-{TEST_UID}.slice/user@{TEST_UID}.service",
                str(daemon.relative_to("/sys/fs/cgroup")),
            )

        nft(host.nft_policy(TEST_UID))
        isolated(worker, lambda: reaches("10.20.0.7", 5432, False))
        nft(compiled(120))
        for cgroup, backend, router, loopback in [
            (worker, True, False, True),
            (daemon, False, True, False),
            (None, False, False, False),
        ]:
            isolated(
                cgroup, lambda allowed=backend: reaches("10.20.0.7", 5432, allowed)
            )
            isolated(cgroup, lambda allowed=backend: reaches("1.1.1.1", 443, allowed))
            isolated(
                cgroup, lambda allowed=router: reaches("10.30.0.4", 18080, allowed)
            )
            isolated(
                cgroup, lambda allowed=loopback: reaches("127.0.0.1", 18081, allowed)
            )
            isolated(cgroup, lambda: reaches("169.254.169.254", 80, False))
            isolated(cgroup, lambda: reaches("203.0.113.7", 443, False))
            isolated(cgroup, lambda allowed=backend: reaches("127.0.0.53", 53, allowed))
            isolated(cgroup, lambda allowed=backend: reaches_dns(allowed))
            isolated(cgroup, lambda: reaches("::1", 18081, False))

        reply(worker, "10.30.0.4", 18082)
        reply(daemon, "127.0.0.1", 18083)

        # The larger endpoint set is explicit v3 authority, not a v2 relaxation.
        isolated(daemon, lambda: reaches("10.30.0.4", 18087, False))
        nft(compiled(120, rollout=True))
        isolated(daemon, lambda: reaches("10.30.0.4", 18087, True))
        isolated(None, lambda: reaches("10.30.0.4", 18087, False))
        isolated(daemon, lambda: reaches("169.254.169.254", 80, False))

        nft(compiled(3))

        def expires():
            with socket.create_connection(("10.20.0.7", 5432), timeout=1) as connection:
                connection.sendall(b"before")
                assert connection.recv(32) == b"before"
                time.sleep(4)
                try:
                    connection.sendall(b"after")
                    assert connection.recv(32) != b"after"
                except OSError:
                    pass

        isolated(worker, expires)
        nft(compiled(120))
        isolated(worker, lambda: reaches("10.20.0.7", 5432, True))
        nft(host.nft_policy(TEST_UID))
        isolated(worker, lambda: reaches("10.20.0.7", 5432, False))
        isolated(daemon, lambda: reaches("10.30.0.4", 18080, False))
        print(
            json.dumps(
                {
                    "schema": "coding-connectivity-kernel-test-v2",
                    "synthetic_only": True,
                    "passed": True,
                }
            )
        )
    finally:
        for pid in listeners:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        # No recursive deletion or cross-worker process killing. rmdir fails
        # closed if an unexpected process remains in one of our exact cgroups.
        for path in reversed(made):
            path.rmdir()


if __name__ == "__main__":
    main()
