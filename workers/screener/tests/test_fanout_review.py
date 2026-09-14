import asyncio
import hashlib
import json

import httpx
import pytest

from ditto_screener.fanout_review import (
    ALLOWED_RESPONSE_MODELS,
    FOCI,
    ExperimentalReviewer,
    FanoutBudget,
    FanoutBudgetExhausted,
    _normalize_candidate_adjudications,
    _normalize_final_adjudication,
    _provisional_candidate_basis,
    _provisional_finding,
    plan_file_groups,
    review_archive,
)
from ditto_screener.source_review import TarSourceRepository


async def test_atomic_request_reservations_cannot_oversubscribe_tokens_or_cost():
    budget = FanoutBudget(
        max_requests=40,
        max_total_tokens=10_000,
        max_reported_cost_usd=0.3,
    )

    async def reserve():
        await budget.before_request(
            input_token_bound=1_000, completion_token_bound=2_400
        )

    results = await asyncio.gather(
        *(reserve() for _ in range(8)), return_exceptions=True
    )
    assert sum(result is None for result in results) == 2
    assert all(
        result is None or isinstance(result, FanoutBudgetExhausted)
        for result in results
    )
    usage = budget.snapshot()
    assert usage["requests"] == 2
    assert usage["reserved_tokens"] == 6_800
    assert usage["reserved_cost_usd"] <= 0.3


async def test_missing_metering_stops_later_request_admission():
    budget = FanoutBudget(
        max_requests=4,
        max_total_tokens=100_000,
        max_reported_cost_usd=3,
    )
    reservation = await budget.before_request(
        input_token_bound=1_000, completion_token_bound=2_400
    )
    await budget.record_response(
        {"usage": {"prompt_tokens": 10}}, reservation_id=reservation
    )
    with pytest.raises(FanoutBudgetExhausted, match="metering unavailable"):
        await budget.before_request(
            input_token_bound=1_000, completion_token_bound=2_400
        )


async def test_response_model_mismatch_stops_later_request_admission():
    budget = FanoutBudget(
        max_requests=4,
        max_total_tokens=100_000,
        max_reported_cost_usd=3,
    )
    reservation = await budget.before_request(
        input_token_bound=1_000, completion_token_bound=2_400
    )
    await budget.record_response(
        {
            "model": "router-selected-different-model",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "cost": 0.001,
            },
        },
        reservation_id=reservation,
    )
    with pytest.raises(FanoutBudgetExhausted, match="response model changed"):
        await budget.before_request(
            input_token_bound=1_000, completion_token_bound=2_400
        )


@pytest.mark.parametrize("response_model", sorted(ALLOWED_RESPONSE_MODELS))
async def test_verified_router_response_model_ids_are_accepted(response_model):
    budget = FanoutBudget(
        max_requests=4,
        max_total_tokens=100_000,
        max_reported_cost_usd=3,
    )
    reservation = await budget.before_request(
        input_token_bound=1_000, completion_token_bound=2_400
    )
    await budget.record_response(
        {
            "model": response_model,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "cost": 0.001,
            },
        },
        reservation_id=reservation,
    )
    await budget.before_request(input_token_bound=1_000, completion_token_bound=2_400)
    assert budget.snapshot()["model_mismatch"] is False


async def test_default_envelope_fits_specialists_file_groups_and_critic_first_turns():
    """Five specialists, four file groups, and one critic fit the pilot bounds."""
    budget = FanoutBudget(
        max_requests=40,
        max_total_tokens=750_000,
        max_reported_cost_usd=3,
    )
    for _ in range(10):
        reservation = await budget.before_request(
            input_token_bound=64_000, completion_token_bound=2_400
        )
        await budget.record_response(
            {
                "model": "z-ai/glm-5.3-flash",
                "usage": {
                    "prompt_tokens": 16_000,
                    "completion_tokens": 1_200,
                    "cost": 0.01,
                },
            },
            reservation_id=reservation,
        )
    usage = budget.snapshot()
    assert usage["requests"] == 10
    assert usage["reserved_tokens"] == 172_000
    assert usage["reserved_cost_usd"] < 3
    assert usage["unmetered_responses"] == 0


@pytest.mark.parametrize(
    "critic_risk,expected",
    [("low", "unresolved_candidate"), ("high", "critic_also_flagged")],
)
async def test_single_specialist_survives_majority_and_transcripts_are_independent(
    tmp_path, critic_risk, expected
):
    from .test_source_review import _archive

    active = peak = 0
    instances = []

    class Reviewer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.usage = {
                "requests": 1,
                "reported_cost_usd": 0.01,
                "unmetered_requests": 0,
            }
            self.response_models = {"test-model"}
            self.opened_paths = set()
            instances.append(self)

        async def review_provisional(self, *_args, **_kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            risk = "high" if FOCI["benchmark_engine"] in self.kwargs["focus"] else "low"
            return {
                "raw_review": {
                    "risk_level": risk,
                    "categories": ["benchmark_emulation"]
                    if risk == "high"
                    else ["none"],
                    "evidence": [],
                    "invariants": [],
                    "summary": "Provisional specialist note.",
                },
                "notes": [],
                "inspection_complete": True,
            }

        async def adjudicate_review(
            self, _archive_path, *, candidates, all_pass_summaries, **_kwargs
        ):
            assert len(all_pass_summaries) == 5
            assert {row["outcome"] for row in all_pass_summaries} == {"provisional"}
            disposition = "supported" if critic_risk == "high" else "unresolved"
            return {
                "revision": "fanout-adjudicator-v2",
                "outcome": expected,
                "final_review": {"risk_level": critic_risk},
                "clearance_certified": critic_risk == "low",
                "evidence_verified": True,
                "candidate_assessments": [
                    {
                        "candidate_id": row["candidate_id"],
                        "source_pass": row["source_pass"],
                        "disposition": disposition,
                        "supporting_evidence": [],
                        "counterevidence": [],
                        "summary": "Bounded test assessment.",
                    }
                    for row in candidates
                ],
                "summary": "Bounded test adjudication.",
            }

    archive = _archive(tmp_path, "fn main() { call_model(); }")
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file="unused",
        partition="specialists",
        concurrency=2,
        reviewer_factory=Reviewer,
    )
    assert peak == 2
    assert len(instances) == 6
    assert all(not r.kwargs["leads"] for r in instances[:5])
    assert all("Exact active policy manifest" in r.kwargs["focus"] for r in instances)
    assert all(r.kwargs["timeout_seconds"] == 120 for r in instances)
    assert all(r.kwargs["max_completion_request_seconds"] == 120 for r in instances)
    assert instances[-1].kwargs["leads"] == []
    assert result["incremental_candidate"] is True
    assert result["outcome"] == expected
    assert result["usage"]["requests"] == 6


