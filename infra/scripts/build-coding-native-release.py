#!/usr/bin/env python3
"""Build or reverify a public four-language release set; never approve or deploy.

Only tracked Git bytes enter builds. Existing image/runtime verifiers own their
formats. This index binds them together, not to a host or custodian signature.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    "python": "python-call-ast-v2",
    "node": "node-call-ast-v2",
    "go": "go-call-ast-v1",
    "rust": "rust-call-ast-v1",
}
MAX_INDEX = 64 << 10


def load_policy(name, role):
    path = ROOT / f"infra/ansible/roles/{role}/files/{name}-bundle.py"
    spec = importlib.util.spec_from_file_location(f"native_release_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMAGE = load_policy("image", "coding_hosted_image")
RUNTIME = load_policy("runtime", "coding_hosted_runtime")


def require(condition):
    if not condition:
        raise ValueError("native release set rejected")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def revision_valid(revision):
    require(isinstance(revision, str))
    require(re.fullmatch(r"[0-9a-f]{40}", revision) and revision != "0" * 40)


@contextlib.contextmanager
def artifact(path, limit):
    """Bounded regular, non-link public inputs in operator-controlled storage.

    Held descriptors and metadata rechecks detect ordinary concurrent drift.
    A malicious same-UID/root writer is outside this offline verifier's boundary.
    """
    require(path.is_absolute() and path.resolve() == path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
        require(0 < before.st_size <= limit and not before.st_mode & 0o022)
        yield stream
        after = os.fstat(fd)
        require(
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        )
        current = path.lstat()
        require((current.st_dev, current.st_ino) == (after.st_dev, after.st_ino))


def describe(directory, revision):
    """Fully rehash/reparse all five artifacts without extraction or execution."""
    revision_valid(revision)
    require(directory.is_absolute() and directory.resolve() == directory)
    images = {}
    for language, profile in PROFILES.items():
        prefix = directory / language
        with (
            artifact(prefix / "approval.json", IMAGE.MAX_JSON) as approval_file,
            artifact(prefix / "runtime.oci.tar", IMAGE.MAX_ARCHIVE) as stream,
        ):
            raw = approval_file.read(IMAGE.MAX_JSON + 1)
            approval = IMAGE.verify(stream, raw, sha(raw))
            require(approval["source_revision"] == revision)
            require(approval["driver_profile"] == profile)
            require(
                approval["image_ref"].split("@")[0]
                == f"coding-runtime.invalid/{language}/runtime"
            )
            images[language] = {
                "archive": f"{language}/runtime.oci.tar",
                "approval": f"{language}/approval.json",
                "approval_sha256": sha(raw),
                "archive_sha256": approval["archive_sha256"],
                "image_ref": approval["image_ref"],
                "config_digest": approval["config_digest"],
                "driver_profile": profile,
            }
    with artifact(directory / "native/runtime.tar", RUNTIME.MAX_ARCHIVE) as stream:
        checksum = RUNTIME.digest(stream, os.fstat(stream.fileno()).st_size)
        manifest, _ = RUNTIME.inspect(stream, checksum, revision)
    return {
        "schema": "dittobench-coding-native-release-set-v2",
        "source_revision": revision,
        "images": images,
        "runtime": {
            "archive": "native/runtime.tar",
            "archive_sha256": checksum,
            "manifest_sha256": sha(RUNTIME.canonical(manifest)),
            "worker_sha256": manifest["files"]["bin/dittobench-coding-hosted-worker"][
                "sha256"
            ],
            "python_sha256": manifest["python_sha256"],
            "debian_packages": manifest["debian_packages"],
        },
        "independent_approval_required": True,
        "native_imported": False,
        "runtime_qualification": False,
        "canary_completed": False,
        "shadow_only": True,
        "weight_eligible": False,
    }


def verify(directory, revision, expected_sha):
    require(
        isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha)
    )
    with artifact(directory / "release.json", MAX_INDEX) as stream:
        raw = stream.read(MAX_INDEX + 1)
        require(sha(raw) == expected_sha)
        value = IMAGE.json_object(raw)
        require(raw == IMAGE.json_bytes(value))
        # Comparing canonical bytes also distinguishes JSON booleans from 0/1.
        require(raw == IMAGE.json_bytes(describe(directory, revision)))
    return value


def check_source(revision):
    revision_valid(revision)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=30
    ).strip()
    require(head == revision)
    subprocess.run(
        ["git", "diff-index", "--quiet", "HEAD", "--"],
        cwd=ROOT,
        check=True,
        timeout=30,
    )


def build_image(language, revision, directory):
    require(language in PROFILES)
    with subprocess.Popen(
        ["git", "archive", "--format=tar", revision], cwd=ROOT, stdout=subprocess.PIPE
    ) as source:
        try:
            subprocess.run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--provenance=false",
                    "--target",
                    "runtime",
                    "--file",
                    f"services/dittobench-api/Dockerfile.coding-{language}",
                    "--build-arg",
                    f"DITTOBENCH_SOURCE_SHA={revision}",
                    "--output",
                    f"type=oci,dest={directory}/source.oci.tar",
                    "-",
                ],
                stdin=source.stdout,
                cwd=ROOT,
                check=True,
                timeout=3600,
            )
        finally:
            assert source.stdout is not None
            source.stdout.close()
        require(source.wait(timeout=30) == 0)
    IMAGE.prepare(
        directory / "source.oci.tar",
        directory / "runtime.oci.tar",
        directory / "approval.json",
        f"coding-runtime.invalid/{language}/runtime",
        revision,
    )


def build(output, revision):
    check_source(revision)
    require(
        output.is_absolute()
        and output.resolve() == output
        and not output.is_relative_to(ROOT)
        and not output.exists()
        and "," not in str(output)
        and not any(ord(c) < 32 for c in str(output))
    )
    output.mkdir(mode=0o700)
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "infra/scripts/build-coding-hosted-runtime.py"),
            "--revision",
            revision,
            "--output",
            str(output / "native"),
        ],
        cwd=ROOT,
        check=True,
        timeout=3600,
    )
    for language in PROFILES:
        directory = output / language
        directory.mkdir(mode=0o700)
        build_image(language, revision, directory)
    value = describe(output, revision)
    check_source(revision)
    raw = IMAGE.json_bytes(value)
    require(len(raw) <= MAX_INDEX)
    with (output / "release.json").open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return sha(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    builder = modes.add_parser("build")
    builder.add_argument("--output", type=Path, required=True)
    verifier = modes.add_parser("verify")
    verifier.add_argument("--directory", type=Path, required=True)
    verifier.add_argument("--manifest-sha256", required=True)
    for child in (builder, verifier):
        child.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    if args.mode == "build":
        checksum = build(args.output, args.revision)
    else:
        verify(args.directory, args.revision, args.manifest_sha256)
        checksum = args.manifest_sha256
    print(
        json.dumps(
            {
                "release_manifest_sha256": checksum,
                "independent_approval_required": True,
                "runtime_qualification": False,
                "weight_eligible": False,
            }
        )
    )


if __name__ == "__main__":
    os.umask(0o077)
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
            "native release rejected; partial public artifacts retained for review",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
