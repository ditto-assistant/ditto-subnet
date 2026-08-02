"""Exception hierarchy for the code-embedding client."""

from __future__ import annotations


class EmbeddingError(Exception):
    """Base exception for :mod:`ditto.api_server.embedding`."""


class EmbeddingConfigError(EmbeddingError):
    """Raised at boot when the embedder configuration is invalid.

    Fail-fast, mirroring the other ``*ConfigError`` types: an operator who sets
    ``CODE_EMBEDDER_URL`` but leaves the model id blank, or a non-positive timeout /
    dimension, should crash on startup rather than silently disable the signal.
    """
