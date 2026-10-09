#!/usr/bin/env python3
"""Real Docker dump/restore controls using synthetic canonical-order keys."""

import importlib.util
import json
import subprocess
import time
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "drill", Path(__file__).with_name("platform-pg-restore-drill.py")
)
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def sql(container, database, query):
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-XAtq",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            database,
        ],
        input=query.encode(),
        capture_output=True,
        check=True,
    ).stdout


def start(container, args):
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
            "POSTGRES_HOST_AUTH_METHOD=trust",
            *args,
            "pgvector/pgvector:pg17",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        ready = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                'test "$(cat /proc/1/comm)" = postgres && pg_isready -U postgres',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("test database did not become ready")


def main():
    prefix = "sn49-locale-proof-" + uuid.uuid4().hex
    default, matching = prefix + "-default", prefix + "-matching"
    try:
        start(default, [])
        sql(
            default,
            "postgres",
            "CREATE DATABASE source TEMPLATE template0 "
            "ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'; "
            "CREATE DATABASE control;",
        )
        sql(
            default,
            "source",
            "CREATE TABLE owner_attestations "
            "(hotkey_lo text, hotkey_hi text, CONSTRAINT canonical_order "
            "CHECK (hotkey_lo < hotkey_hi)); "
            "INSERT INTO owner_attestations VALUES ('5A', '5a');",
        )
        dump = subprocess.check_output(
            [
                "docker",
                "exec",
                default,
                "pg_dump",
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "-U",
                "postgres",
                "source",
            ]
        )

        def restore(container):
            return subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "-U",
                    "postgres",
                    "-d",
                    "control",
                ],
                input=dump,
                capture_output=True,
            )

        negative = restore(default)
        assert negative.returncode != 0 and b"canonical_order" in negative.stderr
        start(matching, ["-e", f"POSTGRES_INITDB_ARGS={drill.POSTGRES_INITDB_ARGS}"])
        sql(matching, "postgres", "CREATE DATABASE control;")
        profile = json.loads(sql(matching, "control", drill.backup.DATABASE_LOCALE_SQL))
        assert profile == {
            "encoding": "UTF8",
            "collate": "C.UTF-8",
            "ctype": "C.UTF-8",
            "locale_provider": "c",
        }
        drill.validate_database_locale({"database_locale": profile}, "synthetic")
        positive = restore(matching)
        assert positive.returncode == 0
        assert (
            sql(matching, "control", "SELECT count(*) FROM owner_attestations;").strip()
            == b"1"
        )
        assert (
            sql(
                matching,
                "control",
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'canonical_order';",
            ).strip()
            == b"t"
        )
        print("Default-locale negative and recovery-init positive controls passed")
    finally:
        # Remove only this invocation's two UUID-owned synthetic containers.
        subprocess.run(
            ["docker", "rm", "-f", "-v", default, matching],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )


if __name__ == "__main__":
    main()
