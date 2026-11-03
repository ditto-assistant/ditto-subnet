"""Build the public distinctive-item corpus for the unreleased-copy signal.

Usage (from ``apps/platform``)::

    uv run python scripts/build_public_items.py \
        --repo ../.. \
        --extra-repo /path/to/ditto-harness \
        --extra-repo /path/to/dittobench-starter-kit \
        --extra-repo /path/to/dittobench-datagen

Every blob reachable in the history of the public sources is extracted with
:mod:`ditto.api_server.distinctive_content` and its item keys are unioned:

* every ``miners/*`` starter kit (all revisions in the monorepo history);
* ``research/dittobench-datagen`` (public generator vocabulary and templates);
* the public upstream repositories (all refs, all revisions): the
  ``ditto-harness`` crate the starter kit depends on, and the pre-monorepo
  ``dittobench-starter-kit`` and ``dittobench-datagen`` histories that miners
  forked before the monorepo cutover.

Output is a sorted, gzip-compressed big-endian int64 stream plus a manifest
pinning the exact revisions, both committed under ``ditto/anticopy`` so upload
processing never needs network access. Released agents are *not* in this
bundle: they are subtracted at comparison time from the live index, as of the
candidate's upload.
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

OUTPUT = Path(__file__).parents[1] / "ditto" / "anticopy"
MODULE = Path(__file__).parents[1] / "ditto" / "api_server" / "distinctive_content.py"
REPO_PATHS = ("miners", "research/dittobench-datagen")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("distinctive_content", MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True)


def _blobs(repo: Path, paths: tuple[str, ...]) -> dict[str, str]:
    """``blob sha -> one path`` for every blob ever reachable under ``paths``."""
    out: dict[str, str] = {}
    log = _git(repo, "log", "--all", "--format=", "--raw", "--no-abbrev", "--", *paths)
    for line in log.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) >= 4 and set(fields[3]) != {"0"}:
            out.setdefault(fields[3], path.split("\t")[-1])
    # Blobs that were never modified after the first commit appear only in trees.
    for path_line in _git(repo, "ls-tree", "-r", "HEAD", "--", *paths).splitlines():
        meta, _, path = path_line.partition("\t")
        fields = meta.split()
        if len(fields) >= 3 and fields[1] == "blob":
            out.setdefault(fields[2], path)
    return out


def _read_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=("\n".join(shas) + "\n").encode(),
        capture_output=True,
        check=True,
    )
    data = proc.stdout
    out: dict[str, bytes] = {}
    pos = 0
    while pos < len(data):
        header_end = data.index(b"\n", pos)
        sha, kind, size = data[pos:header_end].decode().split()
        start = header_end + 1
        out[sha] = data[start : start + int(size)] if kind == "blob" else b""
        pos = start + int(size) + 1
    return out


def collect(repo: Path, paths: tuple[str, ...], dc: ModuleType) -> set[int]:
    blobs = _blobs(repo, paths)
    keys: set[int] = set()
    shas = list(blobs)
    for i in range(0, len(shas), 2000):
        chunk = shas[i : i + 2000]
        contents = _read_blobs(repo, chunk)
        for sha in chunk:
            raw = contents.get(sha, b"")
            path = blobs[sha]
            if len(raw) > dc._MAX_ITEM_FILE_BYTES or dc._skipped_path(path):
                continue
            keys.update(item.key for item in dc.file_items(path, raw))
    return keys


def _public_label(path: Path) -> str:
    """A machine-independent label: the last three path segments."""
    return "/".join(path.resolve().parts[-3:])


def collect_tree(root: Path, dc: ModuleType) -> set[int]:
    """Items of every source file under an installed library tree."""
    keys: set[int] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.stat().st_size > dc._MAX_ITEM_FILE_BYTES:
            continue
        rel = path.relative_to(root).as_posix()
        if dc._skipped_path(rel):
            continue
        raw = path.read_bytes()
        keys.update(
            item.key for item in dc.file_items(rel, raw, dc.file_constants(rel, raw))
        )
    return keys


def collect_binary_strings(binary: Path, dc: ModuleType) -> set[int]:
    """Printable strings of a compiled library, read as prose (one per line)."""
    out = subprocess.check_output(["strings", "-n", "12", str(binary)])
    return {item.key for item in dc.file_items("strings.txt", out)}


def runtime_messages() -> list[str]:
    """Messages CPython and pydantic-core format at runtime.

    Ports of a Python agent's calculator, parser or validator reproduce these
    verbatim, and no source file spells them out: CPython builds them from
    argument-clinic templates, pydantic-core from its error table.
    """
    import builtins
    import math
    import sys

    sys.set_int_max_str_digits(4300)

    messages: list[str] = []
    try:
        from pydantic_core._pydantic_core import list_all_errors

        for error in list_all_errors():
            messages += [
                error["message_template_python"],
                error["example_message_python"],
            ]
    except ImportError:  # pragma: no cover - pydantic-core is a Platform dependency
        pass

    def capture(call) -> None:
        try:
            call()
        except Exception as error:  # noqa: BLE001 - every message is wanted
            messages.append(str(error))

    # Pure functions only: nothing that reads input, opens files, or debugs.
    pure = [
        getattr(builtins, name)
        for name in (
            "abs",
            "all",
            "any",
            "ascii",
            "bin",
            "bool",
            "chr",
            "complex",
            "dict",
            "divmod",
            "enumerate",
            "filter",
            "float",
            "format",
            "frozenset",
            "hash",
            "hex",
            "int",
            "isinstance",
            "issubclass",
            "iter",
            "len",
            "list",
            "map",
            "max",
            "min",
            "next",
            "oct",
            "ord",
            "pow",
            "range",
            "repr",
            "reversed",
            "round",
            "set",
            "slice",
            "sorted",
            "str",
            "sum",
            "tuple",
            "zip",
        )
    ] + [
        getattr(math, name)
        for name in sorted(dir(math))
        if not name.startswith("_") and callable(getattr(math, name))
    ]
    for fn in pure:
        for args in ((), (1, 2, 3, 4), ("x",), (None,), (-1,), (1j,), (10**400,)):
            capture(lambda fn=fn, args=args: fn(*args))
    for expression in (
        "01",
        "0o",
        "1 if 2",
        "(" * 210 + ")" * 210,
        "a = (]",
        "\\ x",
        "*a",
        "1 +",
        "lambda: (yield)",
        "f'{'",
        "x = 1 = 2",
        "def f(:",
        "1__0",
        "0b2",
        "1/0",
        "1//0",
        "1%0",
        "float('inf') // 1",
        "int('x')",
        "float('x')",
        "complex('x')",
        "round(1j)",
        "abs()",
        "0.0 ** -1",
        "(-8) ** 0.5 > 1",
        "divmod(1j, 1)",
        "math.sqrt(-1)",
        "math.log(0)",
        "math.factorial(-1)",
        "math.factorial(1.5)",
        "int(10**5000)",
        "float(10**400)",
        "1 < 1j",
        "[] < 1",
        "iter(1)",
        "len(1)",
        "'a' + 1",
        "1 + 'a'",
        "'a' * 'b'",
        "None()",
        "{}[0]",
        "[][0]",
        "().x",
        "int('9' * 5000)",
        "str(10**5000)",
        "chr(-1)",
        "ord('ab')",
    ):
        capture(lambda e=expression: eval(compile(e, "<calc>", "eval"), {"math": math}))
        capture(lambda e=expression: exec(compile(e, "<calc>", "exec"), {"math": math}))
    return sorted(set(messages))


def collect_runtime_messages(dc: ModuleType) -> set[int]:
    text = "\n\n".join(runtime_messages()).encode()
    return {item.key for item in dc.file_items("messages.txt", text)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--extra-repo", type=Path, action="append", default=[])
    parser.add_argument(
        "--extra-tree",
        type=Path,
        action="append",
        default=[],
        help="installed library source (stdlib, site-packages, cargo crates)",
    )
    parser.add_argument(
        "--binary-strings",
        type=Path,
        action="append",
        default=[],
        help="compiled library whose printable strings are public messages",
    )
    args = parser.parse_args()
    dc = _load()
    keys = collect(args.repo, REPO_PATHS, dc)
    sources = [
        {
            "repo": "ditto-assistant/ditto-subnet",
            "paths": list(REPO_PATHS),
            "head": _git(args.repo, "rev-parse", "HEAD").strip(),
        }
    ]
    for extra in args.extra_repo:
        keys |= collect(extra, (".",), dc)
        origin = _git(extra, "remote", "get-url", "origin").strip()
        sources.append(
            {
                "repo": re.sub(r"^.*github\.com[:/]", "", origin.removesuffix(".git")),
                "paths": ["."],
                "head": _git(extra, "rev-parse", "HEAD").strip(),
            }
        )
    for tree in args.extra_tree:
        keys |= collect_tree(tree, dc)
        sources.append({"tree": _public_label(tree)})
    keys |= collect_runtime_messages(dc)
    sources.append({"runtime_messages": "cpython builtins/math/parser + pydantic-core"})
    for binary in args.binary_strings:
        keys |= collect_binary_strings(binary, dc)
        sources.append({"binary": _public_label(binary)})
    payload = b"".join(k.to_bytes(8, "big", signed=True) for k in sorted(keys))
    (OUTPUT / dc.PUBLIC_CORPUS_BUNDLE).write_bytes(gzip.compress(payload, mtime=0))
    manifest = {"version": dc.ITEMS_VERSION, "items": len(keys), "sources": sources}
    (OUTPUT / f"public_items_{dc.ITEMS_VERSION}.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
