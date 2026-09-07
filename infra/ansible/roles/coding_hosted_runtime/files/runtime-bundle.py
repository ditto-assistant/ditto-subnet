#!/usr/bin/python3
"""Build-time packing and offline, no-overwrite native runtime installation."""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

SCHEMA = "dittobench-coding-hosted-runtime-bundle-v2"
BASE = Path("/opt/ditto-coding-hosted")
PYTHON = Path("/usr/bin/python3.13")
MAX_ARCHIVE = 4 << 30
MAX_FILE = 256 << 20
MAX_MANIFEST = 8 << 20
PACKAGES = (
    "python3.13",
    "python3.13-minimal",
    "libpython3.13-stdlib",
    "libpython3.13-minimal",
    "libc6",
)
ROOTS = ("bin/", "apps/platform/", "packages/ditto-screening-protocol/")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        del message
        print("native runtime bundle arguments invalid", file=sys.stderr)
        raise SystemExit(64)


def require(condition):
    if not condition:
        raise ValueError("native runtime bundle rejected")


def digest(stream, size):
    require(type(size) is int and 0 <= size <= MAX_ARCHIVE)
    value = hashlib.sha256()
    while size:
        part = stream.read(min(size, 1 << 20))
        require(part)
        value.update(part)
        size -= len(part)
    return value.hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return digest(stream, path.stat().st_size)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def unique(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def relative(name):
    require(type(name) is str and 0 < len(name.encode()) <= 255)
    path = PurePosixPath(name)
    require(not path.is_absolute() and str(path) == name)
    require(not any(part in {".", "..", ".git", ".env"} for part in path.parts))
    require(not any(ord(c) < 32 or c == "\\" for c in name))
    require(name.startswith(ROOTS))
    return path


def link_target(name, target, revision):
    require(type(target) is str and 0 < len(target.encode()) <= 100)
    require(not any(ord(c) < 32 or c == "\\" for c in target))
    root = BASE / revision
    # No external dependency except the separately pinned Debian interpreter.
    resolved = (root / name).parent.joinpath(target).resolve(strict=False)
    require(resolved == PYTHON or resolved.is_relative_to(root))


def metadata(value, revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None)
    require(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "source_revision",
            "python_sha256",
            "debian_packages",
            "files",
            "shadow_only",
            "weight_eligible",
        }
    )
    require(value["schema"] == SCHEMA and value["source_revision"] == revision)
    require(value["shadow_only"] is True and value["weight_eligible"] is False)
    require(re.fullmatch(r"[0-9a-f]{64}", value["python_sha256"]) is not None)
    packages = value["debian_packages"]
    require(type(packages) is dict and set(packages) == set(PACKAGES))
    require(
        all(
            type(v) is str and 0 < len(v) < 128 and not any(c.isspace() for c in v)
            for v in packages.values()
        )
    )
    entries = value["files"]
    require(type(entries) is dict and 1 <= len(entries) <= 60000)
    for name, item in entries.items():
        path = relative(name)
        require(not any(str(parent) in entries for parent in path.parents))
        require(type(item) is dict)
        if set(item) == {"link"}:
            link_target(name, item["link"], revision)
        else:
            require(set(item) == {"sha256", "size", "executable"})
            require(type(item["size"]) is int and 0 <= item["size"] <= MAX_FILE)
            require(type(item["executable"]) is bool)
            require(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None)
    required = {
        "bin/dittobench-coding-hosted-worker",
        "apps/platform/ditto/coding_hosted_worker.py",
        "apps/platform/.venv/bin/python",
        "apps/platform/uv.lock",
    }
    require(required <= entries.keys())
    require(entries["bin/dittobench-coding-hosted-worker"].get("executable") is True)
    return value


