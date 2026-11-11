"""Tenant/version store, bounded adapter cache, counters, and fault injection.

The store holds exactly one live adapter version per tenant as raw encoded
float32 bytes. The cache holds materialized, ready-to-apply ``AdapterWeights``
keyed by the full ``(tenant_id, version, digest)`` triple. Nothing is keyed by
version alone, so two tenants that share a version number never collide.
"""

from __future__ import annotations

import threading
import zlib
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from ..contract import (
    CONTRACT_VERSION,
    DIM,
    HIDDEN,
    AdapterRef,
    AdapterWeights,
    ContractError,
    ErrorCode,
    F32Array,
    ProvisionRequest,
)

FAULT_MODES: Final = ("none", "perturb", "nan", "bad-norm", "wrong-echo", "swap-tenant", "slow")

CacheKey = tuple[str, int, str]

_LAYOUT: Final = (
    ("W1", (DIM, HIDDEN)),
    ("b1", (HIDDEN,)),
    ("W2", (HIDDEN, DIM)),
    ("b2", (DIM,)),
)


@dataclass(frozen=True)
class StoredAdapter:
    """One live adapter version as provisioned: ref, alpha and raw f32le bytes."""

    ref: AdapterRef
    alpha: float
    raw: bytes

    @property
    def key(self) -> CacheKey:
        return (self.ref.tenant_id, self.ref.adapter_version, self.ref.adapter_digest)

    @classmethod
    def from_request(cls, req: ProvisionRequest) -> StoredAdapter:
        w = req.weights
        raw = b"".join(
            np.ascontiguousarray(arr, dtype="<f4").tobytes() for arr in (w.W1, w.b1, w.W2, w.b2)
        )
        return cls(ref=req.ref, alpha=w.alpha, raw=raw)

    def materialize(self) -> AdapterWeights:
        """Decode the stored bytes and re-verify the provisioned digest."""
        arrays: dict[str, F32Array] = {}
        offset = 0
        for name, shape in _LAYOUT:
            count = int(np.prod(shape))
            arr = (
                np.frombuffer(self.raw, dtype="<f4", count=count, offset=offset)
                .astype(np.float32)
                .reshape(shape)
            )
            arr.flags.writeable = False
            arrays[name] = arr
            offset += 4 * count
        if offset != len(self.raw):
            raise ContractError(ErrorCode.BAD_SHAPE, "stored adapter has the wrong byte length")
        weights = AdapterWeights(
            W1=arrays["W1"], b1=arrays["b1"], W2=arrays["W2"], b2=arrays["b2"], alpha=self.alpha
        )
        if weights.digest() != self.ref.adapter_digest:
            raise ContractError(
                ErrorCode.DIGEST_MISMATCH, "stored adapter failed digest re-verification"
            )
        return weights


