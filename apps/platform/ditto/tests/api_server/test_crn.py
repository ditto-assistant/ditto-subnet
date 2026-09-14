from uuid import UUID, uuid4

from ditto.api_server.crn import (
    CRN_BLOCK_BINDING_MIN_BENCH_VERSION,
    active_confirmation_seed_set,
    bounded_continual_seed_set,
    champion_anchored_seeds,
    crn_block_binding_active,
    crn_seed,
    elastic_confirmation_seed_ceiling,
    fold_seed_bound,
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


# Identical to ditto/tests/validator/test_crn.py: the two crn modules are
# byte-for-byte mirrors of one consensus encoding, and the shared vector table
# is how a drift between them fails a test instead of a fleet.
_CHAMPION = UUID("550e8400-e29b-41d4-a716-446655440000")
_BLOCK_HASH = "0x" + "ab" * 32
_LEGACY_VECTORS = {
    (("a", "b"), 3, 0): 8501849424598278624,
    (("a", "b"), 3, 2): 5718795657813926813,
    (("",), 0, 0): 2921998849593980123,
    ((str(_CHAMPION),), 12, 0): 7629001111604106833,
    ((str(_CHAMPION),), 12, 1): 5791690032916641678,
    ((str(_CHAMPION),), 12, 2): 2218791399408279363,
    ((str(_CHAMPION),), 13, 0): 3147062871295850865,
}
_BOUND_VECTORS = {
    ((str(_CHAMPION),), 13, 0): 89674146298908029,
    ((str(_CHAMPION),), 13, 1): 13997399428938499,
    ((str(_CHAMPION),), 13, 2): 822610508492091013,
    (("a", "b"), 3, 0): 1945832075495936339,
}


def test_legacy_vectors_are_byte_identical() -> None:
    for (ids, version, k), expected in _LEGACY_VECTORS.items():
        assert crn_seed(ids, version=version, k=k) == expected
        assert crn_seed(ids, version=version, k=k, block_hash=None) == expected


def test_bound_vectors_match_the_validator_table() -> None:
    for (ids, version, k), expected in _BOUND_VECTORS.items():
        assert crn_seed(ids, version=version, k=k, block_hash=_BLOCK_HASH) == expected
        assert crn_seed(ids, version=version, k=k) != expected


def test_champion_anchored_seeds_bind_to_the_reign_block() -> None:
    legacy = champion_anchored_seeds(_CHAMPION, version=13, max_seeds=3)
    bound = champion_anchored_seeds(
        _CHAMPION, version=13, max_seeds=3, block_hash=_BLOCK_HASH
    )
    assert legacy == [_LEGACY_VECTORS[((str(_CHAMPION),), 13, 0)], *legacy[1:]]
    assert bound == [_BOUND_VECTORS[((str(_CHAMPION),), 13, k)] for k in range(3)]
    assert (
        champion_anchored_seeds(
            _CHAMPION, version=13, max_seeds=3, block_hash="AB" * 32
        )
        == bound
    )


def test_binding_floor_is_a_floor() -> None:
    assert CRN_BLOCK_BINDING_MIN_BENCH_VERSION == 13
    assert not crn_block_binding_active(12)
    assert all(crn_block_binding_active(v) for v in (13, 14, 99))


def test_bounded_continual_seed_set_without_fresh_seeds_plans_coverage_only() -> None:
    champion = uuid4()
    peer = uuid4()
    history = {champion: {11: 0.8, 22: 0.8}, peer: {11: 0.8, 33: 0.8}}

    waiting = bounded_continual_seed_set(
        champion,
        version=13,
        composites_by_agent=history,
        allow_fresh_seeds=False,
    )
    assert waiting == (11, 22, 33)

    pinned = bounded_continual_seed_set(
        champion,
        version=13,
        composites_by_agent=history,
        block_hash=_BLOCK_HASH,
    )
    assert pinned[:3] == (11, 22, 33)
    assert len(pinned) == 8
    assert set(pinned[3:]).issubset(
        set(
            champion_anchored_seeds(
                champion, version=13, max_seeds=8, block_hash=_BLOCK_HASH
            )
        )
    )
    assert set(pinned[3:]).isdisjoint(
        set(champion_anchored_seeds(champion, version=13, max_seeds=8))
    )


def test_fold_seed_bound_honours_the_binding() -> None:
    champion = uuid4()
    seeds_by_agent = {champion: (5, 6)}

    waiting = fold_seed_bound(
        champion_agent_id=champion,
        anchor_version=13,
        seeds_by_agent=seeds_by_agent,
        allow_fresh_seeds=False,
    )
    assert waiting == (5, 6)

    bound = fold_seed_bound(
        champion_agent_id=champion,
        anchor_version=13,
        seeds_by_agent=seeds_by_agent,
        block_hash=_BLOCK_HASH,
    )
    assert bound[:2] == (5, 6)
    assert set(bound[2:]) <= set(
        champion_anchored_seeds(
            champion, version=13, max_seeds=15, block_hash=_BLOCK_HASH
        )
    )
