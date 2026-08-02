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
from typing import cast

import tree_sitter_rust
from tree_sitter import Language, Node, Parser

ROOT = Path("/workspace")
MANIFESTS = Path("/opt/starter-manifests")
RUST_LANGUAGE = Language(tree_sitter_rust.language())
MAX_FILES = 512
MAX_WALK_ENTRIES = 1_024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIGEST_BYTES = 20 * 1024 * 1024
MAX_OUTPUT = 256_000
MAX_AST_NODES = 250_000
MAX_FUNCTIONS = 1_000
MAX_CALLS_PER_FUNCTION = 512
MAX_ROUTE_CALLS = 512
MAX_GRAPH_NODES = 1_000
MAX_GRAPH_SAMPLES = 300
MAX_INTEGRITY_HITS_PER_SURFACE = 32
MAX_SCORER_FLOWS = 48
MAX_FUNCTION_DIFFS = 256
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
}

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

SCORED_FIELD = re.compile(
    r"(?:^|_)(?:answer|abstain|final_text|tool_calls?)(?:$|_)", re.I
)
SCORE_CONTROL = re.compile(
    r"(?:score|scoring|scorer|a[_-]?b|ab[_-]?(?:test|score|result)|on[_-]?chain|"
    r"canary|composite|leaderboard|benchmark|grader)",
    re.I,
)
CLEAR_VALUE = re.compile(
    r"^(?:(?:[A-Za-z0-9_:]+::)?None|Default::default\s*\(|String::new\s*\(|Vec::new\s*\(|"
    r"serde_json::Value::Null|null\b|\"\"|false\b)",
    re.I,
)


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
    needle = query.casefold()
    files, workspace_truncated = _files_with_truncation()
    for path in files:
        relative = _relative(path)
        if prefix and not relative.startswith(prefix):
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
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
                        "truncated": True,
                    }
    return {
        "hits": hits,
        "omitted": omitted[:32],
        "omitted_count": len(omitted),
        "truncated": workspace_truncated or bool(omitted),
    }


def _call_tail(target: str) -> str:
    return target.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def _file_module_path(path: Path) -> list[str]:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts and parts[-1] in {"lib", "main", "mod"}:
        parts.pop()
    return ["crate", *parts]


def _function_module_path(source: bytes, path: Path, node: Node) -> str:
    scopes: list[str] = []
    parent = node.parent
    while parent is not None:
        field = None
        if parent.type == "mod_item":
            field = parent.child_by_field_name("name")
        elif parent.type == "impl_item":
            field = parent.child_by_field_name("type")
        if field is not None:
            value = _node_text(source, field).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                scopes.append(value)
        parent = parent.parent
    return "::".join([*_file_module_path(path), *reversed(scopes)])


def _entry_candidates(
    entry: str, definitions: list[dict[str, object]]
) -> list[dict[str, object]]:
    if "::" not in entry:
        return [item for item in definitions if item["name"] == entry]
    qualified = entry if entry.startswith("crate::") else f"crate::{entry}"
    return [item for item in definitions if item["qualified_name"] == qualified]


