"""Admin contracts for the batched ATH rulings court.

A board review produces 5-20 rulings at once. The one-at-a-time
``/copy-reviews/{agent_id}/open`` and ``/resolve`` routes stay the only
writers; this surface batches them behind the quarantine court's shape:
upload a rulings document, dry-run it against live state, then execute the
exact previewed document under a signed, actor-bound preview token.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

AthRulingAction = Literal["open", "clear", "reject"]
AthRulingDisposition = Literal[
    "ready", "already_applied", "stale_guard", "conflict", "not_found", "invalid"
]
ATH_RULINGS_CONFIRMATION = "APPLY ATH RULINGS BATCH"
ATH_RULINGS_MAX_ITEMS = 50
# Presigned rulings uploads are capped at one mebibyte; a document that large
# is not a board review, it is a mistake.
ATH_RULINGS_MAX_BYTES = 1 << 20
ATH_RULINGS_UPLOAD_TTL_SECONDS = 300
ATH_RULINGS_KEY_PREFIX = "ath-rulings/v1/"
ATH_RULINGS_CONTENT_TYPE: Literal["application/json"] = "application/json"

Sha256Hex = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-f]{64}$")
]
# ``path:line`` or ``path:line-line`` -- the citation shape every miner-visible
# reject in the review bar already carries.
EvidenceReference = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=512,
        pattern=r"^[^\s:]+(?:/[^\s:]+)*:\d+(?:-\d+)?$",
    ),
]


class AdminAthRuling(BaseModel):
    """One guarded ruling: the same guards ``open_ath_review`` takes.

    ``reason`` is public and miner-visible; it is deliberately unbounded above
    so a detailed citation-bearing reason survives every validation surface.
    """

    model_config = ConfigDict(extra="ignore")

    action: AthRulingAction
    agent_id: UUID
    expected_sha256: Sha256Hex
    expected_score_count: Annotated[int, Field(ge=0)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]
    evidence_references: Annotated[list[EvidenceReference], Field(max_length=64)] = (
        Field(default_factory=list)
    )


class AdminAthRulingsDocument(BaseModel):
    """The uploaded rulings JSON."""

    model_config = ConfigDict(extra="ignore")

    rulings: Annotated[
        list[AdminAthRuling], Field(min_length=1, max_length=ATH_RULINGS_MAX_ITEMS)
    ]
    # Free-form provenance for the audit trail, e.g. the review write-up path.
    source: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _one_ruling_per_agent(self) -> "AdminAthRulingsDocument":
        ids = [ruling.agent_id for ruling in self.rulings]
        if len(set(ids)) != len(ids):
            raise ValueError("each agent_id may appear only once per batch")
        return self


class AdminAthRulingsUploadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content_type: Literal["application/json"] = ATH_RULINGS_CONTENT_TYPE


class AdminAthRulingsUploadResponse(BaseModel):
    bucket: str
    key: str
    url: str
    method: Literal["PUT"] = "PUT"
    content_type: str
    expires_in: int
    max_bytes: int


class AdminAthRulingsPreviewRequest(BaseModel):
    """Exactly one of ``upload_key`` (presigned upload) or inline ``rulings``."""

    model_config = ConfigDict(extra="ignore")

    upload_key: str | None = Field(default=None, max_length=512)
    rulings: (
        Annotated[
            list[AdminAthRuling],
            Field(min_length=1, max_length=ATH_RULINGS_MAX_ITEMS),
        ]
        | None
    ) = None
    source: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "AdminAthRulingsPreviewRequest":
        if (self.upload_key is None) == (self.rulings is None):
            raise ValueError("provide exactly one of upload_key or rulings")
        return self


class AdminAthRulingsBoardProjection(BaseModel):
    """The crown arithmetic the batch was previewed (or executed) against.

    Read from the validator-equivalent KOTH fold (eligible ledger with stderr,
    quorum, confirmation and efficiency inputs) under the same fleet-gated tie
    weighting and ceiling-band-clamp flags the public leaderboard's
    ``emissions`` block applies, so the champion here is the one the board
    shows.
    """

    bench_version: int
    read_at: datetime
    ranked_count: int
    champion_agent_id: UUID | None
    champion_hotkey: str | None
    champion_score: float | None
    raw_leader_agent_id: UUID | None
    raw_leader_score: float | None
    # SHA-256 over champion, raw leader, and the top-five ordered agent ids;
    # a changed fingerprint means the emission set moved between reads.
    fingerprint: str


class AdminAthRulingPreviewItem(BaseModel):
    index: int
    action: AthRulingAction
    agent_id: UUID
    agent_name: str | None = None
    agent_version: int | None = None
    miner_hotkey: str | None = None
    agent_status: str | None = None
    artifact_sha256: str | None = None
    score_count: int | None = None
    ok: bool
    disposition: AthRulingDisposition
    # True when expected_sha256 / expected_score_count no longer match the row.
    stale_guard: bool
    # True when applying this ruling moves the champion or raw leader: rejecting
    # or holding either of them, or clearing an agent whose canonical score
    # would re-enter as champion or raw leader. Judged against the board AFTER
    # the earlier rulings in the batch, so a champion reject flags the
    # runner-up that a later ruling then touches.
    would_change_crown: bool
    # The 409 detail the underlying open/resolve route would answer with (or
    # the preview-time refusal reason); None when the item is ready.
    conflict_reason: str | None = None
    # What execute will perform for this item, in order.
    steps: list[Literal["open", "clear", "reject"]] = Field(default_factory=list)
    reason: str
    evidence_references: list[str] = Field(default_factory=list)
    message: str


class AdminAthRulingsPreviewResponse(BaseModel):
    preview_token: str
    expires_at: datetime
    rulings_sha256: str
    upload_key: str | None = None
    source: str | None = None
    board: AdminAthRulingsBoardProjection
    items: list[AdminAthRulingPreviewItem]
    ready_count: int
    already_applied_count: int
    blocked_count: int
    crown_moving_count: int


class AdminAthRulingsExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    preview_token: Annotated[str, Field(min_length=32, max_length=16384)]
    confirmation: Literal["APPLY ATH RULINGS BATCH"]
    # Required when the batch was previewed inline (no upload_key): the token
    # binds the digest of the rulings, not their bytes.
    rulings: (
        Annotated[
            list[AdminAthRuling],
            Field(min_length=1, max_length=ATH_RULINGS_MAX_ITEMS),
        ]
        | None
    ) = None


class AdminAthRulingExecuteItem(BaseModel):
    index: int
    action: AthRulingAction
    agent_id: UUID
    status: Literal["applied", "already_applied", "failed"]
    agent_status: str | None = None
    would_change_crown: bool
    steps_applied: list[Literal["open", "clear", "reject"]] = Field(
        default_factory=list
    )
    # False on an applied row means the ruling landed but the batch_id /
    # evidence_references annotation on its audit rows did not; the ruling is
    # NOT to be re-run.
    annotated: bool = False
    message: str


class AdminAthRulingsExecuteResponse(BaseModel):
    batch_id: UUID
    rulings_sha256: str
    upload_key: str | None = None
    board_before: AdminAthRulingsBoardProjection
    board_after: AdminAthRulingsBoardProjection
    items: list[AdminAthRulingExecuteItem]
    applied_count: int
    already_applied_count: int
    failed_count: int
