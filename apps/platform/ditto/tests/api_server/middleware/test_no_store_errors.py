"""Private routes stay out of caches; no error response is ever cacheable."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from ditto.api_server.middleware.error_envelope import register_exception_handlers
from ditto.api_server.no_store import NoStoreMiddleware, no_store


class _Body(BaseModel):
    value: int


def _app() -> FastAPI:
    app = FastAPI()
    private = APIRouter(prefix="/private", dependencies=[Depends(no_store)])

    @private.get("/ok")
    async def ok() -> dict:
        return {"ok": True}

    @private.post("/submit")
    async def submit(body: _Body) -> dict:
        return {"value": body.value}

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

    @private.get("/declared-error")
    async def declared_error() -> dict:
        raise HTTPException(
            404, "not yet", headers={"Cache-Control": "public, max-age=60"}
        )

    @private.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("fixture failure")

    @app.get("/public/ok")
    async def public_ok() -> dict:
        return {"ok": True}

    @app.get("/public/missing")
    async def public_missing() -> dict:
        raise HTTPException(404, "not published")

    app.include_router(private)
    app.add_middleware(NoStoreMiddleware)
    register_exception_handlers(app)
    return app


async def test_every_private_response_and_every_error_is_no_store() -> None:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = {
            "ok": await client.get("/private/ok"),
            "redirect": await client.get("/private/redirect"),
            "declared": await client.get("/private/declared"),
            "declared error": await client.get("/private/declared-error"),
            "handler 404": await client.get("/private/missing"),
            "path 422": await client.get("/private/item/not-a-number"),
            # Fails while FastAPI parses the body, before any dependency runs.
            "unparseable body 422": await client.post(
                "/private/submit",
                content=b"{",
                headers={"content-type": "application/json"},
            ),
            "schema 422": await client.post("/private/submit", json={"value": "x"}),
            # Router-level answers never reach a route or its dependencies.
            "unknown route 404": await client.get("/private/nope"),
            "method 405": await client.head("/private/ok"),
            "unhandled 500": await client.get("/private/boom"),
            "public 404": await client.get("/public/missing"),
            "public 200": await client.get("/public/ok"),
        }
    statuses = {name: response.status_code for name, response in responses.items()}
    assert statuses == {
        "ok": 200,
        "redirect": 302,
        "declared": 200,
        "declared error": 404,
        "handler 404": 404,
        "path 422": 422,
        "unparseable body 422": 422,
        "schema 422": 422,
        "unknown route 404": 404,
        "method 405": 405,
        "unhandled 500": 500,
        "public 404": 404,
        "public 200": 200,
    }
    expected = {
        "declared": "public, max-age=60",
        "declared error": "public, max-age=60",
        # A public success keeps whatever policy its route declares.
        "public 200": None,
    }
    for name, response in responses.items():
        assert response.headers.get("cache-control") == expected.get(
            name, "no-store"
        ), name
