"""Crawler documents and live HTML injection for the public dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from ditto.api_server import factory
from ditto.api_server.dashboard_seo import (
    DOC_CACHE_CONTROL,
    HTML_CACHE_CONTROL,
    STATIC_DOC_CACHE_CONTROL,
    SeoMiner,
    SeoSnapshot,
    inject_live_seo,
    llms_txt,
    public_origin,
    reset_seo_cache,
    robots_txt,
    sitemap_xml,
    snapshot_from_leaderboard,
)
from ditto.api_server.factory import create_api_server

from .conftest import make_api_server_config
from .test_dashboard import _get

_ORIGIN = "https://platform-api.heyditto.ai"

_MARKED_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <!--ditto:seo-head-->
  <title>Ditto SN118 · Subnet Leaderboard</title>
  <meta name="description" content="static fallback" />
  <!--/ditto:seo-head-->
</head>
<body>
  <!--ditto:seo-body-->
  <!--/ditto:seo-body-->
  <div id="root"></div>
</body>
</html>
"""


def _snapshot() -> SeoSnapshot:
    return SeoSnapshot(
        generated_at=datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        count=2,
        bench_version=11,
        champion_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        miners=(
            SeoMiner(
                rank=1,
                agent_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                agent_name="Jupiter v3",
                miner_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                official_composite=0.912345,
                handle="jupiter",
            ),
            SeoMiner(
                rank=2,
                agent_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                agent_name="Unnamed",
                miner_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                official_composite=0.801,
                handle=None,
            ),
        ),
    )


class TestRobotsAndLlms:
    def test_robots_allows_all_and_points_at_the_sitemap(self) -> None:
        body = robots_txt(_ORIGIN)
        assert "User-agent: *" in body
        assert "Allow: /" in body
        assert f"Sitemap: {_ORIGIN}/sitemap.xml" in body

    def test_llms_txt_points_at_the_live_json_board(self) -> None:
        body = llms_txt(_ORIGIN)
        assert f"{_ORIGIN}/api/v1/public/leaderboard" in body
        assert "max-age=30" in body
        assert "nothing here is a sample" in body
        assert "sample ranks" not in body.lower()


class TestSitemap:
    def test_static_pages_are_always_listed(self) -> None:
        xml = sitemap_xml(_ORIGIN, None)
        assert "<loc>https://platform-api.heyditto.ai/</loc>" in xml
        assert "<loc>https://platform-api.heyditto.ai/leaderboard</loc>" in xml
        assert "<changefreq>always</changefreq>" in xml
        assert "/reviews" not in xml

    def test_live_miners_are_listed_when_the_board_is_known(self) -> None:
        xml = sitemap_xml(_ORIGIN, _snapshot())
        assert "<loc>https://platform-api.heyditto.ai/h/jupiter</loc>" in xml
        assert (
            "<loc>https://platform-api.heyditto.ai/miner/"
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY</loc>"
        ) in xml
        assert (
            "<loc>https://platform-api.heyditto.ai/agent/"
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa</loc>"
        ) in xml
        assert "<lastmod>2026-08-17T12:00:00Z</lastmod>" in xml


class TestSnapshotFromLeaderboard:
    def test_drops_unranked_rows_and_keeps_reserved_handles(self) -> None:
        board = SimpleNamespace(
            generated_at=datetime(2026, 8, 17, tzinfo=UTC),
            count=2,
            active_bench_version=11,
            emissions=SimpleNamespace(
                champion_miner_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
            ),
            entries=[
                SimpleNamespace(
                    rank=1,
                    agent_id=uuid4(),
                    agent_name="Jupiter v3",
                    miner_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                    official_composite=0.9,
                    name_handle=SimpleNamespace(status="reserved", stem="jupiter"),
                ),
                SimpleNamespace(
                    rank=None,
                    agent_id=uuid4(),
                    agent_name="Provisional",
                    miner_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                    official_composite=0.1,
                    name_handle=None,
                ),
            ],
        )
        snapshot = snapshot_from_leaderboard(board)  # type: ignore[arg-type]
        assert len(snapshot.miners) == 1
        assert snapshot.miners[0].handle == "jupiter"
        assert snapshot.miners[0].profile_path == "/h/jupiter"
        assert snapshot.champion_hotkey is not None
        assert snapshot.champion_hotkey.endswith("694ty")


