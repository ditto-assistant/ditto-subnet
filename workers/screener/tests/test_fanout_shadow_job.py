import pytest

from ditto_screener.fanout_shadow_job import _shadow_inference_route


def test_shadow_inference_route_is_exact_dedicated_router(monkeypatch):
    monkeypatch.setenv("SCREENER_REVIEW_INFERENCE_PROVIDER", "ditto")
    monkeypatch.setenv(
        "SCREENER_SOURCE_REVIEW_BASE_URL", "https://router.heyditto.ai/v1/"
    )
    assert _shadow_inference_route() == (
        "ditto",
        "https://router.heyditto.ai/v1",
    )


def test_shadow_inference_route_rejects_openrouter(monkeypatch):
    monkeypatch.setenv("SCREENER_REVIEW_INFERENCE_PROVIDER", "openrouter")
    monkeypatch.setenv(
        "SCREENER_SOURCE_REVIEW_BASE_URL", "https://openrouter.ai/api/v1"
    )
    with pytest.raises(ValueError, match="dedicated Ditto Router"):
        _shadow_inference_route()


def test_shadow_inference_route_rejects_other_ditto_url(monkeypatch):
    monkeypatch.setenv("SCREENER_REVIEW_INFERENCE_PROVIDER", "ditto")
    monkeypatch.setenv(
        "SCREENER_SOURCE_REVIEW_BASE_URL", "https://inference.heyditto.ai/v1"
    )
    with pytest.raises(ValueError, match="Router URL changed"):
        _shadow_inference_route()
