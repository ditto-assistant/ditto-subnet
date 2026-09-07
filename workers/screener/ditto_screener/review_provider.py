"""Which OpenAI-compatible gateway carries the private source-review traffic.

OpenRouter is the historical default. ``ditto`` is Ditto Inference
(https://developer.heyditto.ai/endpoints): one OpenAI-compatible URL per
endpoint with its own model routes and ``ditto_inf_`` bearer keys. Both serve
``/chat/completions`` (L1, L4) and ``/responses`` (L2, L3); only OpenRouter
understands the attribution headers, the ``provider`` routing block, the
``models`` failover chain, and the metadata header that returns metered cost.
This module is import-light on purpose so config can validate against it.
"""

from __future__ import annotations

REVIEW_INFERENCE_PROVIDERS: tuple[str, ...] = ("openrouter", "ditto")
OPENROUTER_REVIEW_BASE_URL = "https://openrouter.ai/api/v1"
DITTO_INFERENCE_BASE_URL = "https://api.heyditto.ai/v1"

OPENROUTER_ATTRIBUTION_HEADERS: dict[str, str] = {
    # https://openrouter.ai/docs/app-attribution
    "HTTP-Referer": "https://heyditto.ai",
    "X-OpenRouter-Title": "Ditto",
}


def default_review_base_url(provider: str) -> str:
    """Return the OpenAI-compatible root a review provider serves by default."""
    if provider == "ditto":
        return DITTO_INFERENCE_BASE_URL
    return OPENROUTER_REVIEW_BASE_URL


def review_gateway_headers(provider: str) -> dict[str, str]:
    """Return the per-provider request headers that sit beside the bearer token.

    Attribution is an OpenRouter convention; Ditto Inference identifies the
    caller by the endpoint key alone, so OpenRouter's headers are not sent there.
    """
    if provider == "openrouter":
        return dict(OPENROUTER_ATTRIBUTION_HEADERS)
    return {}
