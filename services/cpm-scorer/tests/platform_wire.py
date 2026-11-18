"""Platform's scorer wire models, restated as field sets and checks.

Source: `apps/platform/ditto/api_models/model_context.py` (classes
`ModelContextWorkItem`, `ModelContextWorkSubmission`, `ModelContextBase`,
`ModelContextWorkSettings`, `ScoreComponents`, `SeedResult`,
`ModelContextResultRequest`, `ModelContextFaultRequest`) and the result checks
in `apps/platform/ditto/api_server/model_context_competition.py`
(`check_result`, `SCORE_TOLERANCE`), from Platform PR #3575.

Platform's pydantic models live in the Platform app, which this package does
not install, so the tests copy the exact field sets. Change both together.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

WORK_ITEM_KEYS = {
    "run_id",
    "lease_id",
    "lease_expires_at",
    "kind",
    "submission",
    "base",
    "season",
    "seeds",
    "generate_seeds",
    "bench_version",
    "cpm_score_version",
    "gpu_sku",
    "settings",
}
WORK_SUBMISSION_KEYS = {
    "submission_id",
    "hotkey",
    "hf_repo",
    "revision",
    "manifest",
    "manifest_sha256",
    "weights_sha256",
    "base_id",
    "kind",
}
BASE_KEYS = {
    "base_id",
    "hf_repo",
    "revision",
    "parameters",
    "architectures",
    "lora_target_modules",
}
WORK_SETTINGS_KEYS = {"budget_tokens", "tool_k", "floor_margin"}

COMPONENT_REQUIRED = {
    "memory_recall",
    "distractor_rate",
    "tool_recall",
    "memory_cases",
    "tool_cases",
}
COMPONENT_OPTIONAL = {"packed_tokens"}
SEED_RESULT_REQUIRED = COMPONENT_REQUIRED | {"seed", "score"}
RESULT_REQUIRED = {
    "run_id",
    "lease_id",
    "cpm_score_version",
    "score",
    "components",
    "per_seed",
    "scorer_id",
    "scorer_version",
    "gpu_sku",
}
FAULT_REQUIRED = {"run_id", "lease_id", "error_kind", "scorer_id", "scorer_version"}
FAULT_OPTIONAL = {"detail"}
FAULT_KINDS = {"infra", "nondeterministic", "model_load", "invalid_model"}
SCORE_TOLERANCE = 1e-6


def _unit(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _uuid(v: Any) -> bool:
    try:
        uuid.UUID(str(v))
    except ValueError:
        return False
    return True


def _components_errors(c: Any, where: str, required: set[str]) -> list[str]:
    if not isinstance(c, dict):
        return [f"{where}: not an object"]
    errs = [f"{where}: missing {k}" for k in sorted(required - set(c))]
    for k in ("memory_recall", "distractor_rate", "tool_recall", "score"):
        if k in c and not _unit(c[k]):
            errs.append(f"{where}.{k} not in [0, 1]")
    for k in ("memory_cases", "tool_cases"):
        if k in c and not _count(c[k]):
            errs.append(f"{where}.{k} not a count")
    if c.get("packed_tokens") is not None and not _count(c["packed_tokens"]):
        errs.append(f"{where}.packed_tokens not a count")
    return errs


def result_errors(body: dict, seeds: list[int]) -> list[str]:
    """What `ModelContextResultRequest` + `check_result` would refuse."""
    errs = [f"missing {k}" for k in sorted(RESULT_REQUIRED - set(body))]
    if errs:
        return errs
    if not (_uuid(body["run_id"]) and _uuid(body["lease_id"])):
        errs.append("run_id / lease_id not UUIDs")
    if body["cpm_score_version"] != 1:
        errs.append("cpm_score_version")
    for k, n in (("scorer_id", 120), ("scorer_version", 64), ("gpu_sku", 64)):
        if not isinstance(body[k], str) or not 1 <= len(body[k]) <= n:
            errs.append(f"{k} length")
    if not _unit(body["score"]):
        errs.append("score not in [0, 1]")
    errs += _components_errors(body["components"], "components", COMPONENT_REQUIRED)
    per_seed = body["per_seed"]
    if not isinstance(per_seed, list) or not 1 <= len(per_seed) <= 8:
        return [*errs, "per_seed must hold 1..8 entries"]
    for i, s in enumerate(per_seed):
        errs += _components_errors(s, f"per_seed[{i}]", SEED_RESULT_REQUIRED)
    if errs:
        return errs
    if [s["seed"] % (1 << 64) for s in per_seed] != seeds:
        errs.append("per_seed seeds do not match the season's seeds in order")
    for s in per_seed:
        want = 0.6 * s["memory_recall"] * (1.0 - 0.5 * s["distractor_rate"])
        want += 0.4 * s["tool_recall"]
        if not math.isclose(s["score"], want, abs_tol=SCORE_TOLERANCE):
            errs.append(f"seed {s['seed']} score does not match its components")
    mean = sum(s["score"] for s in per_seed) / len(per_seed)
    if not math.isclose(body["score"], mean, abs_tol=SCORE_TOLERANCE):
        errs.append("score is not the per-seed mean")
    return errs


def fault_errors(body: dict) -> list[str]:
    """What `ModelContextFaultRequest` would refuse."""
    errs = [f"missing {k}" for k in sorted(FAULT_REQUIRED - set(body))]
    if errs:
        return errs
    if not (_uuid(body["run_id"]) and _uuid(body["lease_id"])):
        errs.append("run_id / lease_id not UUIDs")
    if body["error_kind"] not in FAULT_KINDS:
        errs.append(f"error_kind {body['error_kind']!r}")
    if len(body.get("detail", "")) > 4000:
        errs.append("detail longer than 4000")
    for k, n in (("scorer_id", 120), ("scorer_version", 64)):
        if not isinstance(body[k], str) or not 1 <= len(body[k]) <= n:
            errs.append(f"{k} length")
    return errs
