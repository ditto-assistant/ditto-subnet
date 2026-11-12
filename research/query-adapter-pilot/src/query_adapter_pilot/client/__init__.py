"""Ditto-side pilot client for ``query-adapter/v1`` (synthetic-only).

Ditto owns the frozen embedder, every per-user adapter version, and
authorization; the miner only returns the adjusted normalized vector, which
Ditto validates (and, when sampled, verifies) before use. Usage is recorded
for analysis only: there are no payouts.
"""

from .authz import Authorizer, Grant, tenant_alias
from .client import PilotClient, RequestResult, should_verify
from .ledger import UsageLedger
from .transport import HttpResult, Transport, TransportFailure, validate_miner_url

__all__ = [
    "Authorizer",
    "Grant",
    "HttpResult",
    "PilotClient",
    "RequestResult",
    "Transport",
    "TransportFailure",
    "UsageLedger",
    "should_verify",
    "tenant_alias",
    "validate_miner_url",
]
