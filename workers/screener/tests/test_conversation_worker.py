import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto_screener.conversation_worker import consume
from ditto_screening_protocol.conversation import (
    INSTRUMENT_V1,
    INSTRUMENT_V2,
    ConversationLaunch,
    ConversationReport,
    HarnessUsage,
)
from ditto_screening_protocol.conversation_story import story_digest


@pytest.mark.asyncio
async def test_failed_delivery_keeps_private_evidence_and_cannot_rebill_same_claim(
    tmp_path, monkeypatch
):
    key = tmp_path / "key"
    key.write_text("provider-only-secret")
    key.chmod(0o600)
    spool = tmp_path / "spool"
    monkeypatch.setenv("SCREENER_CONVERSATION_OPENROUTER_KEY_FILE", str(key))
    monkeypatch.setenv("SCREENER_CONVERSATION_SPOOL_DIR", str(spool))
    claim = ConversationLaunch(
        assessment_id=uuid4(),
        agent_id=uuid4(),
        artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        screened_image_id="sha256:" + "b" * 64,
        screened_image_url="https://artifact.example/image.tar",
        screened_image_size_bytes=1,
        seed="c" * 64,
        lease_token=uuid4(),
        bench_version=12,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        instrument=INSTRUMENT_V2,
    )
    calls = []

    class Platform:
        async def conversation_request(self, path, payload=None):
            calls.append((path, payload))
            if path == "/claim":
                return claim.model_dump(mode="json")
            raise RuntimeError("delivery unavailable")

    class Runtime:
        def __init__(self, *_args):
            pass

        async def start(self):
            return "http://127.0.0.1:8080"

        def transport(self, **_kwargs):
            return None

        async def stop(self):
            return HarnessUsage(
                profile="conversation-openrouter-oss20b-pplx768-v1",
                requests=0,
                tokens=0,
                spent_microusd=0,
                unmetered=False,
                failed=True,
                failure={"code": "unsupported_message_content", "stage": "request"},
            )

    evaluations = []

    async def evaluate(**kwargs):
        evaluations.append(kwargs)
        return ConversationReport(
            assessment_id=claim.assessment_id,
            agent_id=claim.agent_id,
            artifact_sha256=claim.artifact_sha256,
            screened_image_sha256=claim.screened_image_sha256,
            bench_version=12,
            story_sha256=story_digest(claim.seed),
            provider_model="anthropic/claude-opus-5.5",
            status="incomplete",
            error_code="judge_refused",
            exchanges=[],
            judge_requests=1,
            input_tokens=100,
            output_tokens=1,
            reserved_microusd=25000000,
            spent_microusd=1050,
        )

    monkeypatch.setattr(
        "ditto_screener.conversation_worker.ConversationRuntime", Runtime
    )
    monkeypatch.setattr("ditto_screener.conversation_worker.evaluate", evaluate)
    config = SimpleNamespace(require_rootless_docker=True)
    assert await consume(config, Platform())
    assert not await consume(config, Platform())
    assert len(evaluations) == 1
    report_path = spool / f"{claim.assessment_id}.report.json"
    assert report_path.stat().st_mode & 0o777 == 0o600
    saved = report_path.read_text()
    assert "provider-only-secret" not in saved and claim.seed not in saved
    assert json.loads(saved)["spent_microusd"] == 1050
    assert json.loads(saved)["harness_usage"]["failure"] == {
        "code": "unsupported_message_content",
        "stage": "request",
        "http_status": None,
    }
    assert json.loads(saved)["grades"] is None
    assert len([p for p, _ in calls if p.endswith("/result")]) == 1
    assert calls[0] == (
        "/claim",
        {"instruments": [INSTRUMENT_V2], "reservation_microusd": 1_000_000},
    )


@pytest.mark.asyncio
async def test_v1_claim_from_older_platform_frees_the_slot_without_running(
    tmp_path, monkeypatch
):
    key = tmp_path / "key"
    key.write_text("provider-only-secret")
    key.chmod(0o600)
    monkeypatch.setenv("SCREENER_CONVERSATION_OPENROUTER_KEY_FILE", str(key))
    monkeypatch.setenv("SCREENER_CONVERSATION_SPOOL_DIR", str(tmp_path / "spool"))
    claim = ConversationLaunch(
        assessment_id=uuid4(),
        agent_id=uuid4(),
        artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        screened_image_id="sha256:" + "b" * 64,
        screened_image_url="https://artifact.example/image.tar",
        screened_image_size_bytes=1,
        seed="c" * 64,
        lease_token=uuid4(),
        bench_version=17,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert claim.instrument == INSTRUMENT_V1
    results = []

    class Platform:
        async def conversation_request(self, path, payload=None):
            if path == "/claim":
                return claim.model_dump(mode="json", exclude={"instrument"})
            results.append(payload)
            return {}

    class Runtime:
        def __init__(self, *_args):
            pass

        async def start(self):
            raise AssertionError("a v1 claim must not start a sandbox")

    monkeypatch.setattr(
        "ditto_screener.conversation_worker.ConversationRuntime", Runtime
    )
    assert await consume(SimpleNamespace(require_rootless_docker=True), Platform())
    report = ConversationReport.model_validate(results[0]["report"])
    assert report.instrument == INSTRUMENT_V1 and report.status == "incomplete"
    assert report.error_code == "instrument_unsupported"
    assert report.spent_microusd == 0 and report.harness_usage is None


@pytest.mark.asyncio
async def test_exhausted_harness_hold_is_a_budget_outcome(tmp_path):
    from ditto_screener import conversation_worker as module

    usage = HarnessUsage(
        profile="conversation-openrouter-oss20b-pplx768-v1",
        requests=40,
        tokens=100,
        spent_microusd=149_000,
        unmetered=False,
        failed=True,
        failure={"code": "inference_budget_exhausted", "stage": "request"},
    )
    claim = ConversationLaunch(
        assessment_id=uuid4(),
        agent_id=uuid4(),
        artifact_sha256="a" * 64,
        screened_image_sha256="b" * 64,
        screened_image_id="sha256:" + "b" * 64,
        screened_image_url="https://artifact.example/image.tar",
        screened_image_size_bytes=1,
        seed="c" * 64,
        lease_token=uuid4(),
        bench_version=17,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        instrument=INSTRUMENT_V2,
    )
    delivered = []

    class Platform:
        async def conversation_request(self, _path, payload=None):
            delivered.append(payload)
            return {}

    report = module._empty_report(claim, "harness_unresponsive")
    spool = tmp_path / "spool"
    spool.mkdir()
    await module._deliver(Platform(), spool, claim, report, usage)
    sent = ConversationReport.model_validate(delivered[0]["report"])
    assert sent.error_code == "harness_budget_exhausted"
    assert sent.reserved_microusd == 850_000
