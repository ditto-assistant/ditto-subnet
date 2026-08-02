"""Configuration for the ditto-data-pipeline generate client.

The generate service is the PRIVATE, platform-only oracle: it renders the
per-submission benchmark dataset (deterministically, from a seed) and returns its
DatasetArtifact SHA-256. The platform calls it once per submission at job-ready.

Disabled by default: with ``DATA_PIPELINE_URL`` unset the platform runs unchanged
and no dataset is generated (job-ready promotion proceeds without pinning a
dataset — the pre-k3 behavior). Setting the URL turns it on and the remaining
fields are validated fail-fast, matching the embedding sub-config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ditto.api_server.datapipeline.errors import DataPipelineConfigError

_VALID_AUTH = {"none", "gcp_id_token"}
_VALID_RUN_SIZES = {"small", "medium", "full"}


@dataclass(frozen=True)
class DataPipelineConfig:
    """Resolved configuration for the generate client."""

    url: str | None
    """Generate-service base URL (``DATA_PIPELINE_URL``), e.g.
    ``https://ditto-data-pipeline-....run.app``. ``None`` disables generation."""

    run_size: str
    """Generator profile for scored datasets (``DATA_PIPELINE_RUN_SIZE``, default
    ``full``). One of ``small|medium|full``; issued with the ticket so the
    validator scores the same profile."""

    timeout_seconds: float
    """Per-call HTTP timeout (``DATA_PIPELINE_TIMEOUT_SECONDS``). Generous by
    default so a scale-to-zero Cloud Run cold start can complete."""

    auth: str
    """How the client authenticates (``DATA_PIPELINE_AUTH``): ``"none"`` (local /
    unauthenticated) or ``"gcp_id_token"`` (a private Cloud Run service — the
    client mints a Google identity token, audience = the service URL)."""

    @property
    def enabled(self) -> bool:
        """True iff a URL is configured."""
        return bool(self.url)


def parse_data_pipeline_config_from_env() -> DataPipelineConfig:
    """Build :class:`DataPipelineConfig` from ``DATA_PIPELINE_*`` env vars.

    Disabled unless ``DATA_PIPELINE_URL`` is set. Validates before returning so a
    bad value fails at boot.

    Raises:
        DataPipelineConfigError: When a numeric env var cannot be parsed, or the
            resolved config fails validation.
    """
    url = os.environ.get("DATA_PIPELINE_URL") or None
    run_size = (
        os.environ.get("DATA_PIPELINE_RUN_SIZE", "full").strip().lower() or "full"
    )
    auth = os.environ.get("DATA_PIPELINE_AUTH", "none").strip().lower() or "none"
    raw_timeout = os.environ.get("DATA_PIPELINE_TIMEOUT_SECONDS", "30.0").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as e:
        raise DataPipelineConfigError(
            f"DATA_PIPELINE_TIMEOUT_SECONDS must be a float, got {raw_timeout!r}"
        ) from e

    config = DataPipelineConfig(
        url=url,
        run_size=run_size,
        timeout_seconds=timeout_seconds,
        auth=auth,
    )
    check_data_pipeline_config(config)
    return config


def check_data_pipeline_config(config: DataPipelineConfig) -> None:
    """Validate the generate config, fail-fast at boot.

    Raises:
        DataPipelineConfigError: On a non-positive/non-finite timeout, an invalid
            run_size or auth mode, or — when enabled — a non-http URL.
    """
    t = config.timeout_seconds
    if t != t or t in (float("inf"), float("-inf")) or t <= 0:
        raise DataPipelineConfigError(
            f"DATA_PIPELINE_TIMEOUT_SECONDS must be a positive finite float, got {t}"
        )
    if config.run_size not in _VALID_RUN_SIZES:
        raise DataPipelineConfigError(
            f"DATA_PIPELINE_RUN_SIZE must be one of {sorted(_VALID_RUN_SIZES)}, "
            f"got {config.run_size!r}"
        )
    if config.auth not in _VALID_AUTH:
        raise DataPipelineConfigError(
            f"DATA_PIPELINE_AUTH must be one of {sorted(_VALID_AUTH)}, "
            f"got {config.auth!r}"
        )
    if not config.enabled:
        return
    assert config.url is not None  # enabled == url truthy
    if not config.url.startswith(("http://", "https://")):
        raise DataPipelineConfigError(
            f"DATA_PIPELINE_URL must be an http(s) URL, got {config.url!r}"
        )
