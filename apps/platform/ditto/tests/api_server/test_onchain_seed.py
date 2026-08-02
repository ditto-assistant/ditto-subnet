"""Unit tests for :mod:`ditto.api_server.onchain_seed`.

The dataset seed is a verifiable, deterministic function of an on-chain block
hash + the agent id: anyone can recompute it, and it stays in the non-negative
int64 range downstream consumers require.
"""

from __future__ import annotations

from uuid import UUID

from ditto.api_server.onchain_seed import (
    derive_seed,
    derive_validator_seed,
    normalize_block_hash,
)

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_AGENT_B = UUID("550e8400-e29b-41d4-a716-446655440001")
_HASH = "0x1234abcd" + "ef" * 28


class TestDeriveSeed:
    def test_cross_repo_pinned_vector(self) -> None:
        # ditto-subnet's validator re-derives this seed independently
        # (ditto/validator/onchain_seed.py) and pins the SAME value, so a
        # derivation drift on either side fails a test somewhere.
        assert derive_seed(_HASH, _AGENT) == 4688446344444921196

    def test_in_non_negative_int63_range(self) -> None:
        seed = derive_seed(_HASH, _AGENT)
        assert 0 <= seed < (1 << 63)

    def test_deterministic(self) -> None:
        assert derive_seed(_HASH, _AGENT) == derive_seed(_HASH, _AGENT)

    def test_prefix_and_case_insensitive(self) -> None:
        # An independent verifier who fetched the hash in a different form (0x
        # prefix, upper/lower case) must recompute the identical seed.
        bare = _HASH[2:]
        assert derive_seed(_HASH, _AGENT) == derive_seed(bare, _AGENT)
        assert derive_seed(_HASH, _AGENT) == derive_seed(_HASH.upper(), _AGENT)

    def test_varies_by_block(self) -> None:
        other = "0x" + "ab" * 32
        assert derive_seed(_HASH, _AGENT) != derive_seed(other, _AGENT)

    def test_varies_by_agent(self) -> None:
        # Two agents pinned at the same block still get distinct datasets.
        assert derive_seed(_HASH, _AGENT) != derive_seed(_HASH, _AGENT_B)

    def test_validator_binding_gives_quorum_members_distinct_seeds(self) -> None:
        seeds = {
            derive_validator_seed(_HASH, _AGENT, hotkey)
            for hotkey in ("validator-a", "validator-b", "validator-c")
        }
        assert len(seeds) == 3

    def test_validator_seed_is_stable_and_bound_to_all_inputs(self) -> None:
        seed = derive_validator_seed(_HASH, _AGENT, "validator-a")
        # Cross-repo vector pinned in ditto-subnet's independent verifier.
        assert seed == 225366234910597484
        assert seed == derive_validator_seed(_HASH, _AGENT, "validator-a")
        assert seed != derive_validator_seed(_HASH, _AGENT_B, "validator-a")
        assert seed != derive_validator_seed(_HASH, _AGENT, "validator-b")
        assert 0 <= seed < (1 << 63)


class TestNormalizeBlockHash:
    def test_strips_prefix_and_lowercases(self) -> None:
        assert normalize_block_hash("0xABCD") == "abcd"
        assert normalize_block_hash("  0xAbCd  ") == "abcd"
        assert normalize_block_hash("abcd") == "abcd"
