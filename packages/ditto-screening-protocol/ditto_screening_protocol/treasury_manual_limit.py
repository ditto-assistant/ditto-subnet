"""Cold-approved manual range, independent of recurring custody authority."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .treasury import Address, Digest
from .treasury_approval import verify_public_signature

MANUAL_LIMIT_DOMAIN = "ditto-treasury-manual-limit-v1:"
MAX_MANUAL_ALPHA_RAO = 2_500_000_000_000
MAX_MANUAL_SOURCES = 512


class ManualLimitPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    version: Literal[1] = 1
    scope: Literal["manual_once"] = "manual_once"
    collector_policy_digest: Digest
    collector_coldkey: Address
    revision: Annotated[int, Field(gt=0, le=2**53 - 1, strict=True)]
    min_amount_rao: Literal[1] = 1
    max_amount_rao: Annotated[int, Field(gt=0, le=MAX_MANUAL_ALPHA_RAO, strict=True)]
    max_sources: Literal[512] = MAX_MANUAL_SOURCES

    @property
    def digest(self):
        body = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256((MANUAL_LIMIT_DOMAIN + body).encode()).hexdigest()


class ManualLimitApproval(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    policy: ManualLimitPolicy
    signature: Annotated[str, Field(pattern=r"^0x[0-9a-f]{128}$")]

    def verify(self, collector_policy_digest, collector_coldkey):
        if (
            self.policy.collector_policy_digest != collector_policy_digest
            or self.policy.collector_coldkey != collector_coldkey
            or not verify_public_signature(
                collector_coldkey,
                (MANUAL_LIMIT_DOMAIN + self.policy.digest).encode(),
                bytes.fromhex(self.signature[2:]),
            )
        ):
            raise ValueError("manual limit lacks exact collector cold approval")
        return self.policy
