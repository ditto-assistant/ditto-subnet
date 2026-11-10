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
from ditto.db.queries.unreleased_copy import index_keys, load_context, store_items

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


async def _reset_settings(session_maker: async_sessionmaker[AsyncSession]) -> None:
    """Start from no revision (shadow), whatever data migrations seeded."""
    from sqlalchemy import delete

    from ditto.db.models import UnreleasedCopySettingsRevision

    async with session_maker() as session, session.begin():
        await session.execute(delete(UnreleasedCopySettingsRevision))


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
    await _reset_settings(session_maker)
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
    # First tick indexes the backlog; evaluation waits for a complete index.
    assert (await sentinel.tick())["indexed"] == 1
    counters = await sentinel.tick()
    assert counters["flagged"] == 1
    async with session_maker() as session:
        port = await session.get(Agent, port_id)
        assert port is not None and port.status == AgentStatus.SCORED
    # The owner's later agent names still identify it for the namespace hint,
    # and each agent's overlap is its own (an unrelated agent shares nothing).
    unrelated = uuid4()
    async with session_maker() as session, session.begin():
        for agent_id, hotkey, name, hours in (
            (uuid4(), "5Porter", "porter-renamed", 9),
            (unrelated, "5Other", "unrelated", 1),
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
    async with session_maker() as session, session.begin():
        await store_items(session, agent_id=unrelated, extracted=extract_items(BASE))
    async with session_maker() as session:
        port = await session.get(Agent, port_id)
        assert port is not None
        context = await load_context(session, port)
        assert context is not None
        assert context.owner_names == ["port-agent", "porter-renamed"]
        assert unrelated not in context.overlaps
        port_items, source_items = extract_items(PORT), extract_items(SOURCE)
        assert port_items is not None and source_items is not None
        assert set(context.overlaps[source_id]) == set(index_keys(port_items)) & set(
            index_keys(source_items)
        )

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
    assert rows["coverage"]["indexed"] == 3
    assert rows["coverage"]["flagged"] == 1
    assert rows["items"][0]["excluded"] is not None

    # The copy-court feed carries the agent's recorded signal for operators.
    feed = await client.get(
        "/api/v1/admin/copy-court/recommendations",
        params={"agent_id": str(port_id), "pending_only": "false"},
        headers=_HEADERS,
    )
    assert feed.status_code == 200, feed.text
    signal = feed.json()["unreleased_copy"]
    assert signal["recorded"]["flagged"] and signal["live"]["flagged"]
    assert signal["released_overlap"] == []


async def test_enforce_holds_only_new_uploads_for_an_operator_ruling(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import select

    from ditto.api_server.copy_court_rules import (
        HOLD_CLASS_UNKNOWN,
        CandidateIdentity,
        classify_hold,
    )
    from ditto.db.models import AthReview
    from ditto.db.queries.unreleased_copy import finalization_hold

    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    await _reset_settings(session_maker)
    refused = await client.post(
        "/api/v1/admin/unreleased-copy/settings",
        headers=_HEADERS,
        json={
            "expected_revision": 0,
            "settings": {"mode": "enforce", "threshold": 40},
            "reason": "Peyton approved 2026-11-01",
            "actor": "operator@example.com",
            "confirmation": "APPLY UNRELEASED COPY SHADOW",
        },
    )
    assert refused.status_code == 409
    applied = await client.post(
        "/api/v1/admin/unreleased-copy/settings",
        headers=_HEADERS,
        json={
            "expected_revision": 0,
            "settings": {"mode": "enforce", "threshold": 40},
            "reason": "Peyton approved 2026-11-01",
            "actor": "operator@example.com",
            "confirmation": "APPLY UNRELEASED COPY ENFORCE",
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["parent_revision"] == 0

    now = datetime.now(UTC)
    source_id, old_port, new_port, unscored = uuid4(), uuid4(), uuid4(), uuid4()
    async with session_maker() as session, session.begin():
        for agent_id, hotkey, status, created in (
            (source_id, "5Source", AgentStatus.SCORED, now - timedelta(days=2)),
            (old_port, "5OldPorter", AgentStatus.SCORED, now - timedelta(days=1)),
            (new_port, "5NewPorter", AgentStatus.SCORED, now + timedelta(seconds=5)),
            (unscored, "5Pending", AgentStatus.EVALUATING, now + timedelta(seconds=6)),
        ):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=hotkey,
                    name=hotkey.lower(),
                    version=1,
                    sha256=agent_id.hex * 2,
                    status=status,
                    created_at=created,
                )
            )
    blobs = {source_id: SOURCE, old_port: PORT, new_port: PORT, unscored: PORT}
    sentinel = UnreleasedCopySentinel(
        session_maker=session_maker,
        storage=_Storage(blobs),
        isolate_extraction=False,
    )
    assert (await sentinel.tick())["indexed"] == 4
    counters = await sentinel.tick()
    assert counters["flagged"] == 3
    assert counters["held"] == 1

    async with session_maker() as session:
        held = await session.get(Agent, new_port)
        assert held is not None and held.status == AgentStatus.ATH_PENDING_REVIEW
        # The earlier unreleased port is an equally valid source.
        assert held.duplicate_of in {source_id, old_port}
        reason = held.review_reason or ""
        assert "src/guidance/cues.rs:" in reason
        assert "app/request_kinds.py" not in reason
        candidate = CandidateIdentity(
            agent_id=str(held.agent_id),
            miner_hotkey=held.miner_hotkey,
            sha256=held.sha256,
            normalized_source_hash=None,
        )
        assert classify_hold(reason, candidate, None) == HOLD_CLASS_UNKNOWN
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == new_port)
        )
        assert review is not None and review.status == "pending"
        assert review.algorithm_provenance["trigger"] == "unreleased_copy"
        # Uploaded before enforcement: listed, never held.
        old = await session.get(Agent, old_port)
        assert old is not None and old.status == AgentStatus.SCORED
        # Not yet scored: the finalization gate holds it.
        pending = await session.get(Agent, unscored)
        assert pending is not None
        assert (await finalization_hold(session, pending)) is not None
        assert (await finalization_hold(session, old)) is None

    overview = await client.get("/api/v1/admin/copy-court/settings", headers=_HEADERS)
    assert overview.status_code == 200, overview.text
    posture = overview.json()["unreleased_copy"]
    assert posture["effective"]["mode"] == "enforce"
    outcomes = {
        row["agent_id"]: row["enforcement_outcome"] for row in posture["flagged"]
    }
    assert outcomes[str(new_port)] == "held"
    assert outcomes[str(old_port)] == "not_held:uploaded_before_enforcement"
    assert outcomes[str(unscored)] == "deferred:held_at_score_finalization"


