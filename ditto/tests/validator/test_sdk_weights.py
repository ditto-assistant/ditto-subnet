"""Unit tests for the bittensor-SDK weight fallback (localnet path)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ditto.validator.errors import WeightSubmissionError
from ditto.validator.sdk_weights import SdkWeightSetter


class _FakeSubtensor:
    """Records set_weights calls; maps a fixed set of hotkeys to UIDs."""

    def __init__(self, uid_map: dict[str, int], success: bool = True) -> None:
        self._uid_map = uid_map
        self._success = success
        self.calls: list[dict[str, Any]] = []

    def get_uid_for_hotkey_on_subnet(self, hotkey: str, _netuid: int) -> int | None:
        return self._uid_map.get(hotkey)

    def set_weights(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(success=self._success, error_message="boom")


def _setter(
    uid_map: dict[str, int], success: bool = True
) -> tuple[SdkWeightSetter, _FakeSubtensor]:
    config = SimpleNamespace(
        netuid=3, subtensor_network="ws://localhost:9944", validator_hotkey="5Vali"
    )
    setter = SdkWeightSetter(config, keypair=object())  # type: ignore[arg-type]
    fake = _FakeSubtensor(uid_map, success=success)
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
