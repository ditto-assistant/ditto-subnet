from subject import Counter


def test_add() -> None:
    value = Counter(3)
    assert value.add(2) == 5


def test_fresh() -> None:
    value = Counter(3)
    assert value.add(0) == 3
