"""Origin-ownership challenge for Sign in with Ditto.

Ditto verifies that a relying party controls its callback origin by fetching
``<origin>/.well-known/ditto-callback-challenge`` and comparing the body with
the token it issued for that attempt. Operators put that token in
``DITTO_CALLBACK_CHALLENGE_TOKEN``; the path answers 404 until they do. This is
deliberately independent of ``DITTO_LINK_ENABLED``: the origin must be verified
before linking can be switched on, never after.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["ditto-link"])


@router.get("/.well-known/ditto-callback-challenge", response_class=PlainTextResponse)
async def ditto_callback_challenge(request: Request) -> PlainTextResponse:
    client = getattr(request.app.state, "ditto_link", None)
    token = getattr(getattr(client, "config", None), "callback_challenge_token", None)
    if not token:
        raise HTTPException(
            status_code=404, detail="no callback challenge is configured"
        )
    return PlainTextResponse(token, headers={"Cache-Control": "no-store"})
