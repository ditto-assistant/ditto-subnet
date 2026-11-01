"""Unreleased-copy signal: cross-language detection, exclusions, shadow loop, reads."""

from __future__ import annotations

import io
import tarfile
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.distinctive_content import extract_items
from ditto.api_server.unreleased_copy import IndexedAgent, attach_evidence, evaluate
from ditto.api_server.unreleased_copy_sentinel import UnreleasedCopySentinel
from ditto.db.models import Agent, AgentStatus
from ditto.db.queries.unreleased_copy import index_keys, store_items

_T0 = datetime(2026, 10, 29, 12, tzinfo=UTC)
_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}

# Content no public corpus has. The cue phrases are what a port keeps verbatim.
_CUES = [f"zephyr quandary {w} marmalade" for w in "abcdefghijklmnopqrstuvwxyz"]
_PROMPT = [
    f"When the ledger owner {w} asks about the filed order, quote the receipt first."
    for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")
]
_BASE = [
    f"Released base instruction number {i} keeps the answer grounded."
    for i in range(30)
]


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _py_list(name: str, values: list[str]) -> str:
    return f"{name} = (\n" + "".join(f'    "{v}",\n' for v in values) + ")\n"


def _rs_list(name: str, values: list[str]) -> str:
    body = "".join(f'    "{v}",\n' for v in values)
    return f"pub const {name}: &[&str] = &[\n{body}];\n"


BASE = _tar({"app/base.py": _py_list("BASE_NOTES", _BASE)})
SOURCE = _tar(
    {
        "app/base.py": _py_list("BASE_NOTES", _BASE),
        "app/request_kinds.py": _py_list("STORY_CUES", _CUES)
        + _py_list("FILED_ORDER_NOTES", _PROMPT),
    }
)
PORT = _tar(
    {
        "src/guidance/cues.rs": "// ported\n"
        + _rs_list("STORY_CUES", _CUES)
        + _rs_list("FILED_ORDER_NOTES", _PROMPT),
    }
)
FORK = _tar(
    {
        "app/base.py": _py_list("BASE_NOTES", _BASE),
        "app/own.py": 'NOTE = "My own note on weekend travel plans."\n',
    }
)


def _agent(tar: bytes, owner: str, hours: int, *, released: int | None = None):
    extracted = extract_items(tar)
    assert extracted is not None
    return (
        IndexedAgent(
            agent_id=uuid4(),
            owner=owner,
            uploaded_at=_T0 + timedelta(hours=hours),
            released_at=None if released is None else _T0 + timedelta(hours=released),
            keys=frozenset(index_keys(extracted)),
            name=owner,
        ),
        extracted.items,
    )


def test_cross_language_port_of_unreleased_agent_is_flagged_with_evidence() -> None:
    base, _ = _agent(BASE, "owner-base", 0, released=1)
    source, source_items = _agent(SOURCE, "owner-source", 2)
    fork, _ = _agent(FORK, "owner-fork", 3)
    port, port_items = _agent(PORT, "owner-port", 4)
    later_own, _ = _agent(SOURCE, "owner-source", 5)
    everyone = [base, source, fork, port, later_own]

    signal = evaluate(port, everyone)
    assert signal.flagged
    top = signal.matches[0]
    assert top.matched_agent_id == source.agent_id
    assert top.score >= signal.threshold
    evidenced = attach_evidence(signal, port_items, {source.agent_id: source_items})
    paths = {(i.candidate_path, i.matched_path) for i in evidenced.matches[0].items}
    assert ("src/guidance/cues.rs", "app/request_kinds.py") in paths
    assert all(
        i.candidate_line > 0 and i.matched_line > 0 for i in evidenced.matches[0].items
    )

    # A fork of the released base shares only released content.
    assert not evaluate(fork, everyone).flagged
    # The same owner re-using its own work is never a match.
    assert all(
        m.matched_agent_id != source.agent_id
        for m in evaluate(later_own, everyone).matches
    )
    # Once the source is released before the port's upload, the port is lawful.
    released_source = replace(
        source, released_at=port.uploaded_at - timedelta(minutes=1)
    )
    assert not evaluate(port, [base, released_source, fork, port]).flagged


class _Storage:
    def __init__(self, blobs: dict[UUID, bytes]) -> None:
        self.blobs = blobs

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        assert max_bytes > 0
        return self.blobs[UUID(key.split("/", 1)[0])]


async def test_shadow_sentinel_records_signal_and_backroom_reads_it(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    source_id, port_id = uuid4(), uuid4()
    async with session_maker() as session, session.begin():
        for agent_id, hotkey, name, hours in (
            (source_id, "5Source", "source-agent", 0),
            (port_id, "5Porter", "port-agent", 3),
        ):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=hotkey,
                    name=name,
                    version=1,
                    sha256=agent_id.hex * 2,
                    status=AgentStatus.SCORED,
                    created_at=_T0 + timedelta(hours=hours),
                )
            )
    # The source was indexed at upload; the port predates the index and is
    # backfilled from storage by the sentinel.
    async with session_maker() as session, session.begin():
        await store_items(session, agent_id=source_id, extracted=extract_items(SOURCE))
    sentinel = UnreleasedCopySentinel(
        session_maker=session_maker,
        storage=_Storage({source_id: SOURCE, port_id: PORT}),
        isolate_extraction=False,
    )
    counters = await sentinel.tick()
    assert counters["indexed"] == 1
    assert counters["flagged"] == 1
    async with session_maker() as session:
        port = await session.get(Agent, port_id)
        assert port is not None and port.status == AgentStatus.SCORED

    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    app.state.unreleased_copy_sentinel = sentinel

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    response = await client.get(
        f"/api/v1/admin/unreleased-copy/agents/{port_id}", headers=_HEADERS
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "shadow"
    assert body["agent_status"] == "scored"
    recorded = body["recorded"]
    assert recorded["flagged"] and recorded["evidence_status"] == "attached"
    match = recorded["matches"][0]
    assert match["agent_id"] == str(source_id)
    assert match["release_state"] == "unreleased"
    item = match["items"][0]
    assert item["candidate"].startswith("src/guidance/cues.rs:")
    assert item["matched"].startswith("app/request_kinds.py:")
    assert body["runtime"]["mode"] == "shadow"

    listing = await client.get(
        "/api/v1/admin/unreleased-copy/signals", headers=_HEADERS
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert [r["agent_id"] for r in rows["items"]] == [str(port_id)]
    assert rows["items"][0]["top_matched_name"] == "source-agent"
    assert rows["coverage"]["indexed"] == 2
    assert rows["coverage"]["flagged"] == 1
