#!/usr/bin/python3
"""Prepare, verify or import one public native-v2 OCI image; never run it.

An approval SHA is an independent operator input, not a signature or runtime
qualification. No registry, private data, credentials or legacy socket are used.
"""

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import io
import json
import os
import pwd
import re
import selectors
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

MAX_ARCHIVE = 8 << 30
MAX_EXPANDED = 16 << 30
MAX_JSON = 1 << 20
MAX_FILES = 512
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z"
)
SCHEMA = "dittobench-coding-hosted-image-approval-v2"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
}
ENTRYPOINT = ["/usr/local/bin/dittobench-coding-supervisor"]
PROFILE = "python-call-ast-v1"
PROFILES = frozenset(
    {
        PROFILE,
        "python-call-ast-v2",
        "node-call-ast-v2",
        "go-call-ast-v1",
        "rust-call-ast-v1",
    }
)


def profile_environment(profile):
    return [
        "PATH=/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin"
        if profile == "go-call-ast-v1"
        else "PATH=/usr/local/bin:/usr/bin:/bin"
    ]


def profile_workdirs(profile):
    return (
        ("/workspace",)
        if profile in {"go-call-ast-v1", "rust-call-ast-v1"}
        else ("", "/")
    )


PREFIX = "io.heyditto.dittobench."
SOCKET = Path("/run/ditto-coding-hosted/docker.sock")
HOME_DIR = Path("/var/lib/ditto-coding-hosted")
CLIENT = HOME_DIR / "empty-client"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def json_object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    require(len(raw) <= MAX_JSON, "JSON exceeds bound")
    result = json.loads(raw, object_pairs_hook=unique)
    require(isinstance(result, dict), "expected JSON object")
    return result


def digest_file(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def scan_archive(stream):
    """Accept only plain USTAR OCI entries; never extract layer contents.

    Scan physical headers so tarfile cannot hide PAX/GNU extensions, duplicate
    aliases, sparse entries, appended archives or unbounded metadata behind a
    logical member. Blob hashes are checked before Docker sees any bytes.
    """
    stream.seek(0, 2)
    size = stream.tell()
    require(1024 <= size <= MAX_ARCHIVE and size % 512 == 0, "archive size rejected")
    stream.seek(0)
    files, names = {}, set()
    while True:
        header = stream.read(512)
        require(len(header) == 512, "missing tar terminator")
        if header == bytes(512):
            require(stream.read(512) == bytes(512), "incomplete tar terminator")
            while chunk := stream.read(1 << 20):
                require(not any(chunk), "data after tar terminator")
            break
        require(
            header[156:157] in (b"0", b"\0", b"5"), "tar extension or special entry"
        )
        require(header[257:263] == b"ustar\0", "plain USTAR required")
        item = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        name = item.name.rstrip("/") if item.isdir() else item.name
        require(
            name not in names and len(names) < MAX_FILES,
            "duplicate or excessive tar entries",
        )
        names.add(name)
        if item.isdir():
            require(
                name in ("blobs", "blobs/sha256") and item.size == 0,
                "unexpected directory",
            )
            continue
        require(
            name in ("oci-layout", "index.json")
            or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name),
            "unexpected OCI path",
        )
        require(
            item.size >= 0 and stream.tell() + item.size <= size - 1024,
            "truncated OCI member",
        )
        offset = stream.tell()
        digest = hashlib.sha256()
        remaining = item.size
        while remaining:
            chunk = stream.read(min(remaining, 1 << 20))
            require(bool(chunk), "truncated OCI member")
            digest.update(chunk)
            remaining -= len(chunk)
        padding = stream.read((-item.size) % 512)
        require(not any(padding), "nonzero tar padding")
        if name.startswith("blobs/"):
            require(name.endswith(digest.hexdigest()), "OCI blob hash mismatch")
        files[name] = (offset, item.size)
    return files


