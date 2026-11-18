"""`cpm-scorer serve`: lease work from Platform, score it, post the result.

Wire shapes (contract §5.4) match Platform's pydantic models in
`apps/platform/ditto/api_models/model_context.py` (#3575). All JSON, bearer
`DITTO_MODEL_CONTEXT_SCORER_TOKEN`.

- `GET /model-context-scorer/work?scorer_id=&scorer_version=&gpu_sku=` returns
  204 or a `ModelContextWorkItem`: `{run_id, lease_id, lease_expires_at,
  kind: "submission"|"reference", submission: {submission_id, hotkey, hf_repo,
  revision, manifest, manifest_sha256, weights_sha256, base_id, kind} | null,
  base: {base_id, hf_repo, revision, parameters, architectures,
  lora_target_modules}, season, seeds (uint64), generate_seeds (int64),
  bench_version, cpm_score_version, gpu_sku, settings: {budget_tokens, tool_k,
  floor_margin}}`. `kind=reference` scores the unmodified `base` (the season
  floor). Reference runs come only through `/work`; `/reference-work` is a
  status summary and is never leased from.
- `POST /model-context-scorer/results`: `ModelContextResultRequest`
  `{run_id, lease_id, cpm_score_version, score, components, per_seed,
  scorer_id, scorer_version, gpu_sku}`.
- `POST /model-context-scorer/fault`: `ModelContextFaultRequest`
  `{run_id, lease_id, error_kind, detail, scorer_id, scorer_version}`.

Platform accepts one result or fault per lease and answers a repeat with 409.
A 409 after a retried post (the first response was lost) is therefore treated
as already recorded; a 409 on the first attempt is an error.

The token is read from the environment or from the file named by
`DITTO_MODEL_CONTEXT_SCORER_TOKEN_FILE`. It is never logged or echoed.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from cpm_scorer import metrics
from cpm_scorer.bases import BASES
from cpm_scorer.generate import generator_seed_arg

TOKEN_ENV = "DITTO_MODEL_CONTEXT_SCORER_TOKEN"
TOKEN_FILE_ENV = "DITTO_MODEL_CONTEXT_SCORER_TOKEN_FILE"
SCORER_ID_ENV = "DITTO_MODEL_CONTEXT_SCORER_ID"
WORK_PATH = "/model-context-scorer/work"
RESULTS_PATH = "/model-context-scorer/results"
FAULT_PATH = "/model-context-scorer/fault"
# Platform stores fault detail behind its longest kind prefix (#3575):
# 2000 - len("nondeterministic: ") = 1982. Longer details get a 422.
MAX_DETAIL = 1982
MAX_SCORER_ID = 120
MAX_SCORER_VERSION = 64
MAX_GPU_SKU = 64
MAX_PER_SEED = 8
USER_AGENT = "ditto-cpm-scorer/1"
FAULT_KINDS = ("infra", "nondeterministic", "model_load", "invalid_model")
RUN_KINDS = ("submission", "reference")
_SCORER_ID_BAD = re.compile(r"[^A-Za-z0-9._:-]")
_INT64 = (-(2**63), 2**63)


class WorkFault(Exception):
    """A run-level failure reported to Platform with an `error_kind`."""

    def __init__(self, error_kind: str, detail: str = ""):
        if error_kind not in FAULT_KINDS:
            raise ValueError(f"unknown error_kind {error_kind!r}")
        super().__init__(f"{error_kind}: {detail}")
        self.error_kind = error_kind
        self.detail = detail


class AlreadyRecorded(Exception):
    """A retried post got 409: Platform recorded the earlier attempt."""


def load_token(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token and env.get(TOKEN_FILE_ENV):
        with open(env[TOKEN_FILE_ENV]) as f:
            token = f.read().strip()
    if not token:
        raise RuntimeError(f"set {TOKEN_ENV} or {TOKEN_FILE_ENV}")
    return token


def release_revision() -> str:
    """The release commit baked into the scorer image, or empty."""
    return os.environ.get("DITTO_CPM_SCORER_GIT_SHA", "").strip()


def scorer_version() -> str:
    """`<package version>+<git sha>`: the release image's commit, else the
    source tree's when it is a git checkout."""
    try:
        version = metadata.version("ditto-cpm-scorer")
    except metadata.PackageNotFoundError:
        version = "0"
    sha = release_revision()
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            sha = ""
    return (f"{version}+{sha}" if sha else version)[:MAX_SCORER_VERSION]


def sanitize_scorer_id(raw: str) -> str:
    """Platform accepts `^[A-Za-z0-9._:-]+$`, at most 120 characters."""
    return (_SCORER_ID_BAD.sub("-", raw) or "cpm-scorer")[:MAX_SCORER_ID]


def default_scorer_id() -> str:
    return sanitize_scorer_id(os.environ.get(SCORER_ID_ENV) or socket.gethostname())


class PlatformClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 60.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep

    def __repr__(self) -> str:  # never include the token
        return f"PlatformClient({self.base_url!r})"

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        route = path.split("?", 1)[0]
        for attempt in range(self.retries):
            req = urllib.request.Request(
                self.base_url + path, data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    try:
                        parsed = json.loads(raw) if raw else None
                    except ValueError as e:
                        last = RuntimeError(
                            f"{method} {route}: invalid JSON: {type(e).__name__}"
                        )
                    else:
                        return resp.status, parsed
            except urllib.error.HTTPError as e:
                if e.code == 409 and method == "POST" and attempt > 0:
                    raise AlreadyRecorded(f"{method} {route}: 409 on retry") from None
                if e.code < 500 and e.code != 429:
                    raise RuntimeError(f"{method} {route}: HTTP {e.code}") from None
                last = RuntimeError(f"{method} {route}: HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = RuntimeError(f"{method} {route}: {type(e).__name__}")
            if attempt + 1 < self.retries:
                self._sleep(2.0**attempt)
        assert last is not None
        raise last

    def get_work(
        self,
        scorer_id: str,
        scorer_version: str | None = None,
        gpu_sku: str | None = None,
    ) -> dict | None:
        query = {"scorer_id": scorer_id}
        if scorer_version:
            query["scorer_version"] = scorer_version
        if gpu_sku:
            query["gpu_sku"] = gpu_sku[:MAX_GPU_SKU]
        status, body = self._request(
            "GET", f"{WORK_PATH}?{urllib.parse.urlencode(query)}"
        )
        if status == 204 or not body:
            return None
        if not isinstance(body, dict):
            raise RuntimeError(f"GET {WORK_PATH}: expected an object")
        return body

    def _post_once_per_lease(self, path: str, payload: dict) -> str:
        try:
            self._request("POST", path, payload)
        except AlreadyRecorded:
            return "already recorded"
        return "recorded"

    def post_results(self, payload: dict) -> str:
        return self._post_once_per_lease(RESULTS_PATH, payload)

    def post_fault(self, payload: dict) -> str:
        return self._post_once_per_lease(FAULT_PATH, payload)


@dataclass(frozen=True)
class Work:
    run_id: str
    lease_id: str
    lease_expires_at: str | None
    kind: str
    submission: dict | None
    base: dict
    season: int
    seeds: list[int]
    generate_seeds: list[int]
    bench_version: int
    gpu_sku: str | None
    budget_tokens: int
    tool_k: int
    floor_margin: float | None


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_work(body: Mapping[str, Any]) -> Work:
    """Validate a `ModelContextWorkItem`. A malformed item is an `infra`
    fault: the scorer and Platform disagree, not the miner's model."""
    if body.get("cpm_score_version") != metrics.CPM_SCORE_VERSION:
        raise WorkFault(
            "infra", f"unsupported cpm_score_version {body.get('cpm_score_version')!r}"
        )
    seeds = body.get("seeds")
    if (
        not isinstance(seeds, list)
        or not 1 <= len(seeds) <= MAX_PER_SEED
        or not all(_is_int(s) and 0 <= s < 2**64 for s in seeds)
    ):
        raise WorkFault("infra", "seeds must be 1..8 uint64 integers")
    gen = body.get("generate_seeds")
    if (
        not isinstance(gen, list)
        or len(gen) != len(seeds)
        or not all(_is_int(g) and _INT64[0] <= g < _INT64[1] for g in gen)
    ):
        raise WorkFault("infra", "generate_seeds must be int64s, one per seed")
    if [str(g) for g in gen] != [generator_seed_arg(s) for s in seeds]:
        raise WorkFault("infra", "generate_seeds do not name the seeds")
    bv = body.get("bench_version")
    if not _is_int(bv) or bv <= 0:
        raise WorkFault("infra", "bench_version")
    season = body.get("season")
    if not _is_int(season):
        raise WorkFault("infra", "season")
    kind = body.get("kind")
    if kind not in RUN_KINDS:
        raise WorkFault("infra", f"kind {kind!r}")
    base = body.get("base")
    if not isinstance(base, dict) or not isinstance(base.get("base_id"), str):
        raise WorkFault("infra", "base")
    known = BASES.get(base["base_id"])
    if known is None:
        raise WorkFault("infra", f"this scorer does not know base {base['base_id']}")
    if (base.get("hf_repo"), base.get("revision")) != (known.repo, known.revision):
        raise WorkFault("infra", f"base {known.base_id} pin differs from this scorer's")
    sub = body.get("submission")
    if kind == "reference":
        if sub is not None:
            raise WorkFault("infra", "reference work carries a submission")
    elif not isinstance(sub, dict) or sub.get("base_id") != known.base_id:
        raise WorkFault("infra", "submission missing or on another base")
    settings = body.get("settings")
    if not isinstance(settings, dict):
        raise WorkFault("infra", "settings")
    budget, tool_k = settings.get("budget_tokens"), settings.get("tool_k")
    if not _is_int(budget) or budget <= 0 or not _is_int(tool_k) or tool_k <= 0:
        raise WorkFault("infra", "settings.budget_tokens / tool_k")
    sku = body.get("gpu_sku")
    if sku is not None and not isinstance(sku, str):
        raise WorkFault("infra", "gpu_sku")
    return Work(
        run_id=str(body["run_id"]),
        lease_id=str(body["lease_id"]),
        lease_expires_at=body.get("lease_expires_at"),
        kind=kind,
        submission=sub,
        base=base,
        season=season,
        seeds=list(seeds),
        generate_seeds=list(gen),
        bench_version=bv,
        gpu_sku=sku,
        budget_tokens=budget,
        tool_k=tool_k,
        floor_margin=settings.get("floor_margin"),
    )


def _components(src: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "memory_recall": src["memory_recall_at_b"],
        "distractor_rate": src["distractor_rate"],
        "tool_recall": src["tool_recall_at_k"],
    }


def results_payload(
    work: Work,
    result: Mapping[str, Any],
    *,
    scorer_id: str,
    scorer_version: str,
    gpu_sku: str,
) -> dict[str, Any]:
    """Map `score.run` output onto `ModelContextResultRequest`."""
    per_seed = [
        {
            "seed": s["seed"],
            "score": s["score"],
            **_components(s),
            "memory_cases": s["memory_cases"],
            "tool_cases": s["tool_cases"],
            "packed_tokens": s["packed_tokens"],
        }
        for s in result["per_seed"]
    ]
    if [s["seed"] for s in per_seed] != work.seeds:
        raise WorkFault("infra", "scored seeds differ from the work item's")
    return {
        "run_id": work.run_id,
        "lease_id": work.lease_id,
        "cpm_score_version": result["cpm_score_version"],
        "score": result["score"],
        "components": {
            **_components(result["components"]),
            "memory_cases": sum(s["memory_cases"] for s in per_seed),
            "tool_cases": sum(s["tool_cases"] for s in per_seed),
            "packed_tokens": sum(s["packed_tokens"] for s in per_seed),
        },
        "per_seed": per_seed,
        "scorer_id": scorer_id,
        "scorer_version": scorer_version,
        "gpu_sku": gpu_sku[:MAX_GPU_SKU],
    }


def fault_payload(
    run_id: str,
    lease_id: str,
    error_kind: str,
    detail: str,
    *,
    scorer_id: str,
    scorer_version: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "lease_id": lease_id,
        "error_kind": error_kind,
        "detail": detail[:MAX_DETAIL],
        "scorer_id": scorer_id,
        "scorer_version": scorer_version,
    }


ScoreFn = Callable[[Work], dict]


def handle(
    client: PlatformClient,
    body: Mapping[str, Any],
    score_fn: ScoreFn,
    *,
    scorer_id: str,
    scorer_version: str,
    gpu_sku: str,
) -> str:
    """Score one work item and post its result or fault. Returns the outcome."""
    run_id, lease_id = body.get("run_id"), body.get("lease_id")
    if not isinstance(run_id, str) or not isinstance(lease_id, str):
        return "unusable work item: no run_id / lease_id to fault"
    try:
        work = parse_work(body)
        if work.gpu_sku is not None and work.gpu_sku != gpu_sku[:MAX_GPU_SKU]:
            raise WorkFault(
                "infra", f"season is scored on {work.gpu_sku}; this host is {gpu_sku}"
            )
        result = score_fn(work)
        det = result.get("determinism") or {}
        if not det.get("identical"):
            raise WorkFault(
                "nondeterministic", f"seed {det.get('rerun_seed')} re-run differed"
            )
        payload = results_payload(
            work,
            result,
            scorer_id=scorer_id,
            scorer_version=scorer_version,
            gpu_sku=gpu_sku,
        )
    except WorkFault as e:
        kind, detail = e.error_kind, e.detail
    except Exception as e:  # host-side or unexpected: report, keep serving
        kind, detail = "infra", f"{type(e).__name__}: {e}"
    else:
        return f"scored ({client.post_results(payload)})"
    posted = client.post_fault(
        fault_payload(
            run_id,
            lease_id,
            kind,
            detail,
            scorer_id=scorer_id,
            scorer_version=scorer_version,
        )
    )
    return f"fault {kind} ({posted})"


def serve(
    client: PlatformClient,
    score_fn: ScoreFn,
    *,
    scorer_id: str,
    scorer_version: str,
    gpu_sku: str,
    poll_seconds: float = 30.0,
    once: bool = False,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Lease from `/work` (reference and submission runs alike) until stopped.

    `should_stop` drains: once it returns true the loop leases nothing more,
    so a run already leased is scored and posted before `serve` returns. The
    release updater uses this (SIGUSR1) to swap images without abandoning a
    lease.
    """
    while True:
        if should_stop():
            log("drain requested: no new leases, exiting")
            return
        try:
            body = client.get_work(scorer_id, scorer_version, gpu_sku)
        except RuntimeError as e:
            log(f"lease failed: {e}")
            body = None
        if body is not None:
            try:
                outcome = handle(
                    client,
                    body,
                    score_fn,
                    scorer_id=scorer_id,
                    scorer_version=scorer_version,
                    gpu_sku=gpu_sku,
                )
            except RuntimeError as e:  # posting failed after retries
                outcome = f"post failed: {e}"
            log(f"run {body.get('run_id')}: {outcome}")
        if once:
            return
        if body is None:
            sleep(poll_seconds)
