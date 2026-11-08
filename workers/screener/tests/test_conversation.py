import json
from uuid import uuid4

import anthropic
import httpx
import httpx2
import pytest
from pydantic import ValidationError

from ditto_screener.conversation import (
    JUDGE_EFFORT,
    OPENROUTER_MODEL,
    AssessmentFailure,
    JudgeMeter,
    Limits,
    MemoryHarness,
    OpusExaminer,
    evaluate,
)
from ditto_screening_protocol.conversation import (
    DIMENSIONS,
    INSTRUMENT_V1,
    INSTRUMENT_V2,
    JUDGE_MODEL_V1,
    JUDGE_MODEL_V2,
    RUBRIC_PROMPT_V2,
    RUBRIC_SHA256_V1,
    ConversationClaim,
    ConversationReport,
    GradeSheet,
    proposed_quality_micros,
)
from ditto_screening_protocol.conversation_story import story, story_digest

SEED = "a" * 64


def grades(score=4, empty=()):
    return {
        "probes": [
            {
                "turn_id": i,
                "dimension": DIMENSIONS[(i - 11) % 5],
                "score": 0 if i in empty else score,
                "quote": "" if i in empty else f"Answer for turn {i}",
                "rationale": "The observed reply meets the stated expectation.",
            }
            for i in range(11, 31)
        ]
    }


class Rig:
    def __init__(
        self,
        *,
        judge_script=("ok",),
        seed_failure=False,
        run_timeouts=(),
        long_answer_turn=None,
        usage=None,
    ):
        self.turns = []
        self.seeds = []
        self.judge_bodies = []
        self.judge_headers = []
        self.judge_script = list(judge_script)
        self.seed_failure = seed_failure
        self.run_timeouts = set(run_timeouts)
        self.long_answer_turn = long_answer_turn
        self.usage = usage or {"input_tokens": 40_000, "output_tokens": 7_000}

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
        turn = len(self.turns)
        if turn in self.run_timeouts:
            raise httpx.ReadTimeout("harness took too long", request=request)
        text = f"Answer for turn {turn}"
        if turn == self.long_answer_turn:
            text += " | screwdriver set" * 2_000
        return httpx.Response(200, json={"final_text": text, "prompt_tokens": 9})

    def judge(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/v1/messages"
        self.judge_headers.append(dict(request.headers))
        body = json.loads(request.content)
        self.judge_bodies.append(body)
        step = self.judge_script.pop(0) if self.judge_script else "ok"
        if step == "rate_limited":
            return httpx2.Response(429, json={"error": {"message": "slow down"}})
        sheet = grades(empty=self.empty_turns())
        stop = "end_turn"
        if step == "bad_quote":
            sheet["probes"][0]["quote"] = "This never appeared in the transcript"
        if step == "refusal":
            stop = "refusal"
        usage = dict(self.usage)
        return httpx2.Response(
            200,
            json={
                "id": f"gen-{len(self.judge_bodies)}",
                "type": "message",
                "role": "assistant",
                "model": OPENROUTER_MODEL,
                "stop_reason": stop,
                "usage": usage,
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "opaque"},
                    {"type": "text", "text": "```json\n" + json.dumps(sheet) + "\n```"},
                ],
            },
        )

    def empty_turns(self):
        return {t for t in self.run_timeouts if t >= 11}

    async def run(self, limits=None):
        limits = limits or Limits()
        judge = anthropic.AsyncAnthropic(
            auth_token="judge-only",
            api_key=None,
            base_url="https://openrouter.ai/api",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self.judge)
            ),
        )
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(self.harness)
            ) as harness_client,
            judge,
        ):
            return await evaluate(
                assessment_id=uuid4(),
                agent_id=uuid4(),
                artifact_sha256="b" * 64,
                screened_image_sha256="c" * 64,
                bench_version=17,
                seed=SEED,
                harness=MemoryHarness(harness_client, "http://127.0.0.1:8080", limits),
                examiner=OpusExaminer(judge, limits),
            )


