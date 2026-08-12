"""Build the known-starter-kit *file* digest set.

Usage::

    uv run python scripts/build_kit_file_digests.py
    uv run python scripts/build_kit_file_digests.py --extra-tree /path/to/kit@v0.3.1

The shingle bundles (``build_reference_fingerprints.py``) subtract kit *windows*
from a sketch. That is the right tool for a file a miner edited: the kit lines
inside it wash out and the miner's edit survives. It is the wrong tool for a
file the miner never touched at all, because subtraction only removes windows
from the exact revisions the bundle was built from — and the bundle is built
from the in-monorepo kit path, so it knows nothing about the upstream revisions
miners forked before the kit moved in here. An untouched ``README.md`` from a
July fork therefore survives subtraction whole and reads as shared authored
text between every miner who forked at that revision.

This bundle answers the file-level question instead: *is this exact file
published starter-kit content at any revision we know about?* Digests only —
like the shingle bundles it is one-way and cannot yield a path, a line, or a
file. :mod:`ditto.api_server.fingerprint` drops matching members before
shingling, so an unmodified kit file contributes nothing to any fingerprint
while a modified one stays in the sketch in full. That is the property a
filename or extension denylist cannot have: miners do edit ``src/catalog.rs``,
and those edits are authored work that must still count.

Sources, unioned (each recorded in the manifest's ``sources``):

1. every blob ever committed under the kit path in this monorepo, all revisions,
   binary included;
2. the historical text digests already committed in
   ``starter_kit_baseline_v1.json.gz`` — the upstream mainline lineage, which is
   what covers pre-import forks;
3. ``--extra-tree DIR`` (repeatable): every file under a checked-out published
   kit revision. This is the documented way to add a revision that neither of
   the above covers — check out the upstream tag, point this at it, and commit
   the regenerated bundle;
4. ``kit_revisions_extra.json``: hand-curated digests with provenance, for a
   revision that is no longer checkoutable.

Output is deterministic (sorted digests) and committed, so upload processing
never needs network access.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).parents[3]
OUTPUT = Path(__file__).parents[1] / "ditto" / "anticopy"
BUNDLE_NAME = "kit_file_digests_v1.json"
BASELINE_BUNDLE_NAME = "starter_kit_baseline_v1.json.gz"
EXTRA_REVISIONS_NAME = "kit_revisions_extra.json"
DEFAULT_KIT_PATH = "miners/dittobench-starter-kit"
FINGERPRINT_MODULE = (
    Path(__file__).parents[1] / "ditto" / "api_server" / "fingerprint.py"
)


def _load_fingerprint_module() -> ModuleType:
    """Load the pure fingerprint helpers without importing the API package.

    Same reason as ``build_reference_fingerprints.py``: the refresh workflow runs
    on a bare interpreter, and importing ``ditto.api_server`` would drag in the
    whole FastAPI runtime.
    """
    spec = importlib.util.spec_from_file_location(
        "ditto_kit_digest_fingerprint", FINGERPRINT_MODULE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fingerprint helpers from {FINGERPRINT_MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_normalized_source = _load_fingerprint_module()._normalized_source


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def _digests(raw: bytes) -> tuple[str, str | None]:
    """Return ``(raw_sha256, normalized_sha256)`` for one file's bytes.

    The normalized digest is only meaningful for text, and is skipped for a blob
    that is not valid UTF-8 so a binary fixture cannot collide with source.
    """
    exact = hashlib.sha256(raw).hexdigest()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return (exact, None)
    canonical = _normalized_source(raw)
    if not canonical:
        return (exact, None)
    return (exact, hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _add(raw_set: set[str], normalized_set: set[str], raw: bytes) -> None:
    exact, canonical = _digests(raw)
    raw_set.add(exact)
    if canonical is not None:
        normalized_set.add(canonical)


@dataclass(frozen=True)
class KitDigestSource:
    """One provenance record: where a slice of the digest set came from."""

    kind: str
    label: str
    blob_count: int
    revision: str | None = None
    commit_count: int | None = None
    note: str = ""

    def to_json(self) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": self.kind,
            "label": self.label,
            "blob_count": self.blob_count,
        }
        if self.revision is not None:
            record["revision"] = self.revision
        if self.commit_count is not None:
            record["commit_count"] = self.commit_count
        if self.note:
            record["note"] = self.note
        return record


@dataclass(frozen=True)
class KitDigestBundle:
    """The committed digest set plus the sources it was unioned from."""

    identity: str
    raw_sha256: tuple[str, ...]
    normalized_sha256: tuple[str, ...]
    sources: tuple[KitDigestSource, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "format": "kit-file-digests-v1",
            "identity": self.identity,
            "sources": [source.to_json() for source in self.sources],
            "raw_sha256": list(self.raw_sha256),
            "normalized_sha256": list(self.normalized_sha256),
        }


def _from_repo_path(
    repo: Path, kit_path: str, revision: str, raw_set: set[str], norm_set: set[str]
) -> KitDigestSource:
    """Union every blob ever committed under ``kit_path`` in ``repo``."""
    resolved = _git(repo, "rev-parse", revision).decode().strip()
    commits = sorted(_git(repo, "rev-list", revision).decode().splitlines())
    oids: set[str] = set()
    for commit in commits:
        listing = _git(repo, "ls-tree", "-r", commit, "--", kit_path).decode()
        for line in listing.splitlines():
            metadata = line.split("\t", 1)[0].split()
            if len(metadata) >= 3 and metadata[1] == "blob":
                oids.add(metadata[2])
    for oid in sorted(oids):
        _add(raw_set, norm_set, _git(repo, "cat-file", "blob", oid))
    return KitDigestSource(
        kind="monorepo-path-history",
        label=kit_path,
        blob_count=len(oids),
        revision=resolved,
        commit_count=len(commits),
    )


def _from_baseline_bundle(
    bundle_path: Path, raw_set: set[str], norm_set: set[str]
) -> KitDigestSource:
    """Inherit the upstream mainline lineage already committed for review.

    ``historical_sha256`` is ``sha256(text.encode())`` over strictly-decoded
    blobs, which is byte-identical to ``sha256(raw)`` for those files, so the two
    digest spaces are the same and can be unioned directly.
    """
    bundle = json.loads(gzip.decompress(bundle_path.read_bytes()))
    raw_set.update(bundle["historical_sha256"])
    norm_set.update(bundle["historical_normalized_sha256"])
    return KitDigestSource(
        kind="starter-kit-baseline-bundle",
        label=str(bundle["source"]),
        blob_count=len(bundle["historical_sha256"]),
        revision=str(bundle["revision"]),
        commit_count=len(bundle["commits"]),
    )


def _from_tree(tree: Path, raw_set: set[str], norm_set: set[str]) -> KitDigestSource:
    """Hash every file under a checked-out published kit revision."""
    files = sorted(p for p in tree.rglob("*") if p.is_file() and ".git" not in p.parts)
    for path in files:
        _add(raw_set, norm_set, path.read_bytes())
    return KitDigestSource(kind="extra-tree", label=tree.name, blob_count=len(files))


def _from_extra_revisions(
    path: Path, raw_set: set[str], norm_set: set[str]
) -> list[KitDigestSource]:
    """Merge hand-curated digests for revisions that cannot be checked out."""
    if not path.exists():
        return []
    document = json.loads(path.read_text())
    sources: list[KitDigestSource] = []
    for revision in document.get("revisions", []):
        raw_set.update(revision.get("raw_sha256", ()))
        norm_set.update(revision.get("normalized_sha256", ()))
        sources.append(
            KitDigestSource(
                kind="curated-revision",
                label=str(revision.get("label", "unlabeled")),
                blob_count=len(revision.get("raw_sha256", ())),
                note=str(revision.get("note", "")),
            )
        )
    return sources


def build_kit_file_digests(
    *,
    repo: Path = REPO_ROOT,
    kit_path: str = DEFAULT_KIT_PATH,
    revision: str = "HEAD",
    extra_trees: tuple[Path, ...] = (),
    output: Path = OUTPUT,
) -> KitDigestBundle:
    """Build the deterministic digest bundle, write it, and return it."""
    output.mkdir(parents=True, exist_ok=True)
    raw_set: set[str] = set()
    norm_set: set[str] = set()
    sources: list[KitDigestSource] = [
        _from_repo_path(repo, kit_path, revision, raw_set, norm_set)
    ]
    baseline = output / BASELINE_BUNDLE_NAME
    if baseline.exists():
        sources.append(_from_baseline_bundle(baseline, raw_set, norm_set))
    for tree in extra_trees:
        sources.append(_from_tree(tree, raw_set, norm_set))
    sources.extend(
        _from_extra_revisions(output / EXTRA_REVISIONS_NAME, raw_set, norm_set)
    )

    raw_sorted = sorted(raw_set)
    norm_sorted = sorted(norm_set)
    bundle = KitDigestBundle(
        identity=hashlib.sha256(
            ("\n".join(raw_sorted) + "\x00" + "\n".join(norm_sorted)).encode()
        ).hexdigest(),
        raw_sha256=tuple(raw_sorted),
        normalized_sha256=tuple(norm_sorted),
        sources=tuple(sources),
    )
    (output / BUNDLE_NAME).write_text(
        json.dumps(bundle.to_json(), indent=2, sort_keys=True) + "\n"
    )
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--kit-path", default=DEFAULT_KIT_PATH)
    parser.add_argument(
        "--revision",
        default="HEAD",
        help="monorepo lineage whose kit-path blobs are included (default: HEAD)",
    )
    parser.add_argument(
        "--extra-tree",
        type=Path,
        action="append",
        default=[],
        help="checked-out published kit revision to include (repeatable)",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    bundle = build_kit_file_digests(
        repo=args.repo,
        kit_path=args.kit_path,
        revision=args.revision,
        extra_trees=tuple(args.extra_tree),
        output=args.output,
    )
    print(
        f"{BUNDLE_NAME}: {len(bundle.raw_sha256)} exact + "
        f"{len(bundle.normalized_sha256)} normalized digests "
        f"from {len(bundle.sources)} sources @ {bundle.identity[:12]}"
    )


if __name__ == "__main__":
    main()
