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
    plan_file_groups,
    review_archive,
)
from ditto_screener.policy import SourceReviewObservation
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
    await budget.before_request(input_token_bound=1_000, completion_token_bound=2_400)
    await budget.record_response({"usage": {"prompt_tokens": 10}})
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
    await budget.before_request(input_token_bound=1_000, completion_token_bound=2_400)
    await budget.record_response(
        {
            "model": "router-selected-different-model",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "cost": 0.001,
            },
        }
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
    await budget.before_request(input_token_bound=1_000, completion_token_bound=2_400)
    await budget.record_response(
        {
            "model": response_model,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "cost": 0.001,
            },
        }
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
        await budget.before_request(
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
            }
        )
    usage = budget.snapshot()
    assert usage["requests"] == 10
    assert usage["reserved_tokens"] == 664_000
    assert usage["reserved_cost_usd"] < 3
    assert usage["unmetered_responses"] == 0


@pytest.mark.parametrize(
    "critic_risk,expected",
    [("low", "unresolved_candidate"), ("high", "critic_also_flagged")],
)
async def test_single_specialist_survives_majority_and_transcripts_are_independent(
    tmp_path, critic_risk, expected
):
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
            instances.append(self)

        async def review(self, *_args, **_kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            risk = (
                critic_risk
                if self.kwargs["leads"]
                else (
                    "high"
                    if FOCI["benchmark_engine"] in self.kwargs["focus"]
                    else "low"
                )
            )
            return SourceReviewObservation(
                ok=True,
                risk_level=risk,
                finding_digest=None,
                categories=(),
                finding={"evidence": []},
                clearance_certified=True,
            )

        async def adjudicate_candidates(
            self, _archive_path, *, candidates, all_pass_summaries, **_kwargs
        ):
            assert len(all_pass_summaries) == 5
            assert {row["outcome"] for row in all_pass_summaries} == {
                "candidate",
                "no_findings",
            }
            disposition = "supported" if critic_risk == "high" else "unresolved"
            return {
                "revision": "fanout-candidate-adjudicator-v1",
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

    archive = tmp_path / "artifact"
    archive.write_bytes(b"test")
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(b"test").hexdigest(),
        api_key_file="unused",
        partition="specialists",
        concurrency=2,
        reviewer_factory=Reviewer,
    )
    assert peak == 2
    assert len(instances) == 6
    assert all(not r.kwargs["leads"] for r in instances[:5])
    assert all("Exact active policy manifest" in r.kwargs["focus"] for r in instances)
    assert len(instances[-1].kwargs["leads"]) == 1
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
async def test_failures_never_clear_and_skip_critic(tmp_path, hang):
    class Reviewer:
        def __init__(self, **_kwargs):
            self.usage = {"requests": 1, "unmetered_requests": 1}
            self.response_models = set()

        async def review(self, *_args, **_kwargs):
            if hang:
                await asyncio.sleep(60)
            raise TimeoutError()

    archive = tmp_path / "artifact"
    archive.write_bytes(b"test")
    result = await review_archive(
        archive,
        artifact_sha256=hashlib.sha256(b"test").hexdigest(),
        api_key_file="unused",
        partition="specialists",
        reviewer_factory=Reviewer,
        timeout_seconds=0.01,
    )
    assert result["outcome"] == "incomplete"
    assert result["critic"] is None
    assert result["usage"]["unmetered_requests"] == 5


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
                )
            ]
        elif any(
            tool["function"]["name"] == "submit_candidate_adjudications"
            for tool in payload["tools"]
        ):
            calls = [
                _tool(
                    "adjudicate-1",
                    "submit_candidate_adjudications",
                    {
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

        async def review(self, *_args, **_kwargs):
            return SourceReviewObservation(
                ok=True,
                risk_level="low",
                finding_digest=None,
                categories=(),
                clearance_certified=True,
            )

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
    assert result["critic"] is None
