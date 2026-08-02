"""Shipping the v3 code must not start the v3 epoch.

Miners get notice before anything reranks emissions or releases a retired
version's answer keys, so advancing CURRENT_BENCH_VERSION -- which happens the
moment this code deploys -- must be inert. The epoch starts when an operator
activates the rollout, and only then.
"""

from __future__ import annotations

from ditto.api_server.bench import CURRENT_BENCH_VERSION, is_bench_version_retired
from ditto.db.queries.benchmark_rollout import DEFAULT_BENCH_VERSION


def test_shipping_the_bump_does_not_retire_the_live_version() -> None:
    """The answer-key release is irreversible, so it must follow the ACTIVATED
    epoch rather than the shipped constant."""
    active = DEFAULT_BENCH_VERSION  # no activated rollout yet
    assert active < CURRENT_BENCH_VERSION, "this test is meaningless once they agree"
    assert is_bench_version_retired(active, active) is False
    # The previous version only retires once the newer epoch is active.
    assert is_bench_version_retired(active, CURRENT_BENCH_VERSION) is True
