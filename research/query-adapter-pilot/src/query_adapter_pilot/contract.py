"""Versioned wire contract for the per-user Personal Query Adapter pilot.

Ditto owns the frozen embedding model, every per-user adapter version, and
authorization. A miner hosts many small residual MLP adapters on shared compute
and answers one question: given an authorized 768-d query vector and an exact
``(tenant, adapter_version, adapter_digest)``, return the adjusted, normalized
vector. Ditto keeps HNSW search and context assembly.

This contract is synthetic-only. Tenant aliases must start with ``syn-``, the
miner binds loopback only, and the client refuses non-loopback miners. Neither
embeddings nor per-user weights are safe on an untrusted host, so nothing here
is a production routing path.

Wire rules shared by every slice:

* Vectors and weights travel as base64 of little-endian float32, C order.
* Unknown JSON fields are ignored (forward compatibility); known fields are
  validated strictly.
* Every request carries ``contract == CONTRACT_VERSION``; a mismatch is
  ``CONTRACT_MISMATCH``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt

CONTRACT_VERSION: Final = "query-adapter/v1"

# Architecture constants, loaded from hippocampus/train/query_adapter.py
# (Seed Memories v4 Personal Query Adapter): f(q) = normalize(q + a * MLP(q)),
# MLP = Linear(768 -> 128), GELU (tanh approximation), Linear(128 -> 768).
DIM: Final = 768
HIDDEN: Final = 128
DEFAULT_ALPHA: Final = 0.3
ACTIVATION: Final = "gelu_tanh"
NORMALIZATION: Final = "l2_eps_1e-9"
NORM_EPS: Final = 1e-9

# A query must already be an L2-normalized embedding (text-embedding-005
# output). This catches vectors from the wrong model or a broken encoder.
QUERY_NORM_TOLERANCE: Final = 1e-3
# Ditto accepts a miner vector only if it is unit norm within this tolerance.
OUTPUT_NORM_TOLERANCE: Final = 1e-4
# Correct-result parity against the Ditto-side reference computation.
PARITY_MIN_COSINE: Final = 1.0 - 1e-6
PARITY_MAX_ABS: Final = 1e-5

# Request size bounds (bytes of the HTTP body).
MAX_ADAPT_BODY_BYTES: Final = 64 * 1024
MAX_PROVISION_BODY_BYTES: Final = 2 * 1024 * 1024

ENCODING: Final = "f32le-b64"

# Routes.
ROUTE_HEALTH: Final = "/v1/health"
ROUTE_PROVISION: Final = "/v1/adapters"  # PUT
ROUTE_ADAPT: Final = "/v1/adapt"  # POST
TOKEN_HEADER: Final = "X-Pilot-Token"

TENANT_RE: Final = re.compile(r"^syn-[a-z0-9-]{1,48}$")
REQUEST_ID_RE: Final = re.compile(r"^qa1\.[a-z0-9-]{1,32}\.[a-z0-9-]{1,53}\.[0-9]{1,9}$")
DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
MAX_ADAPTER_VERSION: Final = 2**31 - 1


class ErrorCode:
    """Miner error codes and their HTTP status."""

    INVALID_REQUEST: Final = "INVALID_REQUEST"  # 400 malformed JSON / field
    CONTRACT_MISMATCH: Final = "CONTRACT_MISMATCH"  # 400
    BAD_SHAPE: Final = "BAD_SHAPE"  # 400 wrong dimension / byte length
    NON_FINITE: Final = "NON_FINITE"  # 400 NaN or Inf
    BAD_NORM: Final = "BAD_NORM"  # 400 query not unit norm
    UNAUTHORIZED: Final = "UNAUTHORIZED"  # 401 missing / wrong pilot token
    MISSING_VERSION: Final = "MISSING_VERSION"  # 404 tenant/version never provisioned
    STALE_VERSION: Final = "STALE_VERSION"  # 409 version retired by a newer one
    DIGEST_MISMATCH: Final = "DIGEST_MISMATCH"  # 409 digest != provisioned digest
    TOO_LARGE: Final = "TOO_LARGE"  # 413
    CAPACITY: Final = "CAPACITY"  # 507 tenant store full


HTTP_STATUS: Final[dict[str, int]] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.CONTRACT_MISMATCH: 400,
    ErrorCode.BAD_SHAPE: 400,
    ErrorCode.NON_FINITE: 400,
    ErrorCode.BAD_NORM: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.MISSING_VERSION: 404,
    ErrorCode.STALE_VERSION: 409,
    ErrorCode.DIGEST_MISMATCH: 409,
    ErrorCode.TOO_LARGE: 413,
    ErrorCode.CAPACITY: 507,
}


class Outcome:
    """Ditto-side per-request outcome recorded in the pilot report."""

    OK: Final = "ok"  # miner vector accepted
    REJECTED_INVALID: Final = "rejected_invalid"  # shape/finite/norm/echo failed
    REJECTED_INCORRECT: Final = "rejected_incorrect"  # parity check failed
    REJECTED_STALE: Final = "rejected_stale"  # miner said STALE_VERSION
    REJECTED_MISSING: Final = "rejected_missing"  # miner said MISSING_VERSION
    REJECTED_UNAUTHORIZED: Final = "rejected_unauthorized"  # Ditto authz refused
    MINER_ERROR: Final = "miner_error"  # any other miner error code
    UNAVAILABLE: Final = "unavailable"  # connection refused / reset
    TIMEOUT: Final = "timeout"
    DUPLICATE: Final = "duplicate"  # replay of an already-settled request id


OUTCOMES: Final = frozenset(
    v for k, v in vars(Outcome).items() if not k.startswith("_") and isinstance(v, str)
)


class Source:
    """Where the vector Ditto finally used came from."""

    MINER: Final = "miner"
    FALLBACK_LOCAL: Final = "fallback_local"  # Ditto recomputed with its own weights
    # Tier 0: the raw normalized query. Also the vector Ditto uses when it
    # refuses an unauthorized user before any miner call.
    FALLBACK_IDENTITY: Final = "fallback_identity"


class ContractError(ValueError):
    """A known field failed validation. ``code`` is an ``ErrorCode``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


