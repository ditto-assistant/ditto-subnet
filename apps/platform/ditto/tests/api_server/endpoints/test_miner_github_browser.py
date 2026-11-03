"""Real Chromium enforces OAuth redirect CSP and constructs consent headers.

Needs the `browser` dependency group: `uv sync --group dev --group browser`,
then `uv run playwright install chromium`.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

import bittensor
import httpx
import pytest
import uvicorn

from ditto.api_server.github_link import GithubLinkClient
from ditto.tests.api_server.endpoints.test_miner_github_link import _sign_in, setup

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright
except ImportError:
    # CI installs the group and sets DITTO_REQUIRE_BROWSER, so it never skips.
    if os.environ.get("DITTO_REQUIRE_BROWSER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        raise
    pytest.skip(
        "the browser dependency group is not installed", allow_module_level=True
    )


def _browser_required() -> bool:
    """CI sets DITTO_REQUIRE_BROWSER=1 so a missing browser fails, not skips."""
    return os.environ.get("DITTO_REQUIRE_BROWSER", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


async def test_browser_launch_and_consent_reach_accepted_state(app, session_maker):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    provider = setup(app, session_maker)
    config = replace(
        app.state.github_link.config,
        redirect_url=f"{origin}/api/v1/miner-auth/github/callback",
    )
    app.state.config = replace(app.state.config, github_link=config)
    app.state.github_link = GithubLinkClient(config, transport=provider.transport())
    server = uvicorn.Server(
        uvicorn.Config(
            app, lifespan="off", log_level="error", access_log=False, ws="none"
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("browser test server stopped before startup")
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=origin) as client:
            alice = bittensor.Keypair.create_from_uri("//Alice")
            token = await _sign_in(client, alice)
            auth = {"authorization": f"Bearer {token}"}
            started = await client.post("/api/v1/me/github-link/start", headers=auth)
            assert started.status_code == 200, started.text
            launch = started.json()
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.launch(
                        channel=os.environ.get("DITTO_TEST_BROWSER_CHANNEL"),
                    )
                except PlaywrightError as exc:
                    if _browser_required() or "Executable doesn't exist" not in str(
                        exc
                    ):
                        raise
                    pytest.skip(
                        "Chromium is not installed; run "
                        "`uv run playwright install chromium`"
                    )
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    cdp = await context.new_cdp_session(page)

                    async def fixture_oauth(event: dict) -> None:
                        await cdp.send(
                            "Fetch.fulfillRequest",
                            {
                                "requestId": event["requestId"],
                                "responseCode": 200,
                                "responseHeaders": [
                                    {"name": "Content-Type", "value": "text/html"}
                                ],
                                "body": base64.b64encode(
                                    b"<h1>Fixture GitHub sign-in</h1>"
                                ).decode(),
                            },
                        )

                    # Playwright route() covers only the first URL in a redirect
                    # chain. CDP pauses the actual GitHub hop after the form's 302.
                    cdp.on("Fetch.requestPaused", fixture_oauth)
                    await cdp.send(
                        "Fetch.enable",
                        {"patterns": [{"urlPattern": "https://github.com/*"}]},
                    )
                    await page.goto(
                        launch["authorize_url"],
                        wait_until="domcontentloaded",
                        timeout=8000,
                    )
                    await page.wait_for_url("https://github.com/**", timeout=8000)
                    oauth = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
                    assert oauth.get("scope") == [""], page.url
                    assert oauth["code_challenge_method"] == ["S256"]
                    callback = await page.goto(
                        f"{config.redirect_url}?"
                        + urlencode(
                            {"state": oauth["state"][0], "code": "fixture-code"}
                        )
                    )
                    assert callback is not None and callback.status == 200
                    async with page.expect_response(
                        lambda response: (
                            response.url == f"{origin}/api/v1/miner-auth/github/accept"
                            and response.request.method == "POST"
                        )
                    ) as submitted:
                        await page.get_by_role(
                            "button", name="Approve this hotkey"
                        ).click()
                    accepted = await submitted.value
                    assert accepted.request.headers.get("origin") == origin
                    assert accepted.status == 200, await accepted.text()
                    status = await client.get(
                        f"/api/v1/me/github-link/attempts/{launch['attempt_id']}",
                        headers=auth,
                    )
                    assert status.json()["status"] == "accepted"
                    assert status.json()["github_user_id"] == provider.user_id
                finally:
                    await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
