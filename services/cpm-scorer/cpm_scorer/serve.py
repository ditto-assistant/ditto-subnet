"""`cpm-scorer serve`: lease work from Platform, score it, post the result.

Wire shapes (contract §5.4), all JSON, bearer `DITTO_MODEL_CONTEXT_SCORER_TOKEN`:

- `GET /model-context-scorer/reference-work` and `GET /model-context-scorer/work`
  return 204 (nothing to do) or
  `{run_id, submission, season, seeds, bench_version, cpm_score_version,
  settings}`. `submission` is `{hf_repo, revision, base_id, kind,
  manifest_sha256, weights_sha256?}`; reference work (a season's base floor)
  has `submission.kind == "base"` and no repo.
- `POST /model-context-scorer/results` with `{run_id, season,
  cpm_score_version, score, components, per_seed, tokens, determinism,
  weights_sha256, bench_version, generator_sha256, scorer_id, gpu_sku}`.
- `POST /model-context-scorer/fault` with `{run_id, fault, detail, scorer_id}`.

The token is read from the environment or from the file named by
`DITTO_MODEL_CONTEXT_SCORER_TOKEN_FILE`. It is never logged or echoed.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cpm_scorer import metrics

TOKEN_ENV = "DITTO_MODEL_CONTEXT_SCORER_TOKEN"
TOKEN_FILE_ENV = "DITTO_MODEL_CONTEXT_SCORER_TOKEN_FILE"
WORK_PATH = "/model-context-scorer/work"
REFERENCE_WORK_PATH = "/model-context-scorer/reference-work"
RESULTS_PATH = "/model-context-scorer/results"
FAULT_PATH = "/model-context-scorer/fault"
MAX_DETAIL = 2000
USER_AGENT = "ditto-cpm-scorer/1"


class WorkFault(Exception):
    """A run-level failure reported to Platform as a fault code."""

    def __init__(self, fault: str, detail: str = ""):
        super().__init__(f"{fault}: {detail}")
        self.fault = fault
        self.detail = detail


def load_token(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token and env.get(TOKEN_FILE_ENV):
        with open(env[TOKEN_FILE_ENV]) as f:
            token = f.read().strip()
    if not token:
        raise RuntimeError(f"set {TOKEN_ENV} or {TOKEN_FILE_ENV}")
    return token


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
                            f"{method} {path}: invalid JSON: {type(e).__name__}"
                        )
                    else:
                        return resp.status, parsed
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    raise RuntimeError(f"{method} {path}: HTTP {e.code}") from None
                last = RuntimeError(f"{method} {path}: HTTP {e.code}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = RuntimeError(f"{method} {path}: {type(e).__name__}")
            if attempt + 1 < self.retries:
                self._sleep(2.0**attempt)
        assert last is not None
        raise last

    def get_work(self, path: str = WORK_PATH) -> dict | None:
        status, body = self._request("GET", path)
        if status == 204 or not body:
            return None
        if not isinstance(body, dict):
            raise RuntimeError(f"GET {path}: expected an object")
        return body

    def post_results(self, payload: dict) -> None:
        self._request("POST", RESULTS_PATH, payload)

    def post_fault(self, run_id: Any, fault: str, detail: str, scorer_id: str) -> None:
        self._request(
            "POST",
            FAULT_PATH,
            {
                "run_id": run_id,
                "fault": fault,
                "detail": detail[:MAX_DETAIL],
                "scorer_id": scorer_id,
            },
        )


@dataclass(frozen=True)
class Work:
    run_id: Any
    season: Any
    seeds: list[int]
    bench_version: int
    submission: dict
    budget_tokens: int
    tool_k: int


def parse_work(body: dict) -> Work:
    if body.get("cpm_score_version") != metrics.CPM_SCORE_VERSION:
        raise WorkFault(
            "unsupported_score_version", repr(body.get("cpm_score_version"))
        )
    seeds = body.get("seeds")
    if (
        not isinstance(seeds, list)
        or not seeds
        or not all(isinstance(s, int) and 0 <= s < 2**64 for s in seeds)
    ):
        raise WorkFault("bad_work", "seeds must be a non-empty list of uint64")
    bv = body.get("bench_version")
    if not isinstance(bv, int) or bv <= 0:
        raise WorkFault("bad_work", "bench_version")
    sub = body.get("submission")
    if not isinstance(sub, dict) or not sub.get("base_id"):
        raise WorkFault("bad_work", "submission")
    settings = body.get("settings") or {}
    return Work(
        run_id=body.get("run_id"),
        season=body.get("season"),
        seeds=seeds,
        bench_version=bv,
        submission=sub,
        budget_tokens=int(settings.get("budget_tokens", metrics.BUDGET_TOKENS)),
        tool_k=int(settings.get("tool_k", metrics.TOOL_K)),
    )


ScoreFn = Callable[[Work], dict]


def handle(
    client: PlatformClient, body: dict, score_fn: ScoreFn, scorer_id: str
) -> str:
    """Score one work item and post results or a fault. Returns the outcome."""
    run_id = body.get("run_id")
    try:
        work = parse_work(body)
        result = score_fn(work)
        det = result.get("determinism") or {}
        if not det.get("identical"):
            raise WorkFault(
                "nondeterministic", f"seed {det.get('rerun_seed')} re-run differed"
            )
    except WorkFault as e:
        client.post_fault(run_id, e.fault, e.detail, scorer_id)
        return e.fault
    except Exception as e:  # report, keep serving
        client.post_fault(run_id, "scorer_error", f"{type(e).__name__}: {e}", scorer_id)
        return "scorer_error"
    payload = {
        "run_id": run_id,
        "season": work.season,
        "cpm_score_version": result["cpm_score_version"],
        "score": result["score"],
        "components": result["components"],
        "per_seed": result["per_seed"],
        "tokens": result["tokens"],
        "determinism": result["determinism"],
        "weights_sha256": result["model"].get("weights_sha256"),
        "bench_version": result["bench_version"],
        "generator_sha256": result["generator_sha256"],
        "scorer_id": scorer_id,
        "gpu_sku": result.get("gpu_sku") or result.get("device"),
    }
    client.post_results(payload)
    return "scored"


def default_scorer_id() -> str:
    return os.environ.get("DITTO_MODEL_CONTEXT_SCORER_ID") or socket.gethostname()


def serve(
    client: PlatformClient,
    score_fn: ScoreFn,
    *,
    scorer_id: str,
    poll_seconds: float = 30.0,
    once: bool = False,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll reference work first (season floors), then submission work."""
    while True:
        did = False
        for path in (REFERENCE_WORK_PATH, WORK_PATH):
            try:
                body = client.get_work(path)
            except RuntimeError as e:
                log(f"lease failed: {e}")
                body = None
            if body is None:
                continue
            try:
                outcome = handle(client, body, score_fn, scorer_id)
            except RuntimeError as e:  # posting failed after retries
                outcome = f"post failed: {e}"
            log(f"run {body.get('run_id')}: {outcome}")
            did = True
            break
        if once:
            return
        if not did:
            sleep(poll_seconds)
