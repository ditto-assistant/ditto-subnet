#!/usr/bin/env python3
"""Identity of the LongMemEval judge evaluator the rehearsal runs.

LongMemEval's official judge, ``gpt-4o-2024-08-06``, is hard-coded in the
upstream evaluator and retires on 2027-01-14 (OpenAI API) and 2027-04-14
(Azure). SN118 scores with ``deepseek/deepseek-v4.1-flash`` in production, so
the rehearsal has to judge with it too or it measures a different grader than
the one that pays miners.

Substituting the judge needs a forked evaluator rather than an argument,
because the official protocol assumes a non-reasoning judge: it pins
``max_tokens=10``, which a reasoning model spends entirely on reasoning tokens
before emitting visible content. Upstream then reads ``'yes' in ''`` as False
and scores every question "no" -- a run that completes, reports total failure,
and raises no error. The fork raises the bound and refuses an empty completion.

The fork changes three hunks and leaves the five prompt templates in
``get_anscheck_prompt`` byte-identical, which is the property that keeps a
substituted run comparable at all. ``docs/longmemeval-benchmark/judge-evaluator/``
holds the recorded upstream snapshot and the exact delta, and
``test_judge_evaluator.py`` checks that identity offline rather than trusting
this comment.
"""

from __future__ import annotations

# --- upstream, recorded so the delta is auditable ------------------------
UPSTREAM_REPOSITORY = "https://github.com/xiaowu0162/LongMemEval.git"
UPSTREAM_REVISION = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
UPSTREAM_EVALUATOR_SHA256 = (
    "ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251"
)

# --- the fork the judge image is built from ------------------------------
FORK_REPOSITORY = "https://github.com/ditto-assistant/LongMemEval.git"
FORK_REF = "refs/heads/ditto-judge-v1"
FORK_REVISION = "0ef061d3491d23325e17ff82f8a13cbf970de981"
FORK_EVALUATOR_SHA256 = (
    "d15f87dee2c627a7c9ab2a63b34eae974cc349b964ad38b6c90e3b76dc363609"
)
EVALUATOR_PATH = "src/evaluation/evaluate_qa.py"

# --- the judge the fork is built to run ----------------------------------
# Mirrors services/dittobench-api/internal/longmemeval/provider_runtime.go:
# officialJudgeModel, officialJudgeMaxTokens, officialJudgeRouteProvider,
# officialJudgeReceiptProvider and officialJudgeRevision. A rehearsal that
# diverges from those is not rehearsing the lane that scores submissions.
JUDGE_SHORT_NAME = "deepseek-v4.1-flash"
JUDGE_MODEL = "deepseek/deepseek-v4.1-flash"
JUDGE_PROFILE_REVISION = "longmemeval-deepseek-v41-flash-together-v1"
JUDGE_MAX_COMPLETION_TOKENS = 8192
# Routing is pinned to one provider with fallbacks off, so the provider is part
# of the frozen contract. OpenRouter served this model from 23 distinct
# providers with fallbacks on, and agreement with gpt-4o ranged 95.3%-100%
# across them -- an unpinned rehearsal does not describe any lane we run.
JUDGE_ROUTE_PROVIDER = "together"
JUDGE_RECEIPT_PROVIDER = "Together"
# Production sends every judge call under OpenRouter's zero-data-retention
# filter. Together is verified to serve this model under that exact posture;
# two otherwise-viable providers return 404 "No endpoints found matching your
# data policy (Zero data retention)", so the flag changes which providers exist.
JUDGE_ZERO_DATA_RETENTION = True

# A substituted judge is not the official protocol, and no surface that reports
# a number from it may imply otherwise.
OFFICIAL_PROTOCOL = False

# --- the retiring official judge -----------------------------------------
# Kept because the frozen result sets under docs/longmemeval-benchmark/results/
# were produced by it and must keep validating as its rows. Nothing re-runs it.
OFFICIAL_JUDGE_SHORT_NAME = "gpt-4o"
OFFICIAL_JUDGE_MODEL = "gpt-4o-2024-08-06"
OFFICIAL_JUDGE_PROFILE_REVISION = "longmemeval-official-gpt4o-openrouter-v1"
OFFICIAL_JUDGE_MAX_COMPLETION_TOKENS = 10
OFFICIAL_JUDGE_RETIREMENT = {
    "openai_api": "2027-01-14",
    "azure": "2027-04-14",
}


def evaluation_suffix(judge_short_name: str = JUDGE_SHORT_NAME) -> str:
    """The evaluator's result-file suffix for one judge.

    Upstream derives this from ``sys.argv[1]``, so the artifact name carries
    the judge that actually produced it. Reading or writing a fixed
    ``.eval-results-gpt-4o`` while a different judge ran is how a file ends up
    claiming provenance it does not have.
    """
    return f".eval-results-{judge_short_name}"


def judge_provider_routing() -> dict[str, object]:
    """The OpenRouter ``provider`` block the production judge lane sends."""
    routing: dict[str, object] = {
        "order": [JUDGE_ROUTE_PROVIDER],
        "allow_fallbacks": False,
    }
    if JUDGE_ZERO_DATA_RETENTION:
        routing["zdr"] = True
    return routing


def judge_identity() -> dict[str, object]:
    """Judge identity for a report, health response or provenance manifest."""
    return {
        "short_name": JUDGE_SHORT_NAME,
        "model": JUDGE_MODEL,
        "profile_revision": JUDGE_PROFILE_REVISION,
        "max_completion_tokens": JUDGE_MAX_COMPLETION_TOKENS,
        "route_provider": JUDGE_ROUTE_PROVIDER,
        "zero_data_retention": JUDGE_ZERO_DATA_RETENTION,
        "official_protocol": OFFICIAL_PROTOCOL,
        "evaluator": {
            "repository": FORK_REPOSITORY,
            "revision": FORK_REVISION,
            "upstream_revision": UPSTREAM_REVISION,
            "evaluator_sha256": FORK_EVALUATOR_SHA256,
        },
    }
