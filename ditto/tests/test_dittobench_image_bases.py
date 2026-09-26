"""Every external base of a released or deployed DittoBench image is digest-pinned.

A tag such as ``alpine:3.22`` or ``distroless/static:nonroot`` can be repointed
upstream, so the next release would silently build on different bytes. The
digest makes that an explicit, reviewed change; Dependabot (``docker`` on
``/services/dittobench-api``) still proposes updates for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = (
    ROOT / "services/dittobench-api/Dockerfile",
    ROOT / "services/dittobench-api/Dockerfile.egress-proxy",
)
_FROM = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.I | re.M
)


def _external_bases(text: str) -> list[str]:
    stages: set[str] = set()
    bases: list[str] = []
    for match in _FROM.finditer(text):
        image, alias = match.group(1), match.group(2)
        if image != "scratch" and image.lower() not in stages:
            bases.append(image)
        if alias:
            stages.add(alias.lower())
    return bases


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_external_bases_are_digest_pinned(dockerfile: Path) -> None:
    bases = _external_bases(dockerfile.read_text())
    assert bases, f"{dockerfile} has no external FROM line"
    unpinned = [base for base in bases if not re.search(r"@sha256:[0-9a-f]{64}$", base)]
    assert not unpinned, f"{dockerfile.name} has tag-only bases: {unpinned}"


def test_stage_references_are_not_treated_as_bases() -> None:
    text = "FROM golang:1@sha256:" + "a" * 64 + " AS build\nFROM build AS copy\n"
    assert _external_bases(text) == ["golang:1@sha256:" + "a" * 64]
