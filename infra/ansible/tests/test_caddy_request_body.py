"""Caddy caps a platform request body before the proxy reads it."""

from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = (
    ROOT / "infra/ansible/roles/platform_app/templates/Caddyfile.j2"
).read_text()


def _render(**overrides: object) -> str:
    context = {
        "platform_caddy_email": "ops@example.com",
        "platform_domain": "platform.example.com",
        "platform_alias_domains": [],
        "platform_inference_relay_ports": [],
        "platform_upload_admission_relay_enabled": False,
        "platform_public_rate_limit_per_minute": 0,
        "platform_api_port": 8000,
        "platform_request_body_max_size": "32MiB",
    }
    context.update(overrides)
    env = Environment()
    env.filters["bool"] = bool
    return env.from_string(TEMPLATE).render(context)


def test_direct_proxy_caps_the_body() -> None:
    rendered = _render()
    assert "request_body {" in rendered
    assert "max_size 32MiB" in rendered
    assert rendered.index("request_body {") < rendered.index("reverse_proxy")


def test_relay_handles_each_cap_the_body() -> None:
    rendered = _render(
        platform_inference_relay_ports=[9001, 9002],
        platform_upload_admission_relay_enabled=True,
    )
    assert rendered.count("request_body {") == 3
    assert rendered.count("max_size 32MiB") == 3
