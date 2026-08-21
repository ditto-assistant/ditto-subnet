"""Preview URL contract shared by local orchestrator and GitHub Actions."""

from __future__ import annotations

from ditto.preview.composition import PreviewPlan
from ditto.preview.identity import preview_host

PROD_PLATFORM = "https://platform-api.heyditto.ai"
PROD_DASHBOARD = "https://dittobench.ai"
PROD_BACKROOM = "https://backroom.dittobench.ai"


def plan_urls(
    plan: PreviewPlan,
    identity: str,
    *,
    control_url: str,
    zone: str = "preview.dittobench.ai",
    local: bool = False,
    local_control_port: int = 4077,
) -> dict[str, str]:
    """Hostnames or loopback URLs for a resolved plan."""
    urls: dict[str, str] = {"control": control_url, "id": identity}
    if local:
        urls["dashboard"] = (
            f"http://127.0.0.1:5173/?api={PROD_PLATFORM if plan.attach_prod_api else 'http://127.0.0.1:8000/api/v1'}"
        )
        urls["backroom"] = "http://127.0.0.1:3000"
        urls["platform"] = (
            PROD_PLATFORM if plan.attach_prod_api else "http://127.0.0.1:8000"
        )
        urls["fault_proxy"] = f"http://127.0.0.1:{local_control_port + 1}"
        return urls
    platform = (
        PROD_PLATFORM
        if plan.attach_prod_api
        else f"https://{preview_host(identity, 'api', zone)}"
    )
    if plan.dashboard:
        dash = f"https://{preview_host(identity, 'dash', zone)}"
        urls["dashboard"] = f"{dash}/?api={platform}"
    if plan.backroom:
        urls["backroom"] = f"https://{preview_host(identity, 'br', zone)}"
    if plan.stack:
        urls["platform"] = platform
        urls["validator"] = "localnet"
    else:
        urls["platform"] = platform
    return urls
