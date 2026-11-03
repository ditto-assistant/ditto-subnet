"""Read-only report of how one runtime's consumed interfaces compare to audits.

An operator reads this after a Subtensor upgrade: it says whether source
attribution and treasury receipt reading admit the runtime, and for the
treasury signer, whether it keeps signing (its consumed surface is unchanged
from the runtime its cold-signed policy binds) and which calls moved since
then. It changes no admission state and authorizes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ditto.chain.runtime_interface import (
    SOURCE_EMISSION_SURFACE,
    TREASURY_RECEIPT_SURFACE,
    RuntimeInterface,
    RuntimeSurface,
    metadata_at,
    nearest_audited_change,
    runtime_call_digests,
    runtime_interface,
)

_CHANGED_CALL_LIMIT = 100


class BlockNotFinalized(ValueError):
    """The requested block is outside the finalized chain: a caller error."""


@dataclass(frozen=True)
class SurfaceVerdict:
    surface: str
    # "audited_artifact" | "interface" | None (refused)
    admitted_by: str | None
    interface_digest: str | None
    nearest_audited_release: str | None
    changed_entries: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class TreasurySignerView:
    approved_runtime_code_hash: str
    signs_on_this_runtime: bool
    call_baseline_release: str
    # Calls added, removed or reshaped since the baseline (bounded).
    changed_calls: tuple[str, ...]
    changed_calls_truncated: bool = False
    # Set when the call metadata is unreadable; changed_calls is then empty
    # because nothing could be compared, not because nothing changed.
    calls_error: str | None = None


@dataclass(frozen=True)
class RuntimeInterfaceReport:
    block: int
    block_hash: str
    runtime_code_hash: str
    spec_version: int
    surfaces: tuple[SurfaceVerdict, ...]
    treasury_signer: TreasurySignerView


def _verdict(
    metadata: Any,
    surface: RuntimeSurface,
    code: str,
    artifacts: frozenset[str],
    audited: dict[str, tuple[str, RuntimeInterface]],
) -> SurfaceVerdict:
    try:
        interface = runtime_interface(metadata, surface)
    except ValueError as error:
        return SurfaceVerdict(
            surface.name,
            "audited_artifact" if code in artifacts else None,
            None,
            None,
            (),
            str(error)[:300],
        )
    release, changed = nearest_audited_change(interface, audited)
    if code in artifacts:
        admitted: str | None = "audited_artifact"
    elif interface.digest in audited:
        admitted = "interface"
    else:
        admitted = None
    return SurfaceVerdict(
        surface.name, admitted, interface.digest, release, tuple(changed)
    )


async def _signer_surface_unchanged(
    substrate: Any, metadata: Any, live_version: dict[str, Any]
) -> bool:
    """Mirror custody's admission: same signer surface as the signed runtime.

    Custody computes this itself before signing; this is the report's view of
    it. Any read failure reports "does not sign" rather than guessing.
    """
    from ditto_screening_protocol.collector_receipts import (
        AUDITED_COLLECTOR_CODE_HASH,
        AUDITED_COLLECTOR_REFERENCE_BLOCK,
        COLLECTOR_SIGNER_SURFACE,
    )

    number, reference = AUDITED_COLLECTOR_REFERENCE_BLOCK
    try:
        if await substrate.get_block_hash(number) != reference:
            return False
        stored = await substrate.rpc_request(
            "state_getStorageHash", ["0x3a636f6465", reference]
        )
        if stored.get("result") != AUDITED_COLLECTOR_CODE_HASH:
            return False
        signed_version = (
            await substrate.rpc_request("state_getRuntimeVersion", [reference])
        )["result"]
        if (
            live_version["transactionVersion"] != signed_version["transactionVersion"]
            or live_version["specVersion"] < signed_version["specVersion"]
        ):
            return False
        signed = runtime_interface(
            await metadata_at(substrate, reference), COLLECTOR_SIGNER_SURFACE
        )
        live = runtime_interface(metadata, COLLECTOR_SIGNER_SURFACE)
    except Exception:  # noqa: BLE001 - report-only; custody refuses on its own
        return False
    return live.digest == signed.digest


async def read_runtime_interface_report(
    substrate: Any, *, block: int | None = None
) -> RuntimeInterfaceReport:
    """Report the runtime at ``block`` (default: the finalized head)."""
    from ditto.chain.audited_runtime_interfaces import (
        AUDITED_RUNTIME_INTERFACES,
        TREASURY_CALL_BASELINE,
        TREASURY_RECEIPT_INTERFACES,
    )
    from ditto.chain.source_emission_verifier import AUDITED_RUNTIME_CODE_HASHES
    from ditto_screening_protocol.collector_receipts import (
        AUDITED_COLLECTOR_CODE_HASH,
        AUDITED_COLLECTOR_RECEIPT_HASHES,
    )

    head = await substrate.get_chain_finalised_head()
    header = await substrate.get_block_header(block_hash=head)
    raw = header.get("header", header)["number"]
    finalized = int(raw, 16) if isinstance(raw, str) else int(raw)
    number = finalized if block is None else block
    if not 0 < number <= finalized:
        raise BlockNotFinalized("block is not finalized")
    at = head if block is None else await substrate.get_block_hash(number)
    code = (await substrate.rpc_request("state_getStorageHash", ["0x3a636f6465", at]))[
        "result"
    ]
    version = (await substrate.rpc_request("state_getRuntimeVersion", [at]))["result"]
    metadata = await metadata_at(substrate, at)
    baseline_release, baseline_calls = TREASURY_CALL_BASELINE
    calls_error = None
    changed_calls: list[str] = []
    try:
        calls = runtime_call_digests(metadata)
    except ValueError as error:
        # Report each surface's own verdict even when calls are unreadable.
        calls_error = str(error)[:300]
    else:
        changed_calls = sorted(
            call
            for call in calls.keys() | baseline_calls.keys()
            if calls.get(call) != baseline_calls.get(call)
        )
    return RuntimeInterfaceReport(
        block=number,
        block_hash=at,
        runtime_code_hash=code,
        spec_version=int(version["specVersion"]),
        surfaces=(
            _verdict(
                metadata,
                SOURCE_EMISSION_SURFACE,
                code,
                AUDITED_RUNTIME_CODE_HASHES,
                AUDITED_RUNTIME_INTERFACES,
            ),
            _verdict(
                metadata,
                TREASURY_RECEIPT_SURFACE,
                code,
                AUDITED_COLLECTOR_RECEIPT_HASHES,
                TREASURY_RECEIPT_INTERFACES,
            ),
        ),
        treasury_signer=TreasurySignerView(
            approved_runtime_code_hash=AUDITED_COLLECTOR_CODE_HASH,
            signs_on_this_runtime=code == AUDITED_COLLECTOR_CODE_HASH
            or await _signer_surface_unchanged(substrate, metadata, version),
            call_baseline_release=baseline_release,
            changed_calls=tuple(changed_calls[:_CHANGED_CALL_LIMIT]),
            changed_calls_truncated=len(changed_calls) > _CHANGED_CALL_LIMIT,
            calls_error=calls_error,
        ),
    )
