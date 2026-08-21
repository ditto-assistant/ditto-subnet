"""Bind a restored Platform snapshot to the overlay metagraph."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ditto.preview.engine import PreviewEngine, hotkeys_from_mapping

# Distinct miner hotkeys on a Platform snapshot. Validators and screeners
# that only exist on chain (not as agents) can be passed via extra JSON.
_AGENT_HOTKEY_SQL = (
    "SELECT DISTINCT miner_hotkey FROM agents "
    "WHERE miner_hotkey IS NOT NULL AND miner_hotkey <> '';"
)


def hotkeys_from_json(path: Path) -> list[str]:
    """Load hotkeys from a JSON list or ``{\"hotkeys\": [...]}`` / row objects."""
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return hotkeys_from_mapping(
                payload, "miner_hotkey"
            ) or hotkeys_from_mapping(payload, "hotkey")
        return [str(item).strip() for item in payload if str(item).strip()]
    if isinstance(payload, dict):
        if "hotkeys" in payload:
            seen: set[str] = set()
            unique: list[str] = []
            for item in payload["hotkeys"]:
                value = str(item).strip()
                if value and value not in seen:
                    seen.add(value)
                    unique.append(value)
            return unique
        rows = payload.get("agents") or payload.get("rows") or []
        if isinstance(rows, list):
            return hotkeys_from_mapping(rows, "miner_hotkey") or hotkeys_from_mapping(
                rows, "hotkey"
            )
    raise ValueError(f"unrecognized hotkey snapshot: {path}")


def hotkeys_from_postgres(database_url: str) -> list[str]:
    """Read distinct ``agents.miner_hotkey`` values via ``psql``."""
    if not database_url.strip():
        raise ValueError("database url is empty")
    result = subprocess.run(
        ["psql", database_url, "-tAc", _AGENT_HOTKEY_SQL],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"psql failed to read miner_hotkey values (exit {result.returncode})"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def align_engine(
    engine: PreviewEngine,
    *,
    hotkeys: Sequence[str] | None = None,
    json_path: Path | None = None,
    database_url: str | None = None,
) -> list[str]:
    """Register overlay neurons for every hotkey in the snapshot."""
    collected: list[str] = []
    if hotkeys:
        collected.extend(hotkeys)
    if json_path is not None:
        collected.extend(hotkeys_from_json(json_path))
    if database_url:
        collected.extend(hotkeys_from_postgres(database_url))
    if not collected:
        raise ValueError("align_from_db needs hotkeys, json_path, or database_url")
    return engine.align_from_hotkeys(collected)
