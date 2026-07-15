"""Unit tests for :mod:`ditto.api_models.upload`."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ditto.api_models import (
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckRequest,
    UploadCheckResponse,
)
from ditto.api_models.agent_status import AgentStatus

_GOOD_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_GOOD_SHA256 = "1d8a3b6f04e2c7f9a51bd3e5c8f2a7b06d4e9c1f2a3b4c5d6e7f8a9b0c1d2e3f"
_GOOD_SIG = "a" * 128


class TestEvalPricingResponse:
    def test_happy_path(self):
        r = EvalPricingResponse(amount_rao=1000, send_address=_GOOD_SS58)
        assert r.amount_rao == 1000
        assert r.send_address == _GOOD_SS58

    def test_amount_rao_must_be_positive(self):
        with pytest.raises(ValidationError):
            EvalPricingResponse(amount_rao=0, send_address=_GOOD_SS58)

    def test_send_address_must_be_ss58_shaped(self):
        with pytest.raises(ValidationError):
            EvalPricingResponse(amount_rao=1000, send_address="not-ss58")


class TestUploadCheckRequest:
    def test_happy_path(self):
        r = UploadCheckRequest(
            hotkey=_GOOD_SS58,
            sha256=_GOOD_SHA256,
            file_size_bytes=1000,
            signature=_GOOD_SIG,
        )
        assert r.hotkey == _GOOD_SS58

    @pytest.mark.parametrize(
        "field,value",
        [
            ("hotkey", "not-ss58"),
            ("sha256", "tooshort"),
            ("sha256", "G" * 64),  # G is not a hex digit
            ("signature", "a" * 127),  # too short
            ("signature", "a" * 129),  # too long
            ("signature", "z" * 128),  # not hex
            ("file_size_bytes", 0),  # must be >= 1
            ("file_size_bytes", -10),
        ],
    )
    def test_field_validation_rejects_bad_values(self, field: str, value: object):
        kwargs: dict[str, Any] = {
            "hotkey": _GOOD_SS58,
            "sha256": _GOOD_SHA256,
            "file_size_bytes": 1000,
            "signature": _GOOD_SIG,
        }
        kwargs[field] = value
        with pytest.raises(ValidationError):
            UploadCheckRequest(**kwargs)


class TestUploadCheckResponse:
    def test_happy_path(self):
        r = UploadCheckResponse(ok=True, error_codes=[], messages=[])
        assert r.ok is True

    def test_failure_shape(self):
        r = UploadCheckResponse(
            ok=False,
            error_codes=[1100, 1101],
            messages=["bad sig", "not registered"],
        )
        assert r.ok is False
        assert r.error_codes == [1100, 1101]
        assert len(r.messages) == 2


class TestUploadAgentResponse:
    def test_happy_path(self):
        agent_id = "11111111-1111-1111-1111-111111111111"
        r = UploadAgentResponse(
            agent_id=agent_id, version=1, status=AgentStatus.UPLOADED
        )
        assert str(r.agent_id) == agent_id
        assert r.version == 1
        assert r.status == AgentStatus.UPLOADED

    def test_status_serializes_to_string(self):
        r = UploadAgentResponse(
            agent_id="11111111-1111-1111-1111-111111111111",
            version=2,
            status=AgentStatus.UPLOADED,
        )
        body = r.model_dump(mode="json")
        assert body["status"] == "uploaded"

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            UploadAgentResponse(
                agent_id="11111111-1111-1111-1111-111111111111",
                version=1,
                status="invented-status",  # type: ignore[arg-type]
            )

    def test_rejects_malformed_uuid(self):
        with pytest.raises(ValidationError):
            UploadAgentResponse(
                agent_id="not-a-uuid",  # type: ignore[arg-type]
                version=1,
                status=AgentStatus.UPLOADED,
            )
