"""Bounded build/runtime reachability for static source preflight.

This is intentionally a proof engine, not a best-effort inventory classifier.
Only statically resolved build commands, entrypoints, imports, Cargo targets,
and source inclusions earn ``proven_reachable``.  Unsupported or dynamic forms
stay ``unresolved`` and can be routed to ordinary source review without being
promoted to a decisive pre-build finding.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import posixpath
import re
import shlex
import tomllib
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ditto_screener.rust_test_items import is_rust_test_only_attribute


class ReachabilityState(StrEnum):
    PROVEN_REACHABLE = "proven_reachable"
    PROVEN_INERT = "proven_inert"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ReachabilityEvidence:
    state: ReachabilityState
    bases: tuple[str, ...] = ()


@dataclass
class _Stage:
    index: int
    name: str | None
    parent: str
    instructions: list[tuple[str, str]]


@dataclass
class _StageState:
    paths: dict[str, set[str]]
    workdir: str = "/"
    unresolved: bool = False
    runtime: dict[str, tuple[str, str, dict[str, set[str]], bool]] | None = None

    def __post_init__(self) -> None:
        if self.runtime is None:
            self.runtime = {}


_SOURCE_SUFFIXES = (
    ".rs",
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".sh",
    ".bash",
    ".zsh",
)
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh", ".bash", ".zsh")
_DYNAMIC_MARKERS = re.compile(
    r"(?:include!\s*\(\s*(?:env|concat!\s*\([^\"']*env)|"
    r"importlib\.|__import__\s*\(|import\s*\([^\"']|require\s*\([^\"'])"
)


def _mask_rust_test_items(source: str) -> str:
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not is_rust_test_only_attribute(line):
            continue
        depth = 0
        opened = False
        for cursor in range(index, min(len(lines), index + 512)):
            original = lines[cursor]
            depth += original.count("{")
            depth -= original.count("}")
            lines[cursor] = ""
            if "{" in original:
                opened = True
            if opened and depth <= 0:
                break
            if not opened and cursor > index + 8:
                break
    return "\n".join(lines)


def _logical_instructions(source: str) -> tuple[list[tuple[str, str]], bool]:
    instructions: list[tuple[str, str]] = []
    pending = ""
    unresolved = False
    for raw in source.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instruction, separator, value = pending.partition(" ")
        if separator:
            instructions.append((instruction.upper(), value.strip()))
        else:
            unresolved = True
        if "<<" in pending:
            unresolved = True
        pending = ""
    if pending:
        unresolved = True
    return instructions, unresolved


def _parse_stages(source: str) -> tuple[list[_Stage], bool]:
    instructions, unresolved = _logical_instructions(source)
    stages: list[_Stage] = []
    current: _Stage | None = None
    for instruction, value in instructions:
        if instruction == "FROM":
            match = re.match(
                r"(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+([A-Za-z0-9_.-]+))?$",
                value,
                re.IGNORECASE,
            )
            if match is None:
                unresolved = True
                continue
            current = _Stage(len(stages), match.group(2), match.group(1), [])
            stages.append(current)
        elif current is None:
            if instruction not in {"ARG"}:
                unresolved = True
        else:
            current.instructions.append((instruction, value))
    return stages, unresolved or not stages


def _relevant_stage_indexes(stages: list[_Stage]) -> tuple[set[int], bool]:
    if not stages:
        return set(), True
    by_name = {stage.name: stage.index for stage in stages if stage.name}
    relevant = {stages[-1].index}
    pending = [stages[-1].index]
    unresolved = False
    while pending:
        index = pending.pop()
        stage = stages[index]
        parents: list[str] = [stage.parent]
        for instruction, value in stage.instructions:
            if instruction in {"COPY", "ADD"}:
                match = re.search(r"(?:^|\s)--from=([^\s]+)", value)
                if match:
                    parents.append(match.group(1))
        for parent in parents:
            candidate: int | None = None
            if parent.isdigit() and int(parent) < len(stages):
                candidate = int(parent)
            elif parent in by_name:
                candidate = by_name[parent]
            elif parent.startswith("$"):
                unresolved = True
            if candidate is not None and candidate not in relevant:
                relevant.add(candidate)
                pending.append(candidate)
    return relevant, unresolved


def _copy_parts(
    value: str,
) -> tuple[list[str], str | None, str | None, bool]:
    rest = value.strip()
    from_ref: str | None = None
    unsupported = False
    while rest.startswith("--"):
        flag, separator, rest = rest.partition(" ")
        if not separator:
            return [], None, None, True
        if flag.startswith("--from="):
            from_ref = flag.removeprefix("--from=")
        elif not flag.startswith(("--chown=", "--chmod=")) and flag != "--link":
            unsupported = True
        rest = rest.lstrip()
    if rest.startswith("["):
        try:
            tokens = json.loads(rest)
        except json.JSONDecodeError:
            return [], None, from_ref, True
        if not isinstance(tokens, list) or not all(
            isinstance(token, str) for token in tokens
        ):
            return [], None, from_ref, True
    else:
        try:
            tokens = shlex.split(rest)
        except ValueError:
            return [], None, from_ref, True
    if len(tokens) < 2:
        return [], None, from_ref, True
    return list(tokens[:-1]), tokens[-1], from_ref, unsupported


def _select_source(source: str, members: set[str]) -> tuple[set[str], str | None, bool]:
    normalized = posixpath.normpath(source.removeprefix("./"))
    if "$" in normalized or normalized.startswith("/") or normalized == "..":
        return set(), None, True
    if normalized in {".", ""}:
        return set(members), "", False
    if any(character in normalized for character in "*?["):
        matches = {
            member for member in members if fnmatch.fnmatchcase(member, normalized)
        }
        return matches, None, not matches
    if normalized in members:
        return {normalized}, None, False
    prefix = normalized.rstrip("/") + "/"
    directory_members = {member for member in members if member.startswith(prefix)}
    if directory_members:
        return directory_members, prefix, False
    return set(), None, True


def _container_path(value: str, workdir: str) -> str | None:
    if not value or "$" in value or "*" in value or "?" in value or "[" in value:
        return None
    if value.startswith("/"):
        return posixpath.normpath(value)
    return posixpath.normpath(posixpath.join(workdir, value))


def _copy_local_sources(
    *,
    sources: list[str],
    destination: str,
    workdir: str,
    members: set[str],
) -> tuple[dict[str, set[str]], set[str], bool]:
    target = _container_path(destination, workdir)
    if target is None:
        return {}, set(), True
    mappings: dict[str, set[str]] = {}
    selected_all: set[str] = set()
    unresolved = False
    multiple = len(sources) > 1
    for source in sources:
        selected, directory_prefix, source_unresolved = _select_source(source, members)
        unresolved |= source_unresolved
        selected_all.update(selected)
        source_is_directory = directory_prefix is not None
        for member in selected:
            if source_is_directory:
                relative = member.removeprefix(directory_prefix or "")
                container = posixpath.join(target, relative)
            elif (
                multiple
                or destination.endswith("/")
                or any(character in source for character in "*?[")
            ):
                container = posixpath.join(target, posixpath.basename(member))
            else:
                container = target
            mappings.setdefault(posixpath.normpath(container), set()).add(member)
    return mappings, selected_all, unresolved


def _copy_stage_sources(
    *,
    sources: list[str],
    destination: str,
    workdir: str,
    source_paths: Mapping[str, set[str]],
) -> tuple[dict[str, set[str]], bool]:
    target = _container_path(destination, workdir)
    if target is None:
        return {}, True
    mappings: dict[str, set[str]] = {}
    unresolved = False
    multiple = len(sources) > 1
    for source in sources:
        source_path = _container_path(source, "/")
        if source_path is None:
            unresolved = True
            continue
        exact = source_paths.get(source_path)
        descendants = {
            path: origins
            for path, origins in source_paths.items()
            if path.startswith(source_path.rstrip("/") + "/")
        }
        if exact is not None:
            container = (
                posixpath.join(target, posixpath.basename(source_path))
                if multiple or destination.endswith("/")
                else target
            )
            mappings.setdefault(posixpath.normpath(container), set()).update(exact)
        elif descendants:
            for path, origins in descendants.items():
                relative = path.removeprefix(source_path.rstrip("/") + "/")
                mappings.setdefault(posixpath.join(target, relative), set()).update(
                    origins
                )
        else:
            # Build artifacts need not map back to a source file. They do not
            # prove source reachability, but are not themselves ambiguous.
            continue
    return mappings, unresolved


def _merge_paths(
    target: dict[str, set[str]], additions: Mapping[str, set[str]]
) -> None:
    for container, origins in additions.items():
        target[container] = set(origins)


def _command_tokens(command: str) -> tuple[list[list[str]], bool]:
    stripped = command.strip()
    if not stripped:
        return [], True
    if stripped.startswith("["):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return [], True
        if not isinstance(value, list) or not all(
            isinstance(token, str) for token in value
        ):
            return [], True
        return [value], any("$" in token for token in value)
    try:
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [], True
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(character in ";&|" for character in token):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment], any(
        "$" in token for token in tokens
    )


def _command_source_paths(
    command: str,
    *,
    workdir: str,
    paths: Mapping[str, set[str]],
) -> tuple[set[str], bool]:
    segments, unresolved = _command_tokens(command)
    invoked: set[str] = set()
    interpreters = re.compile(
        r"^(?:python\d*|node|deno|bun|ruby|php|java|dotnet|(?:ba|z|k)?sh)$"
    )
    ignored_commands = {"chmod", "chown", "cp", "mv", "install", "test", "["}
    for segment in segments:
        if not segment:
            continue
        executable = posixpath.basename(segment[0])
        candidate: str | None = None
        if interpreters.match(executable):
            for argument in segment[1:]:
                if argument.startswith("-"):
                    continue
                candidate = argument
                break
        elif executable not in ignored_commands:
            candidate = segment[0]
        if candidate is None:
            continue
        # A bare basename is resolved by image PATH, which is outside the
        # archive proof. Require an absolute path or an explicit relative path.
        if "/" not in candidate and not candidate.startswith("."):
            continue
        container = _container_path(candidate, workdir)
        if container is None:
            unresolved = True
            continue
        origins = paths.get(container)
        if origins is None:
            continue
        if len(origins) == 1:
            invoked.update(origins)
        else:
            unresolved = True
    return invoked, unresolved


def _rust_references(path: str, source: str) -> tuple[set[str], bool]:
    references: set[str] = set()
    unresolved = bool(_DYNAMIC_MARKERS.search(source))
    parent = posixpath.dirname(path)
    # Literal include and #[path] forms. Test-gated references are removed by
    # the caller's test-only masking before they reach this parser.
    for match in re.finditer(
        r"(?:include!\s*\(\s*|#\s*\[\s*path\s*=\s*)"
        r"(?:r#*)?[\"']([^\"']+)[\"']",
        source,
    ):
        references.add(posixpath.normpath(posixpath.join(parent, match.group(1))))
    # Rust's ordinary `mod child;` resolution.
    stem = posixpath.splitext(posixpath.basename(path))[0]
    module_parent = (
        parent if stem in {"main", "lib", "mod"} else posixpath.join(parent, stem)
    )
    for match in re.finditer(
        r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", source
    ):
        module = match.group(1)
        references.add(posixpath.join(module_parent, f"{module}.rs"))
        references.add(posixpath.join(module_parent, module, "mod.rs"))
    return references, unresolved


def _python_references(path: str, source: str) -> tuple[set[str], bool]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), True
    references: set[str] = set()
    parent_parts = posixpath.dirname(path).split("/") if "/" in path else []
    unresolved = False
    for node in ast.walk(tree):
        names: list[tuple[int, str]] = []
        if isinstance(node, ast.Import):
            names.extend((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append((node.level, node.module or ""))
            names.extend(
                (
                    node.level,
                    ".".join(part for part in (node.module, alias.name) if part),
                )
                for alias in node.names
                if alias.name != "*"
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            unresolved = True
        for level, name in names:
            base: list[str] = []
            if level:
                base = parent_parts[:]
                base = base[: max(0, len(base) - level + 1)]
            module_parts = [part for part in name.split(".") if part]
            joined = "/".join((*base, *module_parts))
            if joined:
                references.update({f"{joined}.py", f"{joined}/__init__.py"})
    if "importlib." in source:
        unresolved = True
    return references, unresolved


def _literal_script_references(path: str, source: str) -> tuple[set[str], bool]:
    parent = posixpath.dirname(path)
    references: set[str] = set()
    unresolved = bool(_DYNAMIC_MARKERS.search(source))
    for match in re.finditer(
        r"(?:from\s+|import\s*(?:\(|)|require\s*\(|source\s+|\.\s+)"
        r"[\"']([^\"']+)[\"']",
        source,
    ):
        value = match.group(1)
        if not value.startswith("."):
            continue
        candidate = posixpath.normpath(posixpath.join(parent, value))
        references.add(candidate)
        if not posixpath.splitext(candidate)[1]:
            references.update(
                f"{candidate}{suffix}" for suffix in (".js", ".ts", ".py", ".sh")
            )
            references.update(
                posixpath.join(candidate, f"index{suffix}") for suffix in (".js", ".ts")
            )
    return references, unresolved


def _cargo_roots(
    files: Mapping[str, str], copied: set[str], manifests: set[str]
) -> tuple[set[str], bool]:
    roots: set[str] = set()
    unresolved = False
    pending = deque(manifests)
    visited: set[str] = set()
    while pending:
        manifest_path = pending.popleft()
        if manifest_path in visited:
            continue
        visited.add(manifest_path)
        source = files.get(manifest_path)
        if source is None:
            unresolved = True
            continue
        try:
            manifest = tomllib.loads(source)
        except tomllib.TOMLDecodeError:
            unresolved = True
            continue
        base = posixpath.dirname(manifest_path)
        package = manifest.get("package")
        if isinstance(package, dict):
            build = package.get("build", "build.rs")
            if build is not False and isinstance(build, str):
                candidate = posixpath.normpath(posixpath.join(base, build))
                if candidate in copied:
                    roots.add(candidate)
            for default in ("src/main.rs", "src/lib.rs"):
                candidate = posixpath.normpath(posixpath.join(base, default))
                if candidate in copied:
                    roots.add(candidate)
        for kind in ("lib", "bin"):
            targets = manifest.get(kind)
            if isinstance(targets, dict):
                targets = [targets]
            if isinstance(targets, list):
                for target in targets:
                    if isinstance(target, dict) and isinstance(target.get("path"), str):
                        candidate = posixpath.normpath(
                            posixpath.join(base, target["path"])
                        )
                        if candidate in copied:
                            roots.add(candidate)
        for dependency_kind in ("dependencies", "build-dependencies"):
            dependencies = manifest.get(dependency_kind)
            if not isinstance(dependencies, dict):
                continue
            for specification in dependencies.values():
                if isinstance(specification, dict) and isinstance(
                    specification.get("path"), str
                ):
                    child = posixpath.normpath(
                        posixpath.join(base, specification["path"], "Cargo.toml")
                    )
                    if child in copied:
                        pending.append(child)
    return roots, unresolved


def _cargo_manifest_origins(
    command: str, *, workdir: str, paths: Mapping[str, set[str]]
) -> tuple[set[str], bool]:
    manifest_match = re.search(r"--manifest-path(?:=|\s+)([^\s;&|]+)", command)
    candidates: list[str] = []
    if manifest_match:
        manifest = _container_path(manifest_match.group(1), workdir)
        if manifest is None:
            return set(), True
        candidates.append(manifest)
    else:
        current = workdir
        while True:
            candidates.append(posixpath.join(current, "Cargo.toml"))
            if current == "/":
                break
            current = posixpath.dirname(current)
    for candidate in candidates:
        origins = paths.get(posixpath.normpath(candidate))
        if origins is None:
            continue
        manifests = {path for path in origins if path.endswith("Cargo.toml")}
        return manifests, len(origins) != 1 or len(manifests) != 1
    return set(), False


def analyze_reachability(files: Mapping[str, str]) -> dict[str, ReachabilityEvidence]:
    """Classify every readable archive member against the effective build/runtime."""
    members = set(files)
    evidence: dict[str, set[str]] = {path: set() for path in files}
    unresolved_paths: set[str] = set()
    dockerfile = files.get("Dockerfile")
    if dockerfile is None:
        return {
            path: ReachabilityEvidence(
                ReachabilityState.UNRESOLVED, ("missing-dockerfile",)
            )
            for path in files
        }
    evidence["Dockerfile"].add("docker-build-definition")
    stages, docker_unresolved = _parse_stages(dockerfile)
    relevant, stage_unresolved = _relevant_stage_indexes(stages)
    global_unresolved = docker_unresolved or stage_unresolved
    copied: set[str] = {"Dockerfile"}
    cargo_contexts: list[tuple[set[str], set[str]]] = []
    states: list[_StageState] = []
    by_name: dict[str, int] = {}
    for stage in stages:
        parent_index: int | None = None
        if stage.parent.isdigit() and int(stage.parent) < len(states):
            parent_index = int(stage.parent)
        elif stage.parent in by_name:
            parent_index = by_name[stage.parent]
        if parent_index is None:
            state = _StageState(paths={})
        else:
            parent = states[parent_index]
            state = _StageState(
                paths={path: set(origins) for path, origins in parent.paths.items()},
                workdir=parent.workdir,
                unresolved=parent.unresolved,
                runtime={
                    key: (value, workdir, dict(paths), unresolved)
                    for key, (value, workdir, paths, unresolved) in (
                        parent.runtime or {}
                    ).items()
                },
            )
        stage_copied: set[str] = set()
        for instruction, value in stage.instructions:
            if instruction == "WORKDIR":
                workdir = _container_path(value, state.workdir)
                if workdir is None:
                    state.unresolved = True
                else:
                    state.workdir = workdir
                continue
            if instruction in {"COPY", "ADD"}:
                sources, destination, from_ref, unresolved = _copy_parts(value)
                if destination is None or (
                    instruction == "ADD"
                    and any(
                        re.match(r"(?:https?|git)://", source) for source in sources
                    )
                ):
                    state.unresolved = True
                    continue
                if instruction == "ADD":
                    # ADD may unpack local archives or retrieve remote content.
                    # COPY is the only source-to-container mapping we prove.
                    state.unresolved = True
                    continue
                if from_ref is None:
                    additions, selected, copy_unresolved = _copy_local_sources(
                        sources=sources,
                        destination=destination,
                        workdir=state.workdir,
                        members=members,
                    )
                    stage_copied.update(selected)
                else:
                    source_index: int | None = None
                    if from_ref.isdigit() and int(from_ref) < len(states):
                        source_index = int(from_ref)
                    elif from_ref in by_name:
                        source_index = by_name[from_ref]
                    if source_index is None:
                        additions, copy_unresolved = {}, True
                    else:
                        additions, copy_unresolved = _copy_stage_sources(
                            sources=sources,
                            destination=destination,
                            workdir=state.workdir,
                            source_paths=states[source_index].paths,
                        )
                _merge_paths(state.paths, additions)
                state.unresolved |= unresolved or copy_unresolved
                continue
            if instruction == "RUN":
                if value.lstrip().startswith("--mount="):
                    state.unresolved = True
                    continue
                if (
                    stage.index in relevant
                    and not state.unresolved
                    and not global_unresolved
                ):
                    invoked, command_unresolved = _command_source_paths(
                        value, workdir=state.workdir, paths=state.paths
                    )
                    if command_unresolved:
                        state.unresolved = True
                    else:
                        for path in invoked:
                            evidence[path].add(f"docker-run:stage-{stage.index}")
                    if not command_unresolved and re.search(
                        r"\bcargo\s+(?:build|install|rustc)\b", value
                    ):
                        manifests, cargo_unresolved = _cargo_manifest_origins(
                            value, workdir=state.workdir, paths=state.paths
                        )
                        if cargo_unresolved:
                            state.unresolved = True
                        elif manifests:
                            cargo_contexts.append(
                                (
                                    manifests,
                                    {
                                        origin
                                        for origins in state.paths.values()
                                        for origin in origins
                                    },
                                )
                            )
                continue
            if instruction in {"CMD", "ENTRYPOINT"}:
                assert state.runtime is not None
                state.runtime[instruction] = (
                    value,
                    state.workdir,
                    {path: set(origins) for path, origins in state.paths.items()},
                    state.unresolved,
                )
                continue
            if instruction not in {
                "ARG",
                "ENV",
                "EXPOSE",
                "HEALTHCHECK",
                "LABEL",
                "MAINTAINER",
                "ONBUILD",
                "SHELL",
                "STOPSIGNAL",
                "USER",
                "VOLUME",
            }:
                state.unresolved = True
        states.append(state)
        if stage.name:
            by_name[stage.name] = stage.index
        if stage.index in relevant:
            copied.update(stage_copied)
            copied.update(
                origin for origins in state.paths.values() for origin in origins
            )
            if state.unresolved:
                unresolved_paths.update(
                    path for path in members if path.endswith(_SOURCE_SUFFIXES)
                )
    if global_unresolved:
        unresolved_paths.update(members - {"Dockerfile"})

    queue: deque[str] = deque()
    if states and not global_unresolved:
        final_runtime = states[-1].runtime or {}
        for instruction in ("ENTRYPOINT", "CMD"):
            runtime = final_runtime.get(instruction)
            if runtime is None:
                continue
            command = runtime[0]
            final_state = states[-1]
            workdir = final_state.workdir
            paths = final_state.paths
            unresolved = final_state.unresolved
            invoked, command_unresolved = _command_source_paths(
                command, workdir=workdir, paths=paths
            )
            if unresolved or command_unresolved:
                unresolved_paths.update(
                    origin
                    for origins in paths.values()
                    for origin in origins
                    if origin.endswith(_SOURCE_SUFFIXES)
                )
                continue
            for path in invoked:
                evidence[path].add(f"docker-{instruction.casefold()}:final-stage")

    for path, path_evidence in evidence.items():
        if path != "Dockerfile" and path_evidence:
            queue.append(path)

    cargo_manifests: set[str] = set()
    for manifests, available in cargo_contexts:
        cargo_manifests.update(manifests)
        roots, cargo_unresolved = _cargo_roots(files, available, manifests)
        for root in roots:
            evidence[root].add(
                "cargo-build-script" if root.endswith("build.rs") else "cargo-target"
            )
            queue.append(root)
        if cargo_unresolved:
            unresolved_paths.update(path for path in copied if path.endswith(".rs"))

    # A common `cargo build` with a broad COPY still has a deterministic default
    # root even when Cargo.toml was omitted from the readable map for some reason.
    for manifest in cargo_manifests:
        evidence[manifest].add("cargo-manifest")

    scanned: set[str] = set()
    while queue:
        path = queue.popleft()
        if path in scanned or path not in files:
            continue
        scanned.add(path)
        source = files[path]
        references: set[str] = set()
        unresolved = False
        if path.endswith(".rs"):
            references, unresolved = _rust_references(
                path, _mask_rust_test_items(source)
            )
        elif path.endswith(".py"):
            references, unresolved = _python_references(path, source)
        elif path.endswith(
            (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".bash", ".zsh")
        ):
            references, unresolved = _literal_script_references(path, source)
        if unresolved:
            unresolved_paths.update(
                candidate
                for candidate in copied
                if candidate.endswith(_SOURCE_SUFFIXES) and candidate not in scanned
            )
        for reference in references:
            if reference not in copied or reference not in files:
                continue
            evidence[reference].add(f"entrypoint-import:{path}")
            queue.append(reference)

    result: dict[str, ReachabilityEvidence] = {}
    for path in sorted(files):
        bases = tuple(sorted(evidence[path]))
        if bases:
            result[path] = ReachabilityEvidence(
                ReachabilityState.PROVEN_REACHABLE, bases
            )
        elif path in unresolved_paths or (
            path in copied and path.endswith(_SOURCE_SUFFIXES)
        ):
            result[path] = ReachabilityEvidence(
                ReachabilityState.UNRESOLVED,
                ("copied-without-proven-execution",)
                if path in copied
                else ("dynamic-build-or-import",),
            )
        else:
            result[path] = ReachabilityEvidence(
                ReachabilityState.PROVEN_INERT,
                ("excluded-from-effective-build",)
                if path not in copied
                else ("copied-nonexecutable-input",),
            )
    return result


__all__ = [
    "ReachabilityEvidence",
    "ReachabilityState",
    "analyze_reachability",
]
