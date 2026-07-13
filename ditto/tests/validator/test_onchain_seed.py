"""Unit tests for :mod:`ditto.validator.onchain_seed` (prod hardening P2).

The validator's re-derivation must stay byte-compatible with the platform's
``ditto/api_server/onchain_seed.py``; the pinned vector below is asserted in
BOTH repos, so a drift on either side fails a test somewhere.
"""

from __future__ import annotations

from uuid import UUID

from ditto.validator.onchain_seed import (
    derive_seed,
    normalize_block_hash,
    seed_matches,
)

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_AGENT_B = UUID("550e8400-e29b-41d4-a716-446655440001")
_HASH = "0x1234abcd" + "ef" * 28

# Cross-repo pinned vector: derive_seed(_HASH, _AGENT) in the platform repo
# must equal this exact value (see the platform's test_onchain_seed.py).
_PINNED = 4688446344444921196


class TestDeriveSeed:
    def test_cross_repo_pinned_vector(self) -> None:
        assert derive_seed(_HASH, _AGENT) == _PINNED

    def test_in_non_negative_int63_range(self) -> None:
        assert 0 <= derive_seed(_HASH, _AGENT) < (1 << 63)

    def test_prefix_and_case_insensitive(self) -> None:
        bare = _HASH[2:]
        assert derive_seed(_HASH, _AGENT) == derive_seed(bare, _AGENT)
        assert derive_seed(_HASH, _AGENT) == derive_seed(_HASH.upper(), _AGENT)

    def test_varies_by_block_and_agent(self) -> None:
        assert derive_seed(_HASH, _AGENT) != derive_seed("0x" + "ab" * 32, _AGENT)
        assert derive_seed(_HASH, _AGENT) != derive_seed(_HASH, _AGENT_B)


class TestSeedMatches:
    def test_accepts_derived_seed(self) -> None:
        assert seed_matches(_HASH, _AGENT, _PINNED)

    def test_rejects_ground_seed(self) -> None:
        assert not seed_matches(_HASH, _AGENT, _PINNED + 1)
        assert not seed_matches(_HASH, _AGENT_B, _PINNED)


class TestNormalizeBlockHash:
    def test_strips_prefix_and_lowercases(self) -> None:
        assert normalize_block_hash("0xABCD") == "abcd"
        assert normalize_block_hash("  0xAbCd  ") == "abcd"
        assert normalize_block_hash("abcd") == "abcd"
