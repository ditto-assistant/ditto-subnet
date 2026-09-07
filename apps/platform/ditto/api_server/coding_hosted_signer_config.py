"""Non-secret, default-off selection of a separately provisioned Platform signer."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ditto.api_server.errors import ApiServerConfigError


@dataclass(frozen=True, repr=False)
class HostedControlSignerConfig:
    enabled: bool = False
    seed_file: Path | None = None
    expected_hotkey: str | None = None


def check_hosted_signer_config(config: HostedControlSignerConfig) -> None:
    if type(config.enabled) is not bool:
        raise ApiServerConfigError("hosted Coding signer configuration is invalid")
    if not config.enabled:
        return
    if (
        not isinstance(config.seed_file, Path)
        or not config.seed_file.is_absolute()
        or ".." in config.seed_file.parts
        or not isinstance(config.expected_hotkey, str)
        or re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{47,48}", config.expected_hotkey) is None
    ):
        raise ApiServerConfigError("hosted Coding signer configuration is invalid")


def parse_hosted_signer_config_from_env() -> HostedControlSignerConfig:
    raw = os.environ.get("DITTO_CODING_HOSTED_CONTROL_ENABLED", "false").strip().lower()
    if raw not in {"true", "false", "1", "0"}:
        raise ApiServerConfigError("hosted Coding signer activation is invalid")
    if raw in {"false", "0"}:
        # Staged paths/identities are not read or validated while disabled.
        return HostedControlSignerConfig()
    config = HostedControlSignerConfig(
        enabled=True,
        seed_file=Path(os.environ.get("DITTO_CODING_HOSTED_SIGNER_SEED_FILE", "")),
        expected_hotkey=os.environ.get("DITTO_CODING_HOSTED_SIGNER_HOTKEY"),
    )
    check_hosted_signer_config(config)
    return config
