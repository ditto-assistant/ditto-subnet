"""What a miner submits to each competition: the deliverable and its fee.

``GET /public/submission-terms`` serves one entry per competition with an
upload lane. Each entry names the artifact (its kind, format, contract
version, required files and every upload-time limit, each with where that
limit comes from), the upload cooldown, and the competition's own fee. It is
read-only: limits defined in code are reported as such, and the only
operator-tunable values are the ones that were already settings (fees and
cooldowns, the tarball cap's environment override, the generator pack size).

This is the miner submission contract only; build bounties are a separate
system with their own terms.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CompetitionId = Literal["memory", "generator"]
ArtifactKind = Literal["agent_harness_tarball", "generator_source_archive"]
LimitSource = Literal["code", "environment", "setting"]
"""``code``: a constant in the named module, changed only by a release.
``environment``: a deploy-time environment variable on Platform.
``setting``: an operator setting, revisioned and audited through Backroom."""


class ArtifactLimit(BaseModel):
    """One upload-time limit on the artifact, with where it comes from."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    value: int | str
    unit: Literal["bytes", "files", "lines", "characters", "cases", "version"]
    source: LimitSource
    reference: str
    """The constant, environment variable, or setting that defines it."""
    description: str


class ArtifactContract(BaseModel):
    """The deliverable one competition accepts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: ArtifactKind
    format: str
    contract_version: str | None = None
    """The versioned artifact contract the upload must satisfy, when the
    competition has one (generator: ``dittobench-generator/v1``)."""
    required_files: list[str] = Field(default_factory=list)
    limits: list[ArtifactLimit] = Field(default_factory=list)
    upload_endpoint: str
    signature_payload: str
    """The ASCII payload the hotkey signs, with ``{placeholders}``."""
    notes: list[str] = Field(default_factory=list)


class SubmissionFeeTerms(BaseModel):
    """One competition's fee, resolved from the submission settings."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    required: bool
    amount_rao: int
    """What an upload must pay, exactly, when ``required``; shown even when
    not required so a miner sees the price a switch would charge."""
    amount_source: Literal["default", "override"]
    required_source: Literal["always", "competition_settings", "override"]
    send_address: str
    asset: Literal["TAO"] = "TAO"
    settings_revision: int
    """The submission settings revision this fee resolves from (0: the
    built-in default)."""


class SubmissionCooldownTerms(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    seconds: int
    scope: Literal["owner_coldkey", "hotkey"]
    source: LimitSource
    reference: str


class CompetitionSubmissionTerms(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    competition: CompetitionId
    accepting: bool
    deliverable: ArtifactContract
    cooldown: SubmissionCooldownTerms
    fee: SubmissionFeeTerms
    terms_endpoint: str | None = None
    """The per-upload terms (blockers for one hotkey and archive) a client
    reads right before paying, when the lane has one."""


class SubmissionTermsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    competitions: list[CompetitionSubmissionTerms]
