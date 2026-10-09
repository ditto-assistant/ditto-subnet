#!/usr/bin/env python3
"""Restore a verified encrypted backup in a network-isolated disposable database."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

MODULE = (
    Path(__file__).resolve().parents[1]
    / "ansible/roles/postgres_backup/files/backup.py"
)
spec = importlib.util.spec_from_file_location("pg_backup", MODULE)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def validate_manifest(manifest, stamp, now):
    if (
        manifest.get("format_version") != 1
        or manifest.get("database") != backup.DATABASE
    ):
        raise ValueError("unsupported backup manifest")
    started = datetime.fromisoformat(manifest["started_at"])
    ended = datetime.fromisoformat(manifest["completed_at"])
    if started.tzinfo is None or ended.tzinfo is None:
        raise ValueError("backup timestamps must be timezone-aware")
    if started.strftime("%Y%m%dT%H%M%SZ") != stamp:
        raise ValueError("manifest does not match selected backup")
    age = (now - started).total_seconds()
    skew = 5 * 60
    if (
        age < -skew
        or age > 36 * 3600
        or ended < started
        or (ended - now).total_seconds() > skew
    ):
        raise ValueError("backup is stale or has invalid timestamps")
    major = int(manifest["server_version_num"]) // 10000
    if not 14 <= major <= 18:
        raise ValueError("unsupported PostgreSQL major version")
    names = {f"ditto_platform_prod-{stamp}.dump.age", f"globals-{stamp}.sql.age"}
    objects = manifest["objects"]
    if len(objects) != 2 or {item["name"] for item in objects} != names:
        raise ValueError("manifest object set is invalid")
    for item in objects:
        if (
            not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or not 22 <= item["size"] <= 100 * 1024**3
        ):
            raise ValueError("invalid object digest or size")
    if set(manifest["row_counts"]) != set(backup.TABLES):
        raise ValueError("manifest table count set is invalid")
    if any(
        type(value) is not int or value <= 0
        for value in manifest["row_counts"].values()
    ):
        raise ValueError("manifest core tables must be nonempty")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", manifest["alembic_version"]):
        raise ValueError("invalid migration marker")
    return major


def compare_counts(expected, actual):
    if set(actual) != set(expected):
        raise ValueError("restored table count set differs")
    for table, count in expected.items():
        if actual[table] <= 0 or abs(actual[table] - count) > count * 0.05:
            raise ValueError("restored core table count differs")


def report(stage):
    # Constant stage names and aggregate disk bytes only; never exception text.
    print(f"Restore stage: {stage}", flush=True)


def classify_restore_errors(stream, result):
    # Drain stderr concurrently to prevent pipe backpressure. Retain no SQL,
    # role hashes, identifiers or provider URLs; return only fixed categories.
    patterns = {
        "disk-full": b"No space left on device",
        "extension-unavailable": b"extension is not available",
        "role-conflict": b"already exists",
        "permission-denied": b"Permission denied",
        "archive-version": b"unsupported version",
    }
    tail = b""
    while chunk := stream.read(4096):
        window = tail + chunk
        for label, pattern in patterns.items():
            if pattern in window:
                result.add(label)
        tail = window[-128:]
    stream.close()


def stream_restore(source, identity, container, command):
    decrypt = subprocess.Popen(
        ["age", "-d", "-i", str(identity), str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        restore = subprocess.Popen(
            ["docker", "exec", "-i", container, *command],
            stdin=decrypt.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        categories = set()
        drain = threading.Thread(
            target=classify_restore_errors, args=(restore.stderr, categories)
        )
        drain.start()
        decrypt.stdout.close()
        restore_result = restore.wait()
        decrypt_result = decrypt.wait()
        drain.join()
        if restore_result or decrypt_result:
            print(
                f"Restore process failed: decrypt_exit={decrypt_result} "
                f"restore_exit={restore_result} "
                f"categories={','.join(sorted(categories)) or 'other'}",
                flush=True,
            )
            raise RuntimeError("decryption or restore failed")
    finally:
        if decrypt.poll() is None:
            decrypt.terminate()
            decrypt.wait()


def drill(directory):
    os.umask(0o077)
    report("list-backups")
    print(f"Runner free disk bytes: {shutil.disk_usage(directory).free}", flush=True)
    s3 = backup.S3(directory)
    identity = backup.protected_file(directory, "age-identity")
    objects = s3.list("daily/")
    candidates = []
    for item in objects:
        parsed = backup.parse_key(item["key"])
        if parsed and parsed[0] == "daily" and parsed[2] == "manifest":
            candidates.append((parsed[1], item))
    if not candidates:
        raise RuntimeError("no completed daily backup")
    instant, newest = max(candidates, key=lambda row: row[0])
    stamp = instant.strftime("%Y%m%dT%H%M%SZ")
    manifest_file = directory / "manifest.json"
    report("validate-manifest")
    s3.download(newest["key"], manifest_file, max_bytes=65536)
    manifest = json.loads(manifest_file.read_text())
    major = validate_manifest(manifest, stamp, datetime.now(UTC))
    prefix = newest["key"].rsplit("/", 1)[0] + "/"
    paths = {}
    for item in manifest["objects"]:
        report(
            "download-globals"
            if item["name"].startswith("globals-")
            else "download-database"
        )
        destination = directory / item["name"]
        s3.download(prefix + item["name"], destination, max_bytes=item["size"])
        report("verify-encrypted-object")
        if (
            destination.stat().st_size != item["size"]
            or backup.sha256(destination) != item["sha256"]
        ):
            raise RuntimeError("encrypted backup checksum mismatch")
        with destination.open("rb") as stream:
            if stream.read(22) != b"age-encryption.org/v1\n":
                raise RuntimeError("backup is not age encrypted")
        paths[item["name"]] = destination
    run = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    if not run.isdigit() or not attempt.isdigit():
        raise ValueError("invalid drill run identity")
    container = f"ditto-pg-drill-{run}-{attempt}"
    role = "ditto_backup_drill_superuser"
    try:
        report("start-isolated-database")
        print(
            f"Runner free disk bytes: {shutil.disk_usage(directory).free}", flush=True
        )
        (directory / "pgdata").mkdir()
        # No ports, no network, no persistent named volume. Every DB byte lives
        # under the workflow's private staging directory and is shredded later.
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "-e",
                f"POSTGRES_USER={role}",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_DB=restore_drill",
                "-e",
                "PGDATA=/var/lib/postgresql/data",
                "--mount",
                f"type=bind,src={directory / 'pgdata'},dst=/var/lib/postgresql/data",
                f"pgvector/pgvector:pg{major}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        report("wait-for-database")
        for _ in range(60):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-c",
                    'test "$(cat /proc/1/comm)" = postgres && '
                    f"psql -XAtq -U {role} -d restore_drill -c 'SELECT 1'",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("restore database did not become ready")
        report("verify-database-version")
        version = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-XAtq",
                "-U",
                role,
                "-d",
                "restore_drill",
                "-c",
                "SHOW server_version_num",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        if int(version.strip()) // 10000 != major:
            raise RuntimeError("restore database major version differs")
        report("restore-globals")
        stream_restore(
            paths[f"globals-{stamp}.sql.age"],
            identity,
            container,
            ["psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-U", role, "-d", "restore_drill"],
        )
        report("restore-database")
        stream_restore(
            paths[f"ditto_platform_prod-{stamp}.dump.age"],
            identity,
            container,
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "-U",
                role,
                "-d",
                "restore_drill",
            ],
        )
        report("verify-schema-and-counts")
        query = (
            "SELECT json_build_object('alembic_version', "
            "(SELECT version_num FROM alembic_version), "
            "'row_counts', json_build_object("
            + ", ".join(
                f"'{table}', (SELECT count(*) FROM {table})" for table in backup.TABLES
            )
            + "));"
        )
        restored = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "exec",
                    container,
                    "psql",
                    "-XAtq",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    role,
                    "-d",
                    "restore_drill",
                    "-c",
                    query,
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        )
        if restored["alembic_version"] != manifest["alembic_version"]:
            raise RuntimeError("restored migration marker differs")
        compare_counts(manifest["row_counts"], restored["row_counts"])
        # Only an aggregate result, never a dump, role hash or row data.
        print(
            "Platform PostgreSQL restore drill passed: schema and 3 core counts match"
        )
    finally:
        report("remove-isolated-database")
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )


if __name__ == "__main__":
    try:
        drill(Path(os.environ["PG_RESTORE_DIRECTORY"]))
    except Exception:
        print(
            "Platform PostgreSQL restore drill FAILED; private details withheld",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