def test_adjudicator_cannot_confirm_one_candidate_with_an_unrelated_finding(tmp_path):
    from .test_source_review import _archive_files

    archive = _archive_files(
        tmp_path,
        {"src/main.rs": b"trigger();\nauthority();\nunrelated();\n"},
    )
    repository = TarSourceRepository(str(archive))
    candidates = [
        {
            "candidate_id": "candidate-001",
            "source_pass": "answer_authority",
            "finding": {
                "evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 1,
                        "category": "benchmark_emulation",
                    }
                ]
            },
        },
        {
            "candidate_id": "candidate-002",
            "source_pass": "tool_fidelity",
            "finding": {
                "evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 2,
                        "category": "fabricated_tool_trajectory",
                    }
                ]
            },
        },
    ]
    payload = {
        "summary": "One candidate verified; one unrelated location found.",
        "candidate_assessments": [
            {
                "candidate_id": "candidate-001",
                "disposition": "supported",
                "supporting_evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 1,
                        "category": "benchmark_emulation",
                    }
                ],
                "counterevidence": [],
                "summary": "The candidate citation was verified.",
            },
            {
                "candidate_id": "candidate-002",
                "disposition": "supported",
                "supporting_evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 3,
                        "category": "fabricated_tool_trajectory",
                    }
                ],
                "counterevidence": [],
                "summary": "Only an unrelated location was found.",
            },
        ],
    }
    result = _normalize_candidate_adjudications(
        payload,
        candidates=candidates,
        repository=repository,
        opened_lines={("src/main.rs", 1), ("src/main.rs", 3)},
    )
    assert [row["disposition"] for row in result["candidate_assessments"]] == [
        "supported",
        "unresolved",
    ]


def test_concern_note_source_is_retained_in_provisional_candidate(tmp_path):
    from .test_source_review import _archive_files

    archive = _archive_files(tmp_path, {"src/main.rs": b"fn leaked() { send(); }\n"})
    repository = TarSourceRepository(str(archive))
    raw = {
        "risk_level": [],
        "categories": ["none"],
        "evidence": [],
        "invariants": [],
        "summary": "The focused review remained provisional.",
    }
    notes = [
        {
            "kind": "concern",
            "category": "cross_user_access",
            "path": "src/main.rs",
            "line": 1,
            "summary": "Possible cross-user sink.",
        }
    ]
    assert _provisional_candidate_basis(raw, notes) == ["concern_note"]
    finding = _provisional_finding(raw, notes, repository)
    assert finding["evidence"] == [
        {"path": "src/main.rs", "line": 1, "category": "cross_user_access"}
    ]
    assert finding["evidence_provenance"][0]["origins"] == ["concern_note"]
    malformed = {
        **raw,
        "evidence": [{"path": [], "line": {}, "category": []}],
        "invariants": [
            {
                "invariant": "i1_model_invocation",
                "disposition": "breach",
                "evidence_indices": [0],
            }
        ],
    }
    assert _provisional_candidate_basis(malformed, []) == ["failed_invariant"]
    assert _provisional_finding(malformed, [], repository)["evidence"] == []


@pytest.mark.parametrize("policy_version", [12, 13])
def test_final_adjudicator_is_canonical_and_source_read_bound(tmp_path, policy_version):
    from ditto_screening_protocol.models import source_review_invariants_for_policy

    from .test_source_review import (
        _archive_files,
        _with_policy_v10_invariants,
    )

    archive = _archive_files(tmp_path, {"src/main.rs": b"fn leaked() { send(); }\n"})
    repository = TarSourceRepository(str(archive))
    final_review = _with_policy_v10_invariants(
        {
            "risk_level": "high",
            "confidence": 0.95,
            "categories": ["cross_user_access"],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": "cross_user_access",
                }
            ],
            "summary": "The served path exposes cross-user source content.",
        }
    )
    policy_invariants = {
        invariant.value
        for invariant in source_review_invariants_for_policy(policy_version)
    }
    final_review["invariants"] = [
        item
        for item in final_review["invariants"]
        if item["invariant"] in policy_invariants
    ]
    payload = {
        "final_review": final_review,
        "candidate_assessments": [],
        "summary": "Fresh stage two found a source-bound issue.",
    }
    with pytest.raises(ValueError, match="did not read"):
        _normalize_final_adjudication(
            payload,
            artifact_sha256="a" * 64,
            policy_version=policy_version,
            candidates=[],
            repository=repository,
            opened_lines=set(),
            clearance_certified=False,
        )
    result = _normalize_final_adjudication(
        payload,
        artifact_sha256="a" * 64,
        policy_version=policy_version,
        candidates=[],
        repository=repository,
        opened_lines={("src/main.rs", 1)},
        clearance_certified=False,
    )
    assert result["outcome"] == "candidate"
    assert result["final_review"]["risk_level"] == "high"
    assert result["evidence_verified"] is True


