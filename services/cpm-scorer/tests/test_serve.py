"""The serve client against a fake Platform that enforces the #3575 wire
models (see `platform_wire.py`) and Platform's once-per-lease rule."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import platform_wire as pw
import pytest
from cpm_scorer import metrics
from cpm_scorer import serve as sv
from cpm_scorer.bases import BASES

TOKEN = "test-token-not-a-secret"
SEEDS = [1, 2, 2**64 - 1]
GEN_SEEDS = [1, 2, -1]
SKU = "NVIDIA L40S"


class FakePlatform:
    def __init__(self):
        self.work: list[dict] = []
        self.results: list[dict] = []
        self.faults: list[dict] = []
        self.paths: list[str] = []
        self.queries: list[dict] = []
        self.posted_leases: set[str] = set()
        self.auth_failures = 0
        self.fail_next_posts = 0  # 503 without recording
        self.lose_next_responses = 0  # record, then 503 (response lost)
        self.seeds = SEEDS
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    fake.auth_failures += 1
                    self._json(401, {"detail": "invalid scorer bearer token"})
                    return False
                return True

            def _json(self, code: int, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                url = urlsplit(self.path)
                fake.paths.append(url.path)
                if not self._auth():
                    return
                if url.path != sv.WORK_PATH:
                    return self._json(404, {"detail": "not found"})
                q = parse_qs(url.query)
                fake.queries.append(q)
                if not q.get("scorer_id"):
                    return self._json(422, {"detail": "scorer_id required"})
                with fake.lock:
                    item = fake.work.pop(0) if fake.work else None
                if item is None:
                    self.send_response(204)
                    self.end_headers()
                    return
                self._json(200, item)

            def do_POST(self):
                fake.paths.append(self.path)
                if not self._auth():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if fake.fail_next_posts:
                    fake.fail_next_posts -= 1
                    return self._json(503, {"detail": "busy"})
                if self.path == sv.RESULTS_PATH:
                    errs, sink = pw.result_errors(body, fake.seeds), fake.results
                elif self.path == sv.FAULT_PATH:
                    errs, sink = pw.fault_errors(body), fake.faults
                else:
                    return self._json(404, {"detail": "not found"})
                if errs:
                    return self._json(422, {"detail": errs})
                with fake.lock:
                    if body["lease_id"] in fake.posted_leases:
                        return self._json(409, {"detail": "lease is not current"})
                    fake.posted_leases.add(body["lease_id"])
                    sink.append(body)
                if fake.lose_next_responses:
                    fake.lose_next_responses -= 1
                    return self._json(503, {"detail": "response lost"})
                self._json(200, {"run_id": body["run_id"], "status": "scored"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def platform():
    p = FakePlatform()
    yield p
    p.close()


def _client(platform, token=TOKEN):
    return sv.PlatformClient(platform.url, token, timeout=5, sleep=lambda _s: None)


def _base(base_id="qwen3.5-0.8b") -> dict:
    b = BASES[base_id]
    return {
        "base_id": b.base_id,
        "hf_repo": b.repo,
        "revision": b.revision,
        "parameters": 752393024,
        "architectures": ["Qwen3_5ForCausalLM"],
        "lora_target_modules": ["q_proj", "v_proj"],
    }


def _work(kind="submission", **over) -> dict:
    """A `ModelContextWorkItem` as Platform serialises it."""
    w = {
        "run_id": str(uuid.uuid4()),
        "lease_id": str(uuid.uuid4()),
        "lease_expires_at": "2026-11-18T12:00:00Z",
        "kind": kind,
        "submission": (
            {
                "submission_id": str(uuid.uuid4()),
                "hotkey": "5F" + "a" * 46,
                "hf_repo": "miner/cpm",
                "revision": "a" * 40,
                "manifest": {"schema": "ditto-cpm-manifest-v1"},
                "manifest_sha256": "b" * 64,
                "weights_sha256": "c" * 64,
                "base_id": "qwen3.5-0.8b",
                "kind": "lora",
            }
            if kind == "submission"
            else None
        ),
        "base": _base(),
        "season": 3,
        "seeds": list(SEEDS),
        "generate_seeds": list(GEN_SEEDS),
        "bench_version": 18,
        "cpm_score_version": 1,
        "gpu_sku": None,
        "settings": {"budget_tokens": 2048, "tool_k": 8, "floor_margin": 0.01},
    }
    assert set(w) == pw.WORK_ITEM_KEYS
    assert w["submission"] is None or set(w["submission"]) == pw.WORK_SUBMISSION_KEYS
    assert set(w["base"]) == pw.BASE_KEYS
    assert set(w["settings"]) == pw.WORK_SETTINGS_KEYS
    w.update(over)
    return w


def _seed(seed: int, mem: float, dis: float, tool: float) -> dict:
    comps = {
        "memory_recall_at_b": mem,
        "distractor_rate": dis,
        "tool_recall_at_k": tool,
    }
    return {
        "seed": seed,
        "dataset_sha256": "d" * 64,
        **comps,
        "score": metrics.seed_score(comps),
        "memory_cases": 107,
        "tool_cases": 47,
        "pairs_scored": 85053,
        "model_input_tokens": 1000,
        "packed_tokens": 190000,
        "packed_tokens_mean": 1775.7,
        "scores_sha256": "e" * 64,
    }


def _result(identical=True, seeds=SEEDS) -> dict:
    """What `score.run` returns, with its full provenance."""
    per_seed = [_seed(s, 0.5 + 0.1 * i, 0.1, 0.3) for i, s in enumerate(seeds)]
    agg = metrics.aggregate(per_seed)
    return {
        "cpm_score_version": 1,
        "score": agg["score"],
        "components": {
            k: agg[k]
            for k in ("memory_recall_at_b", "distractor_rate", "tool_recall_at_k")
        },
        "per_seed": per_seed,
        "tokens": {"model_input_tokens": 3000},
        "determinism": {"rerun_seed": seeds[0], "identical": identical},
        "model": {"weights_sha256": "c" * 64},
        "bench_version": 18,
        "generator_sha256": "f" * 64,
        "device": "cuda",
        "gpu_sku": SKU,
    }


def _serve(platform, score_fn, *, sku=SKU, logs=None):
    sv.serve(
        _client(platform),
        score_fn,
        scorer_id="scorer-1",
        scorer_version="0.1.0+abc",
        gpu_sku=sku,
        once=True,
        log=(logs.append if logs is not None else lambda _m: None),
    )


def test_load_token_env_and_file(tmp_path):
    assert sv.load_token({sv.TOKEN_ENV: " t1\n"}) == "t1"
    f = tmp_path / "tok"
    f.write_text("t2\n")
    assert sv.load_token({sv.TOKEN_FILE_ENV: str(f)}) == "t2"
    with pytest.raises(RuntimeError) as e:
        sv.load_token({})
    assert sv.TOKEN_ENV in str(e.value)


def test_client_repr_hides_token(platform):
    assert TOKEN not in repr(_client(platform))


def test_scorer_identity_fits_platform():
    assert sv.sanitize_scorer_id("gpu host/1 (L40S)") == "gpu-host-1--L40S-"
    assert len(sv.sanitize_scorer_id("x" * 500)) == sv.MAX_SCORER_ID
    v = sv.scorer_version()
    assert 1 <= len(v) <= sv.MAX_SCORER_VERSION and v.startswith("0.1.0")


def test_get_work_sends_identity_and_handles_204(platform):
    c = _client(platform)
    assert c.get_work("scorer-1", "0.1.0+abc", SKU) is None
    platform.work.append(_work())
    assert c.get_work("scorer-1")["kind"] == "submission"
    assert platform.queries[0] == {
        "scorer_id": ["scorer-1"],
        "scorer_version": ["0.1.0+abc"],
        "gpu_sku": [SKU],
    }


def test_scored_submission_posts_a_platform_valid_result(platform):
    item = _work()
    platform.work.append(item)
    seen = []

    def score_fn(work: sv.Work) -> dict:
        seen.append(work)
        return _result()

    logs = []
    _serve(platform, score_fn, logs=logs)
    (w,) = seen
    assert w.kind == "submission" and w.seeds == SEEDS
    assert w.generate_seeds == GEN_SEEDS
    assert (w.budget_tokens, w.tool_k, w.floor_margin) == (2048, 8, 0.01)
    (r,) = platform.results
    assert set(r) == pw.RESULT_REQUIRED
    assert r["run_id"] == item["run_id"] and r["lease_id"] == item["lease_id"]
    assert r["gpu_sku"] == SKU and r["scorer_version"] == "0.1.0+abc"
    assert set(r["components"]) == pw.COMPONENT_REQUIRED | pw.COMPONENT_OPTIONAL
    assert r["components"]["memory_cases"] == 3 * 107
    assert r["components"]["packed_tokens"] == 3 * 190000
    assert all(
        set(s) == pw.SEED_RESULT_REQUIRED | pw.COMPONENT_OPTIONAL for s in r["per_seed"]
    )
    assert [s["seed"] for s in r["per_seed"]] == SEEDS
    assert not platform.faults
    assert logs == [f"run {item['run_id']}: scored (recorded)"]


def test_reference_runs_come_from_work_only(platform):
    platform.work.append(_work(kind="reference"))
    seen = []

    def score_fn(work):
        seen.append(work)
        return _result()

    _serve(platform, score_fn)
    assert seen[0].kind == "reference" and seen[0].submission is None
    assert seen[0].base["base_id"] == "qwen3.5-0.8b"
    assert len(platform.results) == 1
    assert "/model-context-scorer/reference-work" not in platform.paths


def test_nondeterministic_run_faults(platform):
    item = _work()
    platform.work.append(item)
    _serve(platform, lambda _w: _result(identical=False))
    assert not platform.results
    (f,) = platform.faults
    assert f["error_kind"] == "nondeterministic"
    assert (f["run_id"], f["lease_id"]) == (item["run_id"], item["lease_id"])
    assert set(f) == pw.FAULT_REQUIRED | pw.FAULT_OPTIONAL


@pytest.mark.parametrize(
    "over",
    [
        {"cpm_score_version": 2},
        {"seeds": [], "generate_seeds": []},
        {"seeds": [True, 2, 3], "generate_seeds": [1, 2, 3]},
        {"seeds": [-1, 2, 3]},
        {"generate_seeds": [1, 2]},
        {"generate_seeds": [1, 2, 2**64 - 1]},  # not int64
        {"generate_seeds": [True, 2, -1]},
        {"generate_seeds": [1, 3, -1]},  # names another seed
        {"bench_version": "18"},
        {"kind": "rerun"},
        {"submission": None},
        {"base": {**_base(), "revision": "0" * 40}},
        {"base": {**_base(), "base_id": "llama-1b"}},
        {"settings": {"budget_tokens": True, "tool_k": 8, "floor_margin": 0.01}},
    ],
)
def test_bad_work_items_are_infra_faults(platform, over):
    platform.work.append(_work(**over))
    _serve(platform, lambda _w: pytest.fail("must not score"))
    (f,) = platform.faults
    assert f["error_kind"] == "infra"


def test_reference_work_with_a_submission_is_infra(platform):
    item = _work(kind="reference")
    item["submission"] = _work()["submission"]
    platform.work.append(item)
    _serve(platform, lambda _w: pytest.fail("must not score"))
    assert platform.faults[0]["error_kind"] == "infra"


def test_gpu_sku_mismatch_is_infra_without_scoring(platform):
    platform.work.append(_work(gpu_sku="NVIDIA H100"))
    _serve(platform, lambda _w: pytest.fail("must not score"))
    (f,) = platform.faults
    assert f["error_kind"] == "infra" and "NVIDIA H100" in f["detail"]
    platform.work.append(_work(gpu_sku=SKU))
    _serve(platform, lambda _w: _result())
    assert len(platform.results) == 1


def test_score_fn_faults_keep_their_kind_and_errors_are_infra(platform):
    platform.work.append(_work())

    def invalid(_w):
        raise sv.WorkFault("invalid_model", "lora_rank: r=128")

    _serve(platform, invalid)
    platform.work.append(_work())

    def crash(_w):
        raise ValueError("x" * 5000)

    _serve(platform, crash)
    f1, f2 = platform.faults
    assert (f1["error_kind"], f1["detail"]) == ("invalid_model", "lora_rank: r=128")
    assert f2["error_kind"] == "infra" and len(f2["detail"]) == sv.MAX_DETAIL
    with pytest.raises(ValueError):
        sv.WorkFault("screening:lora_rank")


def test_post_retries_on_5xx(platform):
    platform.work.append(_work())
    platform.fail_next_posts = 2
    _serve(platform, lambda _w: _result())
    assert len(platform.results) == 1


def test_409_after_a_lost_response_is_already_recorded(platform):
    item = _work()
    platform.work.append(item)
    platform.lose_next_responses = 1
    logs = []
    _serve(platform, lambda _w: _result(), logs=logs)
    assert len(platform.results) == 1 and not platform.faults
    assert logs == [f"run {item['run_id']}: scored (already recorded)"]


def test_409_on_the_first_attempt_raises(platform):
    c = _client(platform)
    item = _work()
    platform.posted_leases.add(item["lease_id"])
    payload = sv.fault_payload(
        item["run_id"],
        item["lease_id"],
        "infra",
        "x",
        scorer_id="s",
        scorer_version="v",
    )
    with pytest.raises(RuntimeError) as e:
        c.post_fault(payload)
    assert "409" in str(e.value)
    platform.work.append(item)
    logs = []
    _serve(platform, lambda _w: _result(), logs=logs)
    assert "post failed" in logs[0] and "409" in logs[0]


def test_work_item_without_ids_is_not_faulted(platform):
    item = _work()
    del item["lease_id"]
    platform.work.append(item)
    logs = []
    _serve(platform, lambda _w: pytest.fail("must not score"), logs=logs)
    assert not platform.faults and "unusable" in logs[0]


def test_bad_token_is_not_retried_or_leaked(platform):
    c = _client(platform, token="wrong-token")
    with pytest.raises(RuntimeError) as e:
        c.get_work("s")
    assert "401" in str(e.value) and "wrong-token" not in str(e.value)
    assert platform.auth_failures == 1


def test_serve_survives_lease_errors():
    c = sv.PlatformClient("http://127.0.0.1:9", TOKEN, timeout=1, sleep=lambda _s: None)
    logs = []
    sv.serve(
        c,
        lambda _w: _result(),
        scorer_id="s",
        scorer_version="v",
        gpu_sku=SKU,
        once=True,
        log=logs.append,
    )
    assert logs and all("lease failed" in line for line in logs)
    assert all(TOKEN not in line for line in logs)


def test_malformed_json_2xx_is_retried_then_raised():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            raw = b"not json"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        c = sv.PlatformClient(
            f"http://127.0.0.1:{srv.server_address[1]}",
            TOKEN,
            timeout=5,
            sleep=lambda _s: None,
        )
        with pytest.raises(RuntimeError) as e:
            c.get_work("s")
        assert "invalid JSON" in str(e.value) and TOKEN not in str(e.value)
        assert len(hits) == c.retries
    finally:
        srv.shutdown()
        srv.server_close()


def test_results_payload_maps_run_output_only():
    work = sv.parse_work(_work())
    payload = sv.results_payload(
        work, _result(), scorer_id="s", scorer_version="v", gpu_sku="x" * 100
    )
    assert pw.result_errors(payload, SEEDS) == []
    assert len(payload["gpu_sku"]) == 64
    assert "tokens" not in payload and "determinism" not in payload
    with pytest.raises(sv.WorkFault):
        sv.results_payload(
            work,
            _result(seeds=[1, 2, 3]),
            scorer_id="s",
            scorer_version="v",
            gpu_sku="g",
        )


@pytest.mark.parametrize("detail", [7, None, "x" * 1983])
def test_wire_fault_detail_checks_do_not_crash(detail):
    body = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "lease_id": "00000000-0000-0000-0000-000000000002",
        "error_kind": "infra",
        "detail": detail,
        "scorer_id": "s",
        "scorer_version": "v",
    }
    errs = pw.fault_errors(body)
    assert (errs == []) == (detail is None)


def test_drain_finishes_the_leased_run_then_leases_nothing(platform):
    first, second = _work(), _work()
    platform.work.extend([first, second])
    stop = threading.Event()
    seen = []

    def score_fn(work):
        seen.append(work.run_id)
        stop.set()  # SIGUSR1 arrives while this run is being scored
        return _result()

    logs = []
    sv.serve(
        _client(platform),
        score_fn,
        scorer_id="scorer-1",
        scorer_version="0.1.0+abc",
        gpu_sku=SKU,
        log=logs.append,
        sleep=stop.wait,
        should_stop=stop.is_set,
    )
    # The run in hand is scored and posted; the queued one is never leased.
    assert seen == [first["run_id"]]
    assert [r["run_id"] for r in platform.results] == [first["run_id"]]
    assert platform.work == [second]
    assert logs[-1] == "drain requested: no new leases, exiting"


def test_drain_wakes_an_idle_scorer(platform):
    stop = threading.Event()
    timer = threading.Timer(0.05, stop.set)
    timer.start()
    sv.serve(
        _client(platform),
        lambda _w: _result(),
        scorer_id="scorer-1",
        scorer_version="0.1.0+abc",
        gpu_sku=SKU,
        poll_seconds=3600,
        log=lambda _m: None,
        sleep=stop.wait,
        should_stop=stop.is_set,
    )
    timer.join()
    assert not platform.results


def test_release_revision_names_the_scorer_version(monkeypatch):
    monkeypatch.setenv("DITTO_CPM_SCORER_GIT_SHA", "f" * 40)
    assert sv.release_revision() == "f" * 40
    version = sv.scorer_version()
    assert version.endswith("+" + "f" * 40)
    assert len(version) <= sv.MAX_SCORER_VERSION