F32Array = npt.NDArray[np.float32]


# ─── Encoding ────────────────────────────────────────────────────────────────


def encode_f32(array: npt.ArrayLike) -> str:
    """Encode an array as base64 little-endian float32, C order."""
    arr = np.ascontiguousarray(np.asarray(array, dtype="<f4"))
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_f32(value: Any, shape: tuple[int, ...], field: str) -> F32Array:
    """Decode a base64 float32 field and check its exact shape and finiteness."""
    if not isinstance(value, str):
        raise ContractError(ErrorCode.INVALID_REQUEST, f"{field} must be a string")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ContractError(ErrorCode.INVALID_REQUEST, f"{field} is not base64") from exc
    expected = 4 * int(np.prod(shape))
    if len(raw) != expected:
        raise ContractError(
            ErrorCode.BAD_SHAPE, f"{field} has {len(raw)} bytes, expected {expected}"
        )
    arr = np.frombuffer(raw, dtype="<f4").astype(np.float32).reshape(shape)
    if not np.all(np.isfinite(arr)):
        raise ContractError(ErrorCode.NON_FINITE, f"{field} contains NaN or Inf")
    return arr


def check_unit_norm(vec: F32Array, tolerance: float, field: str) -> None:
    norm = float(np.linalg.norm(vec.astype(np.float64)))
    if abs(norm - 1.0) > tolerance:
        raise ContractError(ErrorCode.BAD_NORM, f"{field} norm {norm:.6f} is not unit")


