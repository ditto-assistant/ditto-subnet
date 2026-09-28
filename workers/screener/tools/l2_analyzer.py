#!/usr/bin/env python3
"""Inert, allowlisted source-navigation tools for the SOL L2 reviewer.

The analyzer runs inside a no-network, read-only, non-root container.  It never
builds or executes submission code.  Requests and responses are JSON on stdin /
stdout so the host can keep the model gateway and all credentials outside the
container.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(os.environ.get("L2_ANALYZER_ROOT", "/workspace"))
MANIFESTS = Path(os.environ.get("L2_ANALYZER_MANIFESTS", "/opt/starter-manifests"))
MAX_FILES = 512
MAX_WALK_ENTRIES = 1_024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIGEST_BYTES = 20 * 1024 * 1024
MAX_OUTPUT = 256_000
MAX_INTEGRITY_HITS_PER_SURFACE = 32
STARTER_MODEL = "fixtures/models/cross-encoder.onnx"
INTEGRITY_SURFACES = {
    "service_entry": re.compile(
        r"(?:/run|/seed|process_run|do_post|route\s*\(|router|handler|serve)", re.I
    ),
    "model_authority": re.compile(
        r"(?:chat|completion|responses?|gateway|openai|ollama|inference|model)", re.I
    ),
    "tool_authority": re.compile(
        r"(?:tool_calls?|tool_endpoint|call_id|execute|dispatch|hop|arguments?)", re.I
    ),
    "answer_contract": re.compile(
        r"(?:final_text|abstain|answer|prompt_tokens|output_tokens|latency_ms)", re.I
    ),
    "benchmark_or_score": re.compile(
        r"(?:benchmark|datagen|generator|grader|scor(?:e|er|ing)|canary|expected)", re.I
    ),
    # Keep generator-construction anchors separate from the broader benchmark
    # surface. Scorer/canary-heavy files can otherwise exhaust the bounded hit
    # sample before the model sees template, distribution, or seeded-expansion
    # definitions needed to distinguish mirroring from a finite answer engine.
    "generator_construction": re.compile(
        r"(?:datagen|generator|template|grammar|seed(?:ed)?|random|distribution|"
        r"expected[_ -]?(?:answer|output|value)|(?:value|option|attribute)[_ -]?pool)",
        re.I,
    ),
    "identity_scope": re.compile(
        r"(?:user_id|tenant|account|owner|subject|cross.user|global.user)", re.I
    ),
    "host_or_secret": re.compile(
        r"(?:credential|secret|metadata|docker\.sock|host\.docker|os\.environ|getenv|env::var)",
        re.I,
    ),
    "mutation_or_fallback": re.compile(
        r"(?:replace|override|fallback|inject|scrub|suppress|omit|rewrite|fabricat)",
        re.I,
    ),
}


def _emit(value: object) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_OUTPUT:
        encoded = json.dumps(
            {
                "error": "analyzer-output-truncated",
                "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    sys.stdout.write(encoded)


def _request() -> dict[str, object]:
    raw = sys.stdin.buffer.read(64_001)
    if len(raw) > 64_000:
        raise ValueError("request exceeds analyzer input cap")
    value = json.loads(raw or b"{}")
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    return value


def _files_with_truncation() -> tuple[list[Path], bool]:
    files: list[Path] = []
    directories = [ROOT]
    visited = 0
    while directories:
        directory = directories.pop()
        children: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_WALK_ENTRIES:
                        return sorted(files), True
                    if entry.name == ".git" or entry.is_symlink():
                        continue
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        children.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if len(files) >= MAX_FILES:
                            return sorted(files), True
                        files.append(path)
        except OSError:
            return sorted(files), True
        directories.extend(sorted(children, reverse=True))
    return sorted(files), False


def _files() -> list[Path]:
    return _files_with_truncation()[0]


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _resolve(raw: object) -> Path:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 240:
        raise ValueError("path is invalid")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise ValueError("path escapes workspace")
    path = ROOT.joinpath(*pure.parts)
    if not path.is_file() or path.is_symlink():
        raise ValueError("path is not a regular workspace file")
    path.resolve().relative_to(ROOT.resolve())
    return path


def _bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file exceeds analyzer read cap")
    return path.read_bytes()


def _text(path: Path) -> str:
    return _bytes(path).decode("utf-8")


def workspace_index(_: dict[str, object]) -> object:
    result = []
    files, truncated = _files_with_truncation()
    omitted: list[dict[str, object]] = []
    for path in files:
        size = path.stat().st_size
        raw = _bytes(path) if size <= MAX_FILE_BYTES else None
        if size > MAX_DIGEST_BYTES:
            omitted.append({"path": _relative(path), "reason": "digest_cap"})
        result.append(
            {
                "path": _relative(path),
                "bytes": size,
                "sha256": _file_sha256(path) if size <= MAX_DIGEST_BYTES else None,
                "text": _is_text(raw) if raw is not None else None,
            }
        )
    return {
        "files": result,
        "omitted": omitted[:32],
        "omitted_count": len(omitted),
        "truncated": truncated or bool(omitted),
    }


def read_file(request: dict[str, object]) -> object:
    path = _resolve(request.get("path"))
    start = request.get("start_line", 1)
    end = request.get("end_line", 240)
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 1 <= start <= end
        or end - start > 399
    ):
        raise ValueError("line range is invalid")
    lines = _text(path).splitlines()
    selected = [
        f"{number}:{lines[number - 1]}"
        for number in range(start, min(end, len(lines)) + 1)
    ]
    content = "\n".join(selected)
    return {
        "path": _relative(path),
        "sha256": hashlib.sha256(_bytes(path)).hexdigest(),
        "start_line": start,
        "end_line": min(end, len(lines)),
        "content": content[:48_000],
        "truncated": len(content) > 48_000,
    }


def search(request: dict[str, object]) -> object:
    query = request.get("query")
    prefix = request.get("prefix", "")
    if not isinstance(query, str) or not 2 <= len(query) <= 160:
        raise ValueError("query is invalid")
    if not isinstance(prefix, str) or len(prefix) > 160:
        raise ValueError("prefix is invalid")
    hits = []
    omitted: list[dict[str, object]] = []
    nontext: list[dict[str, object]] = []
    starter_models = _starter_model_digests()
    needle = query.casefold()
    files, workspace_truncated = _files_with_truncation()
    for path in files:
        relative = _relative(path)
        if prefix and not relative.startswith(prefix):
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            model = _starter_model(path, relative, size, starter_models)
            if model is not None:
                nontext.append(model)
                continue
            omitted.append({"path": relative, "reason": "read_cap"})
            continue
        try:
            lines = _text(path).splitlines()
        except (UnicodeDecodeError, ValueError):
            continue
        for number, line in enumerate(lines, 1):
            if needle in line.casefold():
                hits.append({"path": relative, "line": number})
                if len(hits) >= 120:
                    return {
                        "hits": hits,
                        "omitted": omitted[:32],
                        "omitted_count": len(omitted),
                        "nontext": nontext,
                        "nontext_count": len(nontext),
                        "truncated": True,
                    }
    return {
        "hits": hits,
        "omitted": omitted[:32],
        "omitted_count": len(omitted),
        "nontext": nontext,
        "nontext_count": len(nontext),
        "truncated": workspace_truncated or bool(omitted),
    }


def _starter_manifests() -> list[dict[str, object]]:
    manifests: list[dict[str, object]] = []
    for path in sorted(MANIFESTS.glob("starter-kit-provenance-*.json")):
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            continue
        revision = str(payload.get("revision", ""))
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            continue
        manifests.append(payload)
    if not manifests:
        raise ValueError("no starter manifests are installed")
    return manifests


def _starter_model_digests() -> set[str]:
    try:
        return {
            str(manifest["files"][STARTER_MODEL])
            for manifest in _starter_manifests()
            if isinstance(manifest.get("files"), dict)
            and STARTER_MODEL in manifest["files"]
        }
    except (OSError, ValueError):
        return set()


def _starter_model(
    path: Path, relative: str, size: int, digests: set[str]
) -> dict[str, object] | None:
    """Account for an over-cap file only as the exact published starter model."""
    if relative != STARTER_MODEL or size > MAX_DIGEST_BYTES:
        return None
    try:
        digest = _file_sha256(path)
    except OSError:
        return None
    if digest not in digests:
        return None
    return {
        "path": relative,
        "bytes": size,
        "sha256": digest,
        "provenance": "starter_manifest_digest",
    }


def _workspace_digests() -> tuple[dict[str, str], list[dict[str, object]], bool]:
    actual: dict[str, str] = {}
    files, truncated = _files_with_truncation()
    omitted: list[dict[str, object]] = []
    for path in files:
        if path.stat().st_size <= MAX_DIGEST_BYTES:
            actual[_relative(path)] = _file_sha256(path)
        else:
            omitted.append({"path": _relative(path), "reason": "digest_cap"})
    return actual, omitted, truncated


def _starter_delta(
    manifest: dict[str, object], actual: dict[str, str]
) -> tuple[list[str], list[str], list[str], list[str]]:
    raw_expected = manifest.get("files")
    if not isinstance(raw_expected, dict):
        raise ValueError("starter manifest file index is invalid")
    expected = {str(path): str(digest) for path, digest in raw_expected.items()}
    unchanged = sorted(
        path for path, digest in actual.items() if expected.get(path) == digest
    )
    modified = sorted(
        path
        for path, digest in actual.items()
        if path in expected and expected[path] != digest
    )
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    return unchanged, modified, added, removed


def _select_starter_manifest(
    actual: dict[str, str],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    ranked: list[
        tuple[
            int,
            str,
            dict[str, object],
            tuple[list[str], list[str], list[str], list[str]],
        ]
    ] = []
    for manifest in _starter_manifests():
        revision = str(manifest.get("revision", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("starter manifest revision is invalid")
        delta = _starter_delta(manifest, actual)
        changed = sum(len(items) for items in delta[1:])
        ranked.append((changed, revision, manifest, delta))
    ranked.sort(key=lambda item: (item[0], item[1]))
    candidates = [
        {"revision": revision, "changed_file_count": changed}
        for changed, revision, _manifest, _delta in ranked
    ]
    return ranked[0][2], candidates


def starter_diff(_: dict[str, object]) -> object:
    actual, omitted, truncated = _workspace_digests()
    manifest, candidates = _select_starter_manifest(actual)
    unchanged, modified, added, removed = _starter_delta(manifest, actual)
    return {
        "origin": manifest["origin"],
        "revision": manifest["revision"],
        "selection": "minimum_file_delta",
        "candidates": candidates,
        "unchanged": unchanged,
        "modified": modified,
        "added": added,
        "removed": removed,
        "omitted": omitted[:32],
        "omitted_count": len(omitted),
        "truncated": truncated or bool(omitted),
    }


def build_structure(_: dict[str, object]) -> object:
    result: dict[str, object] = {}
    for name in (
        "Dockerfile",
        "Cargo.toml",
        "Cargo.lock",
        "build.rs",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "go.mod",
        "go.sum",
    ):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            result[name] = {"bytes": path.stat().st_size, "omitted": "read_cap"}
            continue
        raw = _bytes(path)
        entry_result: dict[str, object] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        result[name] = entry_result
        if name == "Dockerfile":
            lines = raw.decode("utf-8").splitlines()
            instructions = [
                {"line": number, "kind": line.strip().split(maxsplit=1)[0].upper()}
                for number, line in enumerate(lines, 1)
                if line.strip() and not line.lstrip().startswith("#")
            ]
            entry_result["instructions"] = instructions[:160]
            entry_result["instructions_truncated"] = len(instructions) > 160
    return result


def integrity_surfaces(_: dict[str, object]) -> object:
    """Return a snippet-free attention map for independent contract review.

    These locations are routing hints, never policy evidence. The model must read
    and causally trace any relevant locations before reaching a disposition.
    A published starter digest proves byte identity, not runtime role.
    """
    grouped: dict[str, list[dict[str, object]]] = {
        name: [] for name in INTEGRITY_SURFACES
    }
    counts: dict[str, int] = dict.fromkeys(INTEGRITY_SURFACES, 0)
    files, workspace_truncated = _files_with_truncation()
    omitted: list[dict[str, object]] = []
    nontext: list[dict[str, object]] = []
    starter_models = _starter_model_digests()
    for path in files:
        relative = _relative(path)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            model = _starter_model(path, relative, size, starter_models)
            if model is not None:
                nontext.append(model)
                continue
            omitted.append(
                {
                    "path": relative,
                    "reason": "digest_cap" if size > MAX_DIGEST_BYTES else "read_cap",
                }
            )
            continue
        try:
            raw = _bytes(path)
            if not _is_text(raw):
                continue
            lines = raw.decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError, ValueError):
            omitted.append({"path": relative, "reason": "read_error"})
            continue
        for number, line in enumerate(lines, 1):
            for name, pattern in INTEGRITY_SURFACES.items():
                terms = sorted(
                    {match.group(0).casefold() for match in pattern.finditer(line)}
                )
                if not terms:
                    continue
                counts[name] += 1
                if len(grouped[name]) < MAX_INTEGRITY_HITS_PER_SURFACE:
                    grouped[name].append(
                        {"path": relative, "line": number, "terms": terms[:4]}
                    )
    return {
        "surfaces": {
            name: {
                "hits": grouped[name],
                "count": counts[name],
                "sampled": counts[name] > len(grouped[name]),
            }
            for name in INTEGRITY_SURFACES
        },
        "omitted": omitted[:32],
        "omitted_count": len(omitted),
        "nontext": nontext[:32],
        "nontext_count": len(nontext),
        "truncated": workspace_truncated or bool(omitted) or len(nontext) > 32,
    }


def sandbox_probe(_: dict[str, object]) -> object:
    """Integration-only proof of the container boundary; not model-allowlisted."""
    workspace_writable = True
    try:
        (ROOT / ".write-probe").write_text("blocked")
    except OSError:
        workspace_writable = False
    scratch = Path("/scratch/write-probe")
    scratch.write_text("ok")
    egress = True
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=0.2).close()
    except OSError:
        egress = False
    return {
        "uid": __import__("os").getuid(),
        "gid": __import__("os").getgid(),
        "workspace_writable": workspace_writable,
        "scratch_writable": scratch.read_text() == "ok",
        "egress": egress,
        "docker_socket": _safe_exists(Path("/var/run/docker.sock")),
        "cloud_paths": any(
            _safe_exists(path)
            for path in (
                Path("/root/.config/gcloud"),
                Path("/home/analyzer/.aws"),
                Path("/var/run/secrets"),
            )
        ),
    }


def _is_text(raw: bytes) -> bool:
    if b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


COMMANDS = {
    "workspace_index": workspace_index,
    "read_file": read_file,
    "search": search,
    "starter_diff": starter_diff,
    "build_structure": build_structure,
    "integrity_surfaces": integrity_surfaces,
    "sandbox_probe": sandbox_probe,
}


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
            raise ValueError("unsupported analyzer command")
        _emit(COMMANDS[sys.argv[1]](_request()))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        _emit({"error": type(error).__name__, "message": str(error)[:240]})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
