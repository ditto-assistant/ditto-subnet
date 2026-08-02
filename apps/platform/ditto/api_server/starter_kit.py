"""Read access to the packaged starter-kit baseline.

See ``docs/anti-copy-reference.md`` for the corpus this pairs with.

Every SN118 submission descends from the official starter kit, so most of a
quarantined tarball is code the miner never wrote. Reviewing it cold means
reading a whole crate to find the handful of files that matter. This module
serves the committed baseline bundle so the review path can subtract the kit and
show only the miner's own work.

The bundle is built offline by ``scripts/build_starter_kit_baseline.py`` and
committed, matching the reference-shingle design: request handling never needs
network access, and the baseline is pinned to an auditable commit set.

Distinct from :func:`ditto.api_server.fingerprint.reference_corpus_provenance`,
which describes the *hashed* corpus used for similarity subtraction. That corpus
is one-way and cannot yield text; this bundle is the text side of the same kit.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from functools import lru_cache
from importlib.resources import files as resource_files
from typing import Any

from ditto.api_server.fingerprint import _normalized_source

BUNDLE_NAME = "starter_kit_baseline_v1.json.gz"

# A submission whose tarball nests the crate under a single top-level directory
# would otherwise share no paths with the kit and read as 100% custom. Aligning
# is bounded to one leading component: deeper guessing risks inventing matches.
_MAX_ROOT_STRIP = 1


@lru_cache(maxsize=1)
def _bundle() -> dict[str, Any]:
    raw = resource_files("ditto.anticopy").joinpath(BUNDLE_NAME).read_bytes()
    bundle: dict[str, Any] = json.loads(gzip.decompress(raw))
    if bundle.get("format") != "starter-kit-baseline-v1":
        raise RuntimeError(f"unexpected starter-kit baseline format in {BUNDLE_NAME}")
    return bundle


def starter_kit_provenance() -> dict[str, str]:
    """Immutable identity of the packaged baseline, safe to expose to operators.

    Contains only the public repository, the exact revision, and the digest of
    the included commit set — never submitted artifact data.
    """
    bundle = _bundle()
    return {
        "source": str(bundle["source"]),
        "revision": str(bundle["revision"]),
        "commit_set_sha256": str(bundle["commit_set_sha256"]),
        "commit_count": str(len(bundle["commits"])),
    }


@lru_cache(maxsize=1)
def starter_kit_head_text() -> dict[str, str]:
    """``path -> text`` for the baseline tip tree: the diff's reference side."""
    bundle = _bundle()
    blobs = bundle["blobs"]
    return {path: blobs[digest] for path, digest in bundle["head"].items()}


@lru_cache(maxsize=1)
def _historical() -> tuple[frozenset[str], frozenset[str]]:
    bundle = _bundle()
    return (
        frozenset(bundle["historical_sha256"]),
        frozenset(bundle["historical_normalized_sha256"]),
    )


def is_stock_kit_text(text: str) -> bool:
    """True when this exact file content is starter-kit code at *any* revision.

    Miners fork the kit at different commits, so a file can be untouched kit
    code while still differing from the tip. Matching against the whole lineage
    keeps those files out of the operator's "what did they write" delta. The
    normalized channel additionally catches a kit file that was only reformatted
    or re-commented, reusing the anti-copy canonicalizer.
    """
    exact, normalized = _historical()
    if hashlib.sha256(text.encode("utf-8")).hexdigest() in exact:
        return True
    canonical = _normalized_source(text.encode("utf-8"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() in normalized


def align_candidate_paths(candidate: dict[str, str]) -> dict[str, str]:
    """Strip a single wrapping directory when that is what makes paths line up.

    Returns the input unchanged unless every candidate path shares one leading
    component and stripping it strictly increases overlap with the baseline, so
    an archive that genuinely has its own top-level layout is left alone.
    """
    if not candidate:
        return candidate
    head = starter_kit_head_text()
    roots = {path.split("/", 1)[0] for path in candidate if "/" in path}
    if len(roots) != 1 or any("/" not in path for path in candidate):
        return candidate
    stripped = {
        path.split("/", _MAX_ROOT_STRIP)[1]: text for path, text in candidate.items()
    }
    if len(stripped) != len(candidate):
        return candidate
    if len(set(stripped) & set(head)) <= len(set(candidate) & set(head)):
        return candidate
    return stripped
