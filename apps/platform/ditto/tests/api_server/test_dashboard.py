"""Tests for the same-origin dashboard SPA served at ``/``.

The platform doubles as the transparency front door: it serves the Vite build
output (``dashboard/dist``) at ``/`` so the SPA's ``/api/v1/public/*`` calls
are same-origin (no CORS). These tests cover the *serving contract* only —
wandb URL injection, cache/encoding headers, the ``/assets/`` route, and the
disabled / missing-build fallbacks — against a minimal fake ``dist/`` fixture
rather than a real build. The old monolith-era tests that asserted on the
dashboard's rendered markup, copy, and inline JS were deleted: the shell is
client-rendered now and that behavior is covered by the dashboard's own vitest
suite (``dashboard/src/**/*.test.tsx``).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from ditto.api_server import factory
from ditto.api_server.factory import create_api_server

from .conftest import make_api_server_config

# A minimal but representative Vite build: the two ``ditto:`` meta tags the
# server and SPA read, the inline theme bootstrap (runs before CSS paints),
# the SPA mount point, and a content-hashed bundle reference. Kept above the
# 1KB SizedGZipMiddleware floor so the gzip test exercises compression.
_FAKE_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ditto SN118 · Subnet Leaderboard</title>
  <meta name="ditto:api-base" content="" />
  <meta name="ditto:wandb-url" content="https://wandb.ai/" />
  <script>
    // Apply the saved theme before CSS paints to avoid a light/dark flash.
    (function () {
      "use strict";
      var STORAGE_KEY = "ditto:dashboard-theme";
      var MODES = { system: true, light: true, dark: true, time: true };

      function fromHour(hour) {
        if (hour >= 5 && hour < 8) return "dawn";
        if (hour >= 8 && hour < 12) return "morning";
        if (hour >= 12 && hour < 17) return "afternoon";
        if (hour >= 17 && hour < 20) return "dusk";
        return "night";
      }

      function readMode() {
        try {
          var saved = localStorage.getItem(STORAGE_KEY);
          return MODES[saved] ? saved : "system";
        } catch (e) {
          return "system";
        }
      }

      function apply(mode) {
        var root = document.documentElement;
        root.dataset.theme = MODES[mode] ? mode : "system";
        root.dataset.timePhase = fromHour(new Date().getHours());
      }

      apply(readMode());
    })();
  </script>
  <script type="module" crossorigin src="/assets/index-Ab12Cd34.js"></script>
</head>
<body>
  <div id="root"></div>
</body>
</html>
"""