def test_ports_of_released_python_and_same_owner_namespaces_do_not_flag() -> None:
    from ditto.api_server.unreleased_copy import refine_released, released_shingles

    prompts = [
        f"Record {w} of the ledger is {{BLOCKED}} until the owner signs it off."
        for w in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")
    ] * 1
    released_py = _tar(
        {
            "app/notes.py": 'BLOCKED = "blocked before execution"\n'
            + "NOTES = (\n"
            + "".join(f'    f"{p}"\n    "\\n"\n' for p in prompts)
            + ")\n"
            + _py_list("CUES", _CUES),
        }
    )
    inlined = [p.replace("{BLOCKED}", "blocked before execution") for p in prompts]
    port_a = _tar(
        {"src/notes.rs": _rs_list("NOTES", inlined) + _rs_list("CUES", _CUES)}
    )
    port_b = _tar(
        {
            "src/text.rs": _rs_list("NOTES", inlined) + _rs_list("CUES", _CUES),
            "Dockerfile": "FROM python:3.12\nRUN curl -L https://huggingface.co/acmelabs/m/resolve/x\n",
        }
    )
    base, base_items = _agent(released_py, "owner-base", 0, released=1)
    first, _ = _agent(port_a, "owner-first", 2)
    second, second_items = _agent(port_b, "owner-second", 3)
    # Two independent ports of the released base: nothing unreleased is shared.
    signal = evaluate(second, [base, first, second])
    signal = refine_released(
        signal, second_items, released_shingles(base_items.values())
    )
    assert not signal.flagged

    # An earlier unreleased agent sharing an Acme-named namespace is the same
    # operator under another key: listed, never flagged.
    src = _tar(
        {
            "app/request_kinds.py": _py_list("STORY_CUES", _CUES)
            + _py_list("FILED_ORDER_NOTES", _PROMPT),
            "Dockerfile": "FROM acmelabs/base:1\n",
        }
    )
    alt, _ = _agent(src, "owner-alt", 4)
    acme, acme_items = _agent(
        _tar(
            {
                "src/guidance/cues.rs": _rs_list("STORY_CUES", _CUES)
                + _rs_list("FILED_ORDER_NOTES", _PROMPT),
                "Dockerfile": "FROM acmelabs/base:1\n",
            }
        ),
        "owner-acme",
        5,
    )
    acme = replace(acme, name="acme")
    raw = evaluate(acme, [alt, acme])
    assert raw.flagged
    refined = refine_released(raw, acme_items, frozenset(), owner_names=["acme"])
    assert not refined.flagged
    assert refined.matches[0].probable_same_owner
    # The same namespace named after the *source* excuses nothing.
    assert refine_released(raw, acme_items, frozenset(), owner_names=["porter"]).flagged


