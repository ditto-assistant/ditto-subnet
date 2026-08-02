"""Code-embedding client for the anti-copy gate (docs/SEMANTIC-CLONE-PREVENTION.md).

A self-hosted text-embeddings-inference (TEI) service holds a small open code model
(Qwen3-Embedding-0.6B primary, jina-embeddings-v2-base-code CPU fallback). This
package is the platform-side client: an env-driven config (disabled unless
``CODE_EMBEDDER_URL`` is set), a best-effort ``Embedder`` that returns ``None`` on any
failure, and a pure :func:`cosine` the gate uses to compare stored vectors.

Usage:
    from ditto.api_server.embedding import (
        create_embedder,
        parse_embedding_config_from_env,
    )

    config = parse_embedding_config_from_env()
    embedder = create_embedder(config)
    try:
        vector = await embedder.embed(source_text)  # None when disabled/failed
    finally:
        await embedder.aclose()
"""

from __future__ import annotations

from ditto.api_server.embedding.client import (
    Embedder,
    NullEmbedder,
    TeiEmbedder,
    cosine,
    create_embedder,
)
from ditto.api_server.embedding.config import (
    EmbeddingConfig,
    check_embedding_config,
    parse_embedding_config_from_env,
)
from ditto.api_server.embedding.errors import EmbeddingConfigError, EmbeddingError

__all__ = [
    "Embedder",
    "EmbeddingConfig",
    "EmbeddingConfigError",
    "EmbeddingError",
    "NullEmbedder",
    "TeiEmbedder",
    "check_embedding_config",
    "cosine",
    "create_embedder",
    "parse_embedding_config_from_env",
]