def config_policy(config, revision):
    require(isinstance(config, dict), "missing image config")
    labels = config.get("Labels", {})
    require(isinstance(labels, dict), "invalid image labels")
    profile = labels.get(PREFIX + "coding-test-driver-profile")
    require(profile in PROFILES, "unapproved driver profile")
    require(config.get("Entrypoint") == ENTRYPOINT, "wrong supervisor entrypoint")
    require(
        config.get("Env") == profile_environment(profile),
        "unexpected image environment",
    )
    require(config.get("User", "") in ("", "0", "0:0"), "wrong supervisor user")
    for field in ("Volumes", "Cmd", "Healthcheck", "OnBuild", "ExposedPorts"):
        require(not config.get(field), "unexpected image execution defaults")
    require(
        config.get("WorkingDir", "") in profile_workdirs(profile),
        "unexpected working directory",
    )
    require(
        labels.get(PREFIX + "coding-supervisor-contract") == "1",
        "wrong supervisor contract",
    )
    require(
        labels.get(PREFIX + "coding-test-driver-profile") in PROFILES,
        "unapproved driver profile",
    )
    require(
        labels.get(PREFIX + "coding-supervisor-fixture", "false") == "false",
        "fixture image rejected",
    )
    require(
        labels.get("org.opencontainers.image.revision") == revision,
        "source revision mismatch",
    )
    return profile


class BlobReader:
    """Expose only one verified blob to the streaming gzip reader."""

    def __init__(self, stream, offset, size):
        self.stream, self.remaining = stream, size
        stream.seek(offset)

    def read(self, size=-1):
        amount = self.remaining if size < 0 else min(size, self.remaining)
        chunk = self.stream.read(amount)
        self.remaining -= len(chunk)
        return chunk


def graph(stream, revision):
    require(
        bool(REVISION.fullmatch(revision)) and revision != "0" * 40,
        "invalid source revision",
    )
    files = scan_archive(stream)
    used = {"oci-layout", "index.json"}

    def read(name):
        require(
            name in files and files[name][1] <= MAX_JSON,
            "missing or oversized OCI JSON",
        )
        offset, size = files[name]
        stream.seek(offset)
        return json_object(stream.read(size))

    def descriptor(value, media):
        require(isinstance(value, dict), "invalid OCI descriptor")
        require(value.get("mediaType") in media, "unsupported OCI media type")
        digest = value.get("digest")
        require(
            isinstance(digest, str) and DIGEST.fullmatch(digest), "invalid OCI digest"
        )
        require(
            not any(k in value for k in ("urls", "data", "artifactType")),
            "external or embedded OCI data rejected",
        )
        name = "blobs/sha256/" + digest[7:]
        require(
            name in files
            and type(value.get("size")) is int
            and value["size"] == files[name][1],
            "OCI descriptor size mismatch",
        )
        used.add(name)
        return name

    require(
        read("oci-layout") == {"imageLayoutVersion": "1.0.0"}, "unsupported OCI layout"
    )
    index = read("index.json")
    require(
        index.get("schemaVersion") == 2 and len(index.get("manifests", [])) == 1,
        "single image manifest required",
    )
    selected = index["manifests"][0]
    manifest = read(descriptor(selected, {MANIFEST_TYPE}))
    require(
        manifest.get("schemaVersion") == 2
        and manifest.get("mediaType") == MANIFEST_TYPE,
        "invalid OCI manifest",
    )
    require(
        not any(k in manifest for k in ("subject", "artifactType")),
        "image artifact rejected",
    )
    config = read(descriptor(manifest.get("config"), {CONFIG_TYPE}))
    require(
        config.get("os") == "linux" and config.get("architecture") == "amd64",
        "unsupported image platform",
    )
    require(config.get("variant", "") == "", "unsupported platform variant")
    profile = config_policy(config.get("config"), revision)
    layers = manifest.get("layers")
    require(isinstance(layers, list) and 1 <= len(layers) <= 128, "invalid layer count")
    for layer in layers:
        descriptor(layer, LAYER_TYPES)
    rootfs = config.get("rootfs", {})
    diff_ids = rootfs.get("diff_ids", [])
    require(
        rootfs.get("type") == "layers"
        and isinstance(diff_ids, list)
        and len(diff_ids) == len(layers),
        "invalid layer identities",
    )
    require(
        all(isinstance(d, str) and DIGEST.fullmatch(d) for d in diff_ids),
        "invalid layer identity",
    )
    require(used == set(files), "unreferenced OCI content")
    expanded = 0
    for layer, expected_diff_id in zip(layers, diff_ids, strict=True):
        blob = BlobReader(stream, *files["blobs/sha256/" + layer["digest"][7:]])
        reader = (
            gzip.GzipFile(fileobj=blob)
            if layer["mediaType"].endswith("+gzip")
            else blob
        )
        digest = hashlib.sha256()
        try:
            while chunk := reader.read(1 << 20):
                expanded += len(chunk)
                require(expanded <= MAX_EXPANDED, "expanded layers exceed bound")
                digest.update(chunk)
        finally:
            if isinstance(reader, gzip.GzipFile):
                reader.close()
        require(
            "sha256:" + digest.hexdigest() == expected_diff_id, "layer diff ID mismatch"
        )
    return files, selected["digest"], manifest["config"]["digest"], selected, profile


