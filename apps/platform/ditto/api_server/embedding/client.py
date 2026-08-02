"""Code-embedding client: a thin, best-effort wrapper over a TEI service.

The embedding is a review-band moderation signal computed once per upload, so the
client never raises into the caller: any failure (service down, timeout, malformed
response) degrades to ``None`` — "no embedding", read downstream as no
code-embedding signal — exactly like the pure fingerprint extractors. The gate does
the cosine comparison later, in Python, over the small eligible ledger; the model is
only hit at embed time.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import time
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from ditto.api_server.embedding.config import EmbeddingConfig

logger = logging.getLogger(__name__)

# GCE / Cloud Run metadata server: mints a Google-signed identity token for the
# instance's service account, scoped to an audience (the target service URL). Used
# to call a private (authenticated) Cloud Run embedder without any static secret.
_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)
# Refresh the cached token this many seconds before it actually expires.
_TOKEN_REFRESH_SKEW = 300.0


class Embedder(Protocol):
    """Surface the upload path and factory rely on."""

    @property
    def model_tag(self) -> str | None:
        """``model@revision`` stored with each vector, or ``None`` when disabled."""
        ...

    async def embed(self, text: str) -> list[float] | None:
        """Return the unit-norm embedding of ``text``, or ``None`` (best-effort)."""
        ...

    async def aclose(self) -> None:
        """Release any underlying connection pool."""
        ...


class NullEmbedder:
    """The disabled embedder: embeds nothing, so the code-embedding column stays null.

    Used whenever ``CODE_EMBEDDER_URL`` is unset, so the platform runs unchanged
    until an operator points it at a live service.
    """

    model_tag: str | None = None

    async def embed(self, text: str) -> list[float] | None:
        del text  # disabled: nothing is embedded
        return None

    async def aclose(self) -> None:
        return None


class TeiEmbedder:
    """Best-effort client for a text-embeddings-inference ``/embed`` endpoint.

    Requests a unit-norm vector; when ``config.dim`` is set it truncates to that
    Matryoshka width and renormalizes so cosine stays a dot product. Every failure
    path returns ``None`` and logs at INFO — the upload must never fail because the
    moderation embedder is unavailable.
    """

    def __init__(self, config: EmbeddingConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._token: str | None = None
        self._token_exp: float = 0.0

    @property
    def model_tag(self) -> str | None:
        return self._config.model_tag

    async def embed(self, text: str) -> list[float] | None:
        if not text:
            return None
        url = self._config.url
        assert url is not None  # a TeiEmbedder is only built when enabled
        try:
            resp = await self._client.post(
                f"{url.rstrip('/')}/embed",
                json={"inputs": text, "normalize": True, "truncate": True},
                headers=await self._auth_header(url),
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.info("code-embed: request failed (%s)", type(e).__name__)
            return None
        vector = _first_vector(payload)
        if vector is None:
            logger.info("code-embed: unexpected response shape")
            return None
        if self._config.dim is not None:
            vector = _l2_normalize(vector[: self._config.dim])
        return vector

    async def _auth_header(self, audience: str) -> dict[str, str]:
        """Return the ``Authorization`` header, or ``{}`` for unauthenticated mode.

        For ``gcp_id_token`` auth, mints/caches a Google identity token (audience =
        the embedder URL) from the metadata server. Best-effort: a metadata failure
        returns no header, so the request goes out unauthenticated and — against a
        private service — 403s into a null vector rather than raising.
        """
        if self._config.auth != "gcp_id_token":
            return {}
        now = time.time()
        if self._token is None or now >= self._token_exp - _TOKEN_REFRESH_SKEW:
            token = await self._fetch_id_token(audience)
            if token is None:
                return {}
            self._token = token
            self._token_exp = _jwt_expiry(token, default=now + 3000.0)
        return {"Authorization": f"Bearer {self._token}"}

    async def _fetch_id_token(self, audience: str) -> str | None:
        try:
            resp = await self._client.get(
                _METADATA_IDENTITY_URL,
                params={"audience": audience, "format": "full"},
                headers={"Metadata-Flavor": "Google"},
            )
            resp.raise_for_status()
            token = resp.text.strip()
        except httpx.HTTPError as e:
            logger.info("code-embed: id-token fetch failed (%s)", type(e).__name__)
            return None
        return token or None

    async def aclose(self) -> None:
        await self._client.aclose()


def create_embedder(config: EmbeddingConfig) -> Embedder:
    """Return a :class:`TeiEmbedder` when enabled, else the :class:`NullEmbedder`.

    Caller owns lifecycle: ``await embedder.aclose()`` once finished (the api_server
    lifespan registers it on the ``AsyncExitStack``).
    """
    if not config.enabled:
        return NullEmbedder()
    client = httpx.AsyncClient(
        timeout=config.timeout_seconds,
        headers={"User-Agent": "ditto-api-server/0.0.1"},
    )
    return TeiEmbedder(config, client)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two vectors in ``[-1, 1]``; ``0.0`` on any missing input.

    Returns ``0.0`` — read as "no code-embedding match" — when either vector is
    ``None``,
    empty, of mismatched length, or has zero norm, so callers can threshold without
    special-casing. Pure + deterministic.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _first_vector(payload: object) -> list[float] | None:
    """Extract the single embedding from a TEI ``/embed`` response.

    TEI returns a list of vectors, one per input; we send one string, so the vector
    is ``payload[0]``. Tolerates a bare ``[float, ...]`` too. Returns ``None`` on any
    other shape or a non-numeric element.
    """
    if not isinstance(payload, list) or not payload:
        return None
    inner = payload[0]
    if isinstance(inner, list):  # [[...]] — the normal case
        candidate = inner
    elif isinstance(inner, (int, float)):  # [...] — a bare vector
        candidate = payload
    else:
        return None
    if not candidate or not all(isinstance(x, (int, float)) for x in candidate):
        return None
    return [float(x) for x in candidate]


def _l2_normalize(vector: list[float]) -> list[float]:
    """Return the unit-norm form of ``vector`` (unchanged if its norm is zero)."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def _jwt_expiry(token: str, *, default: float) -> float:
    """Return the ``exp`` (unix seconds) from a JWT's payload, or ``default``.

    Best-effort and unverified — the token is only cached, never trusted here; the
    embedder service verifies it. A malformed token just falls back to ``default``.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else default
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return default
