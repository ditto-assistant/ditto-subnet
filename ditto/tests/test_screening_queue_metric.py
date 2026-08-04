from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
TEMPLATE = (
    ROOT
    / "infra"
    / "ansible"
    / "roles"
    / "screening_queue_metric"
    / "templates"
    / "publish-screening-queue-depth.py.j2"
)
FLEET_TERRAFORM = (
    ROOT / "infra" / "terraform" / "stacks" / "gcp-platform" / "screener-fleet.tf"
)


def _fallback_depth(counts: dict[str, int], *, activate: bool) -> int:
    source = TEMPLATE.read_text()
    for marker, value in {
        "{{ screening_queue_metric_api_port }}": "8000",
        "{{ screening_queue_metric_type }}": "custom.googleapis.com/test",
        "{{ screening_queue_metric_env }}": "prod",
    }.items():
        source = source.replace(marker, value)
    namespace: dict[str, object] = {"__name__": "screening_queue_metric_test"}
    exec(compile(source, str(TEMPLATE), "exec"), namespace)
    helper = namespace["fallback_depth"]
    assert callable(helper)
    return helper(counts, {"activate_fallback": activate})  # type: ignore[operator]


def test_fresh_controller_suppresses_gce_fallback() -> None:
    assert (
        _fallback_depth({"waiting_screening": 8, "screening": 2}, activate=False) == 0
    )


def test_stale_controller_publishes_bounded_backlog() -> None:
    assert (
        _fallback_depth({"waiting_screening": 8, "screening": 2}, activate=True) == 10
    )


def test_stale_controller_with_empty_queue_stays_at_zero() -> None:
    assert _fallback_depth({"waiting_screening": 0, "screening": 0}, activate=True) == 0


def test_watchdog_can_never_scale_gce_in() -> None:
    terraform = FLEET_TERRAFORM.read_text()
    assert 'mode         = "ONLY_SCALE_OUT"' in terraform
