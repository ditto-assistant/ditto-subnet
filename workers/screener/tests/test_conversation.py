import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from ditto_screener.conversation import (
    AssessmentFailure,
    AstraExaminer,
    JudgeMeter,
    Limits,
    MemoryHarness,
    evaluate,
)
from ditto_screening_protocol.conversation import (
    DIMENSIONS,
    JUDGE_MODEL,
    ConversationReport,
    GradeSheet,
    proposed_quality_micros,
)
from ditto_screening_protocol.conversation_story import story, story_digest

SEED = "a" * 64


def grades(score=4):
    return {
        "probes": [
            {
                "turn_id": i,
                "dimension": DIMENSIONS[(i - 11) % 5],
                "score": score,
                "quote": f"Answer for turn {i}",
                "rationale": "The observed reply meets the stated expectation.",
            }
            for i in range(11, 31)
        ]
    }


class Rig:
    def __init__(self, *, malformed_grade=False, seed_failure=False, refused=False):
        self.turns = []
        self.seeds = []
        self.judge_calls = 0
        self.malformed_grade = malformed_grade
        self.seed_failure = seed_failure
        self.refused = refused

    def harness(self, request):
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        if request.url.path == "/seed":
            self.seeds.append(body)
            return httpx.Response(
                200,
                json={
                    "pairs": 0 if self.seed_failure else len(body["pairs"]),
                    "subjects": 0,
                    "links": 0,
                },
            )
        self.turns.append(body)
        return httpx.Response(
            200,
            json={
                "final_text": f"Answer for turn {len(self.turns)}",
                "prompt_tokens": 999999999,
            },
        )

    def judge(self, request):
        self.judge_calls += 1
        body = json.loads(request.content)
        assert body["model"] == JUDGE_MODEL
        assert body["store"] is False
        assert (
            len([item for item in body["input"] if item.get("type") == "reasoning"])
            == self.judge_calls - 1
        )
        if "tools" in body:
            assert [t["name"] for t in body["tools"]] == ["converse"]
            output = [
                {
                    "type": "function_call",
                    "name": "converse",
                    "call_id": f"call-{self.judge_calls}",
                    "arguments": json.dumps({"turn_id": self.judge_calls}),
                }
            ]
        else:
            sheet = grades()
            if self.malformed_grade:
                sheet["probes"][0]["quote"] = "This never appeared in the transcript"
            output = [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(sheet)}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "status": "incomplete" if self.refused else "completed",
                "model": JUDGE_MODEL,
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "output": [
                    {
                        "type": "reasoning",
                        "id": f"rs-{self.judge_calls}",
                        "summary": [],
                        "encrypted_content": "opaque-judge-state",
                    },
                    *output,
                ],
            },
        )

    async def run(self, limits=None):
        limits = limits or Limits()
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(self.harness)
            ) as harness_client,
            httpx.AsyncClient(
                transport=httpx.MockTransport(self.judge),
                headers={"Authorization": "Bearer judge-only"},
            ) as judge_client,
        ):
            return await evaluate(
                assessment_id=uuid4(),
                agent_id=uuid4(),
                artifact_sha256="b" * 64,
                screened_image_sha256="c" * 64,
                bench_version=13,
                seed=SEED,
                harness=MemoryHarness(harness_client, "http://127.0.0.1:8080", limits),
                examiner=AstraExaminer(judge_client, limits),
            )