def test_low_final_review_needs_stage_two_clearance_and_cannot_hide_support(
    tmp_path,
):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _with_policy_v10_invariants,
    )

    archive = _archive_files(tmp_path, {"src/main.rs": b"fn leaked() { send(); }\n"})
    repository = TarSourceRepository(str(archive))
    candidate = {
        "candidate_id": "candidate-001",
        "source_pass": "answer_authority",
        "finding": {
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": "cross_user_access",
                }
            ]
        },
    }
    payload = {
        "final_review": _with_policy_v10_invariants(_BENIGN_REVIEW),
        "candidate_assessments": [],
        "summary": "Stage two completed.",
    }
    malformed = {
        **payload,
        "final_review": {**payload["final_review"], "risk_level": []},
    }
    with pytest.raises(ValueError, match="final review fields are invalid"):
        _normalize_final_adjudication(
            malformed,
            artifact_sha256="a" * 64,
            policy_version=13,
            candidates=[],
            repository=repository,
            opened_lines=set(),
            clearance_certified=False,
        )
    with pytest.raises(ValueError, match="clearance coverage"):
        _normalize_final_adjudication(
            payload,
            artifact_sha256="a" * 64,
            policy_version=13,
            candidates=[],
            repository=repository,
            opened_lines=set(),
            clearance_certified=False,
        )
    payload["candidate_assessments"] = [
        {
            "candidate_id": "candidate-001",
            "disposition": "supported",
            "supporting_evidence": candidate["finding"]["evidence"],
            "counterevidence": [],
            "summary": "The candidate source was verified.",
        }
    ]
    with pytest.raises(ValueError, match="conflicts with low"):
        _normalize_final_adjudication(
            payload,
            artifact_sha256="a" * 64,
            policy_version=13,
            candidates=[candidate],
            repository=repository,
            opened_lines={("src/main.rs", 1)},
            clearance_certified=True,
        )
    payload["final_review"] = _with_policy_v10_invariants(
        {
            "risk_level": "high",
            "confidence": 0.95,
            "categories": ["cross_user_access"],
            "evidence": candidate["finding"]["evidence"],
            "summary": "The candidate source establishes cross-user access.",
        }
    )
    result = _normalize_final_adjudication(
        payload,
        artifact_sha256="a" * 64,
        policy_version=13,
        candidates=[candidate],
        repository=repository,
        opened_lines={("src/main.rs", 1)},
        clearance_certified=False,
    )
    assert result["outcome"] == "critic_also_flagged"
    assert result["candidate_assessments"][0]["disposition"] == "supported"


async def test_digest_mismatch_prevents_calls(tmp_path):
    archive = tmp_path / "artifact"
    archive.write_bytes(b"test")
    with pytest.raises(ValueError, match="SHA-256"):
        await review_archive(
            archive,
            artifact_sha256="0" * 64,
            api_key_file="unused",
            partition="specialists",
            reviewer_factory=lambda **_: pytest.fail("must not call"),
        )


async def test_manifest_digest_mismatch_prevents_calls(tmp_path):
    archive = tmp_path / "artifact"
    archive.write_bytes(b"test")
    with pytest.raises(ValueError, match="policy manifest digest"):
        await review_archive(
            archive,
            artifact_sha256=hashlib.sha256(b"test").hexdigest(),
            api_key_file="unused",
            policy_manifest_digest="0" * 64,
            reviewer_factory=lambda **_: pytest.fail("must not call"),
        )


@pytest.mark.parametrize("hang", [False, True])
async def test_failures_never_clear_and_adjudicator_failure_is_reported(tmp_path, hang):
    from .test_source_review import _archive

    class Reviewer:
        def __init__(self, **_kwargs):
            self.usage = {"requests": 1, "unmetered_requests": 1}
            self.response_models = set()
            self.opened_paths = set()

        async def review_provisional(self, *_args, **_kwargs):
            if hang:
                await asyncio.sleep(60)
            raise TimeoutError()

        async def adjudicate_review(self, *_args, **_kwargs):
            raise TimeoutError()

    archive = _archive(tmp_path, "fn main() { call_model(); }")
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file="unused",
        partition="specialists",
        reviewer_factory=Reviewer,
        timeout_seconds=0.01,
    )
    assert result["outcome"] == "incomplete"
    assert result["critic"]["error_code"] == "TimeoutError"
    assert result["critic"]["final_review"] is None
    assert result["usage"]["unmetered_requests"] == 6


async def test_http_transport_failure_becomes_incomplete_report(tmp_path):
    from .test_source_review import _archive_files

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() { call_model(); }"})

    async def fail(request):
        raise httpx.ConnectError("router unavailable", request=request)

    transport = httpx.MockTransport(fail)
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file=str(key),
        partition="specialists",
        concurrency=2,
        max_steps=2,
        reviewer_factory=lambda **kwargs: ExperimentalReviewer(
            transport=transport, **kwargs
        ),
    )
    assert result["outcome"] == "incomplete"
    assert any(row["error_code"] == "ConnectError" for row in result["passes"])
    assert result["critic"]["error_code"] == "FanoutBudgetExhausted"
    assert result["usage"]["unmetered_responses"] >= 1


