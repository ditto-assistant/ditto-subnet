"""Unit tests for :mod:`ditto.miner_cli.api_client`.

The HTTP layer is mocked via :class:`httpx.MockTransport` so tests pin
exact wire shapes (paths, query params, multipart fields) without
starting a real ASGI app.

Invariants pinned:

- Path + method for each endpoint
- Multipart form fields exactly match
  ``ditto/api_server/endpoints/upload.py:156-173``
- Envelope code mapping: 1200 → AgentNotFoundError, 1201 →
  HotkeyAgentNotFoundError, others → ApiResponseError subclass
- Successful responses parse into the matching Pydantic model
"""

from __future__ import annotations

import gzip
import io
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from ditto.api_models import (
    EvalPricingResponse,
    OwnerLinkProof,
    OwnerLinkRequest,
    OwnerLinkResponse,
    UploadCheckRequest,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    AttestationRejectedError,
    GeneratorPaymentRejectedError,
    HotkeyAgentNotFoundError,
    LoginRejectedError,
    LoginRequiredError,
    PaymentAmountMismatchError,
    PaymentRecoveryExpiredError,
    PaymentReplayedError,
    PreCheckRejectedError,
    SubmissionCooldownError,
    TransientApiError,
    UploadAgentRejectedError,
)
from ditto.miner_cli.models import PaymentReceipt


def make_client(handler) -> ApiClient:  # type: ignore[no-untyped-def]
    """Build an :class:`ApiClient` whose underlying httpx client is a
    mock transport. The base URL is irrelevant since the transport
    short-circuits the network."""
    transport = httpx.MockTransport(handler)
    client = ApiClient(base_url="http://test")
    client._client.close()
    client._client = httpx.Client(transport=transport, base_url="http://test")
    return client


def _envelope_response(
    status: int,
    code: int,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json={
            "error_code": code,
            "message": message,
        },
        headers=headers,
    )


class TestGeneratorUploadTerms:
    @pytest.mark.parametrize(
        ("status", "code", "definitive"),
        [
            (402, 3203, True),
            (402, 3207, True),
            (402, 3208, True),
            (402, 3201, False),
            (402, 3002, False),
            (503, 3000, False),
        ],
    )
    def test_upload_maps_definitive_payment_refusals(
        self, status: int, code: int, definitive: bool
    ) -> None:
        with (
            make_client(lambda _r: _envelope_response(status, code, "no")) as client,
            pytest.raises(ApiResponseError) as caught,
        ):
            client.post_generator_submission({})
        assert isinstance(caught.value, GeneratorPaymentRejectedError) is definitive

    def test_terms_pass_the_hotkey_and_digest(self) -> None:
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"payment_required": True})

        with make_client(_handler) as client:
            terms = client.get_generator_upload_terms(
                hotkey="5Hot", source_sha256="ab" * 32
            )
        assert terms == {"payment_required": True}
        assert seen[0].url.path == "/api/v1/generator/upload-terms"
        assert seen[0].url.params["hotkey"] == "5Hot"
        assert seen[0].url.params["source_sha256"] == "ab" * 32

    def test_an_older_platform_without_terms_reads_as_none(self) -> None:
        with make_client(lambda _request: httpx.Response(404)) as client:
            assert (
                client.get_generator_upload_terms(hotkey="5Hot", source_sha256="ab")
                is None
            )

    def test_other_failures_raise(self) -> None:
        with (
            make_client(
                lambda _request: _envelope_response(500, 3000, "boom")
            ) as client,
            pytest.raises(ApiResponseError, match="generator upload terms"),
        ):
            client.get_generator_upload_terms(hotkey="5Hot", source_sha256="ab")


