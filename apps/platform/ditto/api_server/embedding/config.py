"""Configuration for the code-embedding client.

The embedder is a self-hosted text-embeddings-inference (TEI) service holding a
small open code model (Qwen3-Embedding-0.6B primary, jina-embeddings-v2-base-code
CPU fallback). It is a *review-band moderation* signal, so it is **disabled by
default**: with ``CODE_EMBEDDER_URL`` unset the platform embeds nothing and the
code-embedding
column stays null (a no-op, never a boot failure). Setting the URL turns it on and
the remaining fields are then validated fail-fast, matching the other configs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ditto.api_server.embedding.errors import EmbeddingConfigError


@dataclass(frozen=True)
class EmbeddingConfig:
    """Resolved configuration for the code-embedding client.

    ``enabled`` is derived from ``url``: no URL means the null embedder. ``model`` +
    ``revision`` are stamped alongside every stored vector so a model change is
    detectable and can trigger a re-embed sweep (the ``bench_version`` re-score
    pattern, applied to embeddings). ``dim`` is the optional Matryoshka truncation
    (Qwen3-Embedding supports 32–1024); ``None`` keeps the model's native width.
    """

    url: str | None
    """TEI base URL (``CODE_EMBEDDER_URL``), e.g. ``http://embedder:80``. ``None``
    disables the signal (the null embedder returns no vector)."""

    model: str
    """Model id the service serves (``CODE_EMBEDDER_MODEL``), e.g.
    ``Qwen/Qwen3-Embedding-0.6B``. Required when enabled; recorded for provenance."""

    revision: str
    """Model revision / commit pin (``CODE_EMBEDDER_REVISION``, default ``main``).
    Part of the provenance tag so a revision bump can drive a re-embed sweep."""

    dim: int | None
    """Matryoshka output dimension (``CODE_EMBEDDER_DIM``). ``None`` = native width.
    Truncating to e.g. 256 shrinks storage + cosine cost for near-neighbor use."""

    timeout_seconds: float
    """Per-call HTTP timeout (``CODE_EMBEDDER_TIMEOUT_SECONDS``). Best-effort: a
    slow/unreachable service degrades to a null vector rather than stalling the
    upload. Raise it (e.g. 30) for a scale-to-zero Cloud Run backend so a cold
    start can complete instead of timing out into a null vector."""

    auth: str
    """How the client authenticates to the service (``CODE_EMBEDDER_AUTH``):
    ``"none"`` (a local/unauthenticated TEI) or ``"gcp_id_token"`` (a private Cloud
    Run service — the client mints a Google-signed identity token, audience = the
    service URL, from the GCE/Cloud Run metadata server and sends it as a bearer)."""

    @property
    def enabled(self) -> bool:
        """True iff a URL is configured (otherwise the null embedder is used)."""
        return bool(self.url)

    @property
    def model_tag(self) -> str:
        """``model@revision`` provenance stamp stored with each vector."""
        return f"{self.model}@{self.revision}"


_VALID_AUTH = {"none", "gcp_id_token"}


def parse_embedding_config_from_env() -> EmbeddingConfig:
    """Build :class:`EmbeddingConfig` from ``CODE_EMBEDDER_*`` env vars.

    Disabled unless ``CODE_EMBEDDER_URL`` is set. Validates via
    :func:`check_embedding_config` before returning, so a bad value fails at boot
    (matching the pricing/storage sub-configs, which also validate at parse).

    Raises:
        EmbeddingConfigError: When a numeric env var cannot be parsed, or the
            resolved config fails validation.
    """
    url = os.environ.get("CODE_EMBEDDER_URL") or None
    model = os.environ.get("CODE_EMBEDDER_MODEL", "").strip()
    revision = os.environ.get("CODE_EMBEDDER_REVISION", "main").strip() or "main"

    auth = os.environ.get("CODE_EMBEDDER_AUTH", "none").strip().lower() or "none"
    raw_dim = os.environ.get("CODE_EMBEDDER_DIM", "").strip()
    raw_timeout = os.environ.get("CODE_EMBEDDER_TIMEOUT_SECONDS", "5.0").strip()
    try:
        dim = int(raw_dim) if raw_dim else None
    except ValueError as e:
        raise EmbeddingConfigError(
            f"CODE_EMBEDDER_DIM must be an integer, got {raw_dim!r}"
        ) from e
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as e:
        raise EmbeddingConfigError(
            f"CODE_EMBEDDER_TIMEOUT_SECONDS must be a float, got {raw_timeout!r}"
        ) from e

    config = EmbeddingConfig(
        url=url,
        model=model,
        revision=revision,
        dim=dim,
        timeout_seconds=timeout_seconds,
        auth=auth,
    )
    check_embedding_config(config)
    return config


def check_embedding_config(config: EmbeddingConfig) -> None:
    """Validate the embedder config, fail-fast at boot.

    A disabled embedder (no URL) skips the enabled-only checks — it is a valid,
    no-op configuration. When enabled, the model id must be non-empty (so stored
    provenance is meaningful) and the URL must be http(s).

    Raises:
        EmbeddingConfigError: On a non-positive/non-finite timeout, a non-positive
            dimension, or — when enabled — a blank model or non-http URL.
    """
    t = config.timeout_seconds
    # NaN != NaN is the canonical NaN check; the inf tuple catches +/- infinity.
    if t != t or t in (float("inf"), float("-inf")) or t <= 0:
        raise EmbeddingConfigError(
            f"CODE_EMBEDDER_TIMEOUT_SECONDS must be a positive finite float, got {t}"
        )
    if config.dim is not None and config.dim <= 0:
        raise EmbeddingConfigError(
            f"CODE_EMBEDDER_DIM must be a positive integer, got {config.dim}"
        )
    if config.auth not in _VALID_AUTH:
        raise EmbeddingConfigError(
            f"CODE_EMBEDDER_AUTH must be one of {sorted(_VALID_AUTH)}, "
            f"got {config.auth!r}"
        )
    if not config.enabled:
        return
    if not config.model:
        raise EmbeddingConfigError(
            "CODE_EMBEDDER_MODEL must be set when CODE_EMBEDDER_URL is configured "
            "(the model id is stored with every vector for re-embed provenance)"
        )
    assert config.url is not None  # enabled == url truthy
    if not config.url.startswith(("http://", "https://")):
        raise EmbeddingConfigError(
            f"CODE_EMBEDDER_URL must be an http(s) URL, got {config.url!r}"
        )
