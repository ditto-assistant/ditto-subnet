"""Local replica of the production Targon/Kaniko miner-screen archive contract.

Production Kaniko (``ditto-submission-builder``) builds a miner Dockerfile
with:

    --destination=ditto-screen/{agent}-{attempt}:latest
    --no-push --tar-path=/workspace/image.tar

Platform binds ``screened_image_id`` to the docker-save **config** digest
(not Kaniko ``--digest-file`` / Artifact Registry manifest digest) and
``screened_image_ref`` to the stable ``ditto-screen/{agent}:latest``.
DittoBench accepts empty tags, that stable ref, or the attempt-scoped
destination. Pinning the wrong digest is how scoring jammed on Kaniko tars.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any, Literal

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_MANIFEST_BYTES = 1 << 20
_MAX_CONFIG_BYTES = 4 << 20
_SOURCE_EXCLUDE_DIR_NAMES = {
    ".git",
    "__pycache__",
    "target",
    "node_modules",
    ".pytest_cache",
}


def scoring_ref(agent_id: str) -> str:
    if _UUID.fullmatch(agent_id) is None:
        raise ValueError("agent_id must be a UUID")
    return f"ditto-screen/{agent_id}:latest"


def kaniko_destination(agent_id: str, attempt_id: str) -> str:
    if _UUID.fullmatch(agent_id) is None or _UUID.fullmatch(attempt_id) is None:
        raise ValueError("agent_id and attempt_id must be UUIDs")
    return f"ditto-screen/{agent_id}-{attempt_id}:latest"


def starter_kit_rental_script(
    *,
    source_sha: str,
    agent_id: str,
    attempt_id: str,
) -> str:
    """Busybox script for a live Kaniko rental of the starter-kit harness."""
    if len(source_sha) != 40 or any(c not in "0123456789abcdef" for c in source_sha):
        raise ValueError("source_sha must be a 40-character lowercase git SHA")
    destination = kaniko_destination(agent_id, attempt_id)
    archive = (
        "https://github.com/ditto-assistant/ditto-subnet/archive/"
        f"{source_sha}.tar.gz"
    )
    context = f"/workspace/src/ditto-subnet-{source_sha}/miners/dittobench-starter-kit"
    return (
        "set -eu; "
        "mkdir -p /workspace/src; "
        f"/busybox/wget -qO /workspace/source.tar.gz {archive}; "
        "/busybox/tar -xzf /workspace/source.tar.gz -C /workspace/src; "
        "/kaniko/executor "
        f"--context=dir://{context} --dockerfile=Dockerfile "
        f"--destination={destination} --no-push --no-push-cache --cache=false "
        "--tar-path=/workspace/image.tar --verbosity=info; "
        "test -s /workspace/image.tar; "
        "echo DITTO_SCREEN_DESTINATION="
        f"{destination}; "
        "echo DITTO_SCREEN_MANIFEST_BEGIN; "
        "/busybox/tar -xOf /workspace/image.tar manifest.json; "
        "echo; echo DITTO_SCREEN_MANIFEST_END; "
        "echo KANIKO_STARTER_PROBE_AVAILABLE; "
        "sleep 600"
    )


def parse_starter_kit_probe_logs(logs: str) -> dict[str, Any]:
    destination = ""
    for line in logs.splitlines():
        if line.startswith("DITTO_SCREEN_DESTINATION="):
            destination = line.split("=", 1)[1].strip()
    begin = "DITTO_SCREEN_MANIFEST_BEGIN"
    end = "DITTO_SCREEN_MANIFEST_END"
    if begin not in logs or end not in logs:
        raise ValueError("starter-kit probe logs are missing the docker-save manifest")
    raw = logs.split(begin, 1)[1].split(end, 1)[0].strip()
    manifest = json.loads(raw)
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError("archive must contain exactly one image")
    entry = manifest[0]
    if not isinstance(entry, dict):
        raise ValueError("manifest.json is invalid")
    tags = entry.get("RepoTags") or []
    config = entry.get("Config")
    return {
        "destination": destination,
        "repo_tags": tags,
        "config": config,
        "ok": (
            isinstance(tags, list)
            and tags == [destination]
            and isinstance(config, str)
            and bool(config)
            and "KANIKO_STARTER_PROBE_AVAILABLE" in logs
        ),
    }


def kaniko_argv(*, destination: str) -> list[str]:
    """Exact miner-build flags production Targon Rentals pass to Kaniko."""
    return [
        "/kaniko/executor",
        "--context=tar:///workspace/source.tar.gz",
        "--dockerfile=Dockerfile",
        f"--destination={destination}",
        "--no-push",
        "--no-push-cache",
        "--cache=false",
        "--tar-path=/workspace/image.tar",
        "--digest-file=/workspace/manifest-digest",
        "--verbosity=info",
    ]


def pack_source_tar(context: Path, output: Path) -> str:
    """Write a gzip miner source tar with Dockerfile at the archive root."""
    context = context.resolve()
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile missing under {context}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(context.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(context)
            if any(part in _SOURCE_EXCLUDE_DIR_NAMES for part in relative.parts):
                continue
            info = archive.gettarinfo(str(path), arcname=str(relative))
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            data = path.read_bytes()
            info.size = len(data)
            archive.addfile(info, fileobj=io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return digest


def inspect_docker_save(path: Path) -> dict[str, Any]:
    """Read RepoTags and the content-addressed config digest from a docker-save."""
    last_error: str | None = None
    modes: tuple[Literal["r:", "r:gz"], ...] = ("r:", "r:gz")
    for mode in modes:
        try:
            return _inspect(path, mode)
        except (OSError, tarfile.TarError, UnicodeError) as error:
            last_error = str(error)
            continue
    raise ValueError(last_error or "archive is not a docker-save tar")


def _inspect(path: Path, mode: Literal["r:", "r:gz"]) -> dict[str, Any]:
    hashed: dict[str, str] = {}
    manifest: object | None = None
    with tarfile.open(path, mode=mode) as archive:
        for info in archive:
            name = str(info.name).lstrip("./")
            if not info.isfile():
                continue
            if name == "manifest.json":
                if info.size <= 0 or info.size > _MAX_MANIFEST_BYTES:
                    raise ValueError("manifest.json has invalid size")
                raw = archive.extractfile(info)
                if raw is None:
                    raise ValueError("manifest.json is unreadable")
                manifest = json.loads(raw.read(info.size))
                continue
            digest_hex = named_config_digest(name)
            if digest_hex is None or info.size <= 0 or info.size > _MAX_CONFIG_BYTES:
                continue
            handle = archive.extractfile(info)
            if handle is None:
                continue
            payload = handle.read(info.size + 1)
            if len(payload) != info.size:
                raise ValueError("image config truncated")
            if hashlib.sha256(payload).hexdigest() == digest_hex:
                hashed[name] = digest_hex
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError("archive must contain exactly one image")
    entry = manifest[0]
    if not isinstance(entry, dict):
        raise ValueError("manifest.json is invalid")
    config_name = entry.get("Config")
    if not isinstance(config_name, str):
        raise ValueError("manifest Config is missing")
    digest_hex = hashed.get(config_name.lstrip("./"))
    if digest_hex is None:
        raise ValueError("docker-save config digest is missing")
    tags = entry.get("RepoTags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("manifest RepoTags is invalid")
    return {
        "config": config_name.lstrip("./"),
        "image_id": "sha256:" + digest_hex,
        "repo_tags": list(tags),
    }


def named_config_digest(name: str) -> str | None:
    name = name.lstrip("./")
    if name.endswith(".json"):
        stem = name[: -len(".json")]
        if "/" not in stem and _HEX64.fullmatch(stem):
            return stem
    blob_prefix = "blobs/sha256/"
    if name.startswith(blob_prefix):
        digest = name[len(blob_prefix) :]
        if _HEX64.fullmatch(digest):
            return digest
    # go-containerregistry (Kaniko --tar-path) names the config "sha256:<hex>".
    algo_prefix = "sha256:"
    if name.startswith(algo_prefix) and "/" not in name:
        digest = name[len(algo_prefix) :]
        if _HEX64.fullmatch(digest):
            return digest
    return None


def validate_screened_archive(
    *,
    path: Path,
    agent_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    expected_scoring = scoring_ref(agent_id)
    expected_destination = kaniko_destination(agent_id, attempt_id)
    inspected = inspect_docker_save(path)
    tags = inspected["repo_tags"]
    errors: list[str] = []
    if tags not in ([], [expected_scoring], [expected_destination]):
        errors.append(
            "RepoTags must be empty, the stable agent ref, or the Kaniko "
            f"attempt destination {expected_destination}; got {tags}"
        )
    result = {
        "agent_id": agent_id,
        "attempt_id": attempt_id,
        "scoring_ref": expected_scoring,
        "kaniko_destination": expected_destination,
        "kaniko_argv": kaniko_argv(destination=expected_destination),
        "image_id": inspected["image_id"],
        "repo_tags": tags,
        "ok": not errors,
        "errors": errors,
    }
    return result


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pack miner source or validate a Targon/Kaniko docker-save tar"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pack = sub.add_parser(
        "pack", help="write a gzip source tar with Dockerfile at root"
    )
    pack.add_argument("--context", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser(
        "validate", help="check a docker-save tar against the scoring identity contract"
    )
    validate.add_argument("--image-tar", type=Path, required=True)
    validate.add_argument("--agent-id", required=True)
    validate.add_argument("--attempt-id", required=True)
    ids = sub.add_parser(
        "ids", help="print the production Kaniko destination and scoring ref"
    )
    ids.add_argument("--agent-id", default=str(uuid.uuid4()))
    ids.add_argument("--attempt-id", default=str(uuid.uuid4()))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "pack":
        digest = pack_source_tar(args.context, args.output)
        print(json.dumps({"source_sha256": digest, "output": str(args.output)}))
        return 0
    if args.command == "ids":
        destination = kaniko_destination(args.agent_id, args.attempt_id)
        print(
            json.dumps(
                {
                    "agent_id": args.agent_id,
                    "attempt_id": args.attempt_id,
                    "scoring_ref": scoring_ref(args.agent_id),
                    "kaniko_destination": destination,
                    "kaniko_argv": kaniko_argv(destination=destination),
                },
                indent=2,
            )
        )
        return 0
    result = validate_screened_archive(
        path=args.image_tar,
        agent_id=args.agent_id,
        attempt_id=args.attempt_id,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