async def test_real_reviewer_uses_inert_tools_policy_and_metering(tmp_path):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive,
        _transport,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive(tmp_path, "fn main() { call_model(); }\n")
    seen = []
    inner = _transport(_with_policy_v10_invariants(_BENIGN_REVIEW), seen)

    async def handler(request):
        response = await inner.handle_async_request(request)
        payload = response.json()
        payload["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cost": 0.001,
        }
        payload["model"] = "z-ai/glm-5.3-flash"
        return httpx.Response(200, json=payload)

    reviewer = ExperimentalReviewer(
        assigned_paths=("src/main.rs",),
        focus=FOCI["answer_authority"],
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=4,
        transport=httpx.MockTransport(handler),
        transport_retry_delays=(),
    )
    result = await reviewer.review(
        str(archive), artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert result.ok
    assert len(seen) == 2
    assert reviewer.usage["reported_cost_usd"] == 0.002
    assert reviewer.usage["unmetered_requests"] == 0
    assert all(
        FOCI["answer_authority"] in row["messages"][0]["content"] for row in seen
    )
    assert seen[0]["provider"]["zdr"] is True
    assert seen[0]["provider"]["data_collection"] == "deny"
    assert "call_model" in json.dumps(seen[1]["messages"])
    assert reviewer.opened_paths == {"src/main.rs"}
    assert "ANY file" in seen[0]["messages"][1]["content"]


@pytest.mark.parametrize("policy_version", [12, 13])
async def test_raw_contradictory_specialists_reach_always_run_adjudicator(
    tmp_path, policy_version
):
    from ditto_screening_protocol.models import source_review_invariants_for_policy

    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() { call_model(); }"})
    stage_two_requests = []
    specialist_requests = []
    policy_invariants = {
        invariant.value
        for invariant in source_review_invariants_for_policy(policy_version)
    }

    def policy_review(review):
        value = _with_policy_v10_invariants(review)
        value["invariants"] = [
            item
            for item in value["invariants"]
            if item["invariant"] in policy_invariants
        ]
        return value

    async def handler(request):
        payload = json.loads(request.content)
        messages = payload["messages"]
        stage_two = any(
            tool["function"]["name"] == "submit_fanout_adjudication"
            for tool in payload["tools"]
        )
        if stage_two:
            stage_two_requests.append(payload)
        else:
            specialist_requests.append(payload)
        if not any(message.get("role") == "tool" for message in messages):
            calls = [
                _tool(
                    f"read-{index}",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 1},
                )
                for index in (1, 2)
            ]
        elif stage_two:
            calls = [
                _tool(
                    "final",
                    "submit_fanout_adjudication",
                    {
                        "final_review": policy_review(_BENIGN_REVIEW),
                        "candidate_assessments": [],
                        "summary": "Stage two independently cleared the source.",
                    },
                )
            ]
        else:
            raw = policy_review(_BENIGN_REVIEW)
            raw["invariants"][0] = {
                **raw["invariants"][0],
                "disposition": "inconclusive",
            }
            if FOCI["benchmark_engine"] in messages[0]["content"]:
                raw["risk_level"] = []
            calls = [_tool("provisional", "submit_review", raw)]
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file=str(key),
        partition="specialists",
        concurrency=2,
        max_steps=3,
        policy_version=policy_version,
        reviewer_factory=lambda **kwargs: ExperimentalReviewer(
            transport=transport, **kwargs
        ),
    )
    assert result["revision"] == "fanout-source-review-v4"
    assert result["coverage_protocol"] == "five-specialists-adjudicator-v2"
    assert result["outcome"] == "no_findings"
    assert result["candidates"] == []
    assert result["critic"]["final_review"]["risk_level"] == "low"
    assert result["critic"]["clearance_certified"] is True
    assert result["critic"]["pass_context_count"] == 5
    assert all(row["outcome"] == "provisional" for row in result["passes"])
    assert all(row["validation_errors"] for row in result["passes"])
    assert all(
        row["raw_review"]["invariants"][0]["disposition"] == "inconclusive"
        for row in result["passes"]
    )
    benchmark = next(
        row for row in result["passes"] if row["name"] == "benchmark_engine"
    )
    assert benchmark["raw_review"]["risk_level"] == []
    assert "TypeError" in benchmark["validation_errors"][0]
    assert len(stage_two_requests) == 2
    assert "inconclusive" in json.dumps(stage_two_requests[0]["messages"])
    assert all(
        "provisional specialist note" in row["messages"][0]["content"]
        for row in specialist_requests
    )


async def test_adjudicator_reserves_read_repair_after_forced_final(tmp_path):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(
        tmp_path,
        {"src/main.rs": b"fn main() { call_model(); }\nfn leaked() { send(); }\n"},
    )
    final_review = _with_policy_v10_invariants(
        {
            **_BENIGN_REVIEW,
            "risk_level": "high",
            "categories": ["cross_user_access"],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 2,
                    "category": "cross_user_access",
                }
            ],
            "summary": "The served path exposes cross-user source content.",
        }
    )
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        turn = len(requests)
        if turn <= 9:
            calls = [
                _tool(
                    f"read-{turn}",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 1},
                )
            ]
        elif turn == 11:
            assert "did not read" in json.dumps(payload["messages"])
            assert any(
                tool["function"]["name"] == "read_file" for tool in payload["tools"]
            )
            calls = [
                _tool(
                    "read-2",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 2, "end_line": 2},
                )
            ]
        else:
            calls = [
                _tool(
                    f"final-{turn}",
                    "submit_fanout_adjudication",
                    {
                        "final_review": final_review,
                        "candidate_assessments": [],
                        "summary": "Fresh stage two found a source-bound issue.",
                    },
                )
            ]
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    reviewer = ExperimentalReviewer(
        focus="Adjudicator",
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        max_steps=12,
        max_read_bytes=180_000,
        max_completion_tokens=8000,
        timeout_seconds=60,
        transport=httpx.MockTransport(handler),
    )
    result = await reviewer.adjudicate_review(
        str(archive),
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        candidates=[],
        all_pass_summaries=[],
        policy_version=13,
        deadline=asyncio.get_running_loop().time() + 60,
    )
    assert result["outcome"] == "candidate"
    assert len(requests) == 12
    assert [tool["function"]["name"] for tool in requests[9]["tools"]] == [
        "submit_fanout_adjudication"
    ]
    assert [tool["function"]["name"] for tool in requests[11]["tools"]] == [
        "submit_fanout_adjudication"
    ]
    assert len(reviewer.validation_errors) == 1
    assert reviewer.validation_errors[0].startswith(
        "fanout adjudicator cited source it did not read"
    )
    assert '"line": 2' in reviewer.validation_errors[0]
    assert '"path": "src/main.rs"' in reviewer.validation_errors[0]
    assert reviewer.opened_lines == {("src/main.rs", 1), ("src/main.rs", 2)}


