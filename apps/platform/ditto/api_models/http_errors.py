"""The JSON envelope emitted by Platform's HTTP exception middleware."""

from pydantic import BaseModel, ConfigDict


class HttpErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    error_code: int
    message: str
    request_id: str | None
