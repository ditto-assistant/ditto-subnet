from __future__ import annotations

import hashlib
import random
from collections import Counter

from ditto_edge_protocol.placement import (
    PlacementNode,
    PlacementObject,
    desired_objects,
    holders,
)

SALT = b"lane-salt"


def _digest(i: int) -> str:
    return hashlib.sha256(f"object-{i}".encode()).hexdigest()


def _nodes(n: int, operators: int | None = None) -> list[PlacementNode]:
    return [
        PlacementNode(
            node_id=f"edn_{i:032x}",
            operator=f"op-{i % operators}" if operators else f"edn_{i:032x}",
        )
        for i in range(n)
    ]


def _objects(n: int, size: int = 10) -> list[PlacementObject]:
    return [PlacementObject(_digest(i), size, float(i)) for i in range(n)]


def test_each_object_gets_replicas_distinct_holders() -> None:
    nodes = _nodes(9)
    for obj in _objects(50):
        chosen = holders(digest=obj.digest, nodes=nodes, replicas=3, salt=SALT)
        assert len(chosen) == 3 == len(set(chosen))


def test_never_two_replicas_behind_one_operator() -> None:
    nodes = _nodes(9, operators=2)
    for obj in _objects(50):
        chosen = holders(digest=obj.digest, nodes=nodes, replicas=3, salt=SALT)
        operators = {n.operator for n in nodes if n.node_id in chosen}
        assert len(chosen) == len(operators) == 2


def test_load_spreads_evenly_and_late_nodes_get_a_share() -> None:
    nodes = _nodes(9)
    objects = _objects(900)
    load = Counter(
        node_id
        for obj in objects
        for node_id in holders(digest=obj.digest, nodes=nodes, replicas=3, salt=SALT)
    )
    expected = 900 * 3 / 9
    assert set(load) == {n.node_id for n in nodes}
    assert all(0.75 * expected < count < 1.25 * expected for count in load.values())


def test_a_joining_node_only_moves_its_own_share() -> None:
    objects = _objects(600)
    before = {
        o.digest: set(holders(digest=o.digest, nodes=_nodes(10), replicas=3, salt=SALT))
        for o in objects
    }
    after = {
        o.digest: set(holders(digest=o.digest, nodes=_nodes(11), replicas=3, salt=SALT))
        for o in objects
    }
    newcomer = _nodes(11)[-1].node_id
    gained = [after[d] - before[d] for d in after]
    # Every replica that moved went to the new node; nothing else reshuffled.
    assert all(added <= {newcomer} for added in gained)
    moved = sum(len(added) for added in gained)
    expected = 600 * 3 / 11
    assert 0.7 * expected < moved < 1.3 * expected


def test_placement_ignores_input_order_and_depends_on_the_salt() -> None:
    nodes = _nodes(7)
    digest = _digest(1)
    expected = holders(digest=digest, nodes=nodes, replicas=3, salt=SALT)
    for seed in range(5):
        shuffled = nodes[:]
        random.Random(seed).shuffle(shuffled)
        assert holders(digest=digest, nodes=shuffled, replicas=3, salt=SALT) == expected
    others = {
        tuple(holders(digest=digest, nodes=nodes, replicas=3, salt=bytes([s])))
        for s in range(20)
    }
    assert len(others) > 1


def test_desired_objects_fit_quota_and_object_limit_oldest_first() -> None:
    nodes = _nodes(1)
    node_id = nodes[0].node_id
    objects = [
        PlacementObject(_digest(0), 60, 3.0),
        PlacementObject(_digest(1), 50, 1.0),
        PlacementObject(_digest(2), 40, 2.0),
        PlacementObject(_digest(3), 101, 0.0),
    ]
    chosen = desired_objects(
        node_id=node_id,
        objects=objects,
        nodes=nodes,
        replicas=1,
        salt=SALT,
        quota_bytes=100,
        max_object_bytes=100,
    )
    # Oldest first: 50 fits, 40 fits (90), 60 would overflow; 101 is too big.
    assert chosen == [_digest(1), _digest(2)]


def test_desired_objects_is_empty_without_quota_or_replicas() -> None:
    nodes = _nodes(1)
    args = {
        "node_id": nodes[0].node_id,
        "objects": _objects(3),
        "nodes": nodes,
        "salt": SALT,
        "max_object_bytes": 100,
    }
    assert desired_objects(replicas=0, quota_bytes=100, **args) == []  # type: ignore[arg-type]
    assert desired_objects(replicas=1, quota_bytes=0, **args) == []  # type: ignore[arg-type]