def approval_for(
    archive_sha, repository, image_digest, config_digest, revision, profile=PROFILE
):
    require(profile in PROFILES, "unapproved driver profile")
    require(
        isinstance(repository, str)
        and len(repository) <= 200
        and REPOSITORY.fullmatch(repository),
        "invalid repository",
    )
    return {
        "schema": SCHEMA,
        "archive_sha256": archive_sha,
        "image_ref": repository + "@" + image_digest,
        "config_digest": config_digest,
        "source_revision": revision,
        "driver_profile": profile,
        "shadow_only": True,
        "weight_eligible": False,
    }


def prepare(source, output, approval_path, repository, revision):
    """Normalize only transport metadata; image manifest/config bytes stay exact."""
    with source.open("rb") as stream:
        files, image_digest, config_digest, _, profile = graph(stream, revision)
        image_ref = repository + "@" + image_digest
        approval_for("", repository, image_digest, config_digest, revision, profile)
        index = json_bytes(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": MANIFEST_TYPE,
                        "digest": image_digest,
                        "size": files["blobs/sha256/" + image_digest[7:]][1],
                        "platform": {"os": "linux", "architecture": "amd64"},
                        "annotations": {"io.containerd.image.name": image_ref},
                    }
                ],
            }
        )
        # Exclusive output; partial files are retained for diagnosis, never replaced.
        with (
            output.open("xb") as raw,
            tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive,
        ):
            for name, (offset, size) in sorted(files.items()):
                item = tarfile.TarInfo(name)
                item.mode = 0o444
                item.size = len(index) if name == "index.json" else size
                stream.seek(offset)
                archive.addfile(
                    item, io.BytesIO(index) if name == "index.json" else stream
                )
        with output.open("rb") as raw:
            approval = approval_for(
                digest_file(raw),
                repository,
                image_digest,
                config_digest,
                revision,
                profile,
            )
            verify(
                raw,
                json_bytes(approval),
                hashlib.sha256(json_bytes(approval)).hexdigest(),
            )
        with approval_path.open("xb") as raw:
            raw.write(json_bytes(approval))
        return approval


