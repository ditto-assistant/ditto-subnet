from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.backfill_reference_fingerprints as backfill
from ditto.api_server.fingerprint import reference_corpus_provenance


def _agent(agent_id: int, content: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        content_fingerprint=content,
        normalized_source_hash="legacy",
        prompt_fingerprint=None,
        status="ath_pending_review",
        duplicate_of="original",
        screening_reason="legacy reason",
    )


def test_is_current_requires_algorithm_and_corpus_identity() -> None:
    corpus = reference_corpus_provenance()["corpus_id"]
    assert backfill._is_current(_agent(1, {"v": 2, "corpus": corpus}))
    assert not backfill._is_current(_agent(1, {"v": 2}))
    assert not backfill._is_current(_agent(1, {"v": 1, "corpus": corpus}))


def test_store_updates_only_fingerprint_metadata() -> None:
    agent = _agent(1)
    backfill._store_fingerprint_metadata(
        agent,
        content={"v": 2, "corpus": "corpus", "m": []},
        normalized="nsh2:corpus:digest",
        prompt={"v": "p2", "corpus": "corpus", "m": []},
    )

    assert agent.content_fingerprint["v"] == 2
    assert agent.normalized_source_hash == "nsh2:corpus:digest"
    assert agent.prompt_fingerprint["v"] == "p2"
    assert agent.status == "ath_pending_review"
    assert agent.duplicate_of == "original"
    assert agent.screening_reason == "legacy reason"


@pytest.mark.asyncio
async def test_run_uses_bounded_batches_and_is_idempotent(monkeypatch) -> None:
    corpus = reference_corpus_provenance()["corpus_id"]
    stale_a, stale_b = _agent(1), _agent(2)
    current = _agent(3, {"v": 2, "corpus": corpus})

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Session:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def scalars(self, statement):
            assert statement._limit_clause.value == 2
            batches = ([stale_a, stale_b], [current], [])
            rows = batches[self.calls]
            self.calls += 1
            return Result(rows)

        async def commit(self):
            self.commits += 1

    class Storage:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_object(self, **_kwargs):
            return b"artifact"

    class Engine:
        async def dispose(self):
            return None

    session = Session()
    monkeypatch.setattr(backfill, "create_db_engine", Engine)
    monkeypatch.setattr(
        backfill, "create_session_maker", lambda _engine: lambda: session
    )
    monkeypatch.setattr(backfill, "parse_storage_config_from_env", lambda: object())
    monkeypatch.setattr(backfill, "create_storage_client", lambda _config: Storage())
    monkeypatch.setattr(
        backfill,
        "compute_content_fingerprint",
        lambda _data: {"v": 2, "corpus": corpus, "card": 8, "m": ["aggregate"]},
    )
    monkeypatch.setattr(
        backfill,
        "compute_normalized_source_hash",
        lambda _data: "nsh2:corpus:digest",
    )
    monkeypatch.setattr(
        backfill,
        "compute_prompt_fingerprint",
        lambda _data: {"v": "p2", "corpus": corpus, "card": 0, "m": []},
    )

    assert await backfill._run(apply=True, limit=None, batch_size=2) == 0
    assert session.calls == 3
    assert session.commits == 2
    assert backfill._is_current(stale_a)
    assert backfill._is_current(stale_b)
    assert current.normalized_source_hash == "legacy"
