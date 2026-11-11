"""Synthetic pilot workload: the input to ``python -m query_adapter_pilot run``.

A workload describes Ditto's side of the world: synthetic users, the adapter
versions Ditto owns for each (weights derived from seeds), which of those it
provisions to the miner and in what order, which version Ditto's authorization
currently allows, and a request sequence. Divergence between ``provision`` and
``authorized_version`` is how negative cases are expressed:

* stale:   provision [1, 2], authorized_version 1  -> miner STALE_VERSION
* missing: provision [1],    authorized_version 2  -> miner MISSING_VERSION
* unauthorized: authorized_version null            -> Ditto refuses locally

``{"replay_of": i}`` re-sends request ``i`` with the same request id, which is
how duplicate usage suppression is exercised.

Schema (unknown fields ignored)::

    {"contract": "query-adapter/v1", "kind": "synthetic-workload", "name": str,
     "users": [{"user_ref": "syn-...", "adapters": [{"version": int, "seed": int,
                "alpha"?: float}], "provision": [int], "authorized_version": int|null}],
     "requests": [{"user_ref": "syn-...", "query_seed": int} | {"replay_of": int}]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import (
    CONTRACT_VERSION,
    DEFAULT_ALPHA,
    MAX_ADAPTER_VERSION,
    TENANT_RE,
    AdapterWeights,
    ContractError,
    ErrorCode,
)
from .reference import synthetic_adapter

WORKLOAD_KIND = "synthetic-workload"


def _bad(message: str) -> ContractError:
    return ContractError(ErrorCode.INVALID_REQUEST, f"workload: {message}")


def _list(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad(f"{name} must be a list")
    return value


def _int(value: Any, name: str, lo: int = 0, hi: int = 2**62) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _bad(f"{name} must be an integer in [{lo}, {hi}]")
    return value


@dataclass(frozen=True)
class AdapterSpec:
    version: int
    seed: int
    alpha: float = DEFAULT_ALPHA

    def weights(self) -> AdapterWeights:
        return synthetic_adapter(self.seed, self.alpha)


@dataclass(frozen=True)
class UserSpec:
    user_ref: str
    adapters: tuple[AdapterSpec, ...]
    provision: tuple[int, ...]
    authorized_version: int | None

    def adapter(self, version: int) -> AdapterSpec | None:
        return next((a for a in self.adapters if a.version == version), None)


@dataclass(frozen=True)
class RequestSpec:
    index: int
    user_ref: str
    query_seed: int
    replay_of: int | None = None


@dataclass(frozen=True)
class Workload:
    name: str
    users: tuple[UserSpec, ...]
    requests: tuple[RequestSpec, ...]

    def user(self, user_ref: str) -> UserSpec:
        for u in self.users:
            if u.user_ref == user_ref:
                return u
        raise KeyError(user_ref)

    @classmethod
    def from_json(cls, raw: Any) -> Workload:
        if not isinstance(raw, dict):
            raise _bad("must be an object")
        if raw.get("contract") != CONTRACT_VERSION or raw.get("kind") != WORKLOAD_KIND:
            raise _bad(f"contract/kind must be {CONTRACT_VERSION!r}/{WORKLOAD_KIND!r}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise _bad("name is required")
        users: list[UserSpec] = []
        for u in _list(raw.get("users"), "users"):
            if not isinstance(u, dict):
                raise _bad("user must be an object")
            ref = u.get("user_ref")
            if not isinstance(ref, str) or not TENANT_RE.fullmatch(ref):
                raise _bad("user_ref must be synthetic (syn-...)")
            adapters = []
            for a in _list(u.get("adapters"), "adapters"):
                if not isinstance(a, dict):
                    raise _bad("adapter must be an object")
                alpha = a.get("alpha", DEFAULT_ALPHA)
                if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
                    raise _bad("alpha must be a number")
                if not 0.0 < float(alpha) <= 1.0:
                    raise _bad("alpha must be in (0, 1]")
                adapters.append(
                    AdapterSpec(
                        version=_int(a.get("version"), "version", 1, MAX_ADAPTER_VERSION),
                        seed=_int(a.get("seed"), "seed"),
                        alpha=float(alpha),
                    )
                )
            if len({a.version for a in adapters}) != len(adapters):
                raise _bad(f"{ref}: duplicate adapter version")
            owned = {a.version for a in adapters}
            provision = tuple(
                _int(v, "provision", 1, MAX_ADAPTER_VERSION)
                for v in _list(u.get("provision"), "provision")
            )
            if not set(provision) <= owned:
                raise _bad(f"{ref}: provisions a version Ditto does not own")
            av = u.get("authorized_version")
            authorized = (
                None if av is None else _int(av, "authorized_version", 1, MAX_ADAPTER_VERSION)
            )
            if authorized is not None and authorized not in owned:
                raise _bad(f"{ref}: authorized_version is not an owned adapter")
            users.append(UserSpec(ref, tuple(adapters), provision, authorized))
        if len({u.user_ref for u in users}) != len(users):
            raise _bad("duplicate user_ref")
        refs = {u.user_ref for u in users}
        requests: list[RequestSpec] = []
        for i, r in enumerate(_list(raw.get("requests"), "requests")):
            if not isinstance(r, dict):
                raise _bad("request must be an object")
            if "replay_of" in r:
                j = _int(r["replay_of"], "replay_of", 0, i - 1) if i else None
                if j is None or requests[j].replay_of is not None:
                    raise _bad(f"request {i}: replay_of must name an earlier original")
                requests.append(RequestSpec(i, requests[j].user_ref, requests[j].query_seed, j))
                continue
            ref = r.get("user_ref")
            if not isinstance(ref, str) or ref not in refs:
                raise _bad(f"request {i}: unknown user_ref")
            requests.append(RequestSpec(i, str(ref), _int(r.get("query_seed"), "query_seed")))
        return cls(name, tuple(users), tuple(requests))

    @classmethod
    def load(cls, path: str | Path) -> Workload:
        return cls.from_json(json.loads(Path(path).read_text()))
