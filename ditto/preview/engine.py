"""In-process SN118 preview world: metagraph overlay, time, leases, faults.

This is the Foundry analog. It never talks to finney. Chain mutations here
are god-mode: register without mnemonics, warp blocks, overlay stake/permits.
When a real localnet is attached later, ``align_from_db`` is what makes a
restored Postgres match the overlay.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_FORBIDDEN_NETWORKS = frozenset({"finney", "mainnet", "nao"})
_FORBIDDEN_ENDPOINT_MARKERS = (
    "finney",
    "opentensor.ai",
    "entrypoint.chain",
    "ws://entrypoint",
    "wss://entrypoint",
)
_HOTKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{46,50}$")


class IsolationError(RuntimeError):
    """The engine was pointed at a public Bittensor network."""


@dataclass
class Neuron:
    """One overlay metagraph row."""

    hotkey: str
    uid: int
    permit: bool = False
    stake: float = 0.0
    registered: bool = True


@dataclass
class Lease:
    """Preview-issued validator lease."""

    lease_id: str
    hotkey: str
    expires_at_block: int
    expired: bool = False


@dataclass
class InferenceGrant:
    """Preview inference grant used by allowance cheatcodes."""

    grant_id: str
    exhausted: bool = False


@dataclass
class PreviewEngine:
    """God-controller for one isolated preview.

    Args:
        network: Logical chain name. ``local`` / ``localnet`` / ``preview``.
        endpoint: Websocket or dummy endpoint. Must not look like finney.
        netuid: Localnet netuid (dev uses 3). 118 is allowed *on localnet*.
        block: Starting block number.
        tempo: Blocks per tempo.
    """

    network: str
    endpoint: str
    netuid: int = 3
    block: int = 1
    tempo: int = 360
    neurons: dict[str, Neuron] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)
    grants: dict[str, InferenceGrant] = field(default_factory=dict)
    provider_status: int | None = None
    relay_dropped: bool = False
    snapshots: dict[str, PreviewEngine] = field(default_factory=dict, repr=False)
    _next_uid: int = 0
    _next_lease: int = 0
    _next_grant: int = 0

    def __post_init__(self) -> None:
        assert_isolated(self.network, self.endpoint)

    # -- cheatcodes ---------------------------------------------------------

    def register(
        self,
        hotkey: str,
        *,
        permit: bool = False,
        stake: float = 0.0,
    ) -> Neuron:
        """God-register ``hotkey`` without a mnemonic."""
        _require_hotkey(hotkey)
        existing = self.neurons.get(hotkey)
        if existing is not None:
            existing.registered = True
            existing.permit = permit or existing.permit
            existing.stake = max(existing.stake, stake)
            return existing
        neuron = Neuron(
            hotkey=hotkey,
            uid=self._next_uid,
            permit=permit,
            stake=stake,
            registered=True,
        )
        self._next_uid += 1
        self.neurons[hotkey] = neuron
        return neuron

    def permit(self, hotkey: str, enabled: bool = True) -> Neuron:
        """Set validator permit on an overlay neuron."""
        neuron = self._require_neuron(hotkey)
        neuron.permit = enabled
        return neuron

    def warp_block(self, n: int) -> int:
        """Advance the overlay head by ``n`` blocks (Foundry ``vm.roll``)."""
        if n < 0:
            raise ValueError("warp_block requires a non-negative delta")
        self.block += n
        self._expire_due_leases()
        return self.block

    def warp_tempo(self, n: int = 1) -> int:
        """Advance ``n`` tempos."""
        if n < 0:
            raise ValueError("warp_tempo requires a non-negative delta")
        return self.warp_block(n * self.tempo)

    def issue_lease(self, hotkey: str, *, lifetime_blocks: int = 100) -> Lease:
        """Issue a preview lease against the overlay metagraph."""
        self._require_neuron(hotkey)
        self._next_lease += 1
        lease = Lease(
            lease_id=f"lease-{self._next_lease}",
            hotkey=hotkey,
            expires_at_block=self.block + lifetime_blocks,
        )
        self.leases[lease.lease_id] = lease
        return lease

    def expire_lease(self, lease_id: str | None = None) -> list[str]:
        """Expire one lease, or every live lease if ``lease_id`` is omitted."""
        expired: list[str] = []
        if lease_id is not None:
            lease = self.leases.get(lease_id)
            if lease is None:
                raise KeyError(f"unknown lease {lease_id}")
            lease.expired = True
            return [lease.lease_id]
        for lease in self.leases.values():
            if not lease.expired:
                lease.expired = True
                expired.append(lease.lease_id)
        return expired

    def issue_grant(self) -> InferenceGrant:
        """Issue a preview inference grant."""
        self._next_grant += 1
        grant = InferenceGrant(grant_id=f"grant-{self._next_grant}")
        self.grants[grant.grant_id] = grant
        return grant

    def exhaust_allowance(self, grant_id: str | None = None) -> list[str]:
        """Mark grant(s) ``inference_allowance_exhausted``."""
        if grant_id is not None:
            grant = self.grants.get(grant_id)
            if grant is None:
                raise KeyError(f"unknown grant {grant_id}")
            grant.exhausted = True
            return [grant.grant_id]
        exhausted = []
        for grant in self.grants.values():
            grant.exhausted = True
            exhausted.append(grant.grant_id)
        return exhausted

    def inject_provider(self, status: int | None) -> None:
        """Force the fault proxy to return ``status`` (429/503) or clear it."""
        if status is not None and status not in {429, 503}:
            raise ValueError("inject_provider accepts 429, 503, or null")
        self.provider_status = status

    def drop_relay(self, dropped: bool = True) -> None:
        """Make the fault proxy refuse relay connections."""
        self.relay_dropped = dropped

    def align_from_hotkeys(self, hotkeys: Iterable[str]) -> list[str]:
        """Register every distinct hotkey with a validator permit (logical fork)."""
        aligned: list[str] = []
        seen: set[str] = set()
        for raw in hotkeys:
            hotkey = str(raw).strip()
            if not hotkey or hotkey in seen:
                continue
            _require_hotkey(hotkey)
            seen.add(hotkey)
            self.register(hotkey, permit=True, stake=1.0)
            aligned.append(hotkey)
        return aligned

    def snapshot(self, name: str) -> None:
        """Named checkpoint of overlay state (Foundry ``vm.snapshot``)."""
        if not name.strip():
            raise ValueError("snapshot name is required")
        clone = copy.deepcopy(self)
        clone.snapshots = {}
        self.snapshots[name] = clone

    def revert(self, name: str) -> None:
        """Restore a named checkpoint."""
        saved = self.snapshots.get(name)
        if saved is None:
            raise KeyError(f"unknown snapshot {name}")
        restored = copy.deepcopy(saved)
        restored.snapshots = self.snapshots
        self.network = restored.network
        self.endpoint = restored.endpoint
        self.netuid = restored.netuid
        self.block = restored.block
        self.tempo = restored.tempo
        self.neurons = restored.neurons
        self.leases = restored.leases
        self.grants = restored.grants
        self.provider_status = restored.provider_status
        self.relay_dropped = restored.relay_dropped
        self._next_uid = restored._next_uid
        self._next_lease = restored._next_lease
        self._next_grant = restored._next_grant

    def state(self) -> dict[str, Any]:
        """JSON-serializable overlay."""
        return {
            "network": self.network,
            "endpoint": self.endpoint,
            "netuid": self.netuid,
            "block": self.block,
            "tempo": self.tempo,
            "provider_status": self.provider_status,
            "relay_dropped": self.relay_dropped,
            "neurons": [
                {
                    "hotkey": n.hotkey,
                    "uid": n.uid,
                    "permit": n.permit,
                    "stake": n.stake,
                    "registered": n.registered,
                }
                for n in sorted(self.neurons.values(), key=lambda item: item.uid)
            ],
            "leases": [
                {
                    "lease_id": item.lease_id,
                    "hotkey": item.hotkey,
                    "expires_at_block": item.expires_at_block,
                    "expired": item.expired,
                }
                for item in self.leases.values()
            ],
            "grants": [
                {
                    "grant_id": item.grant_id,
                    "exhausted": item.exhausted,
                }
                for item in self.grants.values()
            ],
            "snapshots": sorted(self.snapshots),
        }

    def _require_neuron(self, hotkey: str) -> Neuron:
        _require_hotkey(hotkey)
        neuron = self.neurons.get(hotkey)
        if neuron is None or not neuron.registered:
            raise KeyError(f"hotkey {hotkey} is not registered on the overlay")
        return neuron

    def _expire_due_leases(self) -> None:
        for lease in self.leases.values():
            if not lease.expired and self.block >= lease.expires_at_block:
                lease.expired = True


def assert_isolated(network: str, endpoint: str) -> None:
    """Refuse public Bittensor networks. Preview never targets finney."""
    name = (network or "").strip().lower()
    target = (endpoint or "").strip().lower()
    if name in _FORBIDDEN_NETWORKS:
        raise IsolationError(
            f"preview-control refuses public network {network!r}; "
            "use localnet / ws://127.0.0.1"
        )
    for marker in _FORBIDDEN_ENDPOINT_MARKERS:
        if marker in target:
            raise IsolationError(
                f"preview-control refuses endpoint {endpoint!r} ({marker})"
            )
    if not target:
        raise IsolationError("preview-control requires a local endpoint")


def _require_hotkey(hotkey: str) -> None:
    if not _HOTKEY_RE.match(hotkey):
        raise ValueError(f"not an SS58 hotkey: {hotkey!r}")


def hotkeys_from_mapping(rows: Iterable[Mapping[str, Any]], key: str) -> list[str]:
    """Collect distinct hotkeys from dict rows (JSON snapshot / query)."""
    found: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(key, "")).strip()
        if value and value not in seen:
            seen.add(value)
            found.append(value)
    return found