def base_packages():
    result = subprocess.run(
        ["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Version}\n", *PACKAGES],
        check=True,
        capture_output=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    require(len(result.stdout) < 16384)
    return dict(line.split("\t") for line in result.stdout.decode().splitlines())


def check_base(value):
    release = platform.freedesktop_os_release()
    require(platform.system() == "Linux" and platform.machine() == "x86_64")
    require(release.get("ID") == "debian" and release.get("VERSION_ID") == "13")
    protected(PYTHON, owner=0, directory=False)
    require(file_hash(PYTHON) == value["python_sha256"])
    require(base_packages() == value["debian_packages"])


def protected(path, *, owner, directory):
    info = path.lstat()
    require(path.is_absolute() and path.resolve() == path)
    require(info.st_uid == owner and not info.st_mode & 0o022)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if not directory:
        require(info.st_nlink == 1)


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_dir():
                result[path.relative_to(root).as_posix()] = path
    return result


def pack(root, output, revision):
    require(root == BASE / revision and not output.exists())
    entries, selected = {}, {}
    for name, path in sorted(inventory(root).items()):
        if not (
            name.startswith(
                (
                    "bin/",
                    "apps/platform/ditto/",
                    "apps/platform/.venv/",
                    "packages/ditto-screening-protocol/",
                )
            )
            or name
            in {
                "apps/platform/pyproject.toml",
                "apps/platform/uv.lock",
                "apps/platform/README.md",
            }
        ):
            continue
        if "/__pycache__/" in name or name.endswith(".pyc"):
            continue
        if name.startswith("apps/platform/ditto/tests/"):
            continue
        relative(name)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            entries[name] = {"link": os.readlink(path)}
        else:
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            entries[name] = {
                "sha256": file_hash(path),
                "size": info.st_size,
                "executable": bool(info.st_mode & 0o111),
            }
        selected[name] = path
    value = metadata(
        {
            "schema": SCHEMA,
            "source_revision": revision,
            "python_sha256": file_hash(PYTHON),
            "debian_packages": base_packages(),
            "files": entries,
            "shadow_only": True,
            "weight_eligible": False,
        },
        revision,
    )
    body = canonical(value)
    require(len(body) <= MAX_MANIFEST)
    with (
        output.open("xb") as target,
        tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive,
    ):
        header = tarfile.TarInfo("manifest.json")
        header.size, header.mode = len(body), 0o444
        archive.addfile(header, io.BytesIO(body))
        for name, path in selected.items():
            item = entries[name]
            header = tarfile.TarInfo(name)
            if "link" in item:
                header.type, header.linkname, header.mode = (
                    tarfile.SYMTYPE,
                    item["link"],
                    0o777,
                )
                archive.addfile(header)
            else:
                header.size = item["size"]
                header.mode = 0o555 if item["executable"] else 0o444
                with path.open("rb") as source:
                    archive.addfile(header, source)


def inspect(stream, expected_sha, revision):
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha) is not None)
    size = os.fstat(stream.fileno()).st_size
    require(0 < size <= MAX_ARCHIVE and size % 512 == 0)
    stream.seek(0)
    require(digest(stream, size) == expected_sha)
    stream.seek(0)
    value, seen, records = None, set(), []
    while True:
        raw = stream.read(512)
        require(len(raw) == 512)
        if raw == bytes(512):
            trailer = stream.read(10241)
            require(512 <= len(trailer) <= 10240 and not any(trailer))
            require(value is not None and seen == set(value["files"]))
            return value, records
        # Parse a single bounded USTAR header, never a tarfile extension stream.
        require(raw[257:265] == b"ustar\x0000")
        header = tarfile.TarInfo.frombuf(raw, "utf-8", "strict")
        require(header.type in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.SYMTYPE})
        require(header.uid == 0 and header.gid == 0 and header.mtime == 0)
        require(0 <= header.size <= MAX_FILE and stream.tell() + header.size <= size)
        if value is None:
            require(header.name == "manifest.json" and header.isreg())
            require(0 < header.size <= MAX_MANIFEST and header.mode == 0o444)
            body = stream.read(header.size)
            value = metadata(json.loads(body, object_pairs_hook=unique), revision)
            require(canonical(value) == body)
        else:
            name = header.name
            require(name not in seen and name in value["files"])
            item = value["files"][name]
            if "link" in item:
                require(
                    header.issym()
                    and header.linkname == item["link"]
                    and header.size == 0
                )
                require(header.mode == 0o777)
            else:
                require(
                    header.isreg()
                    and header.size == item["size"]
                    and not header.linkname
                )
                require(header.mode == (0o555 if item["executable"] else 0o444))
                offset = stream.tell()
                if name == "bin/dittobench-coding-hosted-worker":
                    elf = stream.read(20)
                    require(
                        elf[:6] == b"\x7fELF\x02\x01"
                        and int.from_bytes(elf[18:20], "little") == 62
                    )
                    stream.seek(offset)
                require(digest(stream, header.size) == item["sha256"])
                records.append((name, offset, header.size))
            seen.add(name)
        padding = stream.read((-header.size) % 512)
        require(len(padding) == (-header.size) % 512 and not any(padding))


