"""Deterministic synthetic workloads for the query adapter pilot.

Every workload here is built from fixed integer seeds, so the committed JSON in
``testdata/workloads/`` is reproducible byte for byte. All user refs are
synthetic (``syn-...``); adapter weights and queries are derived from the seeds
by ``reference.synthetic_adapter`` and ``reference.synthetic_query``.

Regenerate or check for drift::

    python -m query_adapter_pilot.fixtures --write testdata/workloads
    python -m query_adapter_pilot.fixtures --check            # exit 1 on drift

Seed layout (no seed is shared between two workloads):

* adapter seed = 100_000 + 10_000 * workload_index + 10 * user_index + version
* query seed   = 500_000 + 10_000 * workload_index + k
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .contract import CONTRACT_VERSION
from .workload import WORKLOAD_KIND, Workload

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "testdata" / "workloads"

# Fixed order: a workload's index feeds its seeds, so append new workloads at
# the end rather than reordering.
WORKLOAD_NAMES: tuple[str, ...] = (
    "smoke",
    "happy",
    "multi_tenant",
    "stale",
    "missing",
    "unauthorized",
    "replay",
    "isolation",
)

# multi_tenant shape: tenants must exceed the small cache used by the proof
# (``--cache-capacity 8``), then a hot set is queried back to back for hits.
MULTI_TENANT_USERS = 32
MULTI_TENANT_ROUND_ROBIN_PASSES = 2
MULTI_TENANT_HOT_USERS = 4
MULTI_TENANT_HOT_REPEATS = 4


def adapter_seed(workload: str, user_index: int, version: int) -> int:
    return 100_000 + 10_000 * WORKLOAD_NAMES.index(workload) + 10 * user_index + version


def query_seed(workload: str, k: int) -> int:
    return 500_000 + 10_000 * WORKLOAD_NAMES.index(workload) + k


def _user(
    workload: str,
    user_index: int,
    versions: Sequence[int],
    provision: Sequence[int],
    authorized: int | None,
    ref: str | None = None,
) -> dict[str, Any]:
    return {
        "user_ref": ref or f"syn-{workload.replace('_', '-')}-u{user_index:02d}",
        "adapters": [
            {"version": v, "seed": adapter_seed(workload, user_index, v)} for v in versions
        ],
        "provision": list(provision),
        "authorized_version": authorized,
    }


def _doc(name: str, users: list[dict[str, Any]], requests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract": CONTRACT_VERSION,
        "kind": WORKLOAD_KIND,
        "name": name,
        "synthetic": True,
        "users": users,
        "requests": requests,
    }


def _round_robin(
    name: str, refs: Sequence[str], per_user: int, start: int = 0
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    k = start
    for _ in range(per_user):
        for ref in refs:
            out.append({"user_ref": ref, "query_seed": query_seed(name, k)})
            k += 1
    return out


def smoke() -> dict[str, Any]:
    name = "smoke"
    users = [_user(name, 0, [1], [1], 1)]
    return _doc(name, users, _round_robin(name, [users[0]["user_ref"]], 3))


def happy() -> dict[str, Any]:
    name = "happy"
    users = [_user(name, i, [1], [1], 1) for i in range(3)]
    return _doc(name, users, _round_robin(name, [u["user_ref"] for u in users], 10))


def multi_tenant() -> dict[str, Any]:
    """32 tenants x versions [1, 2] (v1 retired by v2, v2 authorized).
    Round-robin passes overflow a small cache (misses, evictions); a final hot
    burst repeats a few tenants back to back (warm hits)."""
    name = "multi_tenant"
    users = [_user(name, i, [1, 2], [1, 2], 2) for i in range(MULTI_TENANT_USERS)]
    refs = [u["user_ref"] for u in users]
    requests = _round_robin(name, refs, MULTI_TENANT_ROUND_ROBIN_PASSES)
    k = len(requests)
    for ref in refs[:MULTI_TENANT_HOT_USERS]:
        for _ in range(MULTI_TENANT_HOT_REPEATS):
            requests.append({"user_ref": ref, "query_seed": query_seed(name, k)})
            k += 1
    return _doc(name, users, requests)


def stale() -> dict[str, Any]:
    """Ditto still authorizes v1 but provisioned v2, which retired v1."""
    name = "stale"
    users = [_user(name, 0, [1, 2], [1, 2], 1)]
    return _doc(name, users, _round_robin(name, [users[0]["user_ref"]], 4))


def missing() -> dict[str, Any]:
    """Ditto authorizes v2 but only ever provisioned v1."""
    name = "missing"
    users = [_user(name, 0, [1, 2], [1], 2)]
    return _doc(name, users, _round_robin(name, [users[0]["user_ref"]], 4))


def unauthorized() -> dict[str, Any]:
    """u00 is authorized; u01 had an adapter provisioned and was then revoked
    (authorized_version null). Ditto must refuse u01 without a miner call."""
    name = "unauthorized"
    users = [_user(name, 0, [1], [1], 1), _user(name, 1, [1], [1], None)]
    return _doc(name, users, _round_robin(name, [u["user_ref"] for u in users], 4))


# Replay layout by request index: ("orig", user_index) sends a new request for
# that user; ("replay", j) re-sends earlier original j with the same request id.
# 6 originals and 4 replays, so the ledger must settle exactly 6 units.
REPLAY_LAYOUT: tuple[tuple[str, int], ...] = (
    ("orig", 0),
    ("orig", 1),
    ("orig", 0),
    ("replay", 0),
    ("orig", 1),
    ("orig", 0),
    ("replay", 2),
    ("replay", 4),
    ("orig", 1),
    ("replay", 8),
)


def replay() -> dict[str, Any]:
    name = "replay"
    users = [_user(name, i, [1], [1], 1) for i in range(2)]
    refs = [u["user_ref"] for u in users]
    requests: list[dict[str, Any]] = []
    for k, (kind, arg) in enumerate(REPLAY_LAYOUT):
        if kind == "orig":
            requests.append({"user_ref": refs[arg], "query_seed": query_seed(name, k)})
        else:
            requests.append({"replay_of": arg})
    return _doc(name, users, requests)


ISOLATION_USERS = 3
ISOLATION_ROUNDS = 6


def isolation() -> dict[str, Any]:
    """Tenants share version numbers [1, 2] with different seeds. Each round
    sends the SAME query to every tenant, interleaved, so any cross-tenant
    cache or routing mix-up produces a wrong (but well-formed) vector."""
    name = "isolation"
    users = [_user(name, i, [1, 2], [1, 2], 2) for i in range(ISOLATION_USERS)]
    requests = [
        {"user_ref": u["user_ref"], "query_seed": query_seed(name, r)}
        for r in range(ISOLATION_ROUNDS)
        for u in users
    ]
    return _doc(name, users, requests)


BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "smoke": smoke,
    "happy": happy,
    "multi_tenant": multi_tenant,
    "stale": stale,
    "missing": missing,
    "unauthorized": unauthorized,
    "replay": replay,
    "isolation": isolation,
}
assert tuple(BUILDERS) == WORKLOAD_NAMES


def build(name: str) -> dict[str, Any]:
    doc = BUILDERS[name]()
    Workload.from_json(doc)  # every committed fixture must parse
    return doc


def build_all() -> dict[str, dict[str, Any]]:
    return {name: build(name) for name in WORKLOAD_NAMES}


def render(doc: dict[str, Any]) -> str:
    """Canonical on-disk form: key order preserved, one user / request per
    line (so diffs stay readable), trailing newline. Always valid JSON."""
    lines = ["{"]
    items = list(doc.items())
    for i, (key, value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        if isinstance(value, list) and value:
            lines.append(f"  {json.dumps(key)}: [")
            for j, element in enumerate(value):
                tail = "," if j < len(value) - 1 else ""
                lines.append(f"    {json.dumps(element)}{tail}")
            lines.append(f"  ]{comma}")
        else:
            lines.append(f"  {json.dumps(key)}: {json.dumps(value)}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, doc in build_all().items():
        path = directory / f"{name}.json"
        path.write_text(render(doc))
        written.append(path)
    return written


def check(directory: Path) -> list[str]:
    """Return one message per drifted, missing, or unexpected workload file."""
    problems: list[str] = []
    expected = {f"{name}.json": render(doc) for name, doc in build_all().items()}
    for filename, text in expected.items():
        path = directory / filename
        if not path.is_file():
            problems.append(f"missing: {path}")
        elif path.read_text() != text:
            problems.append(f"drift: {path}")
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if path.name not in expected:
                problems.append(f"unexpected: {path}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m query_adapter_pilot.fixtures",
        description="Write or check the deterministic synthetic pilot workloads.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", metavar="DIR", type=Path, help="write every workload to DIR")
    group.add_argument(
        "--check",
        metavar="DIR",
        type=Path,
        nargs="?",
        const=DEFAULT_DIR,
        help=f"exit 1 if DIR differs from the builders (default {DEFAULT_DIR})",
    )
    args = parser.parse_args(argv)
    if args.write is not None:
        for path in write(args.write):
            print(f"wrote {path}")
        return 0
    problems = check(args.check)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("fixtures drifted; run: python -m query_adapter_pilot.fixtures --write DIR")
        return 1
    print(f"fixtures clean: {len(WORKLOAD_NAMES)} workloads in {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
