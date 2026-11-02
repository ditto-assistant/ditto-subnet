import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
GCP_ROOT = ROOT / "infra" / "terraform" / "stacks" / "gcp-platform"


def test_cloud_run_scale_to_zero_ignores_only_inapplicable_manual_default() -> None:
    module = (
        ROOT / "infra" / "terraform" / "modules" / "cloudrun" / "main.tf"
    ).read_text()
    datapipeline = (GCP_ROOT / "datapipeline.tf").read_text()

    for terraform in (module, datapipeline):
        assert re.search(r'scaling_mode\s*=\s*"AUTOMATIC"', terraform)
        assert "min_instance_count = 0" in terraform
        assert "scaling[0].manual_instance_count" in terraform
        assert "scaling[0].min_instance_count" not in terraform


def test_controller_owns_only_the_screener_watchdog_mode() -> None:
    fleet = (GCP_ROOT / "screener-fleet.tf").read_text()

    assert 'mode         = "ONLY_SCALE_OUT"' in fleet
    assert "ignore_changes = [autoscaling_policy[0].mode]" in fleet
    assert "autoscaling_policy[0].min_replicas" not in fleet
    assert "autoscaling_policy[0].max_replicas" not in fleet
    assert "autoscaling_policy[0].metric" not in fleet
