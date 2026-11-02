from __future__ import annotations

import re
from pathlib import Path

from ditto.preview.composition import compose
from ditto.preview.urls import PROD_PLATFORM, plan_urls

DASHBOARD = Path(__file__).parents[3] / "apps/platform/dashboard"


def _local(profiles: list[str]) -> dict[str, str]:
    return plan_urls(
        compose(profiles), "id", control_url="http://127.0.0.1:4077", local=True
    )


def test_local_dashboard_url_matches_the_vite_dev_server() -> None:
    vite = (DASHBOARD / "vite.config.ts").read_text()
    port = re.search(r"port: (\d+)", vite)
    assert port is not None
    assert "DITTO_DASHBOARD_PROXY_TARGET" in vite

    for profiles in (["dashboard"], ["stack"]):
        urls = _local(profiles)
        # Same-origin through the Vite proxy: no cross-origin ?api= override.
        assert urls["dashboard"] == f"http://127.0.0.1:{port.group(1)}/"


def test_local_dashboard_proxies_to_the_plan_platform() -> None:
    assert _local(["dashboard"])["dashboard_proxy_target"] == PROD_PLATFORM
    assert _local(["stack"])["dashboard_proxy_target"] == "http://127.0.0.1:8000"
