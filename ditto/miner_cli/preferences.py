"""Small, human-editable miner CLI preferences."""

from __future__ import annotations

import json
import os
from pathlib import Path


def preferences_path() -> Path:
    """Return the preferences file, honoring test/operator overrides."""
    override = os.environ.get("DITTO_CLI_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "ditto" / "config.json"


def _key(*, network: str, hotkey: str) -> str:
    return f"{network}:{hotkey}"


def load_agent_name(*, network: str, hotkey: str) -> str | None:
    """Load the last successful agent name for one network and hotkey."""
    try:
        raw = json.loads(preferences_path().read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    name = raw.get("agent_names", {}).get(_key(network=network, hotkey=hotkey))
    return name if isinstance(name, str) and 1 <= len(name) <= 64 else None


def save_agent_name(*, network: str, hotkey: str, name: str) -> bool:
    """Atomically remember a successful upload name; return whether it persisted."""
    path = preferences_path()
    try:
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        names = raw.get("agent_names")
        if not isinstance(names, dict):
            names = {}
        names[_key(network=network, hotkey=hotkey)] = name
        raw["agent_names"] = names
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
        return True
    except OSError:
        return False
