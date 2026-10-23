"""Unit tests for the generator competition service decisions."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import ditto.api_server.generator_competition as service
from ditto.api_server.generator_competition import (
    DEFAULT_SETTINGS,
    PackRejected,
    inspect_pack,
    settings_from_row,
)

_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def _doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "contract": "dittobench-challenge-pack/v1",
        "miner_hotkey": _HOTKEY,
        "generator": {"name": "gen", "version": "2"},
        "cases": [{} for _ in range(8)],
    }
    doc.update(overrides)
    return doc


def _encode(doc: Any) -> tuple[str, str]:
    raw = json.dumps(doc).encode()
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def test_inspect_pack_accepts_a_v1_header() -> None:
    b64, sha = _encode(_doc())
    header = inspect_pack(pack_base64=b64, pack_sha256=sha, hotkey=_HOTKEY)
    assert (header.case_count, header.generator_name, header.generator_version) == (
        8,
        "gen",
        "2",
    )


@pytest.mark.parametrize(
    "doc",
    [
        _doc(contract="v0"),
        _doc(miner_hotkey="someone-else"),
        _doc(cases=[{}] * 7),
        _doc(cases=[{}] * 65),
        _doc(generator="gen"),
        _doc(generator={"name": "", "version": "1"}),
        [1, 2, 3],
    ],
)
def test_inspect_pack_rejects(doc: Any) -> None:
    b64, sha = _encode(doc)
    with pytest.raises(PackRejected):
        inspect_pack(pack_base64=b64, pack_sha256=sha, hotkey=_HOTKEY)


def test_inspect_pack_rejects_bad_bytes() -> None:
    b64, _ = _encode(_doc())
    with pytest.raises(PackRejected):
        inspect_pack(pack_base64=b64, pack_sha256="0" * 64, hotkey=_HOTKEY)
    with pytest.raises(PackRejected):
        inspect_pack(pack_base64="!!!", pack_sha256="0" * 64, hotkey=_HOTKEY)


def test_corrupt_settings_close_the_lane() -> None:
    row = SimpleNamespace(revision=4, settings={"mode": "shadow", "panel_size": 999})
    settings, valid = settings_from_row(row)  # type: ignore[arg-type]
    assert settings == DEFAULT_SETTINGS and settings.mode == "off" and not valid


@dataclass
class _Row:
    agent_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None
    composite: float
    eligible: bool = True
    first_seen: datetime = datetime(2026, 1, 1, tzinfo=UTC)


async def test_select_panel_takes_top_owners_and_marks_champion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _Row(uuid4(), "hk-low", "ck-low", 0.40),
        _Row(uuid4(), "hk-top", "ck-top", 0.90),
        _Row(uuid4(), "hk-mid", None, 0.70),
        _Row(uuid4(), "hk-held", "ck-held", 0.99, eligible=False),
    ]

    async def _ledger(_session: object, **_kwargs: object) -> list[_Row]:
        return rows

    monkeypatch.setattr(service, "list_eligible_ledger", _ledger)
    panel = await service.select_panel(object(), size=2)  # type: ignore[arg-type]
    assert panel == [
        {
            "agent_id": str(rows[1].agent_id),
            "owner_key": "coldkey:ck-top",
            "champion": True,
        },
        {
            "agent_id": str(rows[2].agent_id),
            "owner_key": "hotkey:hk-mid",
            "champion": False,
        },
    ]
