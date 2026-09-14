import asyncio
import hashlib
import json

import httpx
import pytest

from ditto_screener.fanout_review import (
    FOCI,
    ExperimentalReviewer,
    plan_file_groups,
    review_archive,
)
from ditto_screener.policy import SourceReviewObservation


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
                    if self.kwargs["focus"] == FOCI["benchmark_engine"]
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
    assert len(instances[-1].kwargs["leads"]) == 1
    assert result["incremental_candidate"] is True
    assert result["outcome"] == expected
    assert result["usage"]["requests"] == 6


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