async def test_complete_flow_one_opus_request_with_tariff_spend():
    rig = Rig()
    report = await rig.run()
    assert report.status == "completed", report.error_code
    assert report.instrument == INSTRUMENT_V2 and report.model == JUDGE_MODEL_V2
    assert len(rig.turns) == 30 and len(rig.seeds) == 31
    assert len(rig.judge_bodies) == 1 and report.judge_requests == 1
    body = rig.judge_bodies[0]
    assert body["model"] == OPENROUTER_MODEL
    assert body["system"] == RUBRIC_PROMPT_V2
    assert body["output_config"] == {"effort": JUDGE_EFFORT}
    assert "tools" not in body and "tool_choice" not in body
    assert rig.judge_headers[0]["authorization"] == "Bearer judge-only"
    evidence = json.loads(body["messages"][0]["content"].split("instruction.\n", 1)[1])
    assert [e["turn_id"] for e in evidence["exchanges"]] == list(range(1, 31))
    assert [p["turn_id"] for p in evidence["probes"]] == list(range(11, 31))
    # $4/M input + $20/M output, exact list tariff: 0.16 + 0.14 USD.
    assert report.spent_microusd == 300_000
    assert report.judge_cost_basis == "tariff"
    assert not report.judge_cost_is_upper_bound and not report.unmetered
    assert report.input_tokens == 40_000 and report.output_tokens == 7_000
    assert report.conversation_micros() == 1_000_000
    assert "opaque" not in report.model_dump_json()
    # The harness never sees private expectations or the judge's dimension.
    for turn in rig.turns:
        assert "private_expectation" not in json.dumps(turn)
        assert "dimension" not in turn
    for i, (turn, seed) in enumerate(zip(rig.turns, rig.seeds[1:], strict=True)):
        assert seed["pairs"][0]["prompt"] == turn["user_input"]
        history = json.loads(turn["system_prompt"].split("instructions:\n")[1])
        assert len(history) == i % 3


async def test_billed_cost_is_preferred_and_labelled():
    rig = Rig(usage={"input_tokens": 40_000, "output_tokens": 7_000, "cost": 0.2875})
    report = await rig.run()
    assert report.status == "completed"
    assert report.spent_microusd == 287_500 and report.judge_cost_basis == "billed"


async def test_cache_tokens_are_priced_at_their_own_rates():
    rig = Rig(
        usage={
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 10_000,
            "cache_read_input_tokens": 30_000,
            "output_tokens": 5_000,
        }
    )
    report = await rig.run()
    # 1000*4 + 10000*5 + 30000*0.2 + 5000*20 microUSD-per-M = 160,000.
    assert report.spent_microusd == 4_000 + 50_000 + 6_000 + 100_000
    assert report.input_tokens == 41_000


async def test_rate_limit_is_retried_unbilled():
    rig = Rig(judge_script=("rate_limited", "ok"))
    report = await rig.run()
    assert report.status == "completed"
    assert report.judge_requests == 2 and report.spent_microusd == 300_000


async def test_bad_quote_gets_one_stateless_repair():
    rig = Rig(judge_script=("bad_quote", "ok"))
    report = await rig.run()
    assert report.status == "completed"
    assert report.judge_requests == 2 and report.spent_microusd == 600_000
    repair = rig.judge_bodies[1]["messages"]
    assert len(repair) == 1 and "failed host validation" in repair[0]["content"]
    assert '"type": "thinking"' not in json.dumps(repair)  # No block replay.


@pytest.mark.parametrize(
    "script,reason",
    [
        (("bad_quote",) * 3, "judge_invalid_grade"),
        (("refusal",), "judge_refused"),
        (("rate_limited",) * 3, "judge_unavailable"),
    ],
)
async def test_unverifiable_grading_never_scores(script, reason):
    report = await Rig(judge_script=script).run()
    assert report.status == "incomplete"
    assert report.error_code == reason
    assert report.conversation_micros() is None


async def test_a_slow_turn_is_an_empty_reply_not_an_aborted_run():
    rig = Rig(run_timeouts=(13,))
    report = await rig.run()
    assert report.status == "completed", report.error_code
    failed = report.exchanges[12]
    assert failed.failure == "harness_run_timeout" and failed.assistant == ""
    assert len(rig.turns) == 30 and len(rig.seeds) == 30  # No ingest of nothing.
    evidence = rig.judge_bodies[0]["messages"][0]["content"]
    assert '"reply_failed": true' in evidence
    assert next(p for p in report.grades.probes if p.turn_id == 13).score == 0


async def test_three_consecutive_failures_stop_without_paying_the_judge():
    rig = Rig(run_timeouts=(4, 5, 6))
    report = await rig.run()
    assert report.error_code == "harness_unresponsive"
    assert len(rig.turns) == 6 and not rig.judge_bodies
    assert report.spent_microusd == 0


async def test_runaway_answer_is_truncated_and_graded():
    rig = Rig(long_answer_turn=13)
    report = await rig.run()
    assert report.status == "completed", report.error_code
    long = report.exchanges[12]
    assert long.assistant_truncated and len(long.assistant) == 8_000
    assert len(rig.seeds[13]["pairs"][0]["response"]) == 8_000