class TestInjectLiveSeo:
    def test_unmarked_shell_is_left_alone(self) -> None:
        shell = "<html><title>static</title></html>"
        assert (
            inject_live_seo(shell, origin=_ORIGIN, path="/", snapshot=_snapshot())
            == shell
        )

    def test_home_title_and_json_ld_carry_the_live_champion(self) -> None:
        html = inject_live_seo(
            _MARKED_HTML, origin=_ORIGIN, path="/", snapshot=_snapshot()
        )
        assert "<title>Ditto SN118 · #1 jupiter</title>" in html
        assert "0.912345" in html
        assert '"@type":"ItemList"' in html
        assert '"position":1' in html
        assert "/h/jupiter" in html
        assert "<noscript>" in html
        assert "#1 <a href=" in html
        assert "static fallback" not in html

    def test_missing_snapshot_does_not_invent_ranks(self) -> None:
        html = inject_live_seo(_MARKED_HTML, origin=_ORIGIN, path="/", snapshot=None)
        assert "rank #" not in html
        assert '"@type":"ItemList"' not in html
        assert "/api/v1/public/leaderboard" in html
        assert "0.912345" not in html

    def test_miner_path_gets_a_per_entity_canonical(self) -> None:
        html = inject_live_seo(
            _MARKED_HTML,
            origin=_ORIGIN,
            path="/h/jupiter",
            snapshot=_snapshot(),
        )
        assert "jupiter · SN118 rank #1" in html
        assert 'href="https://platform-api.heyditto.ai/h/jupiter"' in html
        assert 'rel="canonical"' in html

    def test_reviews_is_noindex(self) -> None:
        html = inject_live_seo(
            _MARKED_HTML, origin=_ORIGIN, path="/reviews", snapshot=_snapshot()
        )
        assert 'name="robots" content="noindex, nofollow"' in html

    def test_names_are_html_escaped(self) -> None:
        dirty = SeoSnapshot(
            generated_at=datetime(2026, 8, 17, tzinfo=UTC),
            count=1,
            bench_version=11,
            champion_hotkey="hk",
            miners=(
                SeoMiner(
                    rank=1,
                    agent_id="id",
                    agent_name="<script>alert(1)</script>",
                    miner_hotkey="hk",
                    official_composite=0.5,
                    handle=None,
                ),
            ),
        )
        html = inject_live_seo(_MARKED_HTML, origin=_ORIGIN, path="/", snapshot=dirty)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "\\u003cscript\\u003ealert(1)\\u003c/script\\u003e" in html


class TestPublicOrigin:
    def test_prefers_forwarded_host_and_forces_https_on_prod(self) -> None:
        request = SimpleNamespace(
            headers={
                "x-forwarded-host": "platform-api.heyditto.ai",
                "x-forwarded-proto": "http",
                "host": "127.0.0.1:8000",
            },
            url=SimpleNamespace(netloc="127.0.0.1:8000", scheme="http"),
        )
        assert public_origin(request) == "https://platform-api.heyditto.ai"  # type: ignore[arg-type]


@pytest.fixture
def seo_dist(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A Vite-shaped dist with SEO markers so factory injection is exercised."""
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    html = _MARKED_HTML + "<!-- padding so gzip tests stay above the 1KB floor -->\n"
    html = html + ("<!-- " + ("x" * 1200) + " -->\n")
    (dist / "index.html").write_text(html, encoding="utf-8")
    (assets / "index-Ab12Cd34.js").write_text(
        'console.log("ditto dashboard bundle");\n', encoding="utf-8"
    )
    (assets / "paperditto-512.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    monkeypatch.setattr(factory, "_DASHBOARD_DIST", dist)
    monkeypatch.setattr(factory, "_DASHBOARD_FILE", dist / "index.html")
    monkeypatch.setattr(factory, "_DASHBOARD_ASSETS", assets)
    reset_seo_cache()
    yield dist
    reset_seo_cache()


@pytest.mark.usefixtures("seo_dist")
class TestSeoRoutes:
    async def test_robots_and_llms_are_served_next_to_the_spa(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        robots = await _get(app, "/robots.txt")
        assert robots.status_code == 200
        assert robots.headers["Cache-Control"] == STATIC_DOC_CACHE_CONTROL
        assert "Sitemap: http://test/sitemap.xml" in robots.text

        llms = await _get(app, "/llms.txt")
        assert llms.status_code == 200
        assert "/api/v1/public/leaderboard" in llms.text
        assert llms.headers["Cache-Control"] == STATIC_DOC_CACHE_CONTROL

        full = await _get(app, "/llms-full.txt")
        assert full.status_code == 200
        assert "official_composite" in full.text

    async def test_sitemap_is_short_cached_and_lists_public_pages(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/sitemap.xml")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == DOC_CACHE_CONTROL
        assert resp.headers["content-type"].startswith("application/xml")
        assert "<loc>http://test/leaderboard</loc>" in resp.text
        assert "/reviews" not in resp.text

    async def test_crawlable_page_paths_serve_the_spa_shell(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/leaderboard")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == HTML_CACHE_CONTROL
        assert '<div id="root">' in resp.text

    async def test_disabled_dashboard_hides_seo_routes(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=False))
        assert (await _get(app, "/robots.txt")).status_code == 404
        assert (await _get(app, "/sitemap.xml")).status_code == 404
        assert (await _get(app, "/llms.txt")).status_code == 404
        assert (await _get(app, "/leaderboard")).status_code == 404


@pytest.mark.usefixtures("seo_dist")
class TestLiveHtmlThroughFactory:
    async def test_html_stays_short_cached_without_inventing_ranks(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        resp = await _get(app, "/")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == HTML_CACHE_CONTROL
        assert '"@type":"WebSite"' in resp.text
        assert '"@type":"ItemList"' not in resp.text
        assert "rank #" not in resp.text

    async def test_injected_html_revalidates_with_etag(self) -> None:
        app = create_api_server(make_api_server_config(dashboard_enabled=True))
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = await client.get("/leaderboard")
            second = await client.get(
                "/leaderboard", headers={"If-None-Match": first.headers["etag"]}
            )
        assert first.status_code == 200
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["Cache-Control"] == HTML_CACHE_CONTROL
