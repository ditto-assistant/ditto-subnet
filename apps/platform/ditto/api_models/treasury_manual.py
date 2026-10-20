from typing import Annotated, Literal

from pydantic import Field

from ditto_screening_protocol.treasury import Digest
from ditto_screening_protocol.treasury_manual import (
    Bucket,
    ManualEnvelopeWire,
    Positive,
    Wire,
)


class ManualPreviewInput(Wire):
    request_id: str
    bucket_id: Bucket
    amount_rao: Positive
    retained_alpha_rao: Positive
    reason: Annotated[str, Field(min_length=8, max_length=240)]


class ManualSubmitInput(Wire):
    envelope: ManualEnvelopeWire
    confirmation_digest: Digest
    confirmation: Literal["TRANSFER SN118 ALPHA ONCE"]
