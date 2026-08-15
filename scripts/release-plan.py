#!/usr/bin/env python3
"""Resolve the monorepo components affected by a Git change.

The component graph is the release boundary. A component is selected when one
of its own path patterns matches or when one of its declared dependencies is
selected. This keeps workflow conditions small and makes release coupling
reviewable outside GitHub Actions.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT_VERIFICATION_FULL = "full"
ROOT_VERIFICATION_CONTRACT = "contract"
ROOT_VERIFICATION_NONE = "none"
ROOT_VERIFICATION_MODES = frozenset(
    {
        ROOT_VERIFICATION_FULL,
        ROOT_VERIFICATION_CONTRACT,
        ROOT_VERIFICATION_NONE,
    }
)

# These components execute from the root Python project or exercise its runtime
# contract directly. Their exact merge source keeps the complete root gate.
ROOT_VERIFICATION_COMPONENTS = frozenset(
    {
        "miner_cli",
        "screening_protocol",
        "validator",
        "sandbox_docker",
    }
)

# These validator-stack inputs also own the root environment used by the
# verifier itself, so changing either one requires the complete root gate.
ROOT_VERIFICATION_PROJECT_PATHS = frozenset({"pyproject.toml", "uv.lock"})


@dataclass(frozen=True)
class Component:
    paths: tuple[str, ...]
    depends_on: tuple[str, ...]


def load_ignored_paths(path: Path) -> tuple[str, ...]:
    """Load paths that are intentionally excluded from every release unit."""
    with path.open("rb") as source:
        raw = tomllib.load(source)
    release = raw.get("release", {})
    if not isinstance(release, dict):
        raise ValueError("release must be a table")
    return _string_tuple(release.get("ignored_paths", ()), "release.ignored_paths")


def load_components(path: Path) -> dict[str, Component]:
    with path.open("rb") as source:
        raw = tomllib.load(source)

    raw_components = raw.get("components")
    if not isinstance(raw_components, dict) or not raw_components:
        raise ValueError("release graph must define at least one component")

    components: dict[str, Component] = {}
    for name, value in raw_components.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"invalid component name: {name!r}")
        if not isinstance(value, dict):
            raise ValueError(f"component {name!r} must be a table")
        paths = _string_tuple(value.get("paths", ()), f"{name}.paths")
        dependencies = _string_tuple(value.get("depends_on", ()), f"{name}.depends_on")
        if not paths and not dependencies:
            raise ValueError(f"component {name!r} has no paths or dependencies")
        components[name] = Component(paths=paths, depends_on=dependencies)

    for name, component in components.items():
        unknown = set(component.depends_on) - components.keys()
        if unknown:
            raise ValueError(
                f"component {name!r} has unknown dependencies: {sorted(unknown)}"
            )
    _assert_acyclic(components)
    return components


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(value)


def _assert_acyclic(components: Mapping[str, Component]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"release graph contains a dependency cycle at {name!r}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in components[name].depends_on:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in components:
        visit(name)


def normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path.strip().replace("\\", "/"))
        if not raw_path.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"invalid changed path: {raw_path!r}")
        normalized.append(path.as_posix())
    return tuple(dict.fromkeys(normalized))


def select_components(
    components: Mapping[str, Component],
    changed_paths: Iterable[str],
    ignored_paths: Iterable[str] = (),
) -> dict[str, bool]:
    paths = normalize_paths(changed_paths)
    selected = {
        name: any(
            fnmatch.fnmatchcase(changed_path, pattern)
            for changed_path in paths
            for pattern in component.paths
        )
        for name, component in components.items()
    }

    accounted_for = {
        path
        for path in paths
        if any(
            fnmatch.fnmatchcase(path, pattern)
            for component in components.values()
            for pattern in component.paths
        )
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in ignored_paths)
    }
    unmapped = sorted(set(paths) - accounted_for)
    if unmapped:
        formatted = "\n  - ".join(unmapped)
        raise ValueError(
            "changed paths are not mapped to a release component or an explicit "
            f"ignore rule:\n  - {formatted}"
        )

    changed = True
    while changed:
        changed = False
        for name, component in components.items():
            if not selected[name] and any(
                selected[dependency] for dependency in component.depends_on
            ):
                selected[name] = True
                changed = True
    return selected


def select_root_verification(
    components: Mapping[str, Component],
    selected: Mapping[str, bool],
    changed_paths: Iterable[str],
) -> str:
    """Choose the smallest fail-closed root gate for the exact changed paths."""
    paths = normalize_paths(changed_paths)
    if any(selected.get(name, False) for name in ROOT_VERIFICATION_COMPONENTS):
        return ROOT_VERIFICATION_FULL
    if any(path in ROOT_VERIFICATION_PROJECT_PATHS for path in paths):
        return ROOT_VERIFICATION_FULL

    validator_stack = components["validator_stack"]
    validator_stack_changed_directly = any(
        fnmatch.fnmatchcase(path, pattern)
        for path in paths
        for pattern in validator_stack.paths
    )
    if validator_stack_changed_directly:
        return ROOT_VERIFICATION_CONTRACT
    return ROOT_VERIFICATION_NONE


def git_changed_paths(base: str, head: str) -> tuple[str, ...]:
    if set(base) == {"0"}:
        command = [
            "git",
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-z",
            "-r",
            head,
        ]
    else:
        command = [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACDMRT",
            "-z",
            base,
            head,
        ]
    result = subprocess.run(command, check=True, capture_output=True)
    return normalize_paths(
        path.decode("utf-8", errors="strict")
        for path in result.stdout.split(b"\0")
        if path
    )


def write_github_output(
    path: Path,
    selected: Mapping[str, bool],
    root_verification: str,
) -> None:
    if root_verification not in ROOT_VERIFICATION_MODES:
        raise ValueError(f"invalid root verification mode: {root_verification!r}")
    with path.open("a", encoding="utf-8") as output:
        for name, value in sorted(selected.items()):
            output.write(f"{name}={str(value).lower()}\n")
        output.write(f"any={str(any(selected.values())).lower()}\n")
        output.write(f"root_verification={root_verification}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("release/components.toml"))
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.paths is None and args.base is None:
        parser.error("provide --base or at least one --path")
    if args.paths is not None and args.base is not None:
        parser.error("--path and --base are mutually exclusive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    components = load_components(args.config)
    ignored_paths = load_ignored_paths(args.config)
    paths = (
        normalize_paths(args.paths)
        if args.paths is not None
        else git_changed_paths(args.base, args.head)
    )
    selected = select_components(components, paths, ignored_paths)
    root_verification = select_root_verification(components, selected, paths)
    if args.github_output is not None:
        write_github_output(args.github_output, selected, root_verification)
    print(
        json.dumps(
            {
                "changed_paths": paths,
                "components": selected,
                "root_verification": root_verification,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
