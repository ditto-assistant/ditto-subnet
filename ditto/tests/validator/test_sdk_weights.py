"""Unit tests for the bittensor-SDK weight fallback (localnet path)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ditto.validator.errors import WeightSubmissionError
from ditto.validator.sdk_weights import SdkWeightSetter


class _FakeSubtensor:
    """Records set_weights calls; maps a fixed set of hotkeys to UIDs."""

    def __init__(
        self,
        uid_map: dict[str, int],
        success: bool = True,
        permits: dict[str, bool] | None = None,
        stakes: dict[str, Any] | None = None,
    ) -> None:
        self._uid_map = uid_map
        self._success = success
        self._permits = permits or {}
        self._stakes = stakes or {}
        self.calls: list[dict[str, Any]] = []

    def get_uid_for_hotkey_on_subnet(self, hotkey: str, _netuid: int) -> int | None:
        return self._uid_map.get(hotkey)

    def neuron_for_uid(self, uid: int, _netuid: int) -> Any:
        hotkey = next((hk for hk, u in self._uid_map.items() if u == uid), None)
        permit = bool(hotkey is not None and self._permits.get(hotkey, False))
        stake = self._stakes.get(hotkey) if hotkey is not None else None
        return SimpleNamespace(validator_permit=permit, stake=stake)

    def tempo(self, _netuid: int) -> int:
        return 360

    def weights_rate_limit(self, _netuid: int) -> int:
        return 100

    def set_weights(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(success=self._success, error_message="boom")


def _setter(
    uid_map: dict[str, int],
    success: bool = True,
    permits: dict[str, bool] | None = None,
    stakes: dict[str, Any] | None = None,
) -> tuple[SdkWeightSetter, _FakeSubtensor]:
    config = SimpleNamespace(
        netuid=3,
        subtensor_network="ws://localhost:9944",
        validator_hotkey="5Vali",
        weight_version_key=42,
    )
    setter = SdkWeightSetter(config, keypair=object())  # type: ignore[arg-type]
    fake = _FakeSubtensor(uid_map, success=success, permits=permits, stakes=stakes)
    # Pre-inject so _ensure() skips real bittensor construction.
    setter._subtensor = fake
    setter._wallet = object()
    return setter, fake


class TestSdkWeightSetter:
    async def test_maps_hotkeys_to_uids_in_order(self) -> None:
        setter, fake = _setter({"hkA": 3, "hkB": 7})
        await setter.put_weights({"hkA": 0.9, "hkB": 0.1})
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["netuid"] == 3
        assert call["uids"] == [3, 7]
        assert call["weights"] == [0.9, 0.1]

    async def test_stamps_version_key(self) -> None:
        # The configured mechanism version must ride every set_weights call so
        # the chain groups our weights by version.
        setter, fake = _setter({"hkA": 3})
        await setter.put_weights({"hkA": 1.0})
        assert fake.calls[0]["version_key"] == 42

    async def test_has_validator_permit_true(self) -> None:
        setter, _ = _setter({"5Vali": 1}, permits={"5Vali": True})
        assert await setter.has_validator_permit("5Vali", 3) is True

    async def test_has_validator_permit_false(self) -> None:
        setter, _ = _setter({"5Vali": 1}, permits={"5Vali": False})
        assert await setter.has_validator_permit("5Vali", 3) is False

    async def test_has_validator_permit_unregistered_is_none(self) -> None:
        setter, _ = _setter({})  # 5Vali not registered
        assert await setter.has_validator_permit("5Vali", 3) is None

    async def test_skips_unregistered_hotkeys(self) -> None:
        setter, fake = _setter({"hkA": 3})  # hkB not registered
        await setter.put_weights({"hkA": 1.0, "hkB": 0.5})
        assert fake.calls[0]["uids"] == [3]
        assert fake.calls[0]["weights"] == [1.0]

    async def test_no_resolvable_uids_skips_set_weights(self) -> None:
        setter, fake = _setter({})  # nothing registered
        await setter.put_weights({"hkA": 1.0})
        assert fake.calls == []

    async def test_empty_weights_is_noop(self) -> None:
        setter, fake = _setter({"hkA": 3})
        await setter.put_weights({})
        assert fake.calls == []

    async def test_failed_submission_raises(self) -> None:
        setter, _ = _setter({"hkA": 3}, success=False)
        with pytest.raises(WeightSubmissionError):
            await setter.put_weights({"hkA": 1.0})

    async def test_get_stake_tao_plain_number(self) -> None:
        setter, _ = _setter({"5Vali": 1}, stakes={"5Vali": 123.5})
        assert await setter.get_stake_tao("5Vali", 3) == 123.5

    async def test_get_stake_tao_unwraps_balance(self) -> None:
        # bittensor's Balance exposes ``.tao``; the setter must unwrap it.
        setter, _ = _setter({"5Vali": 1}, stakes={"5Vali": SimpleNamespace(tao=42.0)})
        assert await setter.get_stake_tao("5Vali", 3) == 42.0

    async def test_get_stake_tao_unregistered_is_none(self) -> None:
        setter, _ = _setter({})
        assert await setter.get_stake_tao("5Vali", 3) is None

    async def test_get_tempo_and_rate_limit(self) -> None:
        setter, _ = _setter({})
        assert await setter.get_tempo(3) == 360
        assert await setter.get_weights_rate_limit(3) == 100
