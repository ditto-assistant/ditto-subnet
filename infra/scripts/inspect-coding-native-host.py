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
SOCKET = "/run/ditto-coding-hosted/docker.sock"
DAEMON_IDENTITY_SCHEMA = "dittobench-coding-native-daemon-identity-v1"
DAEMON_IMAGE_STORE = "io.containerd.snapshotter.v1"
DAEMON_TEXT = re.compile(r"[\x21-\x7e]{1,512}")
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


def daemon_identity(info, socket_path=SOCKET):
    """Canonical daemon identity; mirrors native.daemon_identity and Go's
    catalog.DaemonIdentityFromInfo, all pinned to one shared vector."""

    def text(value):
        return type(value) is str and DAEMON_TEXT.fullmatch(value) is not None

    require(type(info) is dict and text(socket_path))
    require(socket_path.startswith("/") and not socket_path.startswith("//"))
    require(os.path.normpath(socket_path) == socket_path)
    fields = {
        "engine_id": "ID",
        "server_version": "ServerVersion",
        "docker_root_dir": "DockerRootDir",
        "storage_driver": "Driver",
        "cgroup_driver": "CgroupDriver",
        "cgroup_version": "CgroupVersion",
        "default_runtime": "DefaultRuntime",
    }
    identity = {name: info.get(key) for name, key in fields.items()}
    require(all(text(item) for item in identity.values()))
    containerd = info.get("Containerd")
    require(type(containerd) is dict and text(containerd.get("Address")))
    options = info.get("SecurityOptions")
    require(type(options) is list and all(text(item) for item in options))
    require(len(set(options)) == len(options) and "name=rootless" in options)
    status = info.get("DriverStatus")
    require(type(status) is list and ["driver-type", DAEMON_IMAGE_STORE] in status)
    require(
        identity["cgroup_driver"] == "systemd" and identity["cgroup_version"] == "2"
    )
    identity.update(
        schema=DAEMON_IDENTITY_SCHEMA,
        rootless=True,
        socket_path=socket_path,
        image_store=DAEMON_IMAGE_STORE,
        containerd_address=containerd["Address"],
        security_options=sorted(options),
    )
    return identity


# nft ruleset normalization. The native network enforcement evidence tooling
# loads this file from the same reviewed checkout rather than copying it.

NFT_DYNAMIC_ELEMENT_KEYS = frozenset({"timeout", "expires"})
NFT_BASE_CHAIN_KEYS = frozenset(
    {"family", "table", "name", "type", "hook", "prio", "policy"}
)
NFT_OBJECT_ORDER = ("table", "chain", "set", "map")


def _nft_canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def nft_strip(value):
    """One listing value without handles or counter values.

    Counter statements and named counters keep their identity with zeroed
    packets and bytes.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "handle":
                continue
            result[key] = nft_strip(item)
            if key == "counter" and isinstance(item, dict):
                result[key].update(packets=0, bytes=0)
        return result
    if isinstance(value, list):
        return [nft_strip(item) for item in value]
    return value


def nft_entries(value):
    """Listing entries in kernel order, stripped; ``None`` if malformed.

    Elements of named sets and maps also lose their kernel-side ``timeout`` and
    ``expires``; their values, counters' presence and comments stay.
    """
    if type(value) is not dict or set(value) != {"nftables"}:
        return None
    if type(value["nftables"]) is not list:
        return None
    result = []
    for entry in value["nftables"]:
        if type(entry) is not dict or len(entry) != 1:
            return None
        kind, body = next(iter(entry.items()))
        if type(body) is not dict:
            return None
        if kind == "metainfo":
            continue
        body = nft_strip(body)
        if kind in ("set", "map") and "elem" in body:
            if type(body["elem"]) is not list:
                return None
            body["elem"] = [
                {
                    "elem": {
                        k: v
                        for k, v in item["elem"].items()
                        if k not in NFT_DYNAMIC_ELEMENT_KEYS
                    }
                }
                if type(item) is dict
                and set(item) == {"elem"}
                and type(item["elem"]) is dict
                else item
                for item in body["elem"]
            ]
        result.append({kind: body})
    return result


def _nft_references(value, sets, chains):
    if isinstance(value, dict):
        for key, item in value.items():
            target = item.get("target") if isinstance(item, dict) else None
            if key in ("jump", "goto") and type(target) is str:
                chains.add(target)
            _nft_references(item, sets, chains)
    elif isinstance(value, list):
        for item in value:
            _nft_references(item, sets, chains)
    elif isinstance(value, str) and value.startswith("@"):
        sets.add(value[1:])


def _nft_sorted_elements(value):
    """Set membership is unordered: sort named and anonymous element lists."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            item = _nft_sorted_elements(item)
            if key in ("elem", "set") and isinstance(item, list):
                item = sorted(item, key=_nft_canonical)
            result[key] = item
        return result
    if isinstance(value, list):
        return [_nft_sorted_elements(item) for item in value]
    return value


