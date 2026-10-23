"""Load and validate the pool bundles written by ``poolgen/cmd/rerank-pools``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

POOLS_SCHEMA = "dittobench-rerank-pools-v1"


class PoolFormatError(ValueError):
    """The bundle is not a valid dittobench-rerank-pools-v1 document."""


@dataclass(frozen=True)
class Pool:
    pool_id: str
    query: str
    candidate_ids: tuple[str, ...]
    candidate_texts: tuple[str, ...]
    gold: frozenset[str]


@dataclass(frozen=True)
class PoolBundle:
    pools: tuple[Pool, ...]
    sha256: str
    bench_version: int
    seeds: tuple[int, ...]


def parse_bundle(raw: bytes, max_pool_size: int) -> PoolBundle:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PoolFormatError(f"not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != POOLS_SCHEMA:
        raise PoolFormatError(f"schema must be {POOLS_SCHEMA!r}")
    sets = doc.get("sets")
    if not isinstance(sets, list) or not sets:
        raise PoolFormatError("bundle has no sets")
    pools: list[Pool] = []
    seen_ids: set[str] = set()
    seeds: list[int] = []
    for s in sets:
        seeds.append(int(s.get("seed", 0)))
        for p in s.get("pools") or []:
            pool = _parse_pool(p, max_pool_size)
            if pool.pool_id in seen_ids:
                raise PoolFormatError(f"duplicate pool id {pool.pool_id}")
            seen_ids.add(pool.pool_id)
            pools.append(pool)
    if not pools:
        raise PoolFormatError("bundle has no pools")
    return PoolBundle(
        pools=tuple(pools),
        sha256=hashlib.sha256(raw).hexdigest(),
        bench_version=int(doc.get("bench_version", 0)),
        seeds=tuple(seeds),
    )


def load_bundle(path: str | Path, max_pool_size: int) -> PoolBundle:
    return parse_bundle(Path(path).read_bytes(), max_pool_size)


def _parse_pool(p: object, max_pool_size: int) -> Pool:
    if not isinstance(p, dict):
        raise PoolFormatError("pool must be an object")
    pool_id = p.get("pool_id")
    query = p.get("query")
    cands = p.get("candidates")
    gold = p.get("gold")
    if not isinstance(pool_id, str) or not pool_id:
        raise PoolFormatError("pool_id missing")
    if not isinstance(query, str) or not query.strip():
        raise PoolFormatError(f"pool {pool_id}: empty query")
    if not isinstance(cands, list) or not 0 < len(cands) <= max_pool_size:
        raise PoolFormatError(f"pool {pool_id}: 1..{max_pool_size} candidates")
    ids: list[str] = []
    texts: list[str] = []
    for c in cands:
        if not isinstance(c, dict):
            raise PoolFormatError(f"pool {pool_id}: candidate must be an object")
        cid, text = c.get("id"), c.get("text")
        if not isinstance(cid, str) or not isinstance(text, str):
            raise PoolFormatError(f"pool {pool_id}: candidate needs id and text")
        ids.append(cid)
        texts.append(text)
    if len(set(ids)) != len(ids):
        raise PoolFormatError(f"pool {pool_id}: duplicate candidate ids")
    if not isinstance(gold, list) or not gold:
        raise PoolFormatError(f"pool {pool_id}: no gold")
    gold_set = frozenset(gold)
    if not gold_set <= set(ids):
        raise PoolFormatError(f"pool {pool_id}: gold is not a subset of candidates")
    return Pool(pool_id, query, tuple(ids), tuple(texts), gold_set)
