"""Unit tests for the code embedder (:mod:`ditto.api_server.embedding.client`)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from ditto.api_server.embedding import (
    EmbeddingConfig,
    NullEmbedder,
    TeiEmbedder,
    cosine,
    create_embedder,
)


def _cfg(**kw: object) -> EmbeddingConfig:
    base: dict[str, object] = {
        "url": "http://embedder:80",
        "model": "test-model",
        "revision": "main",
        "dim": None,
        "timeout_seconds": 5.0,
        "auth": "none",
    }
    base.update(kw)
    return EmbeddingConfig(**base)  # type: ignore[arg-type]


def _tei(handler: object, config: EmbeddingConfig) -> TeiEmbedder:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TeiEmbedder(config, httpx.AsyncClient(transport=transport))


class TestCreateAndNull:
    def test_create_disabled_returns_null(self) -> None:
        embedder = create_embedder(_cfg(url=None, model=""))
        assert isinstance(embedder, NullEmbedder)

    def test_create_enabled_returns_tei(self) -> None:
        embedder = create_embedder(_cfg())
        assert isinstance(embedder, TeiEmbedder)

    async def test_null_embedder_returns_none(self) -> None:
        embedder = NullEmbedder()
        assert await embedder.embed("anything") is None
        assert embedder.model_tag is None
        await embedder.aclose()


class TestTeiEmbedder:
    async def test_embeds_and_tags(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/embed"
            return httpx.Response(200, json=[[0.0, 3.0, 4.0]])

        embedder = _tei(handler, _cfg())
        vector = await embedder.embed("fn main() {}")
        assert vector == [0.0, 3.0, 4.0]
        assert embedder.model_tag == "test-model@main"
        await embedder.aclose()

    async def test_dim_truncates_and_renormalizes(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[[3.0, 4.0, 9.0, 9.0]])

        embedder = _tei(handler, _cfg(dim=2))
        vector = await embedder.embed("code")
        assert vector is not None and len(vector) == 2
        # [3,4] renormalized -> [0.6, 0.8]
        assert vector[0] == pytest.approx(0.6)
        assert vector[1] == pytest.approx(0.8)
        await embedder.aclose()

    async def test_empty_text_returns_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not call the service for empty text")

        embedder = _tei(handler, _cfg())
        assert await embedder.embed("") is None
        await embedder.aclose()

    async def test_http_error_returns_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        embedder = _tei(handler, _cfg())
        assert await embedder.embed("code") is None
        await embedder.aclose()

    async def test_malformed_shape_returns_none(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "a list"})

        embedder = _tei(handler, _cfg())
        assert await embedder.embed("code") is None
        await embedder.aclose()

    async def test_timeout_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        embedder = _tei(handler, _cfg())
        assert await embedder.embed("code") is None
        await embedder.aclose()


class TestCosine:
    def test_identical_is_one(self) -> None:
        assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_is_zero(self) -> None:
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_is_negative_one(self) -> None:
        assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_missing_or_mismatched_is_zero(self) -> None:
        assert cosine(None, [1.0]) == 0.0
        assert cosine([1.0], None) == 0.0
        assert cosine([], [1.0]) == 0.0
        assert cosine([1.0, 2.0], [1.0]) == 0.0  # length mismatch
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero norm


def _fake_id_token(exp: float = 9999999999.0) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"header.{payload}.sig"


class TestGcpAuth:
    async def test_bearer_from_metadata_added_to_embed(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "metadata.google.internal":
                assert request.headers.get("Metadata-Flavor") == "Google"
                assert request.url.params.get("audience") == "http://embedder:80"
                return httpx.Response(200, text=_fake_id_token())
            # /embed must carry the bearer minted above.
            seen["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json=[[1.0, 0.0]])

        embedder = _tei(handler, _cfg(auth="gcp_id_token"))
        vector = await embedder.embed("fn main() {}")
        assert vector == [1.0, 0.0]
        assert seen["auth"].startswith("Bearer header.")
        await embedder.aclose()

    async def test_metadata_failure_degrades_to_unauthenticated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "metadata.google.internal":
                return httpx.Response(500, text="no metadata server")
            # No token was obtained -> a private service would 403; simulate that.
            if not request.headers.get("Authorization"):
                return httpx.Response(403, text="forbidden")
            return httpx.Response(200, json=[[1.0]])  # pragma: no cover

        embedder = _tei(handler, _cfg(auth="gcp_id_token"))
        assert await embedder.embed("code") is None  # best-effort, no crash
        await embedder.aclose()

    async def test_no_auth_header_when_disabled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers
            return httpx.Response(200, json=[[0.5]])

        embedder = _tei(handler, _cfg(auth="none"))
        assert await embedder.embed("code") == [0.5]
        await embedder.aclose()