async def test_adjudicator_repairs_malformed_atomic_arguments_in_remaining_turns(
    tmp_path,
):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn leaked() { send(); }\n"})
    final_review = _with_policy_v10_invariants(
        {
            **_BENIGN_REVIEW,
            "risk_level": "high",
            "categories": ["cross_user_access"],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": "cross_user_access",
                }
            ],
            "summary": "The served path exposes cross-user source content.",
        }
    )
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        turn = len(requests)
        if turn == 1:
            calls = [
                {
                    "id": "malformed-final",
                    "type": "function",
                    "function": {
                        "name": "submit_fanout_adjudication",
                        "arguments": '{"final_review":',
                    },
                }
            ]
        elif turn == 2:
            assert "JSONDecodeError" in json.dumps(payload["messages"])
            # The real Router's strict upstream rejects invalid argument JSON
            # anywhere in replayed tool calls, before it can generate a repair.
            for message in payload["messages"]:
                for call in message.get("tool_calls", []):
                    json.loads(call["function"]["arguments"])
            failed_output = next(
                message
                for message in payload["messages"]
                if message.get("role") == "assistant"
                and "Invalid, unexecuted" in message.get("content", "")
            )
            original = json.loads(failed_output["content"].split("\n", 1)[1])
            assert original["tool_calls"][0]["id"] == "malformed-final"
            assert (
                original["tool_calls"][0]["function"]["arguments"] == '{"final_review":'
            )
            assert not any(
                message.get("tool_call_id") == "malformed-final"
                for message in payload["messages"]
            )
            assert any(
                message.get("role") == "user"
                and "JSONDecodeError" in message.get("content", "")
                for message in payload["messages"]
            )
            assert any(
                tool["function"]["name"] == "read_file" for tool in payload["tools"]
            )
            calls = [
                _tool(
                    "read-source",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 1},
                )
            ]
        else:
            calls = [
                _tool(
                    "corrected-final",
                    "submit_fanout_adjudication",
                    {
                        "final_review": final_review,
                        "candidate_assessments": [],
                        "summary": "Fresh stage two found a source-bound issue.",
                    },
                )
            ]
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    reviewer = ExperimentalReviewer(
        focus="Adjudicator",
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        max_steps=3,
        max_read_bytes=180_000,
        max_completion_tokens=8000,
        timeout_seconds=60,
        transport=httpx.MockTransport(handler),
    )
    result = await reviewer.adjudicate_review(
        str(archive),
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        candidates=[],
        all_pass_summaries=[],
        policy_version=13,
        deadline=asyncio.get_running_loop().time() + 60,
    )
    assert result["outcome"] == "candidate"
    assert len(requests) == 3
    assert reviewer.validation_errors == [
        "fanout adjudicator arguments are invalid (JSONDecodeError)"
    ]


async def test_malformed_atomic_arguments_on_final_turn_fail_closed(tmp_path):
    from .test_source_review import _archive_files

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() {}\n"})

    async def handler(_request):
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "malformed-final",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_fanout_adjudication",
                                        "arguments": "{",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    reviewer = ExperimentalReviewer(
        focus="Adjudicator",
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        max_steps=1,
        max_read_bytes=180_000,
        max_completion_tokens=8000,
        timeout_seconds=60,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(
        ValueError, match="fanout adjudicator final review remained invalid"
    ):
        await reviewer.adjudicate_review(
            str(archive),
            artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            candidates=[],
            all_pass_summaries=[],
            policy_version=13,
            deadline=asyncio.get_running_loop().time() + 60,
        )
    assert reviewer.validation_errors == [
        "fanout adjudicator arguments are invalid (JSONDecodeError)"
    ]


@pytest.mark.parametrize(
    "calls,error",
    [
        (
            [
                {
                    "id": "",
                    "type": "function",
                    "function": {
                        "name": "submit_fanout_adjudication",
                        "arguments": "{",
                    },
                }
            ],
            "fanout adjudicator final tool call envelope is invalid",
        ),
        (
            [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "submit_fanout_adjudication",
                        "arguments": "{",
                    },
                }
                for call_id in ("first", "second")
            ],
            "shadow final tool call must be exclusive",
        ),
    ],
)
async def test_invalid_or_multiple_atomic_tool_envelopes_fail_closed(
    tmp_path, calls, error
):
    from .test_source_review import _archive_files

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() {}\n"})
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    reviewer = ExperimentalReviewer(
        focus="Adjudicator",
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        max_steps=3,
        max_read_bytes=180_000,
        max_completion_tokens=8000,
        timeout_seconds=60,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match=error):
        await reviewer.adjudicate_review(
            str(archive),
            artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            candidates=[],
            all_pass_summaries=[],
            policy_version=13,
            deadline=asyncio.get_running_loop().time() + 60,
        )
    assert requests == 1


