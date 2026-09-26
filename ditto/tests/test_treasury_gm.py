import pytest

from ditto.treasury.gm import parse_credit_balance


def test_credit_balance_is_exact() -> None:
    balance = parse_credit_balance(
        b'{"object":"credit_balance","balance":{"usd":"12.345678901","nano_usd":12345678901}}'
    )
    assert balance.nano_usd == 12_345_678_901


@pytest.mark.parametrize(
    "raw",
    [
        b'{"object":"error"}',
        b'{"object":"credit_balance","balance":{"usd":"1.000000000","nano_usd":2}}',
        b'{"object":"credit_balance","balance":{"usd":"1","nano_usd":1000000000}}',
    ],
)
def test_refuses_inconsistent_balance(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_credit_balance(raw)
