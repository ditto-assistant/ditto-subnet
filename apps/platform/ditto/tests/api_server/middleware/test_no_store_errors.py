"""Private routes keep Cache-Control: no-store on every response they build."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response

from ditto.api_server.middleware.error_envelope import register_exception_handlers
from ditto.api_server.no_store import NoStoreMiddleware, no_store


def _app() -> FastAPI:
    app = FastAPI()
    private = APIRouter(prefix="/private", dependencies=[Depends(no_store)])

    @private.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    @private.get("/missing")
    async def missing() -> dict:
        raise HTTPException(404, "unknown attempt")

    @private.get("/item/{number}")
    async def item(number: int) -> dict:
        return {"number": number}

    @private.get("/redirect")
    async def redirect() -> Response:
        return RedirectResponse("/elsewhere", status_code=302)

    @private.get("/declared")
    async def declared() -> Response:
        # An explicit policy (the public CLA text) is kept as declared.
        return Response(b"fixture", headers={"Cache-Control": "public, max-age=60"})

    @private.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("fixture failure")

    @app.get("/public/missing")
    async def public_missing() -> dict:
        raise HTTPException(404, "not published")

    app.include_router(private)
    app.add_middleware(NoStoreMiddleware)
    register_exception_handlers(app)
    return app


async def test_private_responses_are_no_store_unless_declared() -> None:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = {
            path: await client.get(path)
            for path in (
                "/private/ok",
                "/private/redirect",
                "/private/declared",
                "/private/missing",
                "/private/item/not-a-number",
                "/private/boom",
                "/public/missing",
            )
        }
    assert [response.status_code for response in responses.values()] == [
        200,
        302,
        200,
        404,
        422,
        500,
        404,
    ]
    for path, response in responses.items():
        expected = {"/public/missing": None, "/private/declared": "public, max-age=60"}
        assert response.headers.get("cache-control") == expected.get(
            path, "no-store"
        ), path
