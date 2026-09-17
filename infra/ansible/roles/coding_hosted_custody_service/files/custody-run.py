"""Fixed per-run lifecycle for the native-v2 custody service.

The stable install (package, unwrap proxy, base configuration and locked unit
template) happens once. Each hosted run then gets one owner-only configuration
bound to the exact assigned worker UUID, a transient
ditto-coding-custody@<worker>.service instance, and cleanup when it stops. The
worker UUID only ever comes from this root-run argv and the unit instance name,
never from request data. No private material is read or printed.
"""

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from types import SimpleNamespace

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ZERO = "00000000-0000-0000-0000-000000000000"
BASE_SCHEMA = "ditto-coding-custody-base-v1"
CONFIG_SCHEMA = "dittobench-coding-private-v2-custody-config-v1"
CONFIG_NAME = "config.json"
UNIT = "ditto-coding-custody@{}.service"
LIMIT = 1 << 16
STABLE_FIELDS = {
    "schema",
    "shadow_only",
    "weight_eligible",
    "registration_file",
    "transport_manifest",
    "payload_authority",
    "publication_receipt",
    "curator_public_key",
    "reader_authority_sha256",
    "private_key_file",
    "postgres_environment_file",
    "socket_path",
    "client_uid",
}
PATH_FIELDS = {
    "registration_file",
    "transport_manifest",
    "payload_authority",
    "publication_receipt",
    "curator_public_key",
    "private_key_file",
    "postgres_environment_file",
    "socket_path",
}
LIVE = {"active", "activating", "deactivating", "reloading", "refreshing"}


def require(condition):
    if not condition:
        raise ValueError("native custody lifecycle rejected")


def worker(value):
    require(isinstance(value, str) and UUID.fullmatch(value) and value != ZERO)
    return value


def owned(info, *, uid, mode, kind):
    return (
        kind(info.st_mode) and info.st_uid == uid and stat.S_IMODE(info.st_mode) == mode
    )


def read_owned(path, *, uid):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        require(
            owned(info, uid=uid, mode=0o600, kind=stat.S_ISREG)
            and info.st_nlink == 1
            and info.st_size <= LIMIT
        )
        raw = os.read(descriptor, LIMIT + 1)
    finally:
        os.close(descriptor)
    require(len(raw) <= LIMIT)
    return raw


