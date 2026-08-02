"""Translate DittoBench seed waves into isolated native Hermes session databases."""

from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .protocol import MemoryPair, SeedRequest


def _timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class HermesStateStore:
    """Own one FTS-backed Hermes ``SessionDB`` for each benchmark user graph.

    Hermes' native ``session_search`` searches a whole state database and does
    not filter by gateway session key. Physical DB separation is therefore the
    only faithful way to satisfy DittoBench's cross-user isolation contract.
    """

    def __init__(
        self,
        root: Path,
        *,
        session_db_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._session_db_factory = session_db_factory
        self._pairs: dict[str, dict[str, MemoryPair]] = defaultdict(dict)
        self._dbs: dict[str, Any] = {}
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    @staticmethod
    def _user_key(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def _path(self, user_id: str) -> Path:
        return self.root / f"{self._user_key(user_id)}.db"

    def lock_for(self, user_id: str) -> threading.RLock:
        return self._locks[user_id]

    def _factory(self) -> Callable[..., Any]:
        if self._session_db_factory is not None:
            return self._session_db_factory
        from hermes_state import SessionDB

        return SessionDB

    def db_for(self, user_id: str) -> Any:
        with self.lock_for(user_id):
            db = self._dbs.get(user_id)
            if db is None:
                db = self._factory()(db_path=self._path(user_id))
                self._dbs[user_id] = db
            return db

    def seed(self, request: SeedRequest) -> dict[str, int]:
        """Idempotently apply a wave, then rebuild Hermes' canonical FTS store.

        Rebuilding from the accumulated pair map avoids relying on private
        pair-id columns Hermes does not have. It also makes a retried wave an
        upsert instead of duplicating transcript messages.
        """

        with self.lock_for(request.user_id):
            if request.wave == 0:
                self._pairs[request.user_id].clear()
            for pair in request.pairs:
                self._pairs[request.user_id][pair.pair_id] = pair
            self._rebuild(request.user_id)
        return {
            "pairs": len(request.pairs),
            # Hermes derives recall from raw transcript text. Subjects and links
            # are accepted for wire compatibility but do not alter its engine.
            "subjects": len(request.subjects),
            "links": len(request.links),
        }

    def _close(self, user_id: str) -> None:
        db = self._dbs.pop(user_id, None)
        if db is not None:
            close = getattr(db, "close", None)
            if callable(close):
                close()

    def _remove_db_files(self, path: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    def _rebuild(self, user_id: str) -> None:
        path = self._path(user_id)
        self._close(user_id)
        self._remove_db_files(path)
        db = self._factory()(db_path=path)
        self._dbs[user_id] = db

        sessions: dict[str, list[MemoryPair]] = defaultdict(list)
        for pair in self._pairs[user_id].values():
            external_session = pair.session_id or f"pair:{pair.pair_id}"
            sessions[external_session].append(pair)

        for external_session, pairs in sorted(sessions.items()):
            session_id = "dittobench_seed_" + hashlib.sha256(
                f"{user_id}\0{external_session}".encode("utf-8")
            ).hexdigest()[:32]
            db.create_session(session_id, "dittobench_seed")
            ordered = sorted(pairs, key=lambda pair: (pair.timestamp, pair.pair_id))
            for pair in ordered:
                ts = _timestamp(pair.timestamp)
                db.append_message(session_id, "user", pair.prompt, timestamp=ts)
                # Preserve order even when both halves share the source timestamp.
                assistant_ts = None if ts is None else ts + 0.000001
                db.append_message(session_id, "assistant", pair.response, timestamp=assistant_ts)
            end_session = getattr(db, "end_session", None)
            if callable(end_session):
                end_session(session_id, "dittobench_seed")

    def close(self) -> None:
        for user_id in list(self._dbs):
            self._close(user_id)
