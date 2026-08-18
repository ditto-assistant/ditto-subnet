"""Per-URL Open Graph / Twitter / JSON-LD for public miner profile pages.

The dashboard is an SPA. Crawlers that unfurl
``https://dittobench.ai/miner/<hotkey>`` never run the client, so the HTML
shell has to carry the share card itself.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "/assets/paperditto-512.png"
_DEFAULT_IMAGE_ALT = "Ditto paper mascot"
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(
    r'<meta\s+name="description"\s+content="[^"]*"\s*/?>', re.IGNORECASE
)
_CANONICAL_RE = re.compile(
    r'<link\s+rel="canonical"\s+href="[^"]*"\s*/?>', re.IGNORECASE
)
_OG_RE = re.compile(
    r'<meta\s+property="og:[^"]+"\s+content="[^"]*"\s*/?>\s*', re.IGNORECASE
)
_TWITTER_RE = re.compile(
    r'<meta\s+name="twitter:[^"]+"\s+content="[^"]*"\s*/?>\s*', re.IGNORECASE
)
_JSONLD_RE = re.compile(
    r'<script\s+type="application/ld\+json">.*?</script>\s*',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ShareCard:
    title: str
    description: str
    url: str
    image: str
    image_alt: str
    json_ld: dict[str, object]


def request_origin(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto")
    proto = (forwarded or request.url.scheme or "https").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def shorten_hotkey(hotkey: str) -> str:
    if len(hotkey) <= 14:
        return hotkey
    return f"{hotkey[:6]}…{hotkey[-4:]}"


def path_share_card(*, origin: str, path: str, miner_id: str) -> ShareCard:
    """Card when we only know the URL (no profile row, or no DB yet)."""
    label = shorten_hotkey(miner_id)
    url = f"{origin}{path}"
    return ShareCard(
        title=f"{label} · Ditto SN118 miner",
        description=(
            "Public miner profile on Ditto SN118: submissions, scores, and "
            "handle reservation."
        ),
        url=url,
        image=f"{origin}{_DEFAULT_IMAGE}",
        image_alt=_DEFAULT_IMAGE_ALT,
        json_ld={
            "@context": "https://schema.org",
            "@type": "Person",
            "name": label,
            "identifier": miner_id,
            "url": url,
            "image": f"{origin}{_DEFAULT_IMAGE}",
        },
    )


def profile_share_card(*, origin: str, profile: Any) -> ShareCard:
    hotkey = str(profile.miner_hotkey)
    handle = profile.name_handle
    stem = None if handle is None else handle.stem
    status = None if handle is None else handle.status
    if stem and status in {"reserved", "pending", "disputed"}:
        label = str(stem)
        if status == "pending":
            title = f"{label} (pending) · Ditto SN118 miner"
        elif status == "disputed":
            title = f"{label} · Ditto SN118 miner"
        else:
            title = f"{label} · Ditto SN118 miner"
    else:
        label = shorten_hotkey(hotkey)
        title = f"{label} · Ditto SN118 miner"

    submissions = list(profile.submissions or [])
    socials = profile.profile
    bits: list[str] = []
    if stem and status == "reserved":
        bits.append(f"Reserved handle {stem}.")
    elif stem and status == "pending":
        bits.append(f"Pending handle {stem}.")
    if submissions:
        noun = "submission" if len(submissions) == 1 else "submissions"
        bits.append(f"{len(submissions)} public {noun}.")
    same_as: list[str] = []
    if socials is not None:
        x_url = socials.x_url
        github = socials.github_url
        discord = socials.discord_handle
        if x_url:
            bits.append(f"X {x_url}")
            same_as.append(str(x_url))
        if github:
            bits.append(f"GitHub {github}")
            same_as.append(str(github))
        if discord:
            bits.append(f"Discord @{discord}")
    bits.append("Live scores and evidence on Ditto SN118.")
    description = " ".join(bits)

    avatar = profile.avatar_url
    image = f"{origin}{avatar}" if avatar else f"{origin}{_DEFAULT_IMAGE}"
    image_alt = f"{label} profile picture" if avatar else _DEFAULT_IMAGE_ALT
    url = f"{origin}/miner/{hotkey}"
    person: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": label,
        "identifier": hotkey,
        "url": url,
        "image": image,
    }
    if same_as:
        person["sameAs"] = same_as
    return ShareCard(
        title=title,
        description=description,
        url=url,
        image=image,
        image_alt=image_alt,
        json_ld=person,
    )


def apply_share_card(page: str, card: ShareCard) -> str:
    """Rewrite title / description / OG / Twitter / JSON-LD in the SPA shell."""
    esc = html.escape

    def attr(tag: str, name: str, value: str, *, prop: bool) -> str:
        key = "property" if prop else "name"
        return f'<{tag} {key}="{name}" content="{esc(value, quote=True)}" />'

    title = f"<title>{esc(card.title)}</title>"
    description = attr("meta", "description", card.description, prop=False)
    canonical = f'<link rel="canonical" href="{esc(card.url, quote=True)}" />'
    og_block = "\n    ".join(
        [
            attr("meta", "og:type", "profile", prop=True),
            attr("meta", "og:site_name", "Ditto", prop=True),
            attr("meta", "og:title", card.title, prop=True),
            attr("meta", "og:description", card.description, prop=True),
            attr("meta", "og:url", card.url, prop=True),
            attr("meta", "og:image", card.image, prop=True),
            attr("meta", "og:image:type", "image/png", prop=True),
            attr("meta", "og:image:alt", card.image_alt, prop=True),
            attr("meta", "twitter:card", "summary", prop=False),
            attr("meta", "twitter:title", card.title, prop=False),
            attr("meta", "twitter:description", card.description, prop=False),
            attr("meta", "twitter:image", card.image, prop=False),
            attr("meta", "twitter:image:alt", card.image_alt, prop=False),
        ]
    )
    json_ld = (
        '<script type="application/ld+json">'
        + json.dumps(card.json_ld, ensure_ascii=True, separators=(",", ":")).replace(
            "<", "\\u003c"
        )
        + "</script>"
    )

    rewritten = _TITLE_RE.sub(title, page, count=1) if _TITLE_RE.search(page) else page
    rewritten = (
        _DESC_RE.sub(description, rewritten, count=1)
        if _DESC_RE.search(rewritten)
        else rewritten.replace("<head>", f"<head>\n    {description}", 1)
    )
    rewritten = (
        _CANONICAL_RE.sub(canonical, rewritten, count=1)
        if _CANONICAL_RE.search(rewritten)
        else rewritten.replace("<head>", f"<head>\n    {canonical}", 1)
    )
    rewritten = _OG_RE.sub("", rewritten)
    rewritten = _TWITTER_RE.sub("", rewritten)
    rewritten = _JSONLD_RE.sub("", rewritten)
    inject = f"{og_block}\n    {json_ld}\n    "
    if "<title>" in rewritten:
        rewritten = rewritten.replace("<title>", inject + "<title>", 1)
    else:
        rewritten = rewritten.replace("<head>", f"<head>\n    {inject}", 1)
    return rewritten


async def share_card_for_miner(
    request: Request, *, miner_id: str, path: str
) -> ShareCard:
    origin = request_origin(request)
    fallback = path_share_card(origin=origin, path=path, miner_id=miner_id)
    session_maker = getattr(request.app.state, "session_maker", None)
    if session_maker is None:
        return fallback
    try:
        from ditto.api_server.endpoints.public import public_miner_profile

        async with session_maker() as session:
            profile = await public_miner_profile(miner_id, Response(), session)
    except HTTPException:
        return fallback
    except Exception:
        logger.exception("miner share card lookup failed for %s", miner_id)
        return fallback
    return profile_share_card(origin=origin, profile=profile)
