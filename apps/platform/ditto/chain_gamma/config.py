"""The chain gamma master switch, default off.

``DITTO_CHAIN_GAMMA_ENABLED`` gates every future route, worker and chain read.
This module only parses it; nothing in the API server reads it yet, and it is
deliberately not part of ``check_config`` until a route depends on it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from ditto.api_models.chain_gamma import check_ss58

ENV_ENABLED = "DITTO_CHAIN_GAMMA_ENABLED"
ENV_NETUID = "DITTO_CHAIN_GAMMA_NETUID"
ENV_PUBLISHER_HOTKEYS = "DITTO_CHAIN_GAMMA_PUBLISHER_HOTKEYS"
DEFAULT_NETUID = 118

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}


class ChainGammaConfigError(ValueError):
    """The chain gamma environment is malformed."""


@dataclass(frozen=True)
class ChainGammaConfig:
    """What a chain gamma service needs to know before it serves anything."""

    enabled: bool = False
    netuid: int = DEFAULT_NETUID
    publisher_hotkeys: frozenset[str] = frozenset()
    """Every service hotkey published under on ``netuid``, old ones included.
    Public ss58 addresses only: verifying and crediting need no private key."""


def parse_chain_gamma_from_env(
    env: Mapping[str, str] | None = None,
) -> ChainGammaConfig:
    """Read the switch; disabled unless explicitly enabled and fully configured."""
    source = os.environ if env is None else env
    raw = source.get(ENV_ENABLED, "").strip().lower()
    if raw in _FALSY:
        return ChainGammaConfig()
    if raw not in _TRUTHY:
        raise ChainGammaConfigError(f"{ENV_ENABLED} must be a boolean")
    try:
        netuid = int(source.get(ENV_NETUID, str(DEFAULT_NETUID)))
    except ValueError:
        raise ChainGammaConfigError(f"{ENV_NETUID} must be an integer") from None
    if not 0 <= netuid <= 65535:
        raise ChainGammaConfigError(f"{ENV_NETUID} must fit a u16")
    hotkeys = frozenset(
        part.strip()
        for part in source.get(ENV_PUBLISHER_HOTKEYS, "").split(",")
        if part.strip()
    )
    if not hotkeys:
        raise ChainGammaConfigError(
            f"{ENV_PUBLISHER_HOTKEYS} is required when {ENV_ENABLED} is on"
        )
    for hotkey in hotkeys:
        try:
            check_ss58(hotkey)
        except ValueError:
            raise ChainGammaConfigError(
                f"{ENV_PUBLISHER_HOTKEYS} holds a value that is not ss58"
            ) from None
    return ChainGammaConfig(enabled=True, netuid=netuid, publisher_hotkeys=hotkeys)
