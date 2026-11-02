"""Signed, lease-bound private dataset download contract."""

import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PrivateDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{48}$")]
    dataset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    deadline: datetime
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]

    @model_validator(mode="after")
    def aware_times(self):
        if self.deadline.tzinfo is None or self.requested_at.tzinfo is None:
            raise ValueError("private dataset timestamps require timezone")
        return self

    def signing_message(self, agent_id: UUID) -> bytes:
        return json.dumps(
            [
                "validator-private-dataset-v1",
                self.validator_hotkey,
                str(agent_id),
                self.dataset_sha256,
                self.deadline.astimezone(UTC).isoformat(timespec="microseconds"),
                str(self.nonce),
                self.requested_at.astimezone(UTC).isoformat(timespec="microseconds"),
            ],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
