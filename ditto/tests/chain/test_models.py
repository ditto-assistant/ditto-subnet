"""Unit tests for ditto.chain.models.

Scope is real logic: ``__post_init__`` validation on ``ChainConfig`` and the
``from_pylon`` adapters on each result model. Tests of language behavior
(frozen, default values matching declarations, kwargs override) are
intentionally absent - Python guarantees those.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ditto.chain.models import (
    BlockInfo,
    ChainConfig,
    ExtrinsicInfo,
    NeuronInfo,
    parse_chain_config_from_env,
)


def make_pylon_arg(name: str, value: Any) -> MagicMock:
    """Build a Pylon ``ExtrinsicCallArg``-shaped object.

    ``MagicMock(name=...)`` sets repr-name not the ``.name`` attribute, so
    we assign after construction.
    """
    arg = MagicMock()
    arg.name = name
    arg.type = "u64"
    arg.value = value
    return arg


# --- ChainConfig.__post_init__ validation ---


class TestChainConfigValidation:
    """Tests for ChainConfig.__post_init__ auth-mode validation."""

    def test_open_access_alone_is_valid(self):
        config = ChainConfig(
            pylon_url="http://pylon:8000",
            netuid=118,
            open_access_token="open-tok",
        )
        assert config.open_access_token == "open-tok"
        assert config.identity_name is None
        assert config.identity_token is None

    def test_identity_pair_alone_is_valid(self):
        config = ChainConfig(
            pylon_url="http://pylon:8000",
            netuid=118,
            identity_name="validator",
            identity_token="id-tok",
        )
        assert config.open_access_token is None
        assert config.identity_name == "validator"
        assert config.identity_token == "id-tok"

    def test_both_modes_simultaneously_is_valid(self):
        config = ChainConfig(
            pylon_url="http://pylon:8000",
            netuid=118,
            open_access_token="open-tok",
            identity_name="validator",
            identity_token="id-tok",
        )
        assert config.open_access_token == "open-tok"
        assert config.identity_name == "validator"

    def test_no_auth_at_all_raises(self):
        with pytest.raises(ValueError, match="open_access_token or"):
            ChainConfig(pylon_url="http://pylon:8000", netuid=118)

    def test_identity_name_without_token_raises(self):
        with pytest.raises(ValueError, match="must be provided together"):
            ChainConfig(
                pylon_url="http://pylon:8000",
                netuid=118,
                identity_name="validator",
            )

    def test_identity_token_without_name_raises(self):
        with pytest.raises(ValueError, match="must be provided together"):
            ChainConfig(
                pylon_url="http://pylon:8000",
                netuid=118,
                identity_token="id-tok",
            )


# --- NeuronInfo.from_pylon ---


class TestNeuronInfoFromPylon:
    """Tests for NeuronInfo.from_pylon adapter."""

    def test_dict_key_hotkey_overrides_raw_field(self):
        """When ``GetNeuronsResponse.neurons`` is iterated as ``.items()``,
        the dict key is the authoritative hotkey - ``from_pylon`` must honour
        the override even when ``raw.hotkey`` differs."""
        raw = MagicMock(
            hotkey="5INNER",
            coldkey="5CK",
            uid=4,
            stake=3.0,
            axon_info={"ip": "1.1.1.1"},
            active=True,
            validator_permit=True,
        )
        n = NeuronInfo.from_pylon(raw, hotkey="5KEY")
        assert n.hotkey == "5KEY"

    def test_raw_hotkey_used_when_no_override(self):
        raw = MagicMock(
            hotkey="5RAW",
            coldkey="5CK",
            uid=1,
            stake=0.0,
            axon_info=None,
            active=False,
            validator_permit=False,
        )
        assert NeuronInfo.from_pylon(raw).hotkey == "5RAW"

    def test_active_and_permit_carried_through_as_bool(self):
        """Pylon's ``Neuron.active`` / ``.validator_permit`` reach us as the
        ``is_active`` / ``validator_permit`` fields. Both directions tested."""
        raw_on = MagicMock(active=True, validator_permit=True)
        raw_off = MagicMock(active=False, validator_permit=False)
        assert NeuronInfo.from_pylon(raw_on).is_active is True
        assert NeuronInfo.from_pylon(raw_on).validator_permit is True
        assert NeuronInfo.from_pylon(raw_off).is_active is False
        assert NeuronInfo.from_pylon(raw_off).validator_permit is False

    def test_none_axon_info_becomes_empty_dict(self):
        """Pylon may return ``axon_info=None`` for unserved neurons; adapter
        must not propagate ``None`` into the dataclass."""
        raw = MagicMock(axon_info=None)
        assert NeuronInfo.from_pylon(raw).axon_info == {}

    def test_dict_axon_info_passes_through(self):
        raw = MagicMock(axon_info={"ip": "1.2.3.4", "port": 9000})
        assert NeuronInfo.from_pylon(raw).axon_info == {
            "ip": "1.2.3.4",
            "port": 9000,
        }

    def test_pydantic_model_axon_info_is_flattened_via_model_dump(self):
        """Real Pylon returns ``AxonInfo`` (a Pydantic model). The adapter
        calls ``.model_dump()`` to flatten it to a plain dict."""
        axon = MagicMock()
        axon.model_dump = MagicMock(return_value={"ip": "1.2.3.4", "port": 9000})
        del axon.__class__.__iter__  # ensure isinstance(axon, dict) is False
        raw = MagicMock(axon_info=axon)
        result = NeuronInfo.from_pylon(raw)
        assert result.axon_info == {"ip": "1.2.3.4", "port": 9000}
        axon.model_dump.assert_called_once()


# --- ExtrinsicInfo.from_pylon ---


class TestExtrinsicInfoFromPylon:
    """Tests for ExtrinsicInfo.from_pylon adapter."""

    def test_flattens_list_of_call_args_to_name_value_dict(self):
        """Real Pylon returns ``call_args`` as ``list[ExtrinsicCallArg]`` where
        each arg has ``.name`` and ``.value``. The adapter flattens to a
        ``{name: value}`` dict for caller convenience."""
        raw = MagicMock(
            block_number=42,
            extrinsic_index=3,
            extrinsic_hash="0xhash",
            address="5Signer",
            call=MagicMock(
                call_module="Balances",
                call_function="transfer_keep_alive",
                call_args=[
                    make_pylon_arg("dest", "5Recipient"),
                    make_pylon_arg("value", 42_000),
                ],
            ),
        )
        ext = ExtrinsicInfo.from_pylon(raw)
        assert ext.call_args == {"dest": "5Recipient", "value": 42_000}
        assert ext.block_number == 42
        assert ext.extrinsic_index == 3
        assert ext.extrinsic_hash == "0xhash"
        assert ext.signer_address == "5Signer"
        assert ext.call_module == "Balances"
        assert ext.call_function == "transfer_keep_alive"

    def test_succeeded_param_propagated(self):
        raw = MagicMock(
            block_number=1,
            extrinsic_index=0,
            extrinsic_hash="0x",
            address="5",
            call=MagicMock(call_module="X", call_function="y", call_args=[]),
        )
        assert ExtrinsicInfo.from_pylon(raw, succeeded=True).succeeded is True
        assert ExtrinsicInfo.from_pylon(raw, succeeded=False).succeeded is False
        assert ExtrinsicInfo.from_pylon(raw).succeeded is None

    def test_already_flat_dict_call_args_passes_through(self):
        """Defensive: some Pylon shapes (or tests) hand call_args as a dict.
        Adapter accepts either."""
        raw = MagicMock(
            block_number=1,
            extrinsic_index=0,
            extrinsic_hash="0x",
            address="5",
            call=MagicMock(
                call_module="X",
                call_function="y",
                call_args={"a": 1, "b": 2},
            ),
        )
        assert ExtrinsicInfo.from_pylon(raw).call_args == {"a": 1, "b": 2}

    def test_none_call_args_becomes_empty_dict(self):
        raw = MagicMock(
            block_number=1,
            extrinsic_index=0,
            extrinsic_hash="0x",
            address="5",
            call=MagicMock(call_module="X", call_function="y", call_args=None),
        )
        assert ExtrinsicInfo.from_pylon(raw).call_args == {}

    def test_args_with_missing_name_are_skipped(self):
        """Defensive: an arg whose ``name`` is ``None`` cannot be keyed into
        the dict and must be dropped rather than blowing up on ``str(None)``."""
        unnamed = MagicMock()
        unnamed.name = None
        unnamed.value = "ignored"
        named = make_pylon_arg("real", 1)
        raw = MagicMock(
            block_number=1,
            extrinsic_index=0,
            extrinsic_hash="0x",
            address="5",
            call=MagicMock(
                call_module="X",
                call_function="y",
                call_args=[unnamed, named],
            ),
        )
        assert ExtrinsicInfo.from_pylon(raw).call_args == {"real": 1}

    def test_mixed_list_of_dict_and_object_args(self):
        """Defensive: a list mixing ``ExtrinsicCallArg`` objects with raw
        dicts should both flatten correctly."""
        raw = MagicMock(
            block_number=1,
            extrinsic_index=0,
            extrinsic_hash="0x",
            address="5",
            call=MagicMock(
                call_module="X",
                call_function="y",
                call_args=[
                    {"name": "a", "value": 1},
                    make_pylon_arg("b", 2),
                ],
            ),
        )
        assert ExtrinsicInfo.from_pylon(raw).call_args == {"a": 1, "b": 2}


# --- BlockInfo.from_pylon ---


class TestBlockInfoFromPylon:
    """Tests for BlockInfo.from_pylon adapter."""

    def test_carries_number_hash_timestamp(self):
        raw = MagicMock(number=99, hash="0xblock", timestamp=1700000100)
        block = BlockInfo.from_pylon(raw)
        assert block.number == 99
        assert block.hash == "0xblock"
        assert block.timestamp == 1700000100

    def test_missing_timestamp_defaults_to_zero(self):
        """``BlockInfoBag`` always provides timestamp, but plain ``Block``
        (returned by other endpoints in future use) does not. Adapter must
        handle either."""
        raw = MagicMock(spec=["number", "hash"])
        raw.number = 5
        raw.hash = "0xabc"
        block = BlockInfo.from_pylon(raw)
        assert block.number == 5
        assert block.hash == "0xabc"
        assert block.timestamp == 0


# --- parse_chain_config_from_env ---


class TestParseChainConfigFromEnv:
    def test_open_access_only_is_parsed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PYLON_URL", "http://pylon:9999")
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "open-tok")
        monkeypatch.setenv("NETUID", "118")
        monkeypatch.delenv("PYLON_IDENTITY_NAME", raising=False)
        monkeypatch.delenv("PYLON_IDENTITY_TOKEN", raising=False)

        config = parse_chain_config_from_env()

        assert config.pylon_url == "http://pylon:9999"
        assert config.netuid == 118
        assert config.open_access_token == "open-tok"
        assert config.identity_name is None
        assert config.identity_token is None

    def test_identity_pair_is_parsed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PYLON_OPEN_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("PYLON_IDENTITY_NAME", "validator")
        monkeypatch.setenv("PYLON_IDENTITY_TOKEN", "id-tok")

        config = parse_chain_config_from_env()

        assert config.identity_name == "validator"
        assert config.identity_token == "id-tok"

    def test_empty_string_tokens_become_none(self, monkeypatch: pytest.MonkeyPatch):
        # Empty .env values would otherwise pass the auth-mode truthiness
        # check and look like configured tokens.
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "real-tok")
        monkeypatch.setenv("PYLON_IDENTITY_NAME", "")
        monkeypatch.setenv("PYLON_IDENTITY_TOKEN", "")

        config = parse_chain_config_from_env()

        assert config.identity_name is None
        assert config.identity_token is None

    def test_defaults_apply_when_optional_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok")
        monkeypatch.delenv("PYLON_URL", raising=False)
        monkeypatch.delenv("NETUID", raising=False)
        monkeypatch.delenv("SUBTENSOR_NETWORK", raising=False)

        config = parse_chain_config_from_env()

        # Default Pylon URL aligns with the post-shift compose layout.
        assert config.pylon_url == "http://localhost:8001"
        assert config.netuid == 118
        assert config.subtensor_network == "finney"

    def test_no_auth_configured_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PYLON_OPEN_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("PYLON_IDENTITY_NAME", raising=False)
        monkeypatch.delenv("PYLON_IDENTITY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="open_access_token or"):
            parse_chain_config_from_env()
