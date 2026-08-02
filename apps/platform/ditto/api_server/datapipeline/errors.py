"""Errors for the ditto-data-pipeline generate client."""

from __future__ import annotations


class DataPipelineConfigError(ValueError):
    """Raised at boot when the generate-service config is invalid."""


class DataPipelineError(RuntimeError):
    """Raised when the generate service cannot produce a dataset.

    Unlike the best-effort code embedder, the per-submission dataset is REQUIRED:
    an agent cannot become job-ready without one, so a generation failure must
    surface (the screener leaves the agent unpromoted and the verdict can retry)
    rather than degrade silently.
    """
