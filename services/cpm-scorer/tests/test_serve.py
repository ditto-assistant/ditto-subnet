"""The serve client against a fake Platform (contract §5.4 wire shapes)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cpm_scorer import serve as sv

TOKEN = "test-token-not-a-secret"


class FakePlatform:
    def __init__(self):
        self.work: list[dict] = []
        self.reference: list[dict] = []
        self.results: list[dict] = []
        self.faults: list[dict] = []
        self.auth_failures = 0
        self.fail_next_posts = 0
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _auth(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    fake.auth_failures += 1
                    self.send_response(401)
                    self.end_headers()
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
                if not self._auth():
                    return
                queue = {
                    sv.WORK_PATH: fake.work,
                    sv.REFERENCE_WORK_PATH: fake.reference,
                }.get(self.path)
                if queue is None:
                    return self._json(404, {"error": "not found"})
                with fake.lock:
                    item = queue.pop(0) if queue else None
                if item is None:
                    self.send_response(204)
                    self.end_headers()
                    return
                self._json(200, item)

            def do_POST(self):
                if not self._auth():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if fake.fail_next_posts:
                    fake.fail_next_posts -= 1
                    return self._json(503, {"error": "busy"})
                target = {sv.RESULTS_PATH: fake.results, sv.FAULT_PATH: fake.faults}
                if self.path not in target:
                    return self._json(404, {"error": "not found"})
                target[self.path].append(body)
                self._json(200, {"ok": True})

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


def _work(run_id=1, **over) -> dict:
    w = {
        "run_id": run_id,
        "submission": {
            "hf_repo": "miner/cpm",
            "revision": "a" * 40,
            "base_id": "qwen3.5-0.8b",
            "kind": "lora",
            "manifest_sha256": "b" * 64,
        },
        "season": 3,
        "seeds": [1, 2, 2**64 - 1],
        "bench_version": 18,
        "cpm_score_version": 1,
        "settings": {"budget_tokens": 2048, "tool_k": 8},
    }
    w.update(over)
    return w


def _result(identical=True) -> dict:
    return {
        "cpm_score_version": 1,
        "score": 0.42,
        "components": {
            "memory_recall_at_b": 0.5,
            "distractor_rate": 0.1,
            "tool_recall_at_k": 0.3,
        },
        "per_seed": [{"seed": 1, "score": 0.42}],
        "tokens": {"model_input_tokens": 10},
        "determinism": {"rerun_seed": 1, "identical": identical},
        "model": {"weights_sha256": "c" * 64},
        "bench_version": 18,
        "generator_sha256": "d" * 64,
        "device": "cuda",
        "gpu_sku": "NVIDIA L40S",
    }


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


def test_get_work_204_and_200(platform):
    c = _client(platform)
    assert c.get_work() is None
    platform.work.append(_work())
    assert c.get_work()["run_id"] == 1


def test_scored_run_posts_results(platform):
    platform.work.append(_work(run_id=7))
    seen = []

    def score_fn(work: sv.Work) -> dict:
        seen.append(work)
        return _result()

    logs = []
    sv.serve(_client(platform), score_fn, scorer_id="s1", once=True, log=logs.append)
    assert seen[0].seeds == [1, 2, 2**64 - 1]
    assert seen[0].budget_tokens == 2048 and seen[0].tool_k == 8
    (r,) = platform.results
    assert r["run_id"] == 7 and r["season"] == 3 and r["score"] == 0.42
    assert r["weights_sha256"] == "c" * 64 and r["gpu_sku"] == "NVIDIA L40S"
    assert r["scorer_id"] == "s1" and r["determinism"]["identical"] is True
    assert not platform.faults
    assert all(TOKEN not in line for line in logs)


def test_reference_work_is_leased_first(platform):
    platform.work.append(_work(run_id=2))
    platform.reference.append(
        _work(run_id=1, submission={"base_id": "qwen3.5-0.8b", "kind": "base"})
    )
    order = []

    def score_fn(work):
        order.append(work.run_id)
        return _result()

    sv.serve(_client(platform), score_fn, scorer_id="s", once=True, log=lambda _m: None)
    sv.serve(_client(platform), score_fn, scorer_id="s", once=True, log=lambda _m: None)
    assert order == [1, 2]


def test_nondeterministic_run_faults(platform):
    platform.work.append(_work(run_id=9))
    sv.serve(
        _client(platform),
        lambda _w: _result(identical=False),
        scorer_id="s",
        once=True,
        log=lambda _m: None,
    )
    assert not platform.results
    (f,) = platform.faults
    assert f["run_id"] == 9 and f["fault"] == "nondeterministic"


@pytest.mark.parametrize(
    "over,fault",
    [
        ({"cpm_score_version": 2}, "unsupported_score_version"),
        ({"seeds": []}, "bad_work"),
        ({"seeds": [-1]}, "bad_work"),
        ({"bench_version": "18"}, "bad_work"),
        ({"submission": None}, "bad_work"),
    ],
)
def test_bad_work_faults(platform, over, fault):
    platform.work.append(_work(**over))
    sv.serve(
        _client(platform),
        lambda _w: pytest.fail("must not score"),
        scorer_id="s",
        once=True,
        log=lambda _m: None,
    )
    assert platform.faults[0]["fault"] == fault


def test_score_fn_errors_become_faults(platform):
    platform.work.append(_work(run_id=4))

    def boom(_w):
        raise sv.WorkFault("screening:lora_rank", "r=128")

    sv.serve(_client(platform), boom, scorer_id="s", once=True, log=lambda _m: None)
    platform.work.append(_work(run_id=5))

    def crash(_w):
        raise ValueError("x" * 5000)

    sv.serve(_client(platform), crash, scorer_id="s", once=True, log=lambda _m: None)
    f1, f2 = platform.faults
    assert f1 == {
        "run_id": 4,
        "fault": "screening:lora_rank",
        "detail": "r=128",
        "scorer_id": "s",
    }
    assert f2["fault"] == "scorer_error" and len(f2["detail"]) == sv.MAX_DETAIL


def test_post_retries_on_5xx(platform):
    platform.work.append(_work(run_id=3))
    platform.fail_next_posts = 2
    sv.serve(
        _client(platform),
        lambda _w: _result(),
        scorer_id="s",
        once=True,
        log=lambda _m: None,
    )
    assert [r["run_id"] for r in platform.results] == [3]


def test_bad_token_is_not_retried_or_leaked(platform):
    c = _client(platform, token="wrong-token")
    with pytest.raises(RuntimeError) as e:
        c.get_work()
    assert "401" in str(e.value) and "wrong-token" not in str(e.value)
    assert platform.auth_failures == 1


def test_serve_survives_lease_errors():
    c = sv.PlatformClient("http://127.0.0.1:9", TOKEN, timeout=1, sleep=lambda _s: None)
    logs = []
    sv.serve(c, lambda _w: _result(), scorer_id="s", once=True, log=logs.append)
    assert logs and all("lease failed" in line for line in logs)
    assert all(TOKEN not in line for line in logs)
