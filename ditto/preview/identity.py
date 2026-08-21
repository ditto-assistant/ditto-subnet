"""Stable preview identity: branch slug plus a short commit hash."""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def preview_id(ref: str, sha: str) -> str:
    """Return ``{slug}-{sha8}`` suitable for DNS labels and compose projects.

    Args:
        ref: Branch, tag, or pull-request ref.
        sha: Full or abbreviated git SHA.
    """

    slug = _SLUG_RE.sub("-", (ref or "preview").lower()).strip("-")
    if slug.startswith("refs-heads-"):
        slug = slug[len("refs-heads-") :]
    if slug.startswith("refs-pull-"):
        slug = "pr-" + slug.split("-")[2] if len(slug.split("-")) > 2 else slug
    slug = slug[:32].strip("-") or "preview"
    digest = re.sub(r"[^0-9a-f]", "", (sha or "").lower())[:8]
    if len(digest) < 7:
        raise ValueError("preview identity requires a git SHA")
    return f"{slug}-{digest}"


def preview_host(
    identity: str, component: str, zone: str = "preview.dittobench.ai"
) -> str:
    """Hostname for a preview component under ``*.preview.dittobench.ai``."""
    label = f"{component}-{identity}".replace("_", "-")[:63].strip("-")
    return f"{label}.{zone}"