def base(layout):
    directory = os.lstat(os.path.dirname(layout.base))
    require(owned(directory, uid=layout.root_uid, mode=0o700, kind=stat.S_ISDIR))
    document = json.loads(read_owned(layout.base, uid=layout.root_uid))
    require(isinstance(document, dict) and document.get("schema") == BASE_SCHEMA)
    config = document.get("config")
    require(isinstance(config, dict) and set(config) == STABLE_FIELDS)
    require(
        config["schema"] == CONFIG_SCHEMA
        and config["shadow_only"] is True
        and config["weight_eligible"] is False
        and type(config["client_uid"]) is int
        and config["client_uid"] > 0
        and config["client_uid"] != layout.custody_uid
        and isinstance(config["reader_authority_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", config["reader_authority_sha256"])
        and config["socket_path"] == layout.socket
    )
    for name in PATH_FIELDS:
        value = config[name]
        require(
            isinstance(value, str)
            and os.path.isabs(value)
            and os.path.normpath(value) == value
        )
    return config


def runs(layout):
    info = os.lstat(layout.runs)
    require(owned(info, uid=layout.custody_uid, mode=0o700, kind=stat.S_ISDIR))
    return sorted(os.listdir(layout.runs))


def live_instances(layout):
    result = layout.systemctl(
        ["list-units", "--all", "--plain", "--no-legend", "--full", UNIT.format("*")]
    )
    live = []
    for line in result.splitlines():
        fields = line.split()
        if (
            len(fields) >= 4
            and fields[0].startswith("ditto-coding-custody@")
            and (fields[2] in LIVE or fields[3] in LIVE)
        ):
            live.append(fields[0])
    return live


def expected(layout, value):
    return {**base(layout), "worker_id": worker(value)}


def canonical(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def receipt(action, value, **extra):
    return {
        "schema": "ditto-coding-custody-run-v1",
        "action": action,
        "worker_id": value,
        "unit": UNIT.format(value),
        "shadow_only": True,
        "weight_eligible": False,
        **extra,
    }


def prepare(layout, value):
    config = expected(layout, value)
    require(runs(layout) == [] and not live_instances(layout))
    require(not os.path.lexists(config["socket_path"]))
    raw = canonical(config)
    parent = os.open(layout.runs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.mkdir(value, 0o700, dir_fd=parent)
        os.chown(
            value,
            layout.custody_uid,
            layout.custody_gid,
            dir_fd=parent,
            follow_symlinks=False,
        )
        directory = os.open(
            value, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            descriptor = os.open(
                CONFIG_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                os.fchown(descriptor, layout.custody_uid, layout.custody_gid)
                os.fchmod(descriptor, 0o600)
                require(os.write(descriptor, raw) == len(raw))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(parent)
    environment = config["postgres_environment_file"]
    try:
        info = os.lstat(environment)
        database_ready = owned(
            info, uid=layout.custody_uid, mode=0o600, kind=stat.S_ISREG
        )
    except FileNotFoundError:
        database_ready = False
    return receipt(
        "prepared",
        value,
        config_sha256=hashlib.sha256(raw).hexdigest(),
        postgres_environment_present=database_ready,
        started=False,
    )


def check(layout, value):
    config = expected(layout, value)
    require(runs(layout) == [value])
    require(set(live_instances(layout)) <= {UNIT.format(value)})
    directory = os.path.join(layout.runs, value)
    require(
        owned(
            os.lstat(directory), uid=layout.custody_uid, mode=0o700, kind=stat.S_ISDIR
        )
    )
    raw = read_owned(os.path.join(directory, CONFIG_NAME), uid=layout.custody_uid)
    require(json.loads(raw) == config and raw == canonical(config))
    require(not os.path.lexists(config["socket_path"]))
    return receipt("checked", value, config_sha256=hashlib.sha256(raw).hexdigest())


def release(layout, value):
    worker(value)
    names = runs(layout)
    if value not in names:
        return receipt("released", value, removed=False)
    parent = os.open(layout.runs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.stat(value, dir_fd=parent, follow_symlinks=False)
        require(owned(info, uid=layout.custody_uid, mode=0o700, kind=stat.S_ISDIR))
        directory = os.open(
            value, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            entries = os.listdir(directory)
            require(set(entries) <= {CONFIG_NAME})
            if entries:
                entry = os.stat(CONFIG_NAME, dir_fd=directory, follow_symlinks=False)
                require(stat.S_ISREG(entry.st_mode))
                os.unlink(CONFIG_NAME, dir_fd=directory)
        finally:
            os.close(directory)
        os.rmdir(value, dir_fd=parent)
    finally:
        os.close(parent)
    return receipt("released", value, removed=True)


def discard(layout, value):
    worker(value)
    require(not live_instances(layout))
    return release(layout, value)


def systemctl(arguments):
    result = subprocess.run(
        ["/usr/bin/systemctl", *arguments],
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "SYSTEMD_COLORS": "0",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=True,
    )
    require(len(result.stdout) <= LIMIT)
    return result.stdout.decode()


def production():
    require(os.geteuid() == 0)
    custody = pwd.getpwnam("ditto-coding-custody")
    return SimpleNamespace(
        base="/etc/ditto-coding-custody/base.json",
        runs="/var/lib/ditto-coding-custody/runs",
        socket="/run/ditto-coding-custody/custody.sock",
        root_uid=0,
        custody_uid=custody.pw_uid,
        custody_gid=custody.pw_gid,
        systemctl=systemctl,
    )


ACTIONS = {"prepare": prepare, "check": check, "release": release, "discard": discard}


def main(argv, layout=None):
    require(len(argv) == 2 and argv[0] in ACTIONS)
    result = ACTIONS[argv[0]](layout or production(), worker(argv[1]))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("native custody lifecycle failed", file=sys.stderr)
        raise SystemExit(1) from None