# ─── Adapter weights ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdapterWeights:
    """One per-user adapter version. Shapes follow the source np.savez layout:
    W1 (dim, hidden), b1 (hidden,), W2 (hidden, dim), b2 (dim,)."""

    W1: F32Array
    b1: F32Array
    W2: F32Array
    b2: F32Array
    alpha: float = DEFAULT_ALPHA

    def __post_init__(self) -> None:
        for name, arr, shape in (
            ("W1", self.W1, (DIM, HIDDEN)),
            ("b1", self.b1, (HIDDEN,)),
            ("W2", self.W2, (HIDDEN, DIM)),
            ("b2", self.b2, (DIM,)),
        ):
            if arr.shape != shape:
                raise ContractError(ErrorCode.BAD_SHAPE, f"{name} shape {arr.shape} != {shape}")
            if arr.dtype != np.float32:
                raise ContractError(ErrorCode.INVALID_REQUEST, f"{name} must be float32")
            if not np.all(np.isfinite(arr)):
                raise ContractError(ErrorCode.NON_FINITE, f"{name} contains NaN or Inf")
        if not (isinstance(self.alpha, float) and np.isfinite(self.alpha)):
            raise ContractError(ErrorCode.NON_FINITE, "alpha must be a finite float")
        if not 0.0 < self.alpha <= 1.0:
            raise ContractError(ErrorCode.INVALID_REQUEST, "alpha must be in (0, 1]")

    @property
    def nbytes(self) -> int:
        return int(self.W1.nbytes + self.b1.nbytes + self.W2.nbytes + self.b2.nbytes)

    def digest(self) -> str:
        """Canonical SHA-256 over the architecture header and float32 bytes."""
        header = json.dumps(
            {
                "activation": ACTIVATION,
                "alpha": np.float32(self.alpha).item(),
                "contract": CONTRACT_VERSION,
                "dim": DIM,
                "hidden": HIDDEN,
                "normalization": NORMALIZATION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        h = hashlib.sha256()
        h.update(header)
        h.update(b"\0")
        for arr in (self.W1, self.b1, self.W2, self.b2):
            h.update(np.ascontiguousarray(arr, dtype="<f4").tobytes())
        return h.hexdigest()

    def to_wire(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "W1": encode_f32(self.W1),
            "b1": encode_f32(self.b1),
            "W2": encode_f32(self.W2),
            "b2": encode_f32(self.b2),
        }

    @classmethod
    def from_wire(cls, value: Any) -> AdapterWeights:
        if not isinstance(value, dict):
            raise ContractError(ErrorCode.INVALID_REQUEST, "weights must be an object")
        alpha = value.get("alpha")
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
            raise ContractError(ErrorCode.INVALID_REQUEST, "weights.alpha must be a number")
        return cls(
            W1=decode_f32(value.get("W1"), (DIM, HIDDEN), "weights.W1"),
            b1=decode_f32(value.get("b1"), (HIDDEN,), "weights.b1"),
            W2=decode_f32(value.get("W2"), (HIDDEN, DIM), "weights.W2"),
            b2=decode_f32(value.get("b2"), (DIM,), "weights.b2"),
            alpha=float(alpha),
        )


# ─── Field validators ────────────────────────────────────────────────────────


def _require_contract(body: dict[str, Any]) -> None:
    if body.get("contract") != CONTRACT_VERSION:
        raise ContractError(ErrorCode.CONTRACT_MISMATCH, f"contract must be {CONTRACT_VERSION!r}")


def _str_field(body: dict[str, Any], name: str, pattern: re.Pattern[str]) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContractError(ErrorCode.INVALID_REQUEST, f"{name} is missing or malformed")
    return value


def _version_field(body: dict[str, Any]) -> int:
    value = body.get("adapter_version")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(ErrorCode.INVALID_REQUEST, "adapter_version must be an integer")
    if not 1 <= value <= MAX_ADAPTER_VERSION:
        raise ContractError(ErrorCode.INVALID_REQUEST, "adapter_version out of range")
    return value


def _object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ContractError(ErrorCode.INVALID_REQUEST, "body must be a JSON object")
    return body


# ─── Messages ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdapterRef:
    """The exact adapter Ditto authorized: tenant alias, version, digest."""

    tenant_id: str
    adapter_version: int
    adapter_digest: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "adapter_version": self.adapter_version,
            "adapter_digest": self.adapter_digest,
        }

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> AdapterRef:
        return cls(
            tenant_id=_str_field(body, "tenant_id", TENANT_RE),
            adapter_version=_version_field(body),
            adapter_digest=_str_field(body, "adapter_digest", DIGEST_RE),
        )


@dataclass(frozen=True)
class ProvisionRequest:
    """PUT /v1/adapters. Provisioning version N retires every version < N for
    the tenant; re-provisioning an existing version with the same digest is
    idempotent, a different digest is DIGEST_MISMATCH, a lower version than the
    newest is STALE_VERSION."""

    ref: AdapterRef
    weights: AdapterWeights

    def to_wire(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION,
            **self.ref.to_wire(),
            "encoding": ENCODING,
            "weights": self.weights.to_wire(),
        }

    @classmethod
    def from_wire(cls, raw: Any) -> ProvisionRequest:
        body = _object(raw)
        _require_contract(body)
        if body.get("encoding") != ENCODING:
            raise ContractError(ErrorCode.INVALID_REQUEST, f"encoding must be {ENCODING!r}")
        ref = AdapterRef.from_wire(body)
        weights = AdapterWeights.from_wire(body.get("weights"))
        if weights.digest() != ref.adapter_digest:
            raise ContractError(ErrorCode.DIGEST_MISMATCH, "weights do not match adapter_digest")
        return cls(ref=ref, weights=weights)


@dataclass(frozen=True)
class AdaptRequest:
    """POST /v1/adapt."""

    request_id: str
    ref: AdapterRef
    query: F32Array

    def to_wire(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION,
            "request_id": self.request_id,
            **self.ref.to_wire(),
            "encoding": ENCODING,
            "query": encode_f32(self.query),
        }

    @classmethod
    def from_wire(cls, raw: Any) -> AdaptRequest:
        body = _object(raw)
        _require_contract(body)
        request_id = _str_field(body, "request_id", REQUEST_ID_RE)
        if body.get("encoding") != ENCODING:
            raise ContractError(ErrorCode.INVALID_REQUEST, f"encoding must be {ENCODING!r}")
        ref = AdapterRef.from_wire(body)
        query = decode_f32(body.get("query"), (DIM,), "query")
        check_unit_norm(query, QUERY_NORM_TOLERANCE, "query")
        return cls(request_id=request_id, ref=ref, query=query)


@dataclass(frozen=True)
class AdaptResponse:
    """200 response to POST /v1/adapt. Echoes request_id and the full ref.
    ``compute_us`` is the miner's self-reported adapter compute time and
    ``cache`` is ``"hit"`` or ``"miss"`` for the decoded adapter."""

    request_id: str
    ref: AdapterRef
    vector: F32Array
    compute_us: int
    cache: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION,
            "request_id": self.request_id,
            **self.ref.to_wire(),
            "encoding": ENCODING,
            "vector": encode_f32(self.vector),
            "compute_us": self.compute_us,
            "cache": self.cache,
        }

    @classmethod
    def from_wire(cls, raw: Any) -> AdaptResponse:
        """Parse shape and finiteness only. Norm, echo and parity are the
        Ditto client's job, so a bad-norm vector still parses here."""
        body = _object(raw)
        _require_contract(body)
        request_id = _str_field(body, "request_id", REQUEST_ID_RE)
        ref = AdapterRef.from_wire(body)
        if body.get("encoding") != ENCODING:
            raise ContractError(ErrorCode.INVALID_REQUEST, f"encoding must be {ENCODING!r}")
        vector = decode_f32(body.get("vector"), (DIM,), "vector")
        compute_us = body.get("compute_us")
        if isinstance(compute_us, bool) or not isinstance(compute_us, int) or compute_us < 0:
            raise ContractError(ErrorCode.INVALID_REQUEST, "compute_us must be a non-negative int")
        cache = body.get("cache")
        if cache not in ("hit", "miss"):
            raise ContractError(ErrorCode.INVALID_REQUEST, "cache must be 'hit' or 'miss'")
        return cls(request_id, ref, vector, compute_us, cache)


def error_body(code: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    """Error response body. HTTP status comes from ``HTTP_STATUS[code]``."""
    body: dict[str, Any] = {
        "contract": CONTRACT_VERSION,
        "error": {"code": code, "message": message},
    }
    if request_id is not None:
        body["request_id"] = request_id
    return body


def parse_error(raw: Any) -> tuple[str, str]:
    """Return ``(code, message)`` from an error body; unknown shapes map to
    INVALID_REQUEST so a client never trusts an unparseable error."""
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            code = err["code"]
            if code in HTTP_STATUS:
                return code, str(err.get("message", ""))
    return ErrorCode.INVALID_REQUEST, "unparseable error body"


def make_request_id(run_id: str, tenant_id: str, seq: int) -> str:
    """Scoped request id: ``qa1.<run>.<tenant>.<seq>``. A retry of the same
    logical request reuses the same id, which is what usage dedupe keys on."""
    rid = f"qa1.{run_id}.{tenant_id}.{seq}"
    if not REQUEST_ID_RE.fullmatch(rid):
        raise ContractError(ErrorCode.INVALID_REQUEST, f"cannot form request id {rid!r}")
    return rid
