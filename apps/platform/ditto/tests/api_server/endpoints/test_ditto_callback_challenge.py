"""The origin-ownership challenge is served before linking is enabled, never guessed."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from ditto.api_server.ditto_link import DittoLinkClient, DittoLinkConfig


def _config(token: str | None) -> DittoLinkConfig:
    return DittoLinkConfig(
        enabled=False,
        issuer="https://api.heyditto.ai",
        client_id=None,
        client_secret=None,
        redirect_url="",
        return_url="https://dittobench.ai/#/reviews",
        callback_challenge_token=token,
    )


async def test_challenge_is_served_only_when_configured(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.state.ditto_link = DittoLinkClient(_config(None))
    missing = await client.get("/.well-known/ditto-callback-challenge")
    assert missing.status_code == 404

    app.state.ditto_link = DittoLinkClient(_config("challenge-token-123"))
    served = await client.get("/.well-known/ditto-callback-challenge")
    assert served.status_code == 200
    assert served.text == "challenge-token-123"
    assert served.headers["cache-control"] == "no-store"
    assert served.headers["content-type"].startswith("text/plain")
    # Linking itself stays off: the challenge never implies activation.
    assert (await client.post("/api/v1/me/ditto-link/start")).status_code == 503
