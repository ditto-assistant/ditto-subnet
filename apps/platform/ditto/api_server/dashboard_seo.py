"""Crawler-facing documents and a short-lived HTML snapshot of the live board.

The dashboard SPA is client-rendered. Search engines and AI crawlers that do
not execute JavaScript would otherwise see an empty shell, and even Googlebot
would recrawl a boot-time HTML document for as long as its cache allowed.
This module:

* serves ``robots.txt``, ``sitemap.xml``, and ``llms.txt`` (the landing-astro
  discovery set, adapted for SN118);
* injects page-specific canonicals plus JSON-LD / ``<noscript>`` standings
  into the SPA HTML at request time;
* caches that snapshot for 30s so a crawl storm costs one leaderboard read
  and crawlers still see standings that are at most half a minute old.

A missing database, a failed board read, or a shell without injection
markers fail closed: the static SPA is served unchanged and no sample ranks
are invented.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from fastapi import Request, Response

from ditto.api_models.public import PublicLeaderboardResponse
from ditto.api_server.benchmark_rollout import inference_activation_requirements
from ditto.db.queries.benchmark_rollout import (
    CANARY_BENCH_VERSION,
    bind_inference_activation_requirements,
)

logger = logging.getLogger(__name__)

# Match the public leaderboard's freshness contract. must-revalidate keeps a
# CDN or Googlebot from serving a snapshot past that window without checking.
HTML_CACHE_CONTROL = "public, max-age=30, stale-while-revalidate=120, must-revalidate"
DOC_CACHE_CONTROL = "public, max-age=30, stale-while-revalidate=120"
# robots.txt / llms.txt do not carry live ranks; a slightly longer TTL is fine.
STATIC_DOC_CACHE_CONTROL = "public, max-age=300, stale-while-revalidate=3600"

_SNAPSHOT_TTL_SECONDS = 30.0
_SNAPSHOT_FAILURE_TTL_SECONDS = 5.0
_DEFAULT_ORIGIN = "https://platform-api.heyditto.ai"
_PROD_HOST_SUFFIX = "heyditto.ai"

# Pathnames the SPA also understands as pages. Hash routes stay valid; these
# exist so a sitemap URL is a real document URL, not a fragment.
CRAWLABLE_PAGES: tuple[tuple[str, str, str], ...] = (
    ("/", "overview", "Ditto SN118 · Subnet Leaderboard"),
    ("/overview", "overview", "Overview · Ditto SN118"),
    ("/leaderboard", "leaderboard", "Leaderboard · Ditto SN118"),
    ("/benchmark", "benchmark", "Benchmark · Ditto SN118"),
    ("/pipeline", "pipeline", "Submission pipeline · Ditto SN118"),
    ("/operations", "operations", "Validator fleet · Ditto SN118"),
    ("/submissions", "submissions", "Recent submissions · Ditto SN118"),
    ("/ath", "ath", "ATH reviews · Ditto SN118"),
)

CRAWLABLE_PAGE_PATHS: tuple[str, ...] = tuple(
    path for path, _page, _title in CRAWLABLE_PAGES if path != "/"
)

# Miner sign-in is a form, not a public dataset. Keep it off the sitemap.
_NOINDEX_PATHS = frozenset({"/reviews"})

_HEAD_BLOCK = re.compile(
    r"<!--ditto:seo-head-->.*?<!--/ditto:seo-head-->",
    re.DOTALL,
)
_BODY_BLOCK = re.compile(
    r"<!--ditto:seo-body-->.*?<!--/ditto:seo-body-->",
    re.DOTALL,
)
_ENTITY_PATH = re.compile(
    r"^/(?P<kind>agent|miner|h|agents|miners|validators|screeners)"
    r"/(?P<ident>[^/]+)/?$"
)

_STATIC_DESCRIPTION = (
    "Live Ditto SN118 leaderboard scores, submission progress, and validator health."
)
_DEFAULT_OG_IMAGE = "/assets/paperditto-512.png"
_DEFAULT_OG_ALT = "Ditto paper mascot"


@dataclass(frozen=True)
class SeoMiner:
    """One ranked public-board row, stripped to what a crawler should see."""

    rank: int
    agent_id: str
    agent_name: str
    miner_hotkey: str
    official_composite: float
    handle: str | None
    avatar_url: str | None = None

    @property
    def label(self) -> str:
        return self.handle or self.agent_name

    @property
    def profile_path(self) -> str:
        if self.handle:
            return "/h/" + quote(self.handle, safe="")
        return "/miner/" + quote(self.miner_hotkey, safe="")


@dataclass(frozen=True)
class SeoSnapshot:
    """Cacheable projection of the public leaderboard for HTML / sitemap."""

    generated_at: datetime
    count: int
    bench_version: int
    champion_hotkey: str | None
    miners: tuple[SeoMiner, ...]

    def miner_for(self, ident: str) -> SeoMiner | None:
        needle = ident.casefold()
        for miner in self.miners:
            if miner.miner_hotkey == ident or miner.agent_id == ident:
                return miner
            if miner.handle is not None and miner.handle.casefold() == needle:
                return miner
        return None

    def champion(self) -> SeoMiner | None:
        if self.champion_hotkey:
            for miner in self.miners:
                if miner.miner_hotkey == self.champion_hotkey:
                    return miner
        return self.miners[0] if self.miners else None


@dataclass
class _SnapshotCache:
    expires_at: float
    snapshot: SeoSnapshot | None


_cache_lock = asyncio.Lock()
_cached: _SnapshotCache | None = None


def reset_seo_cache() -> None:
    """Drop the process-local snapshot (tests)."""
    global _cached
    _cached = None


def public_origin(request: Request) -> str:
    """Absolute origin the HTML and sitemap should advertise.

    Prefers the forwarded host Caddy sets so a request that arrived as
    ``platform-api.heyditto.ai`` is not canonicalized to the VM's internal
    name. Production hosts are always https.
    """
    forwarded_host = (
        (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    )
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    forwarded_proto = (
        (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    )
    proto = forwarded_proto or request.url.scheme or "https"
    if host.endswith(_PROD_HOST_SUFFIX):
        proto = "https"
    if not host:
        return _DEFAULT_ORIGIN
    return f"{proto}://{host}"


def robots_txt(origin: str) -> str:
    return (
        "# robots.txt for the Ditto SN118 public dashboard\n"
        "# Allow all crawlers (including AI bots) — wildcard covers everything.\n"
        "\n"
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {origin}/sitemap.xml\n"
    )


def llms_txt(origin: str) -> str:
    api = f"{origin}/api/v1/public"
    return "\n".join(
        [
            "# Ditto SN118",
            "",
            "> Public transparency dashboard for Bittensor Subnet 118 (Ditto).",
            "> Live miner leaderboard, submission pipeline, validator fleet,",
            "> and DittoBench scoring. All numbers come from the platform's",
            "> public read API; nothing here is a sample.",
            "",
            "The dashboard is the human front door. The same data is available",
            "as JSON with `Cache-Control: public, max-age=30`, so a crawler",
            "that wants the current standings should fetch the API rather than",
            "scrape the SPA.",
            "",
            "## Live JSON (max-age=30s)",
            "",
            f"- Leaderboard: {api}/leaderboard",
            f"- Health: {api}/health",
            f"- Submission activity: {api}/activity",
            f"- Validator / screener fleet: {api}/operations",
            f"- Revealed on-chain weights: {api}/weights",
            f"- Benchmark timeline: {api}/bench/timeline",
            f"- Benchmark config: {api}/bench/config",
            f"- Benchmark glossary: {api}/bench/glossary",
            "",
            "## Pages",
            "",
            f"- Overview: {origin}/",
            f"- Leaderboard: {origin}/leaderboard",
            f"- Benchmark: {origin}/benchmark",
            f"- Submission pipeline: {origin}/pipeline",
            f"- Validator fleet: {origin}/operations",
            f"- Recent submissions: {origin}/submissions",
            f"- ATH reviews: {origin}/ath",
            f"- Miner profile: {origin}/miner/{{hotkey}}",
            f"  or {origin}/h/{{handle}}",
            f"- Agent evidence: {origin}/agent/{{agent_id}}",
            "",
            "## Quick facts",
            "",
            "- Subnet: Bittensor SN118 (Ditto)",
            f"- Dashboard: {origin}/",
            "- Product landing: https://heyditto.ai",
            "- App: https://assistant.heyditto.ai",
            "- Source: https://github.com/ditto-assistant/ditto-subnet",
            "- Company: Omni Aura LLC",
            "- Contact: support@heyditto.ai",
            "",
            "## What this site is",
            "",
            "SN118 scores miner-submitted AI memory agents on DittoBench.",
            "Validators independently score each submission; the platform",
            "publishes the aggregate board, the KOTH emissions projection,",
            "and the public evidence record. Rank is by official composite",
            "on the active benchmark, not raw median alone.",
            "",
            "This host is not the consumer Ditto assistant. It does not",
            "accept sign-ups and it does not serve private product data.",
            "",
            "## Machine-readable companion",
            "",
            f"- {origin}/llms-full.txt — public API map and scoring vocabulary",
            f"- {origin}/sitemap.xml — crawlable pages, refreshed with the board",
            "",
        ]
    )


def llms_full_txt(origin: str) -> str:
    api = f"{origin}/api/v1/public"
    extra = "\n".join(
        [
            "## Public API map",
            "",
            "All of the following are unauthenticated GET endpoints under",
            f"`{api}`. Successful responses are cacheable. Authenticated",
            "miner, validator, screener, and admin surfaces are out of",
            "scope for crawlers.",
            "",
            "| Path | What it is |",
            "| --- | --- |",
            "| `/leaderboard` | Ranked miners and KOTH emissions projection |",
            "| `/health` | Coarse subnet / API health |",
            "| `/activity` | Recent submission lifecycle events |",
            "| `/operations` | Validator and screener fleet snapshot |",
            "| `/weights` | Last publicly revealed SN118 weight matrix |",
            "| `/validators` | Signed validator worker availability |",
            "| `/screeners` | Screener heartbeats |",
            "| `/submissions` | Recent scored / in-flight submissions |",
            "| `/agent/{{id}}/summary` | Compact public evidence for one agent |",
            "| `/agent/{{id}}/scores` | Finalized k=3 score record |",
            "| `/agent/{{id}}/pipeline` | Screening + validator ticket history |",
            "| `/miners/{{hotkey}}/avatar` | Public miner avatar |",
            "| `/bench/config` | Frozen scoring setup for the active bench |",
            "| `/bench/glossary` | Metric and category vocabulary |",
            "| `/bench/timeline` | Historical bench releases |",
            "| `/bench/rollout` | In-progress bench version collection |",
            "",
            "Source tarballs become downloadable only after the public-source",
            "window; review-held and rejected source stays private.",
            "In-progress score rows omit validator identity and ticket",
            "signatures.",
            "",
            "## Ranking vocabulary",
            "",
            "- `composite` — canonical three-validator median. Provenance,",
            "  not the rank key once continual waves or confirmation are",
            "  active.",
            "- `official_composite` — the quality that ranks the board and",
            "  drives the weight fold.",
            "- `rank` — 1-based order by `official_composite`. Null on",
            "  unranked provisional / confirmation-gated rows.",
            "- `emissions` — read-only KOTH projection. Validators still",
            "  compute and submit the authoritative weight vector; Yuma",
            "  combines revealed inputs.",
            "- `registered` — whether the miner hotkey currently has a UID.",
            "  A deregistered score stays visible but is excluded from",
            "  weights.",
            "",
            "Do not invent missing numbers. If an endpoint is unavailable,",
            "say so.",
            "",
            "## Related sites",
            "",
            "- Consumer product: https://heyditto.ai",
            "- Assistant app: https://assistant.heyditto.ai",
            f"- This dashboard: {origin}/",
            "",
        ]
    )
    return "# Ditto SN118 — full AI-readable reference\n\n" + llms_txt(origin) + extra


def sitemap_xml(origin: str, snapshot: SeoSnapshot | None) -> str:
    now = (
        snapshot.generated_at.astimezone(UTC)
        if snapshot is not None
        else datetime.now(UTC)
    )
    lastmod = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    urls: list[tuple[str, str, str]] = [
        (origin + "/", "1.0", "always"),
        (origin + "/leaderboard", "0.9", "always"),
        (origin + "/benchmark", "0.7", "hourly"),
        (origin + "/pipeline", "0.6", "hourly"),
        (origin + "/operations", "0.6", "hourly"),
        (origin + "/submissions", "0.5", "hourly"),
        (origin + "/ath", "0.4", "hourly"),
    ]
    if snapshot is not None:
        seen: set[str] = set()
        for miner in snapshot.miners:
            agent_path = "/agent/" + quote(miner.agent_id, safe="")
            for path in (miner.profile_path, agent_path):
                loc = origin + path
                if loc in seen:
                    continue
                seen.add(loc)
                urls.append((loc, "0.5", "hourly"))

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, priority, changefreq in urls:
        parts.extend(
            [
                "  <url>",
                f"    <loc>{xml_escape(loc)}</loc>",
                f"    <lastmod>{lastmod}</lastmod>",
                f"    <changefreq>{changefreq}</changefreq>",
                f"    <priority>{priority}</priority>",
                "  </url>",
            ]
        )
    parts.append("</urlset>\n")
    return "\n".join(parts)


def snapshot_from_leaderboard(board: PublicLeaderboardResponse) -> SeoSnapshot:
    miners: list[SeoMiner] = []
    for entry in board.entries:
        if entry.rank is None:
            continue
        handle = None
        if entry.name_handle is not None and entry.name_handle.status == "reserved":
            handle = entry.name_handle.stem
        miners.append(
            SeoMiner(
                rank=entry.rank,
                agent_id=str(entry.agent_id),
                agent_name=entry.agent_name,
                miner_hotkey=entry.miner_hotkey,
                official_composite=entry.official_composite,
                handle=handle,
                avatar_url=getattr(entry, "avatar_url", None),
            )
        )
    champion_hotkey = None
    if board.emissions is not None:
        champion_hotkey = board.emissions.champion_miner_hotkey
    return SeoSnapshot(
        generated_at=board.generated_at,
        count=board.count,
        bench_version=board.active_bench_version,
        champion_hotkey=champion_hotkey,
        miners=tuple(miners),
    )


async def load_seo_snapshot(request: Request) -> SeoSnapshot | None:
    """Return the cached board projection, refreshing at most every 30s.

    Fail closed: no session maker, or any error while building the public
    board, yields ``None`` rather than a placeholder ranking.
    """
    global _cached
    now = time.monotonic()
    cached = _cached
    if cached is not None and now < cached.expires_at:
        return cached.snapshot

    async with _cache_lock:
        cached = _cached
        if cached is not None and time.monotonic() < cached.expires_at:
            return cached.snapshot
        snapshot, ttl = await _read_snapshot(request)
        _cached = _SnapshotCache(expires_at=time.monotonic() + ttl, snapshot=snapshot)
        return snapshot


async def _read_snapshot(request: Request) -> tuple[SeoSnapshot | None, float]:
    session_maker = getattr(request.app.state, "session_maker", None)
    if session_maker is None:
        return None, _SNAPSHOT_FAILURE_TTL_SECONDS
    # Imported lazily so importing this module from unit tests that never
    # touch the board does not pull the public endpoint's query graph.
    from ditto.api_server.endpoints.public import build_public_leaderboard

    try:
        async with session_maker() as session:
            config = getattr(request.app.state, "config", None)
            inference_proxy = getattr(config, "inference_proxy", None)
            if inference_proxy is not None:
                bind_inference_activation_requirements(
                    session,
                    inference_activation_requirements(
                        inference_proxy,
                        bench_version=CANARY_BENCH_VERSION,
                    ),
                )
            board = await build_public_leaderboard(request, Response(), session)
        return snapshot_from_leaderboard(board), _SNAPSHOT_TTL_SECONDS
    except Exception:
        logger.warning(
            "dashboard SEO snapshot failed; serving static shell",
            exc_info=True,
        )
        return None, _SNAPSHOT_FAILURE_TTL_SECONDS


def inject_live_seo(
    page: str,
    *,
    origin: str,
    path: str,
    snapshot: SeoSnapshot | None,
) -> str:
    """Replace the marked head/body regions with live crawler documents.

    Returns ``page`` unchanged when the shell has no markers (tests, an old
    dist, or a stripped static host).
    """
    if _HEAD_BLOCK.search(page) is None and _BODY_BLOCK.search(page) is None:
        return page
    title, description, canonical, robots = _page_meta(origin, path, snapshot)
    head = _render_head(
        origin=origin,
        path=path,
        title=title,
        description=description,
        canonical=canonical,
        robots=robots,
        snapshot=snapshot,
    )
    body = _render_body(origin=origin, snapshot=snapshot)
    # Callable replacements: the JSON-LD payload contains `\u003c` escapes
    # that re.sub would otherwise treat as replacement-string syntax.
    out = _HEAD_BLOCK.sub(
        lambda _match: f"<!--ditto:seo-head-->\n{head}\n    <!--/ditto:seo-head-->",
        page,
        count=1,
    )
    return _BODY_BLOCK.sub(
        lambda _match: f"<!--ditto:seo-body-->\n{body}\n    <!--/ditto:seo-body-->",
        out,
        count=1,
    )


def _normalize_path(path: str) -> str:
    clean = path if path.startswith("/") else "/" + path
    if clean != "/" and clean.endswith("/"):
        return clean.rstrip("/")
    return clean


def _entity_miner(path: str, snapshot: SeoSnapshot | None) -> SeoMiner | None:
    if snapshot is None:
        return None
    match = _ENTITY_PATH.match(_normalize_path(path))
    if match is None:
        return None
    return snapshot.miner_for(match.group("ident"))


def _absolute_url(origin: str, url: str) -> str:
    if url.startswith("https://") or url.startswith("http://"):
        return url
    if not url.startswith("/"):
        return origin + "/" + url
    return origin + url


def _og_image(
    origin: str, path: str, snapshot: SeoSnapshot | None
) -> tuple[str, str, bool]:
    """Return ``(url, alt, is_default_logo)``.

    Miner / handle / agent pages use the public avatar when the board has
    one. Board pages and miners without a picture keep the paper mascot.
    """
    miner = _entity_miner(path, snapshot)
    if miner is not None and miner.avatar_url:
        return _absolute_url(origin, miner.avatar_url), miner.label, False
    return origin + _DEFAULT_OG_IMAGE, _DEFAULT_OG_ALT, True


def _page_meta(
    origin: str, path: str, snapshot: SeoSnapshot | None
) -> tuple[str, str, str, str]:
    clean = _normalize_path(path)
    title = "Ditto SN118 · Subnet Leaderboard"
    description = _STATIC_DESCRIPTION
    robots = "index, follow, max-image-preview:large"
    for route, _page, route_title in CRAWLABLE_PAGES:
        if route == clean:
            title = route_title
            break
    if clean in _NOINDEX_PATHS:
        title = "Miner sign-in · Ditto SN118"
        description = "Sign in with your SN118 miner hotkey to manage submissions."
        robots = "noindex, nofollow"
    entity = _ENTITY_PATH.match(clean)
    miner = _entity_miner(clean, snapshot)
    if entity is not None:
        if miner is not None and snapshot is not None:
            title = f"{miner.label} · SN118 rank #{miner.rank}"
            description = (
                f"{miner.label} is rank {miner.rank} on Ditto SN118 with official "
                f"composite {miner.official_composite:.6f} on DittoBench v"
                f"{snapshot.bench_version}."
            )
        else:
            title = f"{entity.group('ident')} · Ditto SN118"
    elif snapshot is not None and clean in {"/", "/overview", "/leaderboard"}:
        description = _live_description(snapshot)
        champion = snapshot.champion()
        if champion is not None and clean in {"/", "/overview"}:
            title = f"Ditto SN118 · #{champion.rank} {champion.label}"
    canonical = origin + (clean if clean != "/overview" else "/")
    return title, description, canonical, robots


def _live_description(snapshot: SeoSnapshot) -> str:
    champion = snapshot.champion()
    if champion is None:
        return (
            f"Live Ditto SN118 leaderboard. {snapshot.count} scored miners on "
            f"DittoBench v{snapshot.bench_version}."
        )
    return (
        f"Live Ditto SN118 leaderboard. Current champion {champion.label} at "
        f"{champion.official_composite:.6f} official composite. "
        f"{snapshot.count} scored miners on DittoBench v{snapshot.bench_version}."
    )


def _render_head(
    *,
    origin: str,
    path: str,
    title: str,
    description: str,
    canonical: str,
    robots: str,
    snapshot: SeoSnapshot | None,
) -> str:
    esc_title = html.escape(title, quote=True)
    esc_desc = html.escape(description, quote=True)
    esc_canonical = html.escape(canonical, quote=True)
    image_url, image_alt, is_logo = _og_image(origin, path, snapshot)
    image = html.escape(image_url, quote=True)
    alt = html.escape(image_alt, quote=True)
    if is_logo:
        image_extras = (
            '    <meta property="og:image:type" content="image/png" />\n'
            '    <meta property="og:image:width" content="512" />\n'
            '    <meta property="og:image:height" content="512" />\n'
        )
    else:
        image_extras = ""
    schemas = [_organization_schema(origin), _website_schema(origin, description)]
    if snapshot is not None and robots.startswith("index"):
        schemas.append(_dataset_schema(origin, snapshot))
        if snapshot.miners:
            schemas.append(_item_list_schema(origin, snapshot))
    json_ld = "\n".join(
        '    <script type="application/ld+json">' + _json_ld(schema) + "</script>"
        for schema in schemas
    )
    return f"""    <title>{esc_title}</title>
    <meta name="description" content="{esc_desc}" />
    <meta name="robots" content="{html.escape(robots, quote=True)}" />
    <link rel="canonical" href="{esc_canonical}" />
    <meta property="og:title" content="{esc_title}" />
    <meta property="og:description" content="{esc_desc}" />
    <meta property="og:url" content="{esc_canonical}" />
    <meta property="og:image" content="{image}" />
{image_extras}    <meta property="og:image:alt" content="{alt}" />
    <meta name="twitter:title" content="{esc_title}" />
    <meta name="twitter:description" content="{esc_desc}" />
    <meta name="twitter:image" content="{image}" />
    <meta name="twitter:image:alt" content="{alt}" />
{json_ld}"""


def _render_body(*, origin: str, snapshot: SeoSnapshot | None) -> str:
    if snapshot is None or not snapshot.miners:
        return (
            "    <noscript>\n"
            "      <p>Ditto SN118 public leaderboard. Enable JavaScript for the "
            "live dashboard, or read the machine-readable board at "
            f'<a href="{html.escape(origin, quote=True)}/api/v1/public/leaderboard">'
            " /api/v1/public/leaderboard</a>.</p>\n"
            "    </noscript>"
        )
    generated = (
        snapshot.generated_at.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    rows = []
    for miner in snapshot.miners:
        href = html.escape(origin + miner.profile_path, quote=True)
        label = html.escape(miner.label)
        score = f"{miner.official_composite:.6f}"
        item = f'#{miner.rank} <a href="{href}">{label}</a> — {score}'
        rows.append(f"        <li>{item}</li>")
    items = "\n".join(rows)
    board_href = html.escape(origin, quote=True) + "/api/v1/public/leaderboard"
    summary = (
        f"Updated {html.escape(generated)}. "
        f"DittoBench v{snapshot.bench_version}. "
        f"{snapshot.count} scored miners."
    )
    return (
        "    <noscript>\n"
        "      <h1>Ditto SN118 leaderboard</h1>\n"
        f"      <p>{summary}</p>\n"
        "      <ol>\n"
        f"{items}\n"
        "      </ol>\n"
        f'      <p>JSON: <a href="{board_href}">/api/v1/public/leaderboard</a></p>\n'
        "    </noscript>"
    )


def _json_ld(schema: dict[str, Any]) -> str:
    """Serialize JSON-LD so ``<`` cannot break out of the script element."""
    return (
        json.dumps(schema, separators=(",", ":"), ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _organization_schema(origin: str) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "Ditto",
        "url": "https://heyditto.ai",
        "logo": origin + "/assets/paperditto-512.png",
        "description": (
            "Ditto is the smart home for your agents. SN118 is the public "
            "Bittensor subnet that scores miner-submitted memory agents "
            "on DittoBench."
        ),
        "sameAs": [
            "https://x.com/heydittoai",
            "https://github.com/ditto-assistant",
            "https://heyditto.ai",
        ],
        "contactPoint": {
            "@type": "ContactPoint",
            "email": "support@heyditto.ai",
            "contactType": "customer support",
        },
    }


def _website_schema(origin: str, description: str) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "Ditto SN118",
        "url": origin + "/",
        "description": description,
        "publisher": {"@type": "Organization", "name": "Ditto"},
    }


def _dataset_schema(origin: str, snapshot: SeoSnapshot) -> dict[str, Any]:
    generated = (
        snapshot.generated_at.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Ditto SN118 leaderboard",
        "description": _live_description(snapshot),
        "url": origin + "/leaderboard",
        "dateModified": generated,
        "creator": {"@type": "Organization", "name": "Ditto"},
        "variableMeasured": "official_composite",
        "distribution": {
            "@type": "DataDownload",
            "contentUrl": origin + "/api/v1/public/leaderboard",
            "encodingFormat": "application/json",
        },
    }


def _item_list_schema(origin: str, snapshot: SeoSnapshot) -> dict[str, Any]:
    elements = []
    for miner in snapshot.miners:
        elements.append(
            {
                "@type": "ListItem",
                "position": miner.rank,
                "name": miner.label,
                "url": origin + miner.profile_path,
                "additionalProperty": {
                    "@type": "PropertyValue",
                    "name": "official_composite",
                    "value": miner.official_composite,
                },
            }
        )
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Current SN118 ranked miners",
        "numberOfItems": len(elements),
        "itemListOrder": "https://schema.org/ItemListOrderAscending",
        "itemListElement": elements,
    }
