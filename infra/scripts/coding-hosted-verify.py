#!/usr/bin/python3
"""Fixed read-only verifier for the native coding host.

Runs as root through the protected coding-hosted-operate workflow. It never opens
a private key, secret, or environment file: private paths are checked with lstat
metadata only, the custody key is identified from its public half, and the
rootless daemon is checked by the preinstalled non-root host-policy verifier
until images are imported. That verifier is the first-provisioning check and
requires an empty daemon; after a root-sealed import receipt exists, imported
images are verified by the custodian's post-import preflight instead
(infra/docs/coding-native-host-preflight-v2.md).
The report carries only pass/fail, counts, and path metadata, never host identity
records, revisions, or approvals, because the workflow publishes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import socket
import stat
import subprocess
import sys

EXPECTED_HOSTNAME = "ditto-coding-hosted-v2"
EXPECTED_CUSTODY_SPKI_SHA256 = (
    "1b0be06c162c160028e74d41b64aefdfca72dba60f7961f1a4fec60d1de1bcda"
)
RUNTIME_USER = "ditto-coding-hosted"
CUSTODY_USER = "ditto-coding-custody"
HOST_POLICY = "/usr/local/lib/ditto-coding-hosted/host-policy.py"
DAEMON_HOME = "/var/lib/ditto-coding-hosted"
DAEMON_SOCKET = "/run/ditto-coding-hosted/docker.sock"
CUSTODY_PUBLIC_KEY = "/var/lib/ditto-coding-custody/keys/private-input-rsa-public.pem"
CUSTODY_PRIVATE_KEY = "/var/lib/ditto-coding-custody/keys/private-input-rsa.pem"
CUSTODY_RECEIPT = "/var/lib/ditto-coding-custody/keys/private-input-rsa-receipt.json"
RUNTIME_ROOT = "/opt/ditto-coding-hosted"
RUNTIME_BUNDLE = "/opt/ditto-coding-hosted/runtime-bundle.py"
IMAGE_ROOT = "/opt/ditto-coding-hosted-images"
# Reviewed default locations, not yet provisioned or confirmed: reported only.
# Each reader requires its own owner-only copy inside a 0700 directory it owns.
CUSTODY_POSTGRES_ENVIRONMENT = (
    "/var/lib/ditto-coding-custody/private/postgres-environment.json"
)
WORKER_POSTGRES_ENVIRONMENT = (
    "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
)
CLEAN_PATH = "/usr/sbin:/usr/bin:/bin"

checks: list[dict[str, object]] = []


def record(name: str, ok: bool, detail: object, *, required: bool = True) -> None:
    checks.append(
        {"name": name, "ok": bool(ok), "detail": detail, "required": required}
    )


def run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        env={"PATH": CLEAN_PATH, "LANG": "C", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )


def metadata(path: str) -> dict[str, object]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False}
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        owner = str(info.st_uid)
    kinds = (
        (stat.S_ISREG, "file"),
        (stat.S_ISDIR, "directory"),
        (stat.S_ISLNK, "symlink"),
        (stat.S_ISSOCK, "socket"),
    )
    kind = next((name for test, name in kinds if test(info.st_mode)), "other")
    return {
        "exists": True,
        "type": kind,
        "owner": owner,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "links": info.st_nlink,
    }


def check_units() -> None:
    egress = run(["systemctl", "is-active", "ditto-coding-hosted-egress.service"])
    state = egress.stdout.decode().strip()
    record("egress policy unit active", state == "active", {"state": state})

    rootful = run(["systemctl", "is-active", "docker.service", "docker.socket"])
    states = rootful.stdout.decode().split()
    record(
        "rootful docker inactive",
        len(states) == 2 and "active" not in states,
        {"states": states},
    )

    try:
        uid = pwd.getpwnam(RUNTIME_USER).pw_uid
    except KeyError:
        record("rootless docker daemon active", False, {"user": "missing"})
        return
    rootless = run(
        [
            "runuser",
            "-u",
            RUNTIME_USER,
            "--",
            "env",
            "-i",
            "PATH=/usr/bin:/bin",
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            "systemctl",
            "--user",
            "is-active",
            "coding-hosted-docker.service",
        ]
    )
    state = rootless.stdout.decode().strip()
    record("rootless docker daemon active", state == "active", {"state": state})


def sealed(info: dict[str, object], kind: str) -> bool:
    """Root-owned and not writable by group or others."""
    return (
        info.get("type") == kind
        and info.get("owner") == "root"
        and not int(str(info.get("mode", "0777")), 8) & 0o022
    )


def verified_imports() -> int:
    """Count root-sealed image import receipts; anything unexpected counts as none."""
    if not sealed(metadata(IMAGE_ROOT), "directory"):
        return 0
    try:
        entries = sorted(os.scandir(IMAGE_ROOT), key=lambda entry: entry.name)
    except OSError:
        return 0
    count = 0
    for entry in entries:
        receipt = metadata(os.path.join(entry.path, "import-receipt.json"))
        if (
            sealed(metadata(entry.path), "directory")
            and sealed(receipt, "file")
            and receipt.get("mode") == "0600"
            and receipt.get("links") == 1
        ):
            count += 1
    return count


def check_host_policy(imports: int) -> None:
    for name, path, kind, mode in (
        ("rootless daemon home", DAEMON_HOME, "directory", "0700"),
        (
            "rootless daemon socket directory",
            os.path.dirname(DAEMON_SOCKET),
            "directory",
            "0700",
        ),
        ("rootless daemon socket", DAEMON_SOCKET, "socket", "0600"),
    ):
        info = metadata(path)
        record(
            f"{name} is private to the daemon account",
            info.get("type") == kind
            and info.get("owner") == RUNTIME_USER
            and info.get("mode") == mode,
            info,
        )
    if imports:
        # The bootstrap verifier requires zero images, so it cannot pass once
        # approved images are imported; never treat that as a host failure.
        record(
            "preinstalled non-root host policy verify (socket, paths, empty daemon)",
            False,
            {"applicable": False, "verified_imports": imports},
            required=False,
        )
        return
    policy = run(
        [
            "runuser",
            "-u",
            RUNTIME_USER,
            "--",
            "/usr/bin/python3",
            "-I",
            HOST_POLICY,
            "verify",
        ]
    )
    try:
        report = json.loads(policy.stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        report = None
    ok = (
        policy.returncode == 0
        and isinstance(report, dict)
        and report.get("schema") == "dittobench-coding-hosted-daemon-check-v2"
    )
    record(
        "preinstalled non-root host policy verify (socket, paths, empty daemon)",
        ok,
        {"exit": policy.returncode},
    )


def check_custody() -> None:
    public = metadata(CUSTODY_PUBLIC_KEY)
    der = run(
        ["openssl", "pkey", "-pubin", "-in", CUSTODY_PUBLIC_KEY, "-outform", "DER"]
    )
    spki = hashlib.sha256(der.stdout).hexdigest() if der.returncode == 0 else None
    record(
        "custody public key fingerprint",
        public.get("type") == "file"
        and public.get("owner") == CUSTODY_USER
        and spki == EXPECTED_CUSTODY_SPKI_SHA256,
        {"spki_sha256": spki, **public},
    )
    for name, path in (
        ("custody private key metadata (never opened)", CUSTODY_PRIVATE_KEY),
        ("custody key receipt metadata (never opened)", CUSTODY_RECEIPT),
    ):
        info = metadata(path)
        record(
            name,
            info.get("type") == "file"
            and info.get("owner") == CUSTODY_USER
            and info.get("mode") == "0600"
            and info.get("links") == 1,
            info,
        )


def check_runtime(imports: int) -> None:
    root = metadata(RUNTIME_ROOT)
    record(
        "runtime root is root-owned and not group/world writable",
        root.get("type") == "directory"
        and root.get("owner") == "root"
        and not int(str(root.get("mode", "0777")), 8) & 0o022,
        root,
    )
    bundle = metadata(RUNTIME_BUNDLE)
    try:
        revisions = sorted(
            entry.name
            for entry in os.scandir(RUNTIME_ROOT)
            if entry.is_dir(follow_symlinks=False)
        )
    except OSError:
        revisions = []
    record(
        "runtime installer and installed revisions",
        bundle.get("owner") == "root" and bundle.get("mode") == "0444",
        {"installer": bundle, "installed_revisions": len(revisions)},
        required=False,
    )
    record(
        "verified image imports",
        imports > 0,
        {"verified_imports": imports},
        required=False,
    )


def check_postgres_environment() -> None:
    for reader, path, user in (
        ("custody", CUSTODY_POSTGRES_ENVIRONMENT, CUSTODY_USER),
        ("worker", WORKER_POSTGRES_ENVIRONMENT, RUNTIME_USER),
    ):
        info = metadata(path)
        directory = metadata(os.path.dirname(path))
        record(
            f"{reader} postgres environment metadata (never opened, not provisioned)",
            info.get("type") == "file"
            and info.get("owner") == user
            and info.get("mode") == "0600"
            and info.get("links") == 1
            and directory.get("type") == "directory"
            and directory.get("owner") == user
            and directory.get("mode") == "0700",
            {"file": info, "directory": directory},
            required=False,
        )


def main() -> int:
    expected_host = socket.gethostname() == EXPECTED_HOSTNAME
    record("host identity", expected_host, {"expected": expected_host})
    if expected_host:
        imports = verified_imports()
        check_units()
        check_host_policy(imports)
        check_custody()
        check_runtime(imports)
        check_postgres_environment()
    ok = all(check["ok"] for check in checks if check["required"])
    json.dump(
        {
            "schema": "ditto-coding-host-verify-v1",
            "ok": ok,
            "reads_secrets": False,
            "mutates": False,
            "checks": checks,
        },
        sys.stdout,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
