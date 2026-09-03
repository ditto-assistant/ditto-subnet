from uuid import uuid4

from ditto.api_server.crn import (
    active_confirmation_seed_set,
    bounded_continual_seed_set,
    champion_anchored_seeds,
    elastic_confirmation_seed_ceiling,
)


def test_active_confirmation_seed_set_caps_legacy_history_by_coverage() -> None:
    legacy = uuid4()
    current_a = uuid4()
    current_b = uuid4()
    shared = range(17, 32)

    active = active_confirmation_seed_set(
        {
            legacy: range(32),
            current_a: shared,
            current_b: shared,
        }
    )

    assert active == tuple(shared)


def test_elastic_confirmation_seed_ceiling_is_bounded_by_variance() -> None:
    first = uuid4()
    second = uuid4()

    assert elastic_confirmation_seed_ceiling({}) == 8
    assert elastic_confirmation_seed_ceiling({first: dict.fromkeys(range(8), 0.9)}) == 8
    assert elastic_confirmation_seed_ceiling({first: {0: 0.49, 1: 0.51}}) == 11
    assert (
        elastic_confirmation_seed_ceiling(
            {
                first: dict(enumerate((0.80, 0.90) * 4)),
                second: dict.fromkeys(range(8), 0.85),
            }
        )
        == 15
    )


def test_bounded_continual_seed_set_prefers_shared_history_then_fresh_seeds() -> None:
    champion = uuid4()
    peer = uuid4()
    history = {
        champion: {11: 0.8, 22: 0.8},
        peer: {11: 0.8, 33: 0.8},
    }

    targets = bounded_continual_seed_set(
        champion, version=8, composites_by_agent=history
    )

    assert targets[:3] == (11, 22, 33)
    assert len(targets) == 8
    assert set(targets[3:]).issubset(
        set(champion_anchored_seeds(champion, version=8, max_seeds=8))
    )