def verify(stream, approval_raw, expected_sha):
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha), "invalid approval SHA")
    require(
        hashlib.sha256(approval_raw).hexdigest() == expected_sha,
        "approval SHA mismatch",
    )
    approval = json_object(approval_raw)
    image_ref = approval.get("image_ref", "")
    require(
        isinstance(image_ref, str) and image_ref.count("@") == 1,
        "invalid image reference",
    )
    repository, image_digest = image_ref.split("@")
    stream.seek(0, 2)
    require(stream.tell() <= MAX_ARCHIVE, "archive exceeds bound")
    archive_sha = digest_file(stream)
    require(approval.get("archive_sha256") == archive_sha, "archive SHA mismatch")
    _, actual_digest, config_digest, selected, profile = graph(
        stream, approval.get("source_revision", "")
    )
    require(actual_digest == image_digest, "image manifest digest mismatch")
    require(
        approval
        == approval_for(
            archive_sha,
            repository,
            actual_digest,
            config_digest,
            approval["source_revision"],
            profile,
        ),
        "approval fields mismatch",
    )
    require(
        approval.get("shadow_only") is True
        and approval.get("weight_eligible") is False,
        "invalid approval gates",
    )
    require(
        selected.get("annotations") == {"io.containerd.image.name": image_ref},
        "unapproved import name",
    )
    require(
        selected.get("platform") == {"os": "linux", "architecture": "amd64"},
        "invalid import platform",
    )
    stream.seek(0)
    return approval