async def test_unacknowledged_ingest_is_recorded_and_the_run_continues():
    rig = Rig(seed_failure=True)
    report = await rig.run()
    # The empty-graph seed is acknowledged (0 pairs) so the run starts; every
    # later ingest reports 0 pairs and is recorded as a seed failure.
    assert report.error_code == "harness_unresponsive"
    assert [e.failure for e in report.exchanges] == ["harness_seed_failed"] * 3


async def test_budget_prevents_judge_call_before_spend():
    rig = Rig()
    report = await rig.run(Limits(judge_microusd=1))
    assert report.error_code == "judge_cost_limit"
    assert not rig.turns and not rig.judge_bodies and report.spent_microusd == 0


def test_unknown_usage_keeps_reservation_and_fails_closed():
    meter = JudgeMeter(Limits())
    reservation = meter.reserve({"max_tokens": 100})
    with pytest.raises(AssessmentFailure, match="usage_unverifiable"):
        meter.reconcile({"usage": None}, reservation, 100)
    assert meter.spent == reservation[0]


def test_wrong_model_is_rejected():
    meter = JudgeMeter(Limits())
    reservation = meter.reserve({"max_tokens": 100})
    with pytest.raises(AssessmentFailure, match="model_mismatch"):
        meter.reconcile(
            {"model": "openai/gpt-6-astra", "usage": {"input_tokens": 1}},
            reservation,
            100,
        )


