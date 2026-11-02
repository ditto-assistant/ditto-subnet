#!/usr/bin/env python3
"""Read-only post-import native-host preflight, not execution qualification.

Run only from an independently reviewed, root-protected checkout on the named
host. No installation, import, firewall write, container start or retry exists.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import selectors
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1 << 20
TOOL_FILES = (
    "infra/scripts/inspect-coding-native-host.py",
    "infra/scripts/build-coding-native-release.py",
    "infra/ansible/roles/coding_hosted_image/files/image-bundle.py",
    "infra/ansible/roles/coding_hosted_runtime/files/runtime-bundle.py",
    "infra/ansible/roles/coding_hosted/files/host-policy.py",
)
PENDING = [
    "live_network_and_expiry_enforcement",
    "live_resource_limit_enforcement",
    "candidate_preexec_confinement",
    "candidate_cleanup_and_interruption",
]


def require(condition):
    if not condition:
        raise ValueError("native host preflight rejected")


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def protected(path, *, directory=False, private=False):
    require(path.is_absolute() and path.resolve() == path)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0)
        require(not info.st_mode & 0o022)
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if not directory:
        require(info.st_nlink == 1)
    if private:
        require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600))


def object_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    require(len(raw) <= LIMIT)
    result = json.loads(raw, object_pairs_hook=unique)
    require(type(result) is dict)
    return result


def config_policy(value):
    required = {
        "schema",
        "source_revision",
        "release_directory",
        "release_manifest_sha256",
        "runtime_archive_sha256",
        "image_approval_sha256",
        "machine_id_sha256",
        "boot_id",
        "shadow_only",
        "weight_eligible",
    }
    require(type(value) is dict and required <= value.keys())
    require(value["schema"] == "dittobench-coding-native-host-preflight-config-v2")
    require(value["shadow_only"] is True and value["weight_eligible"] is False)
    require(type(value["source_revision"]) is str)
    require(re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]))
    for key in (
        "release_manifest_sha256",
        "runtime_archive_sha256",
        "machine_id_sha256",
    ):
        require(type(value[key]) is str and re.fullmatch(r"[0-9a-f]{64}", value[key]))
    require(type(value["boot_id"]) is str)
    require(
        re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", value["boot_id"])
    )
    pins = value["image_approval_sha256"]
    require(type(pins) is dict and set(pins) == {"python", "node", "go", "rust"})
    require(
        all(type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v) for v in pins.values())
    )
    require(type(value["release_directory"]) is str)
    path = Path(value["release_directory"])
    require(path.is_absolute() and str(path) == value["release_directory"])
    require(".." not in path.parts)
    # Unknown fields cannot introduce commands, paths or approval authority.
    return {key: value[key] for key in required}


def command(arguments, *, uid=None, gid=None):
    """Only fixed protected inspection binaries; bounded output and lifetime."""
    require(arguments[0] in {"/usr/bin/docker", "/usr/bin/systemctl", "/usr/sbin/nft"})
    protected(Path(arguments[0]))
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    identity = {}
    if uid is not None:
        require(type(uid) is int and uid >= 1000 and type(gid) is int and gid >= 1000)
        env.update(
            {
                "DOCKER_HOST": "unix:///run/ditto-coding-hosted/docker.sock",
                "DOCKER_CONFIG": "/var/lib/ditto-coding-hosted/empty-client",
            }
        )
        identity = {"user": uid, "group": gid, "extra_groups": []}
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd="/",
        **identity,
    )
    deadline = time.monotonic() + 30
    body = bytearray()
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    body.extend(chunk)
                    require(len(body) <= LIMIT)
        require(process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    return bytes(body)


def verify_pins(config, release):
    require(release["source_revision"] == config["source_revision"])
    require(release["runtime"]["archive_sha256"] == config["runtime_archive_sha256"])
    require(set(release["images"]) == set(config["image_approval_sha256"]))
    for language, pin in config["image_approval_sha256"].items():
        require(release["images"][language]["approval_sha256"] == pin)


def host_binding(config):
    require(os.geteuid() == 0)
    require(platform.node() == "ditto-coding-hosted-v2")
    require(platform.system() == "Linux" and platform.machine() == "x86_64")
    os_release = platform.freedesktop_os_release()
    require(os_release.get("ID") == "debian" and os_release.get("VERSION_ID") == "13")
    machine = Path("/etc/machine-id").read_bytes().strip()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    require(re.fullmatch(rb"[0-9a-f]{32}", machine))
    require(
        checksum(machine) == config["machine_id_sha256"] and boot == config["boot_id"]
    )
    return {
        "machine_id_sha256": checksum(machine),
        "boot_id": boot,
        "kernel_release": platform.release(),
    }


def services():
    for name in ("docker.service", "docker.socket"):
        require(
            command(
                ["/usr/bin/systemctl", "show", name, "--property=LoadState", "--value"]
            ).strip()
            == b"masked"
        )
        require(
            command(
                [
                    "/usr/bin/systemctl",
                    "show",
                    name,
                    "--property=ActiveState",
                    "--value",
                ]
            ).strip()
            == b"inactive"
        )
    require(
        command(
            [
                "/usr/bin/systemctl",
                "show",
                "ditto-coding-hosted-worker.service",
                "--property=ActiveState",
                "--value",
            ]
        ).strip()
        == b"inactive"
    )
    require(
        command(
            [
                "/usr/bin/systemctl",
                "show",
                "ditto-coding-hosted-egress.service",
                "--property=ActiveState",
                "--value",
            ]
        ).strip()
        == b"active"
    )


def inspect_host(config):
    config = config_policy(config)
    host = host_binding(config)
    # Validate all local executable Python dependencies before importing them.
    tools = {}
    for name in TOOL_FILES:
        path = ROOT / name
        protected(path)
        require(path.stat().st_size <= LIMIT)
        tools[name] = checksum(path.read_bytes())
    release_tool = load("host_release", ROOT / TOOL_FILES[1])
    native = load("host_identity", ROOT / TOOL_FILES[4])
    image, runtime = release_tool.IMAGE, release_tool.RUNTIME
    directory = Path(config["release_directory"])
    protected(directory, directory=True)
    paths = ["release.json", "native/runtime.tar"]
    for language in config["image_approval_sha256"]:
        paths.extend([f"{language}/approval.json", f"{language}/runtime.oci.tar"])
    for name in paths:
        protected(directory / name)
    release = release_tool.verify(
        directory, config["source_revision"], config["release_manifest_sha256"]
    )
    verify_pins(config, release)
    with release_tool.artifact(
        directory / "native/runtime.tar", runtime.MAX_ARCHIVE
    ) as stream:
        manifest, _ = runtime.inspect(
            stream, config["runtime_archive_sha256"], config["source_revision"]
        )
        runtime.check_base(manifest)
        for parent in (runtime.BASE, *runtime.BASE.parents):
            protected(parent, directory=True)
        runtime.verify_tree(
            manifest,
            runtime.BASE / config["source_revision"],
            config["runtime_archive_sha256"],
        )
    user = native.identity()
    for path in (
        native.DAEMON_HOME,
        native.SOCKET.parent,
        native.DAEMON_HOME / "empty-client",
    ):
        native.private_path(path, user.pw_uid, 0o700)
    native.private_path(native.SOCKET, user.pw_uid, 0o600, socket=True)
    require(not list((native.DAEMON_HOME / "empty-client").iterdir()))
    services()

    def docker(args):
        return command(["/usr/bin/docker", *args], uid=user.pw_uid, gid=user.pw_gid)

    before = object_json(docker(["info", "--format", "{{json .}}"]))
    image.validate_daemon(before)  # zero containers, not zero imported images
    require(docker(["ps", "--all", "--quiet"]).strip() == b"")
    for language in config["image_approval_sha256"]:
        with release_tool.artifact(
            directory / f"{language}/approval.json", image.MAX_JSON
        ) as stream:
            raw = stream.read(image.MAX_JSON + 1)
        require(checksum(raw) == config["image_approval_sha256"][language])
        approval = object_json(raw)
        image.validate_loaded(
            json.loads(docker(["image", "inspect", approval["image_ref"]])), approval
        )
    rules = command(["/usr/sbin/nft", "-j", "list", "table", "inet", native.TABLE])
    nft = object_json(rules).get("nftables")
    require(type(nft) is list)
    tables = [
        entry["table"] for entry in nft if type(entry) is dict and "table" in entry
    ]
    require(len(tables) == 1 and type(tables[0]) is dict)
    require(tables[0].get("family") == "inet" and tables[0].get("name") == native.TABLE)
    after = object_json(docker(["info", "--format", "{{json .}}"]))
    image.validate_daemon(after)
    require(
        type(before.get("ID")) is str
        and bool(before["ID"])
        and before["ID"] == after.get("ID")
    )
    require(docker(["ps", "--all", "--quiet"]).strip() == b"")
    require(host_binding(config) == host)
    return {
        "schema": "dittobench-coding-native-host-preflight-v2",
        "source_revision": config["source_revision"],
        "release_manifest_sha256": config["release_manifest_sha256"],
        "runtime_archive_sha256": config["runtime_archive_sha256"],
        "image_approval_sha256": config["image_approval_sha256"],
        "config_sha256": checksum(image.json_bytes(config)),
        "host": host,
        "daemon_identity_sha256": checksum(before["ID"].encode()),
        "tool_sha256": tools,
        "nft_snapshot_sha256": checksum(rules),
        "checked_at_unix": int(time.time()),
        "host_preflight_passed": True,
        "pending_host_qualification": PENDING,
        "runtime_qualification": False,
        "private_execution_ready": False,
        "shadow_only": True,
        "weight_eligible": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    require(os.geteuid() == 0)
    protected(args.config.parent, directory=True, private=True)
    protected(args.config, private=True)
    require(args.config.stat().st_size <= LIMIT)
    with args.config.open("rb") as stream:
        config = object_json(stream.read(LIMIT + 1))
    print(json.dumps(inspect_host(config), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        OSError,
        EOFError,
        RecursionError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ):
        print(
            "native host preflight rejected; no activation or repair attempted",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