class TestSubmissionTerms:
    def test_reads_one_competition(self) -> None:
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"competition": "memory"})

        with make_client(_handler) as client:
            assert client.get_submission_terms("memory") == {"competition": "memory"}
        assert seen[0].url.path == "/api/v1/public/submission-terms/memory"

    def test_an_older_platform_reads_as_none_and_failures_raise(self) -> None:
        with make_client(lambda _request: httpx.Response(404)) as client:
            assert client.get_submission_terms("generator") is None
        with (
            make_client(
                lambda _request: _envelope_response(500, 3000, "boom")
            ) as client,
            pytest.raises(ApiResponseError, match="submission terms"),
        ):
            client.get_submission_terms("generator")


class TestContributorAuthenticationErrors:
    def test_tokenless_cla_401_keeps_endpoint_error(self) -> None:
        with (
            make_client(
                lambda _request: httpx.Response(
                    401, json={"detail": "endpoint unauthorized"}
                )
            ) as client,
            pytest.raises(ApiResponseError) as caught,
        ):
            client.get_cla_version(version=1)
        assert not isinstance(caught.value, LoginRequiredError)
        assert "ditto login" not in str(caught.value)

    def test_token_bearing_github_401_requests_login(self) -> None:
        with (
            make_client(
                lambda _request: httpx.Response(
                    401, json={"detail": "endpoint unauthorized"}
                )
            ) as client,
            pytest.raises(LoginRequiredError, match="ditto login"),
        ):
            client.get_github_link(token="fixture-token")


class TestContributorAvailabilityErrors:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_polling_status_failures_are_transient(self, status):
        with (
            make_client(
                lambda _request: httpx.Response(status, json={"message": "temporary"})
            ) as client,
            pytest.raises(TransientApiError),
        ):
            client.get_github_link_attempt(
                token="fixture-token", attempt_id=UUID(int=1)
            )

    @pytest.mark.parametrize("operation", ["status", "start"])
    @pytest.mark.parametrize(
        "body", [{"detail": "Not Found"}, {"error_code": 3002, "message": "Not Found"}]
    )
    def test_missing_github_routes_explain_platform_upgrade(
        self, operation, body
    ) -> None:
        with (
            make_client(lambda _request: httpx.Response(404, json=body)) as client,
            pytest.raises(ApiResponseError, match="upgrade Platform"),
        ):
            if operation == "status":
                client.get_github_link(token="fixture-token")
            elif operation == "start":
                client.start_github_link(token="fixture-token")

    @pytest.mark.parametrize(
        "suffix", ["/attempts/" + str(UUID(int=1)), "/confirm", "/unlink"]
    )
    @pytest.mark.parametrize(
        "body",
        [None, {"detail": "Not Found"}, {"error_code": 3002, "message": "Not Found"}],
    )
    def test_generic_action_not_found_does_not_claim_platform_upgrade(
        self, suffix, body
    ):
        with (
            make_client(
                lambda _: (
                    httpx.Response(404, json=body)
                    if body is not None
                    else httpx.Response(404)
                )
            ) as client,
            pytest.raises(ApiResponseError) as caught,
        ):
            client._contributor_response(
                "GET" if suffix.startswith("/attempts/") else "POST",
                "/api/v1/me/github-link" + suffix,
                token="fixture-token",
            )
        assert "HTTP 404" in str(caught.value)
        assert "upgrade Platform" not in str(caught.value)

    @pytest.mark.parametrize("field", ["detail", "message"])
    def test_unknown_attempt_preserves_endpoint_error(self, field) -> None:
        with (
            make_client(
                lambda _request: httpx.Response(
                    404, json={field: "unknown GitHub link attempt"}
                )
            ) as client,
            pytest.raises(
                ApiResponseError, match="unknown GitHub link attempt"
            ) as caught,
        ):
            client.get_github_link_attempt(
                token="fixture-token", attempt_id=UUID(int=1)
            )
        assert "upgrade Platform" not in str(caught.value)

    def test_empty_missing_route_explains_platform_upgrade(self) -> None:
        with (
            make_client(lambda _request: httpx.Response(404)) as client,
            pytest.raises(ApiResponseError, match="upgrade Platform"),
        ):
            client.get_github_link(token="fixture-token")

    def test_missing_cla_status_keeps_default_off_compatibility(self) -> None:
        with make_client(
            lambda _request: httpx.Response(404, json={"detail": "Not Found"})
        ) as client:
            assert client.get_cla_status() is None

    def test_cla_status_routes_hotkey_lookup_and_rejects_empty_hotkey(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json={"netuid": 118, "mode": "off"})

        with make_client(handler) as client:
            client.get_cla_status(hotkey="5Fixture")
            client.get_cla_status()
            with pytest.raises(ValueError, match="non-empty"):
                client.get_cla_status(hotkey="")
        assert paths == ["/api/v1/public/cla/status/5Fixture", "/api/v1/public/cla"]

    def test_unknown_cla_version_preserves_endpoint_error(self) -> None:
        with (
            make_client(
                lambda _request: httpx.Response(
                    404, json={"detail": "unknown CLA version"}
                )
            ) as client,
            pytest.raises(ApiResponseError, match="unknown CLA version") as caught,
        ):
            client.get_cla_version(version=1)
        assert "upgrade Platform" not in str(caught.value)


