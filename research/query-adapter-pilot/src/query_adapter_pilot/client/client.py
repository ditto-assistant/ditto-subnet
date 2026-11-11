"""Ditto-side pilot client: authorize, call the miner, verify, fall back.

A miner vector is used only after it passes every check: it parses, it echoes
the exact request id and ``(tenant, version, digest)`` Ditto sent, it is unit
norm within ``OUTPUT_NORM_TOLERANCE``, and, when the request is sampled for
verification, it matches Ditto's own ``reference.apply`` within the parity
bounds. Anything else falls back to a Ditto-local vector, so a broken or
dishonest miner can cost latency but never quality.

Sampling is the trade-off: with ``verify_rate < 1`` an unsampled request is
accepted on shape, echo and norm alone, so a plausible-but-wrong unit vector
is accepted for it. ``verify_rate=1.0`` (the default) verifies every request.
Sampling is keyed on a per-run client secret the miner never sees, so a miner
that knows the request id still cannot predict which requests go unverified.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from .. import reference
from ..contract import (
    OUTPUT_NORM_TOLERANCE,
    PARITY_MAX_ABS,
    PARITY_MIN_COSINE,
    ROUTE_ADAPT,
    ROUTE_PROVISION,
    AdaptRequest,
    AdaptResponse,
    ContractError,
    ErrorCode,
    F32Array,
    Outcome,
    ProvisionRequest,
    Source,
    check_unit_norm,
    make_request_id,
    parse_error,
)
from ..workload import RequestSpec, Workload
from .authz import Authorizer, Grant
from .ledger import UsageLedger
from .transport import Transport, TransportFailure

FALLBACKS: Final = ("local", "identity")
# Provisioning ships ~1 MB of weights, so it gets a floor well above the
# per-request adapt timeout.
MIN_PROVISION_TIMEOUT_S: Final = 5.0


MIN_VERIFY_SECRET_BYTES: Final = 16


def should_verify(request_id: str, verify_rate: float, *, secret: bytes) -> bool:
    """Verification sampling: ``HMAC-SHA256(secret, request_id)`` against
    ``verify_rate``. Deterministic per ``(secret, request_id)``, so a replay is
    sampled exactly like its original within a run, but unpredictable to a
    miner, which sees the request id and never the secret."""
    mac = hmac.new(secret, request_id.encode(), hashlib.sha256).digest()
    return int.from_bytes(mac[:4], "big") / 2**32 < verify_rate


@dataclass(frozen=True)
class RequestResult:
    """One processed request. ``vector`` is the vector Ditto finally used."""

    index: int
    request_id: str
    user_ref: str
    tenant_id: str
    adapter_version: int | None
    outcome: str
    source: str
    miner_code: str | None
    latency_ms: float
    miner_compute_us: int | None
    cache: str | None
    verified: bool
    reference_cos: float
    reference_max_abs: float
    vector: F32Array
    detail: str = ""

    def to_report(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "request_id": self.request_id,
            "user_ref": self.user_ref,
            "tenant_id": self.tenant_id,
            "adapter_version": self.adapter_version,
            "outcome": self.outcome,
            "source": self.source,
            "miner_code": self.miner_code,
            "latency_ms": self.latency_ms,
            "miner_compute_us": self.miner_compute_us,
            "cache": self.cache,
            "verified": self.verified,
            "reference_cos": self.reference_cos,
            "reference_max_abs": self.reference_max_abs,
        }


@dataclass
class _Attempt:
    outcome: str
    miner_code: str | None = None
    vector: F32Array | None = None
    compute_us: int | None = None
    cache: str | None = None
    verified: bool = False
    detail: str = ""


_CODE_OUTCOMES: Final = {
    ErrorCode.STALE_VERSION: Outcome.REJECTED_STALE,
    ErrorCode.MISSING_VERSION: Outcome.REJECTED_MISSING,
}


class PilotClient:
    """Ditto's side of one pilot run against one loopback miner."""

    def __init__(
        self,
        miner_url: str,
        token: str,
        run_id: str,
        *,
        salt: str,
        timeout_s: float = 0.25,
        verify_rate: float = 1.0,
        fallback: str = "local",
        verify_secret: bytes | None = None,
    ) -> None:
        """``verify_secret`` keys verification sampling. It defaults to fresh
        ``secrets.token_bytes(32)`` per client (per run); inject a fixed value
        only for deterministic tests. It is never sent or reported."""
        if fallback not in FALLBACKS:
            raise ValueError(f"fallback must be one of {FALLBACKS}, got {fallback!r}")
        if not 0.0 <= verify_rate <= 1.0:
            raise ValueError(f"verify_rate must be in [0, 1], got {verify_rate}")
        if not (math.isfinite(timeout_s) and timeout_s > 0.0):
            raise ValueError(f"timeout_s must be finite and positive, got {timeout_s}")
        self.transport = Transport(miner_url, token)
        self.miner_url = self.transport.base_url
        # Validates the run segment of every request id up front.
        make_request_id(run_id, "syn-x", 0)
        self.run_id = run_id
        self.salt = salt
        self.timeout_s = timeout_s
        self.verify_rate = verify_rate
        self.fallback = fallback
        if verify_secret is None:
            verify_secret = secrets.token_bytes(32)
        if not isinstance(verify_secret, bytes) or len(verify_secret) < MIN_VERIFY_SECRET_BYTES:
            raise ValueError(f"verify_secret must be at least {MIN_VERIFY_SECRET_BYTES} bytes")
        self._verify_secret = verify_secret
        self.ledger = UsageLedger()
        self._authorizer: Authorizer | None = None

    def should_verify(self, request_id: str) -> bool:
        """This run's keyed sampling decision for ``request_id``."""
        return should_verify(request_id, self.verify_rate, secret=self._verify_secret)

    # ─── Setup ───────────────────────────────────────────────────────────────

    def load_workload(self, workload: Workload) -> Authorizer:
        """Install Ditto's authorization state for ``workload``."""
        self._authorizer = Authorizer(workload, self.salt)
        return self._authorizer

    @property
    def authorizer(self) -> Authorizer:
        if self._authorizer is None:
            raise RuntimeError("no workload loaded; call provision() or load_workload() first")
        return self._authorizer

    def provision(self, workload: Workload) -> list[dict[str, Any]]:
        """PUT every version in each user's ``provision`` list, in order. Each
        entry records ``status`` (HTTP status) on success or ``code`` (miner
        error code, or a transport outcome) on failure."""
        authz = self.load_workload(workload)
        timeout = max(self.timeout_s, MIN_PROVISION_TIMEOUT_S)
        entries: list[dict[str, Any]] = []
        for user in workload.users:
            for version in user.provision:
                ref = authz.ref(user.user_ref, version)
                body = ProvisionRequest(ref, authz.weights(user.user_ref, version)).to_wire()
                entry: dict[str, Any] = {
                    "user_ref": user.user_ref,
                    "tenant_id": ref.tenant_id,
                    "adapter_version": version,
                }
                try:
                    res = self.transport.call("PUT", ROUTE_PROVISION, body, timeout)
                except TransportFailure as exc:
                    entry["code"] = exc.outcome
                else:
                    if 200 <= res.status < 300:
                        entry["status"] = res.status
                    else:
                        entry["code"] = parse_error(res.body)[0]
                entries.append(entry)
        return entries

    # ─── Requests ────────────────────────────────────────────────────────────

    def request_id(self, spec: RequestSpec) -> str:
        """Scoped id: per run, tenant alias and original index. A replay reuses
        its original's id."""
        original = spec.index if spec.replay_of is None else spec.replay_of
        return make_request_id(self.run_id, self.authorizer.alias(spec.user_ref), original)

    def adapt(self, spec: RequestSpec) -> RequestResult:
        start = time.perf_counter()
        authz = self.authorizer
        tenant_id = authz.alias(spec.user_ref)
        request_id = self.request_id(spec)
        query = reference.synthetic_query(spec.query_seed)
        grant = authz.authorize(spec.user_ref)

        if grant is None:
            # Ditto refuses locally: no miner call, tier-0 identity vector.
            vector = reference.identity(query)
            latency_ms = (time.perf_counter() - start) * 1e3
            self.ledger.reject(request_id, Outcome.REJECTED_UNAUTHORIZED)
            cos, max_abs = reference.parity(vector, reference.identity(query))
            return RequestResult(
                spec.index,
                request_id,
                spec.user_ref,
                tenant_id,
                None,
                Outcome.REJECTED_UNAUTHORIZED,
                Source.FALLBACK_IDENTITY,
                None,
                latency_ms,
                None,
                None,
                False,
                cos,
                max_abs,
                vector,
                "no authorized adapter version",
            )

        attempt = self._call_miner(request_id, grant, query)
        expected: F32Array | None = None
        if attempt.outcome == Outcome.OK and attempt.vector is not None:
            attempt.outcome = self.ledger.accept(request_id)
            vector = attempt.vector
            source = Source.MINER
        else:
            self.ledger.reject(request_id, attempt.outcome)
            if self.fallback == "local":
                vector = expected = reference.apply(grant.weights, query)
                source = Source.FALLBACK_LOCAL
            else:
                vector = reference.identity(query)
                source = Source.FALLBACK_IDENTITY
        latency_ms = (time.perf_counter() - start) * 1e3

        # Report-only parity of the final vector; it never feeds the decision.
        if expected is None:
            expected = reference.apply(grant.weights, query)
        cos, max_abs = reference.parity(vector, expected)
        return RequestResult(
            spec.index,
            request_id,
            spec.user_ref,
            tenant_id,
            grant.ref.adapter_version,
            attempt.outcome,
            source,
            attempt.miner_code,
            latency_ms,
            attempt.compute_us,
            attempt.cache,
            attempt.verified,
            cos,
            max_abs,
            vector,
            attempt.detail,
        )

    def _call_miner(self, request_id: str, grant: Grant, query: F32Array) -> _Attempt:
        body = AdaptRequest(request_id, grant.ref, query).to_wire()
        try:
            res = self.transport.call("POST", ROUTE_ADAPT, body, self.timeout_s)
        except TransportFailure as exc:
            return _Attempt(exc.outcome, detail=exc.detail)

        if not 200 <= res.status < 300:
            code, message = parse_error(res.body)
            return _Attempt(
                _CODE_OUTCOMES.get(code, Outcome.MINER_ERROR),
                miner_code=code,
                detail=f"HTTP {res.status}: {message}",
            )

        try:
            resp = AdaptResponse.from_wire(res.body)
        except ContractError as exc:
            return _Attempt(Outcome.REJECTED_INVALID, detail=f"unparseable response: {exc}")
        meta: dict[str, Any] = {"compute_us": resp.compute_us, "cache": resp.cache}
        if resp.request_id != request_id:
            return _Attempt(Outcome.REJECTED_INVALID, detail="request_id echo mismatch", **meta)
        for field in ("tenant_id", "adapter_version", "adapter_digest"):
            if getattr(resp.ref, field) != getattr(grant.ref, field):
                return _Attempt(Outcome.REJECTED_INVALID, detail=f"{field} echo mismatch", **meta)
        try:
            check_unit_norm(resp.vector, OUTPUT_NORM_TOLERANCE, "vector")
        except ContractError as exc:
            return _Attempt(Outcome.REJECTED_INVALID, detail=exc.message, **meta)

        if not self.should_verify(request_id):
            return _Attempt(Outcome.OK, vector=resp.vector, **meta)
        cos, max_abs = reference.parity(resp.vector, reference.apply(grant.weights, query))
        if not (cos >= PARITY_MIN_COSINE and max_abs <= PARITY_MAX_ABS and np.isfinite(cos)):
            return _Attempt(
                Outcome.REJECTED_INCORRECT,
                verified=True,
                detail=f"parity cos={cos:.9f} max_abs={max_abs:.3e}",
                **meta,
            )
        return _Attempt(Outcome.OK, vector=resp.vector, verified=True, **meta)
