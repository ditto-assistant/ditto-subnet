"""Read-only runtime upgrade report; no admission, signing or transfer authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RuntimeSurfaceVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    surface: Literal["source_emission", "treasury_receipt"]
    # None means the reader refuses this runtime for this surface.
    admitted_by: Literal["audited_artifact", "interface"] | None
    interface_digest: str | None
    nearest_audited_release: str | None
    changed_entries: list[str]
    error: str | None = None


class TreasurySignerRuntimeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    # The reviewed collector artifact the cold-signed policy binds. The
    # validator signer refuses every other runtime until a new approval.
    approved_runtime_code_hash: str
    signs_on_this_runtime: bool
    call_baseline_release: str
    # Pallet.call entries added, removed or reshaped since the baseline.
    changed_calls: list[str]
    changed_calls_truncated: bool
    # Set when call metadata is unreadable; changed_calls is then not evidence
    # that nothing changed.
    calls_error: str | None = None


class RuntimeInterfaceReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    block: int
    block_hash: str
    runtime_code_hash: str
    spec_version: int
    surfaces: list[RuntimeSurfaceVerdict]
    treasury_signer: TreasurySignerRuntimeView
