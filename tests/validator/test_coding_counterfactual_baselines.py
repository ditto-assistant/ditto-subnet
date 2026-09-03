from __future__ import annotations

from ditto.validator.coding_counterfactual_baselines import audit_adversarial_baselines


def test_adversarial_baselines_preserve_monotone_incentives() -> None:
    audit = audit_adversarial_baselines()
    assert audit.v0_sandbagging.useful_lift > audit.honest.useful_lift
    assert (
        audit.honest.monotone_shadow_score > audit.v0_sandbagging.monotone_shadow_score
    )
    assert (
        audit.honest.monotone_shadow_score > audit.stale_follower.monotone_shadow_score
    )