def _call_candidates(
    target: str,
    caller: dict[str, object],
    definitions: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*", target):
        return []
    module = str(caller["module_path"])
    if "::" not in target:
        local = [
            item
            for item in definitions
            if item["module_path"] == module and item["name"] == target
        ]
        if local:
            return local
        return []
    if target.startswith("crate::"):
        qualified = target
    elif target.startswith("self::"):
        qualified = f"{module}::{target.removeprefix('self::')}"
    elif target.startswith("super::"):
        parent = module.rsplit("::", 1)[0] if "::" in module else module
        qualified = f"{parent}::{target.removeprefix('super::')}"
    else:
        local_qualified = f"{module}::{target}"
        root = f"crate::{target}"
        return [
            item
            for item in definitions
            if item["qualified_name"] in {local_qualified, root}
        ]
    return [item for item in definitions if item["qualified_name"] == qualified]


def rust_structure(request: dict[str, object]) -> object:
    path = _resolve(request.get("path"))
    if path.suffix != ".rs":
        raise ValueError("rust_structure requires a .rs file")
    raw = _bytes(path)
    tree = Parser(RUST_LANGUAGE).parse(raw)
    functions: list[dict[str, object]] = []
    routes: list[int] = []
    nodes, ast_truncated = _walk(tree.root_node)
    calls_truncated = False
    for node in nodes:
        if node.type != "function_item":
            continue
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        calls: dict[str, int] = {}
        body_nodes, body_truncated = _walk(body)
        ast_truncated = ast_truncated or body_truncated
        for child in body_nodes:
            if child.type != "call_expression":
                continue
            function = child.child_by_field_name("function")
            if function is None:
                continue
            called = _node_text(raw, function).strip()
            if 1 <= len(called) <= 240:
                calls.setdefault(called, child.start_point[0] + 1)
            if _call_tail(called) in {"route", "service", "nest"}:
                routes.append(child.start_point[0] + 1)
        calls_truncated = calls_truncated or len(calls) > MAX_CALLS_PER_FUNCTION
        name = _node_text(raw, name_node)
        module_path = _function_module_path(raw, path, node)
        functions.append(
            {
                "id": f"{_relative(path)}:{node.start_point[0] + 1}:{name}",
                "name": name,
                "module_path": module_path,
                "qualified_name": f"{module_path}::{name}",
                "line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "calls": [
                    {"target": target, "line": line}
                    for target, line in sorted(calls.items())[:MAX_CALLS_PER_FUNCTION]
                ],
            }
        )
    return {
        "path": _relative(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "ast_has_error": tree.root_node.has_error,
        "functions": functions[:MAX_FUNCTIONS],
        "route_call_lines": routes[:MAX_ROUTE_CALLS],
        "ast_truncated": ast_truncated,
        "functions_truncated": len(functions) > MAX_FUNCTIONS,
        "calls_truncated": calls_truncated,
        "routes_truncated": len(routes) > MAX_ROUTE_CALLS,
    }


def call_graph(request: dict[str, object]) -> object:
    entry = request.get("entry", "main")
    if not isinstance(entry, str) or not re.fullmatch(
        r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*", entry
    ):
        raise ValueError("entry is invalid")
    definitions: list[dict[str, object]] = []
    files, workspace_truncated = _files_with_truncation()
    analysis_truncated = workspace_truncated
    for path in files:
        if path.suffix != ".rs":
            continue
        structure = rust_structure({"path": _relative(path)})
        assert isinstance(structure, dict)
        analysis_truncated = analysis_truncated or any(
            structure.get(key) is True
            for key in (
                "ast_truncated",
                "functions_truncated",
                "calls_truncated",
                "routes_truncated",
            )
        )
        for function in structure["functions"]:
            assert isinstance(function, dict)
            definitions.append({"path": _relative(path), **function})
    by_id = {str(item["id"]): item for item in definitions}
    entry_candidates = _entry_candidates(entry, definitions)
    queue = [str(entry_candidates[0]["id"])] if len(entry_candidates) == 1 else []
    seen: set[str] = set()
    nodes: list[dict[str, object]] = []
    ambiguous_calls: list[dict[str, object]] = []
    unresolved_calls: list[dict[str, object]] = []
    while queue and len(nodes) < MAX_GRAPH_NODES:
        function_id = queue.pop(0)
        if function_id in seen:
            continue
        seen.add(function_id)
        definition = by_id[function_id]
        nodes.append(definition)
        calls = definition.get("calls", [])
        assert isinstance(calls, list)
        for call in calls:
            assert isinstance(call, dict)
            target = str(call["target"])
            candidates = _call_candidates(target, definition, definitions)
            location = {
                "caller": function_id,
                "path": definition["path"],
                "line": call["line"],
                "target": target,
            }
            if len(candidates) == 1:
                candidate_id = str(candidates[0]["id"])
                if candidate_id not in seen:
                    queue.append(candidate_id)
            elif len(candidates) > 1:
                ambiguous_calls.append(
                    {
                        **location,
                        "candidates": [str(item["id"]) for item in candidates],
                    }
                )
            else:
                unresolved_calls.append(location)
    reachable_truncated = bool(queue)
    return {
        "entry": entry,
        "nodes": nodes,
        "unresolved": not entry_candidates,
        "entry_ambiguous": len(entry_candidates) > 1,
        "ambiguous_calls": ambiguous_calls[:MAX_GRAPH_SAMPLES],
        "ambiguous_count": len(ambiguous_calls),
        "ambiguous_sampled": len(ambiguous_calls) > MAX_GRAPH_SAMPLES,
        "unresolved_calls": unresolved_calls[:MAX_GRAPH_SAMPLES],
        "unresolved_count": len(unresolved_calls),
        "unresolved_sampled": len(unresolved_calls) > MAX_GRAPH_SAMPLES,
        "definition_count": len(definitions),
        "analysis_truncated": analysis_truncated,
        "reachable_truncated": reachable_truncated,
        "truncated": analysis_truncated or reachable_truncated,
    }


def _starter_manifests() -> list[dict[str, object]]:
    manifests = [
        json.loads(path.read_text()) for path in sorted(MANIFESTS.glob("*.json"))
    ]
    if not manifests:
        raise ValueError("no starter manifests are installed")
    return manifests


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


def _workspace_rust_functions() -> tuple[
    dict[tuple[str, str, int], dict[str, object]], bool
]:
    result: dict[tuple[str, str, int], dict[str, object]] = {}
    files, truncated = _files_with_truncation()
    for path in files:
        if path.suffix != ".rs":
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            truncated = True
            continue
        raw = _bytes(path)
        tree = Parser(RUST_LANGUAGE).parse(raw)
        if tree.root_node.has_error:
            truncated = True
        nodes, node_truncated = _walk(tree.root_node)
        truncated = truncated or node_truncated
        ordinals: dict[str, int] = {}
        for node in (item for item in nodes if item.type == "function_item"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                truncated = True
                continue
            name = _node_text(raw, name_node)
            ordinal = ordinals.get(name, 0)
            ordinals[name] = ordinal + 1
            key = (_relative(path), name, ordinal)
            result[key] = {
                "path": key[0],
                "name": name[:120],
                "ordinal": ordinal,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "sha256": hashlib.sha256(
                    raw[node.start_byte : node.end_byte]
                ).hexdigest(),
            }
    return result, truncated


def starter_function_diff(_: dict[str, object]) -> object:
    """Return changed Rust functions versus the closest supported starter."""
    workspace_digests, _omitted, _workspace_truncated = _workspace_digests()
    manifest, candidates = _select_starter_manifest(workspace_digests)
    raw_expected = manifest.get("rust_functions")
    if not isinstance(raw_expected, list):
        raise ValueError("starter manifest has no Rust function index")
    expected: dict[tuple[str, str, int], str] = {}
    for item in raw_expected:
        if not isinstance(item, dict):
            raise ValueError("starter function index is invalid")
        key = (str(item["path"]), str(item["name"]), int(item["ordinal"]))
        digest = str(item["sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("starter function digest is invalid")
        expected[key] = digest
    actual, truncated = _workspace_rust_functions()
    modified = [
        value
        for key, value in actual.items()
        if key in expected and value["sha256"] != expected[key]
    ]
    added = [value for key, value in actual.items() if key not in expected]
    removed = [
        {"path": path, "name": name, "ordinal": ordinal}
        for path, name, ordinal in sorted(set(expected) - set(actual))
    ]
    sampled = any(
        len(values) > MAX_FUNCTION_DIFFS for values in (modified, added, removed)
    )

    def bounded(values: list[dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            values,
            key=lambda item: (
                str(item["path"]),
                str(item["name"]),
                cast(int, item["ordinal"]),
            ),
        )[:MAX_FUNCTION_DIFFS]

    return {
        "origin": manifest["origin"],
        "revision": manifest["revision"],
        "selection": "minimum_file_delta",
        "candidates": candidates,
        "unchanged_count": sum(
            key in expected and value["sha256"] == expected[key]
            for key, value in actual.items()
        ),
        "modified": bounded(modified),
        "modified_count": len(modified),
        "added": bounded(added),
        "added_count": len(added),
        "removed": bounded(removed),
        "removed_count": len(removed),
        "sampled": sampled,
        "truncated": truncated or sampled,
    }


def build_structure(_: dict[str, object]) -> object:
    result: dict[str, object] = {}
    for name in ("Dockerfile", "Cargo.toml", "Cargo.lock", "build.rs"):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
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
    """
    grouped: dict[str, list[dict[str, object]]] = {
        name: [] for name in INTEGRITY_SURFACES
    }
    counts: dict[str, int] = dict.fromkeys(INTEGRITY_SURFACES, 0)
    files, workspace_truncated = _files_with_truncation()
    omitted: list[dict[str, object]] = []
    for path in files:
        relative = _relative(path)
        if path.suffix.lower() not in SOURCE_SUFFIXES and path.name not in {
            "Dockerfile",
            "Cargo.toml",
            "build.rs",
        }:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            omitted.append({"path": relative, "reason": "read_cap"})
            continue
        try:
            lines = _text(path).splitlines()
        except (UnicodeDecodeError, ValueError):
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
        "truncated": workspace_truncated or bool(omitted),
    }


def _named_descendant(node: Node, node_types: set[str]) -> Node | None:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in node_types:
            return current
        stack.extend(reversed(current.named_children))
    return None


def _assignment_fact(raw: bytes, node: Node) -> tuple[str, str] | None:
    named = node.named_children
    if node.type == "assignment_expression" and len(named) >= 2:
        left, value = named[0], named[-1]
        field = _named_descendant(left, {"field_identifier"})
        if field is None:
            field = _named_descendant(left, {"identifier"})
    elif node.type in {"field_initializer", "let_declaration"} and len(named) >= 2:
        field, value = named[0], named[-1]
    else:
        return None
    if field is None:
        return None
    field_name = _node_text(raw, field).casefold()
    if not SCORED_FIELD.search(field_name):
        return None
    value_text = _node_text(raw, value).strip()
    if CLEAR_VALUE.search(value_text):
        state = "cleared"
    elif "None" in value_text and SCORE_CONTROL.search(value_text):
        state = "conditional_clear"
    else:
        state = "populated"
    return field_name[:80], state


def _binding_name(raw: bytes, node: Node) -> str | None:
    named = node.named_children
    if node.type in {"assignment_expression", "let_declaration"} and len(named) >= 2:
        left = named[0]
    else:
        return None
    identifier = _named_descendant(left, {"field_identifier"})
    if identifier is None:
        identifier = _named_descendant(left, {"identifier"})
    return _node_text(raw, identifier).casefold() if identifier is not None else None


def _score_terms(raw: bytes, node: Node, score_bindings: set[str]) -> list[str]:
    text = _node_text(raw, node)
    terms = {match.group(0).casefold() for match in SCORE_CONTROL.finditer(text)}
    identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.casefold()))
    if identifiers & score_bindings:
        terms.add("derived_score_control")
    return sorted(terms)[:6]


def _derive_score_bindings(raw: bytes, nodes: list[Node]) -> set[str]:
    bindings: set[str] = set()
    candidates = [
        node
        for node in nodes
        if node.type in {"assignment_expression", "let_declaration"}
        and len(node.named_children) >= 2
    ]
    changed = True
    while changed:
        changed = False
        for node in candidates:
            binding = _binding_name(raw, node)
            value = node.named_children[-1]
            if (
                binding
                and binding not in bindings
                and _score_terms(raw, value, bindings)
            ):
                bindings.add(binding)
                changed = True
    return bindings


def _enclosing_score_control(
    raw: bytes, node: Node, function: Node, score_bindings: set[str]
) -> tuple[Node, list[str]] | None:
    current: Node | None = node
    while current is not None and current != function:
        if current.type == "if_expression" and current.named_children:
            condition = current.named_children[0]
            terms = _score_terms(raw, condition, score_bindings)
            if terms:
                return condition, terms
        current = current.parent
    return None


def scorer_field_flow(_: dict[str, object]) -> object:
    """Locate score-controlled response-field clearing without returning snippets.

    This is a bounded attention map, not a verdict. It gives the coding agent
    structural locations needed to distinguish a never-used optional field from
    a populated response slot whose survival is controlled by score feedback.
    """
    flows: list[dict[str, object]] = []
    score_controls: list[dict[str, object]] = []
    field_clears: list[dict[str, object]] = []
    field_populations: list[dict[str, object]] = []
    sampled: set[str] = set()
    files, workspace_truncated = _files_with_truncation()
    analysis_truncated = workspace_truncated
    for path in files:
        if path.suffix.lower() != ".rs":
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            analysis_truncated = True
            continue
        raw = _bytes(path)
        tree = Parser(RUST_LANGUAGE).parse(raw)
        nodes, truncated = _walk(tree.root_node)
        analysis_truncated = analysis_truncated or truncated or tree.root_node.has_error
        for function in (node for node in nodes if node.type == "function_item"):
            name_node = function.child_by_field_name("name")
            function_name = (
                _node_text(raw, name_node)[:120]
                if name_node is not None
                else "<unknown>"
            )
            function_nodes, function_truncated = _walk(function)
            analysis_truncated = analysis_truncated or function_truncated
            score_bindings = _derive_score_bindings(raw, function_nodes)
            assignments: list[tuple[int, str, str]] = []
            for node in function_nodes:
                fact = _assignment_fact(raw, node)
                if fact is not None:
                    assignments.append((node.start_point[0] + 1, *fact))
                    field, state = fact
                    target = field_populations if state == "populated" else field_clears
                    if len(target) < MAX_SCORER_FLOWS:
                        target.append(
                            {
                                "path": _relative(path),
                                "function": function_name,
                                "field": field,
                                "line": node.start_point[0] + 1,
                                "state": state,
                            }
                        )
                    else:
                        sampled.add(
                            "field_populations"
                            if state == "populated"
                            else "field_clears"
                        )
            for node in function_nodes:
                if node.type != "if_expression" or not node.named_children:
                    continue
                condition = node.named_children[0]
                terms = _score_terms(raw, condition, score_bindings)
                if terms and len(score_controls) < MAX_SCORER_FLOWS:
                    score_controls.append(
                        {
                            "path": _relative(path),
                            "function": function_name,
                            "condition_line": condition.start_point[0] + 1,
                            "condition_terms": terms,
                        }
                    )
                elif terms:
                    sampled.add("score_controls")
            for node in function_nodes:
                fact = _assignment_fact(raw, node)
                if fact is None or fact[1] not in {"cleared", "conditional_clear"}:
                    continue
                field, mutation = fact
                controlled = _enclosing_score_control(
                    raw, node, function, score_bindings
                )
                if controlled is None and mutation == "conditional_clear":
                    terms = _score_terms(raw, node, score_bindings)
                    if terms:
                        controlled = (node, terms[:6])
                if controlled is None:
                    continue
                condition, terms = controlled
                line = node.start_point[0] + 1
                prior = [
                    assignment_line
                    for assignment_line, assignment_field, state in assignments
                    if assignment_field == field
                    and state == "populated"
                    and assignment_line < line
                ]
                if len(flows) < MAX_SCORER_FLOWS:
                    flows.append(
                        {
                            "path": _relative(path),
                            "function": function_name,
                            "field": field,
                            "condition_line": condition.start_point[0] + 1,
                            "condition_terms": terms,
                            "mutation_line": line,
                            "mutation": mutation,
                            "prior_populated_lines": prior[-8:],
                        }
                    )
                else:
                    sampled.add("flows")
    function_keys = sorted(
        {
            (str(item["path"]), str(item["function"]))
            for item in score_controls + field_clears + field_populations
        }
    )
    candidates: list[dict[str, object]] = []
    for candidate_path, candidate_function in function_keys:
        controls = [
            item
            for item in score_controls
            if item["path"] == candidate_path and item["function"] == candidate_function
        ]
        clears = [
            item
            for item in field_clears
            if item["path"] == candidate_path and item["function"] == candidate_function
        ]
        populations = [
            item
            for item in field_populations
            if item["path"] == candidate_path and item["function"] == candidate_function
        ]
        if not (controls and clears and populations):
            continue
        candidates.append(
            {
                "path": candidate_path,
                "function": candidate_function,
                "score_controls": [
                    {
                        "condition_line": item["condition_line"],
                        "condition_terms": item["condition_terms"],
                    }
                    for item in controls
                ],
                "field_clears": [
                    {
                        "field": item["field"],
                        "line": item["line"],
                        "state": item["state"],
                    }
                    for item in clears
                ],
                "field_populations": [
                    {"field": item["field"], "line": item["line"]}
                    for item in populations
                ],
            }
        )
        if len(candidates) >= MAX_SCORER_FLOWS:
            sampled.add("same_function_candidates")
            break
    return {
        "flows": flows,
        "flow_count": len(flows),
        "same_function_candidates": candidates,
        "score_controls": score_controls,
        "field_clears": field_clears,
        "field_populations": field_populations,
        "sampled": sorted(sampled),
        "truncated": analysis_truncated,
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


def _walk(root: Node) -> tuple[list[Node], bool]:
    nodes: list[Node] = []
    stack = [root]
    while stack and len(nodes) < MAX_AST_NODES:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.children))
    return nodes, bool(stack)


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


COMMANDS = {
    "workspace_index": workspace_index,
    "read_file": read_file,
    "search": search,
    "rust_structure": rust_structure,
    "call_graph": call_graph,
    "starter_diff": starter_diff,
    "starter_function_diff": starter_function_diff,
    "build_structure": build_structure,
    "integrity_surfaces": integrity_surfaces,
    "scorer_field_flow": scorer_field_flow,
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
