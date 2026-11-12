"""Ditto-side authorization and tenant aliasing.

Ditto owns every per-user adapter version and decides which one a request may
use. The miner only ever sees a salted alias of the user, never Ditto's
``user_ref``, and only the exact ``(tenant, version, digest)`` Ditto authorized.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..contract import AdapterRef, AdapterWeights
from ..workload import UserSpec, Workload


def tenant_alias(user_ref: str, salt: str) -> str:
    """Salted, one-way miner-facing alias for a Ditto user (matches TENANT_RE)."""
    return "syn-" + hashlib.sha256(f"{salt}|{user_ref}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Grant:
    """One authorized adapter: the ref sent to the miner plus Ditto's own
    weights for that version (used for verification and local fallback)."""

    user_ref: str
    ref: AdapterRef
    weights: AdapterWeights


class Authorizer:
    """Per-user authorization state derived from a synthetic workload."""

    def __init__(self, workload: Workload, salt: str) -> None:
        self._salt = salt
        self._users = {u.user_ref: u for u in workload.users}
        self._weights: dict[tuple[str, int], AdapterWeights] = {}

    def alias(self, user_ref: str) -> str:
        return tenant_alias(user_ref, self._salt)

    def user(self, user_ref: str) -> UserSpec:
        return self._users[user_ref]

    def weights(self, user_ref: str, version: int) -> AdapterWeights:
        """Ditto-owned weights for ``version`` (derived from the workload seed)."""
        key = (user_ref, version)
        cached = self._weights.get(key)
        if cached is None:
            spec = self._users[user_ref].adapter(version)
            if spec is None:
                raise KeyError(f"{user_ref} does not own adapter version {version}")
            cached = self._weights[key] = spec.weights()
        return cached

    def ref(self, user_ref: str, version: int) -> AdapterRef:
        return AdapterRef(self.alias(user_ref), version, self.weights(user_ref, version).digest())

    def authorize(self, user_ref: str) -> Grant | None:
        """The grant for the user's authorized version, or ``None`` when Ditto
        authorizes no version (or does not know the user)."""
        user = self._users.get(user_ref)
        if user is None or user.authorized_version is None:
            return None
        version = user.authorized_version
        return Grant(user_ref, self.ref(user_ref, version), self.weights(user_ref, version))