async def test_default_budget_completes_two_turn_fanout_and_source_read(tmp_path):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(
        tmp_path,
        {"src/main.rs": b"fn main() { call_model(); }\nfn answer() { shortcut(); }"},
    )
    seen = []

    async def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        messages = payload["messages"]
        if not any(message.get("role") == "tool" for message in messages):
            calls = [
                _tool(
                    "read-1",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 20},
                ),
                _tool(
                    "read-2",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 20},
                ),
            ]
        elif any(
            tool["function"]["name"] == "submit_fanout_adjudication"
            for tool in payload["tools"]
        ):
            calls = [
                _tool(
                    "adjudicate-1",
                    "submit_fanout_adjudication",
                    {
                        "final_review": _with_policy_v10_invariants(_BENIGN_REVIEW),
                        "candidate_assessments": [
                            {
                                "candidate_id": "candidate-001",
                                "disposition": "unresolved",
                                "supporting_evidence": [],
                                "counterevidence": [],
                                "summary": "The bounded check remains unresolved.",
                            }
                        ],
                        "summary": "The bounded check remains unresolved.",
                    },
                )
            ]
        else:
            system = messages[0]["content"]
            final = _BENIGN_REVIEW
            if FOCI["benchmark_engine"] in system:
                final = {
                    "risk_level": "high",
                    "confidence": 0.95,
                    "categories": ["benchmark_emulation"],
                    "evidence": [
                        {
                            "path": "src/main.rs",
                            "line": 1,
                            "category": "benchmark_emulation",
                        },
                        {
                            "path": "src/main.rs",
                            "line": 2,
                            "category": "benchmark_emulation",
                        },
                    ],
                    "summary": "Candidate benchmark-specific answer path.",
                }
            calls = [
                _tool(
                    "submit-1",
                    "submit_review",
                    _with_policy_v10_invariants(final),
                )
            ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": calls,
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 16_000,
                    "completion_tokens": 1_200,
                    "cost": 0.01,
                },
                "model": "glm-5.3-flash",
            },
        )

    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return ExperimentalReviewer(transport=transport, **kwargs)

    report = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file=str(key),
        concurrency=2,
        max_steps=4,
        max_groups=4,
        max_requests=40,
        max_total_tokens=1_500_000,
        max_reported_cost_usd=3,
        reviewer_factory=factory,
    )
    assert len(report["passes"]) == 6
    assert report["critic"] is not None
    assert report["outcome"] == "unresolved_candidate"
    assert report["usage"]["requests"] == 14
    assert report["usage"]["reserved_tokens"] <= 1_500_000
    assert report["usage"]["reserved_cost_usd"] <= 3
    assert report["usage"]["unmetered_responses"] == 0
    assert report["budgets"]["timeout_seconds_per_request"] == 120
    assert (
        sum(
            any(message.get("role") == "tool" for message in payload["messages"])
            for payload in seen
        )
        == 7
    )


def test_file_plan_is_deterministic_bounded_and_reports_omissions(tmp_path):
    from .test_source_review import _archive_files

    archive = _archive_files(
        tmp_path,
        {
            "src/main.rs": b"fn main() {}",
            "src/helper.rs": b"x" * 70,
            "README.md": b"docs",
            "config.json": b"{}",
            "../escape": b"ignored",
        },
    )
    plan = plan_file_groups(archive, files_per_group=1, group_bytes=64, max_groups=2)
    assert plan == plan_file_groups(
        archive, files_per_group=1, group_bytes=64, max_groups=2
    )
    assert plan["total_files"] == 4
    assert plan["scheduled_files"] == 2
    assert plan["omitted_files"] == 2
    assert plan["truncated"]
    assert plan["oversized_files"] == 1
    full = plan_file_groups(archive, files_per_group=4, group_bytes=64, max_groups=64)
    assert full["scheduled_files"] == 4
    assert not full["truncated"]
    assert ["src/helper.rs"] in full["groups"]


@pytest.mark.parametrize(
    "max_groups,opened,expected",
    [
        (1, True, "incomplete"),
        (8, False, "incomplete"),
        (8, True, "no_findings"),
    ],
)
async def test_file_scheduling_and_actual_read_coverage(
    tmp_path, max_groups, opened, expected
):
    from .test_source_review import _archive

    class Reviewer:
        def __init__(self, **kwargs):
            self.usage = {"requests": 1}
            self.response_models = set()
            self.opened_paths = set(kwargs["assigned_paths"]) if opened else set()

        async def review_provisional(self, *_args, **_kwargs):
            return {
                "raw_review": {
                    "risk_level": "low",
                    "categories": ["none"],
                    "evidence": [],
                    "invariants": [],
                    "summary": "Provisional specialist note.",
                },
                "notes": [],
                "inspection_complete": True,
            }

        async def adjudicate_review(self, *_args, **_kwargs):
            return {
                "revision": "fanout-adjudicator-v2",
                "outcome": "no_findings",
                "final_review": {"risk_level": "low"},
                "clearance_certified": True,
                "evidence_verified": True,
                "candidate_assessments": [],
                "summary": "Bounded test adjudication.",
            }

    archive = _archive(tmp_path, "fn main() {}")
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        api_key_file="unused",
        partition="files",
        files_per_group=1,
        max_groups=max_groups,
        reviewer_factory=Reviewer,
    )
    assert result["outcome"] == expected
    assert len(result["passes"]) == 1 + min(max_groups, 3)
    assert result["passes"][0]["name"] == "generalist"
    assert result["critic"] is not None


