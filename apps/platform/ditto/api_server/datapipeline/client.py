"""Client for the private ditto-data-pipeline generate service.

Renders the per-submission benchmark dataset. Generation is deterministic, so the
platform only needs the seed + the artifact's SHA-256: it draws a fresh seed,
POSTs ``/generate?seed=&run_size=``, and reads the ``X-Dataset-SHA256`` header.
The bytes themselves need not be stored — any validator (and the scoring API)
regenerates them from the seed.

Unlike the best-effort code embedder, the dataset is REQUIRED: every failure path
raises :class:`DataPipelineError`, so a generation failure leaves the agent
unpromoted (the screener does not flip it to ``evaluating``) rather than pinning a
null dataset a validator could not score.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from ditto.api_server.datapipeline.errors import DataPipelineError

if TYPE_CHECKING:
    from ditto.api_server.datapipeline.config import DataPipelineConfig

logger = logging.getLogger(__name__)

# GCE / Cloud Run metadata server: mints a Google-signed identity token for the
# instance's service account, scoped to an audience (the target service URL). Lets
# the platform call the PRIVATE (authenticated) Cloud Run generate service with no
# static secret. Mirrors the code-embedding client.
_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)
_TOKEN_REFRESH_SKEW = 300.0

# The header the generate service sets with the DatasetArtifact SHA-256.
_SHA_HEADER = "X-Dataset-SHA256"


class DatasetGenerator(Protocol):
    """Surface the job-ready path relies on."""

    @property
    def run_size(self) -> str | None:
        """Configured scoring profile, or ``None`` when disabled."""
        ...

    async def generate(self, seed: int, bench_version: int) -> str:
        """Generate the dataset for ``seed`` and return its SHA-256 (hex).

        Raises:
            DataPipelineError: on any failure (service down, bad status, missing
                hash header) — the dataset is required, never best-effort.
        """
        ...

    async def fetch_dataset(
        self, seed: int, run_size: str, bench_version: int
    ) -> tuple[dict[str, Any], str]:
        """Return the FULL labeled dataset artifact for ``(seed, run_size)``.

        Unlike :meth:`generate` (which discards the body and keeps only the hash),
        this returns the whole DatasetArtifact including answer keys, for the
        public finalized-dataset reveal (independent re-grading). Returns
        ``(artifact_json, sha256_hex)``.

        Raises:
            DataPipelineError: on any failure (service down, bad status, missing
                hash header, unparseable body).
        """
        ...

    async def aclose(self) -> None:
        """Release the underlying connection pool."""
        ...


class NullGenerator:
    """The disabled generator: raises if asked to generate.

    Used when ``DATA_PIPELINE_URL`` is unset. ``run_size`` is ``None`` so the
    job-ready path can detect "generation disabled" and skip pinning a dataset
    (pre-k3 behavior) instead of failing.
    """

    run_size: str | None = None

    async def generate(self, seed: int, bench_version: int) -> str:
        del seed, bench_version
        raise DataPipelineError(
            "data-pipeline generate service is not configured (DATA_PIPELINE_URL "
            "unset); cannot generate a per-submission dataset"
        )

    async def fetch_dataset(
        self, seed: int, run_size: str, bench_version: int
    ) -> tuple[dict[str, Any], str]:
        del seed, run_size, bench_version
        raise DataPipelineError(
            "data-pipeline generate service is not configured (DATA_PIPELINE_URL "
            "unset); cannot reveal a dataset"
        )

    async def aclose(self) -> None:
        return None


class HttpDatasetGenerator:
    """Calls the generate service's ``POST /generate`` and returns the artifact hash."""

    def __init__(self, config: DataPipelineConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._token: str | None = None
        self._token_exp: float = 0.0

    @property
    def run_size(self) -> str | None:
        return self._config.run_size

    async def generate(self, seed: int, bench_version: int) -> str:
        assert self._config.run_size is not None  # only built when enabled
        # generate() only needs the hash; the body (if any) is discarded, so it
        # never requires a parseable JSON payload.
        _, sha = await self._post_generate(seed, self._config.run_size, bench_version)
        return sha

    async def fetch_dataset(
        self, seed: int, run_size: str, bench_version: int
    ) -> tuple[dict[str, Any], str]:
        resp, sha = await self._post_generate(seed, run_size, bench_version)
        try:
            body = resp.json()
        except ValueError as e:
            raise DataPipelineError("generate service returned invalid JSON") from e
        if not isinstance(body, dict):
            raise DataPipelineError("generate service returned a non-object body")
        return body, sha

    async def _post_generate(
        self, seed: int, run_size: str, bench_version: int
    ) -> tuple[httpx.Response, str]:
        """POST ``/generate``, returning the response + its verified SHA header."""
        url = self._config.url
        assert url is not None  # only built when enabled
        endpoint = f"{url.rstrip('/')}/generate"
        try:
            resp = await self._client.post(
                endpoint,
                params={
                    "seed": str(seed),
                    "run_size": run_size,
                    "bench_version": str(bench_version),
                },
                headers=await self._auth_header(url),
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise DataPipelineError(
                f"generate service request failed ({type(e).__name__})"
            ) from e
        sha = resp.headers.get(_SHA_HEADER, "").strip().lower()
        if not sha:
            raise DataPipelineError(
                f"generate service response missing {_SHA_HEADER} header"
            )
        reported_version = resp.headers.get("X-Bench-Version")
        if reported_version is not None and reported_version != str(bench_version):
            raise DataPipelineError("generate service benchmark version mismatch")
        if bench_version != 2 and reported_version is None:
            raise DataPipelineError("generate service omitted benchmark version")
        return resp, sha

    async def _auth_header(self, audience: str) -> dict[str, str]:
        """Return the ``Authorization`` header, or ``{}`` for unauthenticated mode.

        For ``gcp_id_token`` auth, mints/caches a Google identity token (audience =
        the service URL) from the metadata server. Unlike the embedder, a token
        failure RAISES: an unauthenticated call to the private service would 403,
        and a 403 must not be mistaken for "no dataset".
        """
        if self._config.auth != "gcp_id_token":
            return {}
        now = time.time()
        if self._token is None or now >= self._token_exp - _TOKEN_REFRESH_SKEW:
            token = await self._fetch_id_token(audience)
            self._token = token
            self._token_exp = _jwt_expiry(token, default=now + 3000.0)
        return {"Authorization": f"Bearer {self._token}"}

    async def _fetch_id_token(self, audience: str) -> str:
        try:
            resp = await self._client.get(
                _METADATA_IDENTITY_URL,
                params={"audience": audience, "format": "full"},
                headers={"Metadata-Flavor": "Google"},
            )
            resp.raise_for_status()
            token = resp.text.strip()
        except httpx.HTTPError as e:
            raise DataPipelineError(
                f"identity-token fetch failed ({type(e).__name__})"
            ) from e
        if not token:
            raise DataPipelineError("identity-token fetch returned an empty token")
        return token

    async def aclose(self) -> None:
        await self._client.aclose()


def create_generator(config: DataPipelineConfig) -> DatasetGenerator:
    """Return :class:`HttpDatasetGenerator` when enabled, else :class:`NullGenerator`.

    Caller owns lifecycle (``await generator.aclose()``); the api_server lifespan
    registers it on the ``AsyncExitStack``, mirroring the embedder.
    """
    if not config.enabled:
        return NullGenerator()
    client = httpx.AsyncClient(
        timeout=config.timeout_seconds,
        headers={"User-Agent": "ditto-api-server/0.0.1"},
    )
    return HttpDatasetGenerator(config, client)


def _jwt_expiry(token: str, *, default: float) -> float:
    """Return the ``exp`` (unix seconds) from a JWT payload, or ``default``.

    Best-effort and unverified — the token is only cached, never trusted here; the
    generate service verifies it.
    """
    import base64
    import binascii
    import json

    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else default
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return default