class TestClaTextStreaming:
    @pytest.mark.parametrize("status", [200, 503])
    def test_oversized_response_stops_transfer_and_closes(self, status):
        class Chunks(httpx.SyncByteStream):
            consumed = 0
            closed = False

            def __iter__(self):
                for _ in range(1000):
                    self.consumed += 1
                    yield b"x" * 8192

            def close(self):
                self.closed = True

        stream = Chunks()
        with (
            make_client(lambda _: httpx.Response(status, stream=stream)) as client,
            pytest.raises(ApiResponseError, match="size limit") as caught,
        ):
            client.get_cla_text(version=1)
        assert stream.consumed == 33
        assert stream.closed
        assert f"HTTP {status}" in str(caught.value)

    def test_gzip_text_is_decoded_once(self):
        document = "TEST FIXTURE ONLY. No legal terms. é\n".encode()
        with make_client(
            lambda _: httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=httpx.ByteStream(gzip.compress(document)),
            )
        ) as client:
            assert client.get_cla_text(version=1) == document

    def test_small_error_response_keeps_endpoint_message(self):
        with (
            make_client(
                lambda _: httpx.Response(403, json={"detail": "fixture refusal"})
            ) as client,
            pytest.raises(ApiResponseError, match="fixture refusal"),
        ):
            client.get_cla_text(version=1)

    def test_stream_timeout_becomes_transient_api_error(self):
        class Timeout(httpx.SyncByteStream):
            def __iter__(self):
                yield b"fixture"
                raise httpx.ReadTimeout("fixture timeout")

        with (
            make_client(lambda _: httpx.Response(200, stream=Timeout())) as client,
            pytest.raises(TransientApiError, match="timed out"),
        ):
            client.get_cla_text(version=1)


class TestEvalPricing:
    def test_happy_path(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={
                    "amount_rao": 1_500_000_000,
                    "send_address": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                },
            )

        with make_client(handler) as client:
            result = client.get_eval_pricing()

        assert captured["method"] == "GET"
        assert captured["url"].endswith("/api/v1/upload/eval-pricing")
        assert isinstance(result, EvalPricingResponse)
        assert result.amount_rao == 1_500_000_000

    def test_non_200_raises_api_response_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(503, 3101, "oracle unreachable")

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_eval_pricing()

        assert "503" in str(e.value)
        assert "3101" in str(e.value)


