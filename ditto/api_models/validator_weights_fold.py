"""Which pinned ledger a validator folded, echoed on its heartbeat.

The epoch pin makes every validator's fold input identical; this is the proof
that a given validator actually folded it. ``ledger_digest`` is the pin's own
digest replayed, ``vector_digest`` covers the exact weight vector handed to
Pylon, and ``champion_agent_id`` is the crown that fold derived. The Platform
matches these against the pin and the revealed on-chain matrix so the board
can say "9 of 11 validators folded pin #25028" rather than infer it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class WeightsFold(BaseModel):
    """Signed summary of one validator weight fold, under heartbeat protocol v27."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    epoch_index: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Chain SubnetEpochIndex of the pinned ledger that was folded; "
                "null when the ledger served was a live (unpinned) read."
            ),
        ),
    ] = None
    ledger_digest: Annotated[
        str | None,
        Field(
            default=None,
            pattern=_DIGEST_PATTERN,
            description="The served ledger_digest, replayed verbatim.",
        ),
    ] = None
    vector_digest: Annotated[
        str,
        Field(
            pattern=_DIGEST_PATTERN,
            description=(
                "SHA-256 of the canonical JSON of the [hotkey, weight] pairs handed "
                "to Pylon, sorted by hotkey."
            ),
        ),
    ]
    champion_agent_id: Annotated[
        UUID | None,
        Field(default=None, description="The champion this fold derived, if any."),
    ] = None
    folded_at: Annotated[
        int, Field(ge=0, description="Unix timestamp (UTC) the fold was submitted.")
    ]


def weights_vector_digest(weights: dict[str, float]) -> str:
    """Canonical digest of a weight vector: sorted [hotkey, weight] pairs."""
    encoded = json.dumps(
        [[hotkey, float(weight)] for hotkey, weight in sorted(weights.items())],
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def weights_fold_signing_token(fold: WeightsFold | None) -> str:
    """Return one length-prefixed canonical JSON heartbeat token, ``""`` for none."""
    if fold is None:
        return ""
    encoded = json.dumps(
        fold.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{len(encoded.encode())}:{encoded}"


__all__ = ["WeightsFold", "weights_fold_signing_token", "weights_vector_digest"]