def nft_semantic_entries(entries):
    """The enforcement-relevant content of one stripped table listing.

    Kept: every rule (in chain order), every referenced chain or set, every
    set or map with elements, every stateful object and every base chain that
    is not a provable no-op. Dropped, because no packet verdict can depend on
    them: a regular chain with no rules that no jump or goto names; a set or
    map with no elements that no rule, set or map references (``@name``); and
    a ``filter`` base chain with no rules and policy ``accept``, since an
    accept verdict from one base chain never ends traversal of the others.
    A base chain with policy ``drop``, another type or any extra attribute
    is kept even when empty. Objects sort canonically; rules keep their
    order within a chain, since first match is semantic.
    """
    sets, chains = set(), set()
    ruled = set()
    for entry in entries:
        kind = next(iter(entry))
        if kind != "chain" and kind not in ("set", "map"):
            _nft_references(entry[kind], sets, chains)
        if kind in ("set", "map"):
            _nft_references(entry[kind].get("elem", []), sets, chains)
        if kind == "rule":
            ruled.add(entry[kind].get("chain"))
    objects, rules = [], []
    for entry in entries:
        kind = next(iter(entry))
        body = entry[kind]
        if kind == "rule":
            rules.append(entry)
            continue
        if kind == "chain":
            name = body.get("name")
            if name not in ruled and name not in chains:
                if "hook" not in body and "policy" not in body and "type" not in body:
                    continue
                if (
                    set(body) == NFT_BASE_CHAIN_KEYS
                    and body["type"] == "filter"
                    and body["policy"] == "accept"
                ):
                    continue
        if (
            kind in ("set", "map")
            and body.get("name") not in sets
            and not body.get("elem")
        ):
            continue
        objects.append(entry)

    def object_key(entry):
        kind = next(iter(entry))
        rank = NFT_OBJECT_ORDER.index(kind) if kind in NFT_OBJECT_ORDER else 4
        return rank, kind, _nft_canonical(entry)

    objects.sort(key=object_key)
    rules.sort(key=lambda entry: _nft_canonical(entry["rule"].get("chain")))
    return _nft_sorted_elements(objects + rules)


def nft_ruleset_semantic_sha256(raw, table):
    """Digest of ``nft -j list table inet <table>``, stable across worker cycles."""
    entries = nft_entries(object_json(raw))
    require(entries is not None)
    tables = [entry["table"] for entry in entries if "table" in entry]
    require(tables == [{"family": "inet", "name": table}])
    semantic = nft_semantic_entries(entries)
    return checksum(_nft_canonical(semantic).encode())


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
                "DOCKER_HOST": f"unix://{SOCKET}",
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
    require(str(native.SOCKET) == SOCKET)
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
    ruleset_sha256 = nft_ruleset_semantic_sha256(rules, native.TABLE)
    after = object_json(docker(["info", "--format", "{{json .}}"]))
    image.validate_daemon(after)
    identity = daemon_identity(before)
    require(daemon_identity(after) == identity)
    require(docker(["ps", "--all", "--quiet"]).strip() == b"")
    require(host_binding(config) == host)
    return {
        "schema": "dittobench-coding-native-host-preflight-v4",
        "source_revision": config["source_revision"],
        "release_manifest_sha256": config["release_manifest_sha256"],
        "runtime_archive_sha256": config["runtime_archive_sha256"],
        "image_approval_sha256": config["image_approval_sha256"],
        "config_sha256": checksum(image.json_bytes(config)),
        "host": host,
        "daemon_identity": identity,
        "daemon_identity_sha256": checksum(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ),
        "tool_sha256": tools,
        "nft_ruleset_semantic_sha256": ruleset_sha256,
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