def docker(arguments, *, stream=None, timeout=60):
    """Bound output as it arrives; no ambient Docker context, credentials or proxy."""
    command = [
        "/usr/bin/docker",
        "--host",
        f"unix://{SOCKET}",
        "--config",
        str(CLIENT),
        *arguments,
    ]
    with subprocess.Popen(
        command,
        stdin=stream if stream is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
        cwd="/",
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    ) as process:
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, "Docker command timed out")
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        output.extend(chunk)
                        require(len(output) <= MAX_JSON, "Docker output exceeds bound")
            require(
                process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                "Docker command failed",
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        return bytes(output)


def validate_daemon(info):
    require(
        info.get("OSType") == "linux" and info.get("Architecture") == "x86_64",
        "wrong daemon platform",
    )
    security = info.get("SecurityOptions", [])
    require(
        isinstance(security, list)
        and any(
            isinstance(s, str)
            and (s in ("rootless", "name=rootless") or s.startswith("name=rootless,"))
            for s in security
        ),
        "rootless daemon required",
    )
    require(
        isinstance(info.get("Labels"), list)
        and "io.heyditto.dittobench.isolated=true" in info["Labels"],
        "isolated daemon required",
    )
    require(
        ["driver-type", "io.containerd.snapshotter.v1"] in info.get("DriverStatus", []),
        "containerd image store required",
    )
    require(
        info.get("DockerRootDir") == str(HOME_DIR / "docker"), "wrong daemon data root"
    )
    require(
        info.get("CgroupDriver") == "systemd" and info.get("CgroupVersion") == "2",
        "delegated cgroup v2 required",
    )
    require(
        type(info.get("Containers")) is int and info["Containers"] == 0,
        "daemon has containers",
    )
    for field in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        require(info.get(field) is True, "missing daemon limit support")


def validate_loaded(info, approval):
    require(isinstance(info, list) and len(info) == 1, "ambiguous loaded image")
    item = info[0]
    require(
        isinstance(item.get("RepoDigests"), list)
        and approval["image_ref"] in item["RepoDigests"],
        "loaded repository digest mismatch",
    )
    # Containerd-backed Docker exposes the manifest as Id on newer engines;
    # older engines expose the config digest. Neither substitutes for RepoDigests.
    manifest_digest = approval["image_ref"].split("@")[1]
    require(
        item.get("Id") in (manifest_digest, approval["config_digest"]),
        "loaded image ID mismatch",
    )
    require(
        item.get("Descriptor", {}).get("digest") == manifest_digest,
        "loaded manifest descriptor mismatch",
    )
    require(
        item.get("Os") == "linux" and item.get("Architecture") == "amd64",
        "loaded image platform mismatch",
    )
    require(
        config_policy(item.get("Config"), approval["source_revision"])
        == approval["driver_profile"],
        "loaded driver profile mismatch",
    )


def private(path, uid, mode, kind):
    info = path.lstat()
    require(
        path.resolve() == path
        and info.st_uid == uid
        and stat.S_IMODE(info.st_mode) == mode
        and kind(info.st_mode),
        "unprotected native daemon path",
    )


@contextlib.contextmanager
def protected_file(path):
    require(
        path.is_absolute() and path.resolve() == path,
        "absolute non-symlink artifact path required",
    )
    for parent in path.parents:
        info = parent.lstat()
        require(
            info.st_uid == 0
            and stat.S_ISDIR(info.st_mode)
            and not info.st_mode & 0o022,
            "unprotected artifact parent",
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        require(
            info.st_uid == 0
            and stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o444,
            "root-owned readonly artifact required",
        )
        yield stream


def import_image(archive, approval_path, expected_sha):
    user = pwd.getpwnam("ditto-coding-hosted")
    require(
        os.geteuid() == user.pw_uid >= 1000 and os.getegid() == user.pw_gid >= 1000,
        "dedicated daemon identity required",
    )
    require(set(os.getgroups()) <= {user.pw_gid}, "supplementary groups rejected")
    for path in (HOME_DIR, SOCKET.parent, CLIENT):
        private(path, user.pw_uid, 0o700, stat.S_ISDIR)
    private(SOCKET, user.pw_uid, 0o600, stat.S_ISSOCK)
    require(not list(CLIENT.iterdir()), "Docker client configuration must be empty")
    lock_fd = os.open(
        HOME_DIR / "image-import.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    with (
        os.fdopen(lock_fd, "rb") as lock,
        protected_file(archive) as stream,
        protected_file(approval_path) as manifest,
    ):
        info = os.fstat(lock.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == user.pw_uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1,
            "invalid import lock",
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        approval = verify(stream, manifest.read(MAX_JSON + 1), expected_sha)
        validate_daemon(json_object(docker(["info", "--format", "{{json .}}"])))
        # Docker receives the exact still-open verified archive. No path reopen,
        # tag resolution, pull, container execution or retry occurs here.
        docker(["image", "load"], stream=stream, timeout=900)
        validate_loaded(
            json.loads(docker(["image", "inspect", approval["image_ref"]])), approval
        )
        validate_daemon(json_object(docker(["info", "--format", "{{json .}}"])))
        return {
            **approval,
            "schema": "dittobench-coding-hosted-image-import-v2",
            "approval_sha256": expected_sha,
            "qualification_required": True,
            "private_execution_ready": False,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    prepare_parser = modes.add_parser("prepare")
    prepare_parser.add_argument("--source", required=True, type=Path)
    prepare_parser.add_argument("--repository", required=True)
    prepare_parser.add_argument("--revision", required=True)
    for name in ("prepare", "verify", "import"):
        child = prepare_parser if name == "prepare" else modes.add_parser(name)
        child.add_argument("--archive", required=True, type=Path)
        child.add_argument("--approval", required=True, type=Path)
        if name != "prepare":
            child.add_argument("--approval-sha256", required=True)
        if name == "verify":
            child.add_argument(
                "--inspect",
                type=Path,
                help="Validate captured image inspect JSON; not host qualification",
            )
    args = parser.parse_args(argv)
    if args.mode == "prepare":
        result = prepare(
            args.source, args.archive, args.approval, args.repository, args.revision
        )
    elif args.mode == "verify":
        with args.archive.open("rb") as stream, args.approval.open("rb") as manifest:
            result = verify(stream, manifest.read(MAX_JSON + 1), args.approval_sha256)
        if args.inspect:
            with args.inspect.open("rb") as inspected:
                raw = inspected.read(MAX_JSON + 1)
            require(len(raw) <= MAX_JSON, "image inspect JSON exceeds bound")
            validate_loaded(json.loads(raw), result)
    else:
        result = import_image(args.archive, args.approval, args.approval_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        EOFError,
        OSError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ):
        print(
            "coding hosted image operation rejected; "
            "partial imports are retained for review",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