class TestUploadCheck:
    def _body(self) -> UploadCheckRequest:
        return UploadCheckRequest(
            hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            sha256="ab" * 32,
            file_size_bytes=1024,
            signature="cd" * 64,
            signature_timestamp=1_798_000_000,
            signature_nonce=UUID("123e4567-e89b-42d3-a456-426614174000"),
        )

    def test_happy_path_returns_response_with_ok_false_payload(self) -> None:
        """ok=False still returns 200 because the server intentionally
        surfaces every rejection in one round trip. The CLI does NOT
        raise on that path; the orchestrator decides."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"ok": False, "error_codes": [1100], "messages": ["bad sig"]},
            )

        with make_client(handler) as client:
            result = client.post_upload_check(self._body())

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/api/v1/upload/check")
        assert captured["body"]["sha256"] == "ab" * 32
        assert result.ok is False
        assert result.error_codes == [1100]

    def test_non_200_raises_pre_check_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(503, 3001, "validation failure")

        with make_client(handler) as client, pytest.raises(PreCheckRejectedError):
            client.post_upload_check(self._body())

    @pytest.mark.parametrize(
        ("error_code", "error_type"),
        [
            (3203, PaymentAmountMismatchError),
            (3207, PaymentReplayedError),
            (3208, PaymentRecoveryExpiredError),
        ],
    )
    def test_definitive_saved_payment_rejections_are_typed(
        self,
        error_code: int,
        error_type: type[PreCheckRejectedError],
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(402, error_code, "receipt cannot be reused")

        with make_client(handler) as client, pytest.raises(error_type):
            client.post_upload_check(self._body())


class TestUploadAgent:
    def _kwargs(self) -> dict[str, Any]:
        return {
            "agent_tar": io.BytesIO(b"FAKETARBYTES"),
            "agent_tar_filename": "agent.tar.gz",
            "hotkey": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            "sha256": "ab" * 32,
            "name": "smoke-agent",
            "signature": "cd" * 64,
            "signature_timestamp": 1_798_000_000,
            "signature_nonce": UUID("123e4567-e89b-42d3-a456-426614174000"),
            "payment": PaymentReceipt(
                block_hash="0x" + "ef" * 32,
                block_number=42,
                extrinsic_index=3,
            ),
            "admission_token": UUID("22222222-2222-2222-2222-222222222222"),
        }

    def test_multipart_shape_matches_server_form_fields(self) -> None:
        """The server's Form() declarations (upload.py:156-173) name the
        exact fields we must send. Drift = broken upload."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            captured["timeout"] = request.extensions["timeout"]
            return httpx.Response(
                200,
                json={
                    "agent_id": "11111111-1111-1111-1111-111111111111",
                    "version": 2,
                    "status": "uploaded",
                },
            )

        with make_client(handler) as client:
            result = client.post_upload_agent(**self._kwargs())

        assert "multipart/form-data" in captured["content_type"]
        assert captured["timeout"]["read"] == 180.0
        assert b"22222222-2222-2222-2222-222222222222" in captured["body"]

        body_str = captured["body"].decode("latin-1")
        for field in (
            "hotkey",
            "sha256",
            "name",
            "signature",
            "signature_timestamp",
            "signature_nonce",
            "payment_block_hash",
            "payment_block_number",
            "payment_extrinsic_index",
            "agent_tar",
        ):
            assert f'name="{field}"' in body_str, f"missing field {field!r} in body"

        assert str(result.agent_id) == "11111111-1111-1111-1111-111111111111"
        assert result.version == 2

    def test_non_200_after_payment_raises_upload_agent_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(402, 3207, "payment already used")

        with (
            make_client(handler) as client,
            pytest.raises(UploadAgentRejectedError) as e,
        ):
            client.post_upload_agent(**self._kwargs())

        assert "3207" in str(e.value)

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_retryable_status_raises_transient_error(self, status: int) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(status, 3001, "retry shortly")

        with make_client(handler) as client, pytest.raises(TransientApiError):
            client.post_upload_agent(**self._kwargs())

    def test_submission_cooldown_exposes_retry_delay(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(
                429,
                1105,
                "owner coldkey may submit again at 2026-07-24T12:30:00Z",
                headers={"Retry-After": "1800"},
            )

        with (
            make_client(handler) as client,
            pytest.raises(SubmissionCooldownError) as info,
        ):
            client.post_upload_agent(**self._kwargs())

        assert info.value.retry_after_seconds == 1800
        assert "2026-07-24T12:30:00Z" in str(info.value)


class TestAgentStatus:
    def test_happy_path(self) -> None:
        agent_id = UUID("11111111-1111-1111-1111-111111111111")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith(
                f"/api/v1/retrieval/agent/{agent_id}/status"
            )
            return httpx.Response(
                200,
                json={
                    "agent_id": str(agent_id),
                    "status": "rejected",
                    "screening_reason": "Remove the bundled credential and resubmit",
                },
            )

        with make_client(handler) as client:
            result = client.get_agent_status(agent_id=agent_id)

        assert result.agent_id == agent_id
        assert result.screening_reason == "Remove the bundled credential and resubmit"

    def test_404_with_1200_raises_agent_not_found(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(404, 1200, "agent not found")

        with make_client(handler) as client, pytest.raises(AgentNotFoundError):
            client.get_agent_status(agent_id=UUID(int=0))

    def test_404_with_other_code_raises_generic(self) -> None:
        """A 404 with an unexpected envelope falls through to the
        generic ApiResponseError; we don't want to silently mask
        unknown envelope codes as agent-not-found."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(404, 9999, "unknown")

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        assert not isinstance(e.value, AgentNotFoundError)


class TestAgentByHotkey:
    HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"

    def test_happy_path_passes_query_param(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "agent_id": "11111111-1111-1111-1111-111111111111",
                    "miner_hotkey": self.HOTKEY,
                    "name": "alpha",
                    "version": 2,
                    "status": "uploaded",
                    "sha256": "ab" * 32,
                    "created_at": "2026-06-15T12:00:00Z",
                    "screening_reason": "Remove the bundled credential and resubmit",
                },
            )

        with make_client(handler) as client:
            result = client.get_agent_by_hotkey(miner_hotkey=self.HOTKEY)

        assert captured["query"] == {"miner_hotkey": self.HOTKEY}
        assert result.miner_hotkey == self.HOTKEY
        assert result.version == 2
        assert result.screening_reason == "Remove the bundled credential and resubmit"

    def test_404_with_1201_raises_hotkey_not_found(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(404, 1201, "no agent for hotkey")

        with make_client(handler) as client, pytest.raises(HotkeyAgentNotFoundError):
            client.get_agent_by_hotkey(miner_hotkey=self.HOTKEY)


class TestOwnerLink:
    HOTKEY_A = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    HOTKEY_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    COLDKEY_B = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"
    NONCE = "2f1d5f7a-8c4b-4a2e-9f01-6b3c8d5e7a90"

    def _body(self) -> OwnerLinkRequest:
        return OwnerLinkRequest(
            netuid=118,
            hotkey_a=self.HOTKEY_A,
            hotkey_b=self.HOTKEY_B,
            nonce=UUID(self.NONCE),
            issued_at=datetime(2026, 7, 26, 15, 4, 5, 123456, tzinfo=UTC),
            proof_a=OwnerLinkProof(
                key_kind="hotkey",
                signer=self.HOTKEY_A,
                signature="aa" * 64,
            ),
            proof_b=OwnerLinkProof(
                key_kind="coldkey",
                signer=self.COLDKEY_B,
                signature="bb" * 64,
            ),
        )

    def _ok_payload(self) -> dict[str, Any]:
        return {
            "attestation_id": "22222222-2222-2222-2222-222222222222",
            "netuid": 118,
            "hotkey_lo": self.HOTKEY_B,
            "hotkey_hi": self.HOTKEY_A,
            "evidence_grade": "mixed",
            "created_at": "2026-07-26T15:04:06Z",
            "cleared_copy_review_count": 2,
            "scope": "plagiarism-screening-only",
            "grants_additional_emission_slot": False,
        }

    def test_happy_path_pins_method_path_and_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json=self._ok_payload())

        with make_client(handler) as client:
            result = client.post_owner_link(self._body())

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/api/v1/attestations/owner-link")
        assert captured["body"] == {
            "netuid": 118,
            "hotkey_a": self.HOTKEY_A,
            "hotkey_b": self.HOTKEY_B,
            "nonce": self.NONCE,
            "issued_at": "2026-07-26T15:04:05.123456Z",
            "proof_a": {
                "key_kind": "hotkey",
                "signer": self.HOTKEY_A,
                "signature": "aa" * 64,
            },
            "proof_b": {
                "key_kind": "coldkey",
                "signer": self.COLDKEY_B,
                "signature": "bb" * 64,
            },
        }
        assert isinstance(result, OwnerLinkResponse)
        assert result.evidence_grade == "mixed"
        assert result.scope == "plagiarism-screening-only"
        assert result.cleared_copy_review_count == 2
        assert result.grants_additional_emission_slot is False

    def test_200_is_also_accepted(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._ok_payload())

        with make_client(handler) as client:
            result = client.post_owner_link(self._body())

        assert str(result.attestation_id).startswith("22222222")

    def test_rejection_raises_attestation_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _envelope_response(400, 1300, "attestation has expired")

        with (
            make_client(handler) as client,
            pytest.raises(AttestationRejectedError) as e,
        ):
            client.post_owner_link(self._body())

        assert "400" in str(e.value)
        assert "expired" in str(e.value)


class TestTransportErrors:
    """Transport-level failures must surface as :class:`ApiResponseError`
    with a friendly message, not raw ``httpx`` tracebacks.

    The mock transport's handler raises the relevant ``httpx`` exception
    so the request never gets a response; ``_request`` is the
    translation point.
    """

    def test_connect_error_maps_to_api_response_error(self) -> None:
        """API is down. Miner should see a single friendly line, not a
        50-line traceback."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        msg = str(e.value)
        assert "api unreachable" in msg
        assert "http://test" in msg
        assert "Hint" in msg or "hint" in msg

    def test_timeout_maps_to_api_response_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout", request=request)

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_eval_pricing()

        assert "timed out" in str(e.value)
        assert "http://test" in str(e.value)

    def test_other_request_error_maps_to_api_response_error(self) -> None:
        """Catch-all path: DNS / TLS / other ``httpx.RequestError``
        subclasses surface as ApiResponseError too, so the orchestrator
        only ever needs to catch one symbol."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.UnsupportedProtocol("bad scheme", request=request)

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        # Specifically NOT the more-specific subclasses.
        assert not isinstance(e.value, HotkeyAgentNotFoundError)
        assert not isinstance(e.value, AgentNotFoundError)
        assert "http://test" in str(e.value)

    def test_connect_error_chained_for_postmortem(self) -> None:
        """The original httpx exception is chained via ``from e`` so a
        ``-v`` run can still inspect the underlying cause without losing
        the friendly message."""
        original = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal original
            original = httpx.ConnectError("refused", request=request)
            raise original

        with make_client(handler) as client, pytest.raises(ApiResponseError) as e:
            client.get_agent_status(agent_id=UUID(int=0))

        assert isinstance(e.value.__cause__, httpx.ConnectError)


class TestRevokeMinerSession:
    def test_invalid_or_expired_session_is_login_required(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"detail": "miner session is invalid or expired"}
            )

        with make_client(handler) as client, pytest.raises(LoginRequiredError):
            client.revoke_miner_session("ditto_ms_" + "ab" * 32)

    def test_other_failures_stay_login_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "unavailable"})

        with make_client(handler) as client, pytest.raises(LoginRejectedError):
            client.revoke_miner_session("ditto_ms_" + "ab" * 32)

    def test_success_revokes_without_error(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(204)

        with make_client(handler) as client:
            client.revoke_miner_session("ditto_ms_" + "ab" * 32)

        assert paths == ["/api/v1/miner-auth/session/revoke"]