class AdapterLRU:
    """Thread-safe bounded LRU of materialized adapters."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("cache capacity must be >= 1")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[CacheKey, AdapterWeights] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: CacheKey) -> AdapterWeights | None:
        with self._lock:
            weights = self._entries.get(key)
            if weights is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return weights

    def put(self, key: CacheKey, weights: AdapterWeights) -> None:
        with self._lock:
            self._entries[key] = weights
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
                self.evictions += 1

    def discard_tenant(self, tenant_id: str) -> None:
        """Drop every cached version of one tenant (used when versions retire)."""
        with self._lock:
            for key in [k for k in self._entries if k[0] == tenant_id]:
                del self._entries[key]

    def cached_keys(self) -> list[CacheKey]:
        with self._lock:
            return list(self._entries)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }


class MinerState:
    """All mutable miner state behind one lock (the cache has its own as well)."""

    def __init__(self, cache_capacity: int, max_tenants: int, fault: str) -> None:
        if max_tenants < 1:
            raise ValueError("max_tenants must be >= 1")
        self.max_tenants = max_tenants
        self.fault = fault
        self.cache = AdapterLRU(cache_capacity)
        self._lock = threading.Lock()
        self._live: dict[str, StoredAdapter] = {}
        self._adapt_ok = 0
        self._errors: Counter[str] = Counter()

    # ─── Provisioning ────────────────────────────────────────────────────

    def provision(self, req: ProvisionRequest) -> str:
        """Apply one provision. Returns ``"provisioned"`` or ``"unchanged"``."""
        ref = req.ref
        with self._lock:
            current = self._live.get(ref.tenant_id)
            if current is None:
                if len(self._live) >= self.max_tenants:
                    raise ContractError(
                        ErrorCode.CAPACITY, f"tenant store is full ({self.max_tenants} tenants)"
                    )
            else:
                newest = current.ref.adapter_version
                if ref.adapter_version < newest:
                    raise ContractError(
                        ErrorCode.STALE_VERSION,
                        f"version {ref.adapter_version} is older than live version {newest}",
                    )
                if ref.adapter_version == newest:
                    if ref.adapter_digest == current.ref.adapter_digest:
                        return "unchanged"
                    raise ContractError(
                        ErrorCode.DIGEST_MISMATCH,
                        f"version {newest} is already provisioned with a different digest",
                    )
                # A newer version retires every older one: drop it from the
                # store and the cache. Versions below the live one stay retired
                # because any lower version now resolves to STALE_VERSION.
                self.cache.discard_tenant(ref.tenant_id)
            self._live[ref.tenant_id] = StoredAdapter.from_request(req)
            return "provisioned"

    # ─── Resolution ──────────────────────────────────────────────────────

    def _lookup(self, ref: AdapterRef) -> StoredAdapter:
        stored = self._live.get(ref.tenant_id)
        if stored is None or ref.adapter_version > stored.ref.adapter_version:
            raise ContractError(
                ErrorCode.MISSING_VERSION,
                f"{ref.tenant_id} version {ref.adapter_version} is not provisioned",
            )
        if ref.adapter_version < stored.ref.adapter_version:
            raise ContractError(
                ErrorCode.STALE_VERSION,
                f"{ref.tenant_id} version {ref.adapter_version} was retired by version "
                f"{stored.ref.adapter_version}",
            )
        if ref.adapter_digest != stored.ref.adapter_digest:
            raise ContractError(
                ErrorCode.DIGEST_MISMATCH,
                f"digest does not match provisioned {ref.tenant_id} version {ref.adapter_version}",
            )
        return stored

    def resolve(self, ref: AdapterRef) -> tuple[AdapterWeights, str]:
        """Return the ready-to-apply adapter for an exact ref and ``"hit"``/``"miss"``."""
        with self._lock:
            stored = self._lookup(ref)
            cached = self.cache.get(stored.key)
        if cached is not None:
            return cached, "hit"
        weights = stored.materialize()
        with self._lock:
            # Insert only if this exact version is still live; a concurrent
            # provision may have retired it while we were decoding.
            if self._live.get(ref.tenant_id) is stored:
                self.cache.put(stored.key, weights)
        return weights, "miss"

    def other_live(self, tenant_id: str) -> AdapterWeights | None:
        """Another tenant's live adapter (deterministic choice), for the
        ``swap-tenant`` fault only. Bypasses the cache so stats stay honest."""
        with self._lock:
            others = sorted(t for t in self._live if t != tenant_id)
            stored = self._live[others[0]] if others else None
        return None if stored is None else stored.materialize()

    # ─── Counters ────────────────────────────────────────────────────────

    def record_ok(self) -> None:
        with self._lock:
            self._adapt_ok += 1

    def record_error(self, code: str) -> None:
        with self._lock:
            self._errors[code] += 1

    def health(self) -> dict[str, Any]:
        with self._lock:
            tenants = len(self._live)
            adapt_ok = self._adapt_ok
            errors = dict(sorted(self._errors.items()))
        return {
            "contract": CONTRACT_VERSION,
            "status": "ok",
            "tenants": tenants,
            "adapters": tenants,  # exactly one live version per tenant
            "cache": self.cache.snapshot(),
            "requests": {"adapt_ok": adapt_ok, "errors": errors},
            "fault": self.fault,
        }


# ─── Fault injection (test-only) ─────────────────────────────────────────────

PERTURB_NORM: Final = 1e-2


def perturb(vec: F32Array, fault_seed: int, request_id: str) -> F32Array:
    """Add seeded noise of L2 norm ``PERTURB_NORM`` and renormalize: the result
    is unit norm but fails the parity bound."""
    seq = np.random.SeedSequence([fault_seed, zlib.crc32(request_id.encode("ascii"))])
    noise = np.random.default_rng(seq).standard_normal(DIM)
    noise *= PERTURB_NORM / np.linalg.norm(noise)
    out = vec.astype(np.float64) + noise
    return np.asarray(out / np.linalg.norm(out), dtype=np.float32)


def wrong_echo(request_id: str) -> str:
    """A different but well-formed ``qa1.<run>.<tenant>.<seq>`` request id."""
    prefix, _, seq = request_id.rpartition(".")
    return f"{prefix}.{(int(seq) + 1) % 10**9}"