def test_byok_zero_router_fee_does_not_erase_provider_cost():
    meter = JudgeMeter(Limits())
    reservation = meter.reserve({"max_tokens": 100})
    meter.reconcile(
        {
            "model": "anthropic/claude-opus-5.5-20260921",
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
    assert meter.spent == 400 + 1_000 and meter.cost_is_upper_bound


def test_rates_cannot_understate_the_opus_price():
    with pytest.raises(ValueError, match="finite integers"):
        Limits(input_decimicros=float("nan"))
    with pytest.raises(ValueError, match="understate"):
        Limits(output_decimicros=50)


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


async def test_v2_rules_and_a_stored_v1_report_both_stay_verifiable():
    v2 = (await Rig().run()).model_dump(mode="json")
    for change in (
        {"model": JUDGE_MODEL_V1},
        {"judge_requests": 31},
        {"rubric_sha256": RUBRIC_SHA256_V1},
    ):
        with pytest.raises(ValidationError):
            ConversationReport.model_validate({**v2, **change})
    # A v1 report as Platform stored it before v2: Astra, 31 requests, v1 digest.
    v1 = {
        **v2,
        "instrument": INSTRUMENT_V1,
        "model": JUDGE_MODEL_V1,
        "rubric_sha256": RUBRIC_SHA256_V1,
        "provider_model": "openai/gpt-6-astra",
        "judge_requests": 31,
        "judge_cost_is_upper_bound": True,
    }
    v1.pop("judge_cost_basis")
    for exchange in v1["exchanges"]:
        exchange.pop("assistant_truncated")
        exchange.pop("failure")
    assert ConversationReport.model_validate(v1).conversation_micros() == 1_000_000
    with pytest.raises(ValidationError):
        ConversationReport.model_validate({**v1, "judge_requests": 1})


def test_claim_without_instrument_reads_as_v1():
    claim = {
        "assessment_id": str(uuid4()),
        "agent_id": str(uuid4()),
        "artifact_sha256": "a" * 64,
        "screened_image_sha256": "b" * 64,
        "bench_version": 17,
        "seed": SEED,
        "lease_token": str(uuid4()),
        "expires_at": "2026-11-08T03:00:00Z",
    }
    assert ConversationClaim.model_validate(claim).instrument == INSTRUMENT_V1
    claim["instrument"] = INSTRUMENT_V2
    assert ConversationClaim.model_validate(claim).instrument == INSTRUMENT_V2


async def test_failed_run_cannot_claim_a_truncated_reply():
    report = (await Rig(run_timeouts=(13,)).run()).model_dump(mode="json")
    report["exchanges"][12]["assistant_truncated"] = True
    with pytest.raises(ValidationError, match="failed run has no reply"):
        ConversationReport.model_validate(report)


class _MalformedUsageRig(Rig):
    def __init__(self, *, script):
        super().__init__()
        self.script = list(script)

    def judge(self, request):
        step = self.script.pop(0) if self.script else "ok"
        if step == "ok":
            return super().judge(request)
        self.judge_bodies.append(json.loads(request.content))
        if step == "rate_limited":
            return httpx2.Response(429, json={"error": {"message": "slow"}})
        usage = None if step == "no_usage" else {"input_tokens": "many"}
        return httpx2.Response(
            200,
            json={
                "model": OPENROUTER_MODEL,
                "stop_reason": "end_turn",
                "usage": usage,
                "content": [{"type": "text", "text": "{}"}],
            },
        )


@pytest.mark.parametrize(
    "script",
    [("no_usage",), ("bad_usage",), ("rate_limited", "no_usage")],
)
async def test_unreconciled_hold_is_labelled_an_upper_bound(script):
    report = await _MalformedUsageRig(script=script).run()
    assert report.status == "incomplete"
    assert report.error_code == "judge_usage_unverifiable"
    assert report.spent_microusd > 0  # The hold is retained...
    assert report.judge_cost_is_upper_bound  # ...and never presented as a bill.
    assert report.judge_cost_basis == "reservation"


async def test_released_errors_and_valid_settlement_stay_exact():
    report = await Rig(judge_script=("rate_limited", "bad_quote", "ok")).run()
    assert report.status == "completed" and report.judge_requests == 3
    assert report.spent_microusd == 600_000
    assert not report.judge_cost_is_upper_bound
    assert report.judge_cost_basis == "tariff"


def test_one_dollar_hold_bounds_judge_and_harness():
    from ditto_screener.conversation_relay import BUDGET
    from ditto_screening_protocol.conversation import (
        HARNESS_BUDGET_MICROUSD,
        JUDGE_BUDGET_MICROUSD,
        RUN_RESERVATION_MICROUSD,
    )

    assert RUN_RESERVATION_MICROUSD == 1_000_000
    assert JUDGE_BUDGET_MICROUSD + HARNESS_BUDGET_MICROUSD == RUN_RESERVATION_MICROUSD
    assert HARNESS_BUDGET_MICROUSD <= BUDGET
    meter = JudgeMeter(Limits(judge_microusd=JUDGE_BUDGET_MICROUSD))
    meter.preflight()  # One grading request fits the judge hold.


async def test_judge_hold_stops_a_repair_chain_as_a_budget_outcome():
    rig = Rig(judge_script=("bad_quote", "bad_quote", "ok"))
    report = await rig.run(Limits(judge_microusd=850_000))
    assert report.status == "incomplete"
    assert report.error_code == "judge_cost_limit"
    assert report.judge_requests == 2 and len(rig.judge_bodies) == 2
    assert report.spent_microusd == 600_000 <= report.reserved_microusd == 850_000


async def test_typical_grading_completes_inside_the_judge_hold():
    report = await Rig().run(Limits(judge_microusd=850_000))
    assert report.status == "completed"
    assert report.spent_microusd == 300_000 and report.reserved_microusd == 850_000


def test_worst_case_evidence_is_cut_to_fit_the_judge_hold():
    from ditto_screener.conversation import (
        JUDGE_EVIDENCE_BYTES,
        grade_evidence,
        judge_body,
        wire_bytes,
    )
    from ditto_screening_protocol.conversation import Exchange

    turns = story(SEED)
    exchanges = [
        Exchange(
            turn_id=t.turn_id,
            session=(t.turn_id - 1) // 3 + 1,
            user=t.user,
            # Quotes and newlines double when the body re-escapes them.
            assistant=('"\n' * 3_999) + "é!",
        )
        for t in turns
    ]
    evidence = grade_evidence(exchanges, turns)
    assert wire_bytes(evidence) <= JUDGE_EVIDENCE_BYTES
    shown = json.loads(evidence.split("instruction.\n", 1)[1])["exchanges"]
    assert all(e["truncated"] for e in shown)
    assert all(
        e["assistant"] and e["assistant"] in x.assistant
        for e, x in zip(shown, exchanges, strict=True)
    )
    meter = JudgeMeter(Limits(judge_microusd=850_000))
    meter.preflight()
    reservation, _ = meter.reserve(judge_body(evidence))
    assert reservation <= 850_000  # The request preflight priced is affordable.


def test_short_evidence_is_never_cut():
    from ditto_screener.conversation import grade_evidence
    from ditto_screening_protocol.conversation import Exchange

    turns = story(SEED)
    exchanges = [
        Exchange(
            turn_id=t.turn_id,
            session=(t.turn_id - 1) // 3 + 1,
            user=t.user,
            assistant="ok",
        )
        for t in turns
    ]
    shown = json.loads(grade_evidence(exchanges, turns).split("instruction.\n", 1)[1])
    assert not any(e["truncated"] for e in shown["exchanges"])