async def test_transport_failure_is_unmetered_not_a_different_model():
    budget = FanoutBudget(
        max_requests=40, max_total_tokens=100_000, max_reported_cost_usd=3
    )
    reservation = await budget.before_request(
        input_token_bound=10_000, completion_token_bound=2400
    )
    await budget.record_response(None, reservation_id=reservation)
    assert budget.snapshot()["model_mismatch"] is False
    assert budget.snapshot()["unmetered_responses"] == 1
    assert budget.snapshot()["reserved_tokens"] == 12_400
    with pytest.raises(FanoutBudgetExhausted, match="metering unavailable"):
        await budget.before_request(input_token_bound=100, completion_token_bound=100)


async def test_settlement_releases_only_completed_request_token_headroom():
    budget = FanoutBudget(
        max_requests=40, max_total_tokens=30_000, max_reported_cost_usd=3
    )
    first = await budget.before_request(
        input_token_bound=10_000, completion_token_bound=2400
    )
    await budget.before_request(input_token_bound=10_000, completion_token_bound=2400)
    await budget.record_response(
        {
            "model": "glm-5.3-flash",
            "usage": {
                "prompt_tokens": 2500,
                "completion_tokens": 500,
                "cost": 0.002,
            },
        },
        reservation_id=first,
    )
    assert budget.snapshot()["reserved_tokens"] == 15_400
    assert budget.snapshot()["unmetered_requests"] == 1
    await budget.before_request(input_token_bound=10_000, completion_token_bound=2400)
    assert budget.snapshot()["reserved_tokens"] == 27_800
    with pytest.raises(FanoutBudgetExhausted, match="token budget exhausted"):
        await budget.before_request(input_token_bound=3000, completion_token_bound=2400)