async def test_complete_http_flow_memory_boundaries_and_metering():
    rig = Rig()
    report = await rig.run()
    assert report.status == "completed"
    assert len(rig.turns) == 30 and len(rig.seeds) == 31 and rig.judge_calls == 31
    assert report.conversation_micros() == 1_000_000
    assert report.spent_microusd == 31 * (100 * 10 + 50 * 50)
    assert report.input_tokens == 3100  # Never trusts harness-reported usage.
    assert "opaque-judge-state" not in report.model_dump_json()
    assert rig.seeds[0]["pairs"] == []
    assert len({seed["user_id"] for seed in rig.seeds}) == 1
    assert len({seed["pairs"][0]["session_id"] for seed in rig.seeds[1:]}) == 10
    for i, (turn, seed) in enumerate(zip(rig.turns, rig.seeds[1:], strict=True)):
        assert seed["pairs"][0]["prompt"] == turn["user_input"]
        assert seed["pairs"][0]["response"] == f"Answer for turn {i + 1}"
        history = json.loads(turn["system_prompt"].split("instructions:\n")[1])
        assert len(history) == i % 3
        if i % 3 == 0:
            assert history == []  # No old conversation replay at a session boundary.
        assert "private_expectation" not in turn
        assert "seed" not in turn and "dimension" not in turn


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"malformed_grade": True}, "grade_evidence_invalid"),
        ({"seed_failure": True}, "memory_ingest_not_acknowledged"),
        ({"refused": True}, "judge_incomplete"),
    ],
)
async def test_partial_or_unverifiable_evidence_never_scores(kwargs, reason):
    report = await Rig(**kwargs).run()
    assert report.status == "incomplete"
    assert report.error_code == reason
    assert report.conversation_micros() is None


async def test_budget_prevents_call_before_spend():
    rig = Rig()
    report = await rig.run(Limits(judge_microusd=1))
    assert report.error_code == "judge_cost_limit"
    assert not rig.turns and rig.judge_calls == 0


def test_unknown_usage_keeps_reservation_and_fails_closed():
    meter = JudgeMeter(Limits())
    reservation = meter.reserve({"max_output_tokens": 100})
    with pytest.raises(AssessmentFailure, match="usage_unverifiable"):
        meter.reconcile({"usage": {}}, reservation, 100)
    assert meter.spent == reservation[0]
    assert meter.unmetered


def test_byok_zero_router_fee_does_not_erase_provider_cost():
    meter = JudgeMeter(Limits())
    reservation = meter.reserve({"max_output_tokens": 100})
    meter.reconcile(
        {
            "model": JUDGE_MODEL,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cost": 0,
                "is_byok": True,
            },
        },
        reservation,
        100,
    )
    assert meter.spent == 3500 and meter.cost_is_upper_bound
    assert not meter.unmetered


def test_nonfinite_prices_cannot_disable_cost_enforcement():
    with pytest.raises(ValueError, match="finite integers"):
        Limits(input_microusd_per_token=float("nan"))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:8080",
        "http://127.0.0.1@evil.com",
        "http://127.0.0.1:8080/private",
        "http://169.254.169.254",
    ],
)
def test_harness_is_loopback_origin_only(url):
    with pytest.raises(ValueError, match="loopback"):
        MemoryHarness(None, url, Limits())


def test_seeded_story_has_uniform_coverage_and_real_token_pressure():
    first = story(SEED)
    assert first == story(SEED)
    assert first != story("d" * 64)
    assert story_digest(SEED) != story_digest("d" * 64)
    assert len(first) == 30
    assert sum(len(turn.user.split()) for turn in first[:10]) >= 4000
    assert [turn.dimension for turn in first[10:]] == list(DIMENSIONS) * 4
    with pytest.raises(ValueError):
        story("public-seed")


def test_grade_integrity_and_host_composite():
    sheet = GradeSheet.model_validate(grades(3))
    assert sheet.conversation_micros() == 750000
    assert proposed_quality_micros(900000, 300000) == 700000
    duplicate = grades()
    duplicate["probes"][0] = duplicate["probes"][1]
    with pytest.raises(ValidationError, match="exactly once"):
        GradeSheet.model_validate(duplicate)
    malformed = grades()
    malformed["probes"][0]["score"] = True
    with pytest.raises(ValidationError):
        GradeSheet.model_validate(malformed)
    with pytest.raises(ValueError):
        proposed_quality_micros(1000001, 0)


async def test_report_unknown_fields_are_not_authoritative():
    report = await Rig().run()
    data = report.model_dump(mode="json")
    data["conversation_score"] = 0
    data["future_field"] = "ignored"
    parsed = ConversationReport.model_validate(data)
    assert parsed.conversation_micros() == 1000000
    assert "conversation_score" not in parsed.model_dump()