def test_one_string_spelled_two_ways_normalizes_alike() -> None:
    from ditto.api_server.distinctive_content import file_items

    def texts(source: str, constants: dict[str, str] | None = None) -> set[str]:
        return {i.text for i in file_items("app/kinds.py", source.encode(), constants)}

    raw = (
        'CALENDAR = re.compile(r"\\b(block out|slot in|pencil in)\\b'
        '[^.]{0,40}\\bfor me\\b")\n'
    )
    escaped = (
        "ALMANAC = re.compile(\n"
        '    "\\\\b(block out|slot "\n'
        '    "in|pencil in)\\\\b[^.]{0,40}\\\\bfor me\\\\b"\n'
        ")\n"
    )
    assert texts(raw) == texts(escaped)
    assert "block out slot in pencil in for me" in texts(raw)

    # f-string code is code: a renamed variable must not read as prose, and a
    # bare constant still resolves.
    def line(var: str) -> str:
        return (
            f'line = f"{{index}}. {{{var}.name}} '
            f'{{json.dumps({var}.args, ensure_ascii=False)}} done"\n'
        )

    assert texts(line("receipt")) == texts(line("voucher"))
    assert not any("dumps" in t for t in texts(line("voucher")))
    constant = 'NOTE = f"Record {BLOCKED} until the owner signs off."\n'
    resolved = texts(constant, {"BLOCKED": "blocked before execution"})
    assert "record blocked before execution until the owner signs off" in resolved


def test_renamed_port_of_released_agent_from_another_owner_does_not_flag() -> None:
    """A port that renames a released agent's code and respells its regexes.

    Owner A ports the released agent (raw-string regexes, plain names) to
    escaped-string regexes and synonym names; owner B uploads A's port. The
    regex text and f-string code are released content in another spelling;
    what B shares with A beyond that is A's renames.
    """
    from ditto.api_server.unreleased_copy import refine_released, released_shingles

    words = [
        "block out|slot in|book|pencil in",
        "search your tools|search your tool registry|which of your tools",
        "for the whole app|make the app dark|switch the app over",
        "kick off|start run|trigger|launch the saved workflow",
        "in parallel|simultaneously|side by side|concurrently",
        "latest news|fresh headlines|what are people saying",
    ] * 4
    released = "".join(
        f'KIND_{i} = re.compile(r"\\b({w})\\b[^.]{{0,40}}\\bfor me {i}\\b")\n'
        f'NOTE_{i} = f"{{index}}. {{receipt.name}} {{json.dumps(receipt.args)}} '
        f'for case number {i} today"\n'
        for i, w in enumerate(words)
    )
    port = "".join(
        f'GENRE_{i} = _regexp(\n    "\\\\b({w})\\\\b[^.]{{0,40}}"\n'
        f'    "\\\\bfor me {i}\\\\b"\n)\n'
        f'MEMO_{i} = f"{{index}}. {{voucher.name}} '
        f"{{json.dumps(voucher.args, ensure_ascii=False)}} "
        f'for case number {i} today"\n'
        for i, w in enumerate(words)
    )
    base, base_items = _agent(
        _tar({"app/kinds.py": released}), "owner-base", 0, released=1
    )
    first, _ = _agent(_tar({"agentd/genres.py": port}), "owner-first", 2)
    second, second_items = _agent(_tar({"agentd/genres.py": port}), "owner-second", 3)
    signal = evaluate(second, [base, first, second], threshold=40)
    refined = refine_released(
        signal, second_items, released_shingles(base_items.values())
    )
    assert not refined.flagged
    assert all(
        sum(n for k, n in m.shared_by_kind.items() if k not in {"ident", "short"}) == 0
        for m in refined.matches
    )


def test_public_corpus_resolves_kit_constants_across_files(tmp_path) -> None:
    """A kit constant interpolated in another file is public once resolved."""
    import importlib.util
    import subprocess
    from pathlib import Path

    from ditto.api_server import distinctive_content as dc

    script = Path(__file__).parents[3] / "scripts" / "build_public_items.py"
    spec = importlib.util.spec_from_file_location("build_public_items", script)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    kit = {
        "app/notes.py": 'BLOCKED = "blocked before execution by the kit"\n',
        "app/lane.py": (
            "from app.notes import BLOCKED\n"
            'NOTE = f"Record {BLOCKED} until the owner signs it off today."\n'
        ),
    }
    for path, text in kit.items():
        target = tmp_path / "miners" / "kit" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "kit"], check=True)

    public = builder.collect(tmp_path, ("miners",), dc)
    shipped = extract_items(_tar(kit))
    assert shipped is not None
    resolved = "record blocked before execution by the kit until the owner signs it off"
    assert any(resolved in item.text for item in shipped.items.values())
    assert set(shipped.items) <= public