@pytest.mark.parametrize(
    "invalid_field,repair",
    [("categories", True), ("categories", False), ("summary", True)],
)
async def test_shadow_schema_correction_is_bounded_and_cannot_coerce_pass(
    tmp_path, invalid_field, repair
):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() { call_model(); }"})
    seen = []

    async def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            calls = [
                _tool(
                    "read-1",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 5},
                ),
                _tool(
                    "read-2",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 5},
                ),
            ]
        else:
            verdict = _with_policy_v10_invariants(dict(_BENIGN_REVIEW))
            if len(seen) == 2 or not repair:
                verdict[invalid_field] = "x" * 241 if invalid_field == "summary" else []
            calls = [_tool("submit", "submit_review", verdict)]
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [{"message": {"role": "assistant", "tool_calls": calls}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    budget = FanoutBudget(
        max_requests=4, max_total_tokens=500_000, max_reported_cost_usd=3
    )
    reviewer = ExperimentalReviewer(
        focus="Generalist",
        base_url="https://router.example/v1",
        budget=budget,
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        max_steps=2,
        max_read_bytes=180_000,
        max_completion_tokens=2400,
        timeout_seconds=60,
        transport=httpx.MockTransport(handler),
    )
    result = await reviewer.review(
        str(archive), artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert len(seen) == (2 if invalid_field == "summary" else 3 if repair else 4)
    assert result.ok is repair
    if invalid_field == "summary":
        assert reviewer.full_summaries[0]["text"] == "x" * 241
        assert result.finding["summary"] == "x" * 237 + "..."
        assert reviewer.validation_errors == []
    else:
        assert reviewer.validation_errors == ["source review fields are invalid"] * (
            1 if repair else 3
        )
    assert budget.snapshot()["requests"] == len(seen)
    assert [t["function"]["name"] for t in seen[-1]["tools"]] == ["submit_review"]


def test_policy_v13_bounds_all_invariant_summaries_without_changing_decisions(
    tmp_path,
):
    from ditto_screening_protocol import SourceReviewInvariantAssessment

    from .test_source_review import (
        _BENIGN_REVIEW,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    final_review = _with_policy_v10_invariants(dict(_BENIGN_REVIEW))
    decisions = final_review["invariants"]
    assert isinstance(decisions, list) and len(decisions) == 8
    decisions[0] = {
        **decisions[0],
        "disposition": "breach",
        "pass_clause": None,
        "evidence_indices": [0],
    }
    decisions[1] = {
        **decisions[1],
        "disposition": "inconclusive",
        "pass_clause": None,
        "evidence_indices": [],
    }
    for index, decision in enumerate(decisions):
        decision["summary"] = str(index) + "x" * 239
    semantic_fields = [
        {
            key: decision[key]
            for key in ("invariant", "disposition", "pass_clause", "evidence_indices")
        }
        for decision in decisions
    ]
    message = {
        "role": "assistant",
        "tool_calls": [
            _tool(
                "final",
                "submit_fanout_adjudication",
                {
                    "final_review": final_review,
                    "candidate_assessments": [],
                    "summary": "Central adjudication completed.",
                },
            )
        ],
    }
    reviewer = ExperimentalReviewer(
        focus="Adjudicator",
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        max_steps=1,
        max_read_bytes=180_000,
        max_completion_tokens=8000,
        timeout_seconds=60,
    )
    reviewer._review_policy_version = 13

    bounded = reviewer._bound_summary_fields(message)
    arguments = json.loads(bounded["tool_calls"][0]["function"]["arguments"])
    bounded_decisions = arguments["final_review"]["invariants"]

    assert [
        {
            key: decision[key]
            for key in ("invariant", "disposition", "pass_clause", "evidence_indices")
        }
        for decision in bounded_decisions
    ] == semantic_fields
    assert bounded_decisions[0]["disposition"] == "breach"
    assert bounded_decisions[0]["evidence_indices"] == [0]
    assert bounded_decisions[1]["disposition"] == "inconclusive"
    assert bounded_decisions[1]["pass_clause"] is None
    assert all(len(decision["summary"]) == 210 for decision in bounded_decisions)
    assert sum(len(decision["summary"]) for decision in bounded_decisions) == 1_680
    SourceReviewInvariantAssessment.model_validate(
        {"schema_version": 2, "decisions": bounded_decisions}
    )
    assert len(reviewer.full_summaries) == 8
    assert [row["field"] for row in reviewer.full_summaries] == [
        f"final_review.invariants[{index}].summary" for index in range(8)
    ]
    assert all(row["original_chars"] == 240 for row in reviewer.full_summaries)
    assert all(
        row["text"] == str(index) + "x" * 239
        for index, row in enumerate(reviewer.full_summaries)
    )
    assert all(row["truncated"] is False for row in reviewer.full_summaries)


@pytest.mark.parametrize("model", [{}, [], 42])
async def test_malformed_model_identifier_stops_admission_without_crashing(model):
    budget = FanoutBudget(
        max_requests=4, max_total_tokens=100_000, max_reported_cost_usd=3
    )
    reservation = await budget.before_request(
        input_token_bound=10_000, completion_token_bound=8000
    )
    await budget.record_response(
        {
            "model": model,
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.001},
        },
        reservation_id=reservation,
    )
    assert budget.snapshot()["model_mismatch"]
    with pytest.raises(FanoutBudgetExhausted, match="response model changed"):
        await budget.before_request(
            input_token_bound=10_000, completion_token_bound=8000
        )


async def test_one_underreserved_response_cannot_hide_behind_other_inflight_budget():
    budget = FanoutBudget(
        max_requests=4, max_total_tokens=100_000, max_reported_cost_usd=3
    )
    small = await budget.before_request(
        input_token_bound=1000, completion_token_bound=100
    )
    await budget.before_request(input_token_bound=50_000, completion_token_bound=8000)
    await budget.record_response(
        {
            "model": "glm-5.3-flash",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "cost": 0.002,
            },
        },
        reservation_id=small,
    )
    assert budget.snapshot()["price_bound_exceeded"]
    with pytest.raises(FanoutBudgetExhausted, match="pricing bound exceeded"):
        await budget.before_request(input_token_bound=1000, completion_token_bound=100)


async def test_duplicate_settlement_cannot_refund_another_request():
    budget = FanoutBudget(
        max_requests=4, max_total_tokens=100_000, max_reported_cost_usd=3
    )
    first = await budget.before_request(
        input_token_bound=10_000, completion_token_bound=8000
    )
    await budget.before_request(input_token_bound=10_000, completion_token_bound=8000)
    payload = {
        "model": "glm-5.3-flash",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.001},
    }
    await budget.record_response(payload, reservation_id=first)
    before = budget.snapshot()
    with pytest.raises(ValueError, match="already settled"):
        await budget.record_response(payload, reservation_id=first)
    after = budget.snapshot()
    assert after["reserved_tokens"] == before["reserved_tokens"]
    assert after["reserved_cost_usd"] == before["reserved_cost_usd"]
    assert after["reported_cost_usd"] == before["reported_cost_usd"]
    assert after["unmetered_requests"] == 1
    with pytest.raises(FanoutBudgetExhausted, match="metering unavailable"):
        await budget.before_request(input_token_bound=1000, completion_token_bound=100)


@pytest.mark.parametrize("corrected", [False, True])
async def test_conflicting_final_calls_cannot_select_first_clean(tmp_path, corrected):
    from .test_source_review import (
        _BENIGN_REVIEW,
        _archive_files,
        _tool,
        _with_policy_v10_invariants,
    )

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    key.chmod(0o600)
    archive = _archive_files(
        tmp_path,
        {"src/main.rs": b"fn main() { call_model(); }\nfn answer() { shortcut(); }"},
    )
    clean = _with_policy_v10_invariants(dict(_BENIGN_REVIEW))
    flagged = _with_policy_v10_invariants(
        {
            **_BENIGN_REVIEW,
            "risk_level": "high",
            "categories": ["benchmark_emulation"],
            "evidence": [
                {"path": "src/main.rs", "line": line, "category": "benchmark_emulation"}
                for line in (1, 2)
            ],
        }
    )
    from ditto_screener.source_review import _parse_review

    for verdict in (clean, flagged):
        assert _parse_review(
            verdict,
            artifact_sha256="a" * 64,
            repository=TarSourceRepository(str(archive)),
        ).ok
    seen = []

    async def handler(request):
        seen.append(request)
        calls = [
            _tool("clean", "submit_review", clean),
            _tool("flagged", "submit_review", flagged),
        ]
        if corrected and len(seen) == 1:
            calls = [_tool("invalid", "submit_review", {**clean, "categories": []})]
        return httpx.Response(
            200,
            json={
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": calls,
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": 0.001,
                },
            },
        )

    reviewer = ExperimentalReviewer(
        focus="Generalist",
        api_key_file=str(key),
        model="z-ai/glm-5.3-flash",
        base_url="https://router.example/v1",
        max_steps=4,
        max_read_bytes=180_000,
        max_completion_tokens=8000,
        timeout_seconds=60,
        transport=httpx.MockTransport(handler),
    )
    result = await reviewer.review(
        str(archive), artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert not result.ok
    assert result.finding is None
    assert reviewer.usage["requests"] == (2 if corrected else 1)
    assert reviewer.full_summaries == []