def materialize(stream, value, records, destination, archive_sha):
    # Destination is exclusively reserved; partial state is retained on failure.
    destination.mkdir(mode=0o700)
    for name, offset, size in records:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream.seek(offset)
        with path.open("xb") as target:
            remaining, checksum = size, hashlib.sha256()
            while remaining:
                body = stream.read(min(remaining, 1 << 20))
                require(body)
                target.write(body)
                checksum.update(body)
                remaining -= len(body)
            require(checksum.hexdigest() == value["files"][name]["sha256"])
            os.fchmod(
                target.fileno(), 0o555 if value["files"][name]["executable"] else 0o444
            )
            target.flush()
            os.fsync(target.fileno())
    for name, item in value["files"].items():
        if "link" in item:
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.symlink_to(item["link"])
    for name, item in value["files"].items():
        if "link" in item:
            resolved = (destination / name).resolve(strict=True)
            require(resolved == PYTHON or resolved.is_relative_to(destination))
    receipt = {
        "schema": SCHEMA,
        "source_revision": value["source_revision"],
        "archive_sha256": archive_sha,
        "worker_sha256": value["files"]["bin/dittobench-coding-hosted-worker"][
            "sha256"
        ],
        "manifest_sha256": hashlib.sha256(canonical(value)).hexdigest(),
        "shadow_only": True,
        "weight_eligible": False,
        "worker_started": False,
    }
    with (destination / "bundle-receipt.json").open("xb") as target:
        target.write(canonical(receipt))
        os.fchmod(target.fileno(), 0o444)
        target.flush()
        os.fsync(target.fileno())
    for directory, _dirs, _files in os.walk(
        destination, topdown=False, followlinks=False
    ):
        path = Path(directory)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o555)
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    verify_tree(value, destination, archive_sha)
    return receipt


def verify_tree(value, destination, archive_sha):
    protected(destination, owner=os.geteuid(), directory=True)
    require(stat.S_IMODE(destination.stat().st_mode) == 0o555)
    require(
        set(inventory(destination)) == set(value["files"]) | {"bundle-receipt.json"}
    )
    for name, item in value["files"].items():
        path = destination / name
        info = path.lstat()
        require(info.st_uid == os.geteuid())
        if "link" in item:
            require(path.is_symlink() and os.readlink(path) == item["link"])
            resolved = path.resolve(strict=True)
            require(resolved == PYTHON or resolved.is_relative_to(destination))
        else:
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            require(
                stat.S_IMODE(info.st_mode) == (0o555 if item["executable"] else 0o444)
            )
            require(info.st_size == item["size"] and file_hash(path) == item["sha256"])
    expected_dirs = {"."} | {
        str(parent) for name in value["files"] for parent in PurePosixPath(name).parents
    }
    for directory, _dirs, _files in os.walk(destination, followlinks=False):
        path = Path(directory)
        require(path.relative_to(destination).as_posix() in expected_dirs)
        require(
            path.stat().st_uid == os.geteuid()
            and stat.S_IMODE(path.stat().st_mode) == 0o555
        )
    receipt = destination / "bundle-receipt.json"
    protected(receipt, owner=os.geteuid(), directory=False)
    require(
        receipt.stat().st_size <= 4096 and stat.S_IMODE(receipt.stat().st_mode) == 0o444
    )
    expected = {
        "schema": SCHEMA,
        "source_revision": value["source_revision"],
        "archive_sha256": archive_sha,
        "worker_sha256": value["files"]["bin/dittobench-coding-hosted-worker"][
            "sha256"
        ],
        "manifest_sha256": hashlib.sha256(canonical(value)).hexdigest(),
        "shadow_only": True,
        "weight_eligible": False,
        "worker_started": False,
    }
    require(receipt.read_bytes() == canonical(expected))


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("mode", choices=["pack", "inspect", "install", "verify"])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None)
    root = BASE / args.revision
    if args.mode == "pack":
        require(args.confirm is None and args.sha256 is None)
        pack(root, args.archive, args.revision)
        return
    require(args.sha256 is not None)
    if args.mode in {"install", "verify"}:
        require(os.geteuid() == 0)
        require(
            args.confirm
            == ("INSTALL VERIFIED CODING RUNTIME" if args.mode == "install" else None)
        )
        for path in [BASE, *BASE.parents]:
            protected(path, owner=0, directory=True)
        for path in args.archive.parents:
            protected(path, owner=0, directory=True)
        if args.mode == "install":
            require(not root.exists() and not root.is_symlink())
    else:
        require(args.confirm is None)
    protected(args.archive, owner=os.geteuid(), directory=False)
    fd = os.open(
        args.archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    )
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o022
        )
        value, records = inspect(source, args.sha256, args.revision)
        if args.mode == "install":
            check_base(value)
            require(
                shutil.disk_usage(BASE).free
                >= sum(item.get("size", 0) for item in value["files"].values())
                + (256 << 20)
            )
            receipt = materialize(source, value, records, root, args.sha256)
        elif args.mode == "verify":
            check_base(value)
            verify_tree(value, root, args.sha256)
            receipt = {
                "source_revision": args.revision,
                "verified": True,
                "worker_started": False,
            }
        else:
            receipt = {
                "schema": SCHEMA,
                "source_revision": args.revision,
                "archive_sha256": args.sha256,
                "files": len(value["files"]),
                "worker_started": False,
                "host_compatibility_checked": False,
            }
        current = os.fstat(source.fileno())
        require(
            (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            == (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("native runtime bundle unavailable", file=sys.stderr)
        raise SystemExit(70) from None
