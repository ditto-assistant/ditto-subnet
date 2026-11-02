import pytest

from ditto.treasury.quote import Pool, quote_topup


def test_both_paths_from_one_finalized_snapshot() -> None:
    ditto = Pool(118, "0xabc", 100_000_000_000, 200_000_000_000, 0)
    gm = Pool(28, "0xabc", 100_000_000_000, 100_000_000_000, 0)
    quote = quote_topup(
        ditto,
        gm,
        source_alpha_rao=1_000_000_000,
        max_source_alpha_rao=2_000_000_000,
        max_impact_bps=200,
    )
    assert quote.tao_rao == 497_512_437
    assert quote.gm_alpha_rao == 495_049_504
    assert quote.gm_alpha_path_impact_bps > quote.tao_path_impact_bps


@pytest.mark.parametrize(
    ("gm_block", "amount", "limit", "impact"),
    [
        ("0xother", 1, 2, 500),
        ("0xabc", 3, 2, 500),
        ("0xabc", 1_000_000_000, 2_000_000_000, 1),
    ],
)
def test_circuit_breakers(gm_block: str, amount: int, limit: int, impact: int) -> None:
    ditto = Pool(118, "0xabc", 100_000_000_000, 200_000_000_000, 0)
    gm = Pool(28, gm_block, 100_000_000_000, 100_000_000_000, 0)
    with pytest.raises(ValueError):
        quote_topup(
            ditto,
            gm,
            source_alpha_rao=amount,
            max_source_alpha_rao=limit,
            max_impact_bps=impact,
        )