# Just the PNG signature plus filler; enough for signature assertions.
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def fake_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``dashboard/dist`` build the factory serves during the test.

    Writes the minimal build into ``tmp_path`` and points the factory's
    module-level paths at it, so the serving contract is tested without
    running ``npm run build``.
    """
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    html = _FAKE_INDEX_HTML
    # The gzip test relies on the body clearing SizedGZipMiddleware's floor.
    assert len(html.encode("utf-8")) > 1024
    (dist / "index.html").write_text(html, encoding="utf-8")
    # Content-hashed bundle (cache forever) vs. a public/ passthrough file
    # that keeps its name (cache for a day).
    (assets / "index-Ab12Cd34.js").write_text(
        'console.log("ditto dashboard bundle");\n', encoding="utf-8"
    )
    (assets / "paperditto-512.png").write_bytes(_FAKE_PNG)
    monkeypatch.setattr(factory, "_DASHBOARD_DIST", dist)
    monkeypatch.setattr(factory, "_DASHBOARD_FILE", dist / "index.html")
    monkeypatch.setattr(factory, "_DASHBOARD_ASSETS", assets)
    return dist


@pytest.fixture
def missing_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the factory at a ``dist`` directory that was never built."""
    dist = tmp_path / "dist"  # intentionally not created
    monkeypatch.setattr(factory, "_DASHBOARD_DIST", dist)
    monkeypatch.setattr(factory, "_DASHBOARD_FILE", dist / "index.html")
    monkeypatch.setattr(factory, "_DASHBOARD_ASSETS", dist / "assets")
    return dist


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.usefixtures("fake_dist")
class TestDashboard:
    async def test_served_at_root_with_injected_wandb_url(self) -> None:
        url = "https://wandb.ai/ditto/ditto-sn118"
        app = create_api_server(
            make_api_server_config(dashboard_enabled=True, dashboard_wandb_url=url)
        )
        resp = await _get(app, "/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "public, max-age=60, must-revalidate"
        body = resp.text
        # The wandb link is injected into the meta tag the SPA reads.
        assert f'content="{url}"' in body
        assert 'name="ditto:wandb-url"' in body
        # api-base stays empty so the SPA uses its same-origin /api/v1 default.
        assert 'name="ditto:api-base" content=""' in body

    @pytest.mark.parametrize(
        "path",
        [
            "/agent/6c10d0df-fc93-4903-a939-147d51cea1cc",
            "/agents/6c10d0df-fc93-4903-a939-147d51cea1cc",
            "/miners/5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
            "/validators/5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            "/screeners/5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        ],
    )
    async def test_serves_dashboard_at_entity_paths(self, path: str) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "public, max-age=60, must-revalidate"
        # Overlay entity paths serve the same SPA shell as /.
        assert resp.text == (await _get(app, "/")).text

    @pytest.mark.parametrize(
        "path",
        [
            "/miner/5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
            "/h/jupiter",
        ],
    )
    async def test_miner_profile_html_carries_open_graph(self, path: str) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, path)
        assert resp.status_code == 200
        body = resp.text
        assert '<div id="root">' in body
        assert 'property="og:title"' in body
        assert 'property="og:url"' in body
        assert path in body
        assert "Ditto SN118 miner" in body
        assert "application/ld+json" in body
        assert "Subnet Leaderboard" not in body.split("<title>")[1].split("</title>")[0]

    async def test_robots_and_sitemap_are_public(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        robots = await _get(app, "/robots.txt")
        assert robots.status_code == 200
        assert "Allow: /miner/" in robots.text
        assert "Sitemap:" in robots.text
        sitemap = await _get(app, "/sitemap.xml")
        assert sitemap.status_code == 200
        assert sitemap.headers["content-type"].startswith("application/xml")
        assert "<urlset" in sitemap.text

    async def test_wandb_url_is_html_escaped(self) -> None:
        # A stray quote in the configured URL must not break out of the attribute.
        app = create_api_server(
            make_api_server_config(
                dashboard_enabled=True,
                dashboard_wandb_url='https://wandb.ai/"><script>x',
            )
        )
        body = (await _get(app, "/")).text
        assert "<script>x" not in body
        assert "&lt;script&gt;x" in body

    async def test_disabled_returns_404(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=False))
        resp = await _get(app, "/")
        assert resp.status_code == 404

    async def test_dashboard_html_is_gzip_encoded(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        # httpx transparently decodes; the decoded HTML is still the SPA shell.
        assert '<div id="root">' in resp.text

    async def test_dashboard_serves_strong_etag(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/")
        etag = resp.headers.get("etag")
        assert etag is not None
        assert etag.startswith('"') and etag.endswith('"')

    async def test_dashboard_if_none_match_returns_304_empty_body(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = await client.get("/")
            etag = first.headers["etag"]
            second = await client.get("/", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["etag"] == etag
        assert second.headers["Cache-Control"] == "public, max-age=60, must-revalidate"
        # RFC 9110 §15.4.5: the 304 repeats the 200's Vary: Accept-Encoding.
        assert second.headers["Vary"] == "Accept-Encoding"

    async def test_dashboard_entity_path_if_none_match_returns_304(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        path = "/agent/6c10d0df-fc93-4903-a939-147d51cea1cc"
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = await client.get(path)
            etag = first.headers["etag"]
            second = await client.get(path, headers={"If-None-Match": etag})
        assert first.status_code == 200
        assert second.status_code == 304
        assert second.content == b""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/public/leaderboard",
            "/api/v1/public/bench/timeline",
            "/api/v1/public/weights",
            "/api/v1/public/activity",
            "/api/v1/public/operations",
            "/api/v1/public/validators",
            "/api/v1/public/screeners",
            "/api/v1/public/agent/{agent_id}/summary",
        ],
    )
    async def test_api_still_mounted_alongside_dashboard(self, path: str) -> None:
        # Serving HTML at / must not shadow the API routes.
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        # 200 requires a DB; here we only assert the route exists (not 404).
        assert any(getattr(r, "path", None) == path for r in app.routes)


@pytest.mark.usefixtures("fake_dist")
class TestDashboardAssets:
    """The ``/assets/{path}`` route serving the Vite build's static files."""

    async def test_hashed_asset_is_cached_forever(self) -> None:
        # Vite content-hashes bundle names, so the bytes behind a given name
        # never change: safe to cache for a year and mark immutable.
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/assets/index-Ab12Cd34.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert b"ditto dashboard bundle" in resp.content

    async def test_unhashed_asset_is_cached_for_a_day(self) -> None:
        # public/ passthrough files (the og:image PNG) keep their names, so
        # they only get a day.
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/assets/paperditto-512.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["Cache-Control"] == "public, max-age=86400"
        assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")

    async def test_missing_asset_returns_404(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/assets/index-Zz99Yy88.js")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            # Plain dot segments (normalized away by well-behaved clients,
            # contained by the route for everyone else) and encoded variants
            # that reach the handler with ".." in the decoded asset path.
            "/assets/../index.html",
            "/assets/..%2Findex.html",
            "/assets/%2e%2e/index.html",
        ],
    )
    async def test_traversal_attempt_returns_404(self, path: str) -> None:
        # dist/index.html exists right outside assets/, so a containment miss
        # would serve it; the route must 404 instead.
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, path)
        assert resp.status_code == 404


@pytest.mark.usefixtures("missing_dist")
class TestDashboardMissingBuild:
    """A checkout without ``dashboard/dist`` serves the API only."""

    async def test_missing_build_serves_api_only(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        assert (await _get(app, "/")).status_code == 404
        assert (await _get(app, "/assets/index-Ab12Cd34.js")).status_code == 404
        # The API itself is unaffected by the absent build.
        assert any(
            getattr(r, "path", None) == "/api/v1/public/leaderboard" for r in app.routes
        )
