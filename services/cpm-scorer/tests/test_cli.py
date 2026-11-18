from __future__ import annotations

import json
import sys
import types

import pytest
from cpm_scorer import cli
from cpm_scorer.bases import BASES, get_base
from cpm_scorer.screening import ScreeningError


def test_other_base_repo_names_the_matching_base():
    small, big = get_base("qwen3.5-0.8b"), get_base("qwen3.5-2b")
    with pytest.raises(SystemExit) as e:
        cli.resolve_source(f"{big.repo}@{big.revision}", small)
    msg = str(e.value)
    assert f"--base {big.base_id}" in msg and small.revision not in msg


def test_same_base_repo_wrong_revision_names_its_revision():
    base = get_base("qwen3.5-0.8b")
    with pytest.raises(SystemExit) as e:
        cli.resolve_source(f"{base.repo}@{'0' * 40}", base)
    assert base.revision in str(e.value)


def test_load_scorer_screening_error_is_structured(monkeypatch, tmp_path, capsys):
    base = BASES["qwen3.5-0.8b"]
    source = types.SimpleNamespace(base=base, kind="full", path=tmp_path)
    monkeypatch.setattr(cli, "resolve_source", lambda *_a, **_k: source)
    monkeypatch.setattr(cli, "_generate_bin", lambda *_a, **_k: tmp_path / "gen")

    def fail(*_a, **_k):
        raise ScreeningError("full_tensors", "unexpected tensor names")

    fake_score = types.ModuleType("cpm_scorer.score")
    fake_score.load_scorer = fail
    monkeypatch.setitem(sys.modules, "cpm_scorer.score", fake_score)
    import cpm_scorer

    monkeypatch.setattr(cpm_scorer, "score", fake_score, raising=False)

    args = cli.build_parser().parse_args(
        ["score", "--model", str(tmp_path), "--cache-dir", str(tmp_path)]
    )
    assert cli.cmd_score(args) == cli.EXIT_SCREENING
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"screening": "full_tensors", "detail": "unexpected tensor names"}


def _serve_args(tmp_path):
    return cli.build_parser().parse_args(
        ["serve", "--platform", "http://x", "--cache-dir", str(tmp_path)]
    )


def _work(kind="submission"):
    from cpm_scorer.serve import Work

    b = BASES["qwen3.5-0.8b"]
    sub = {
        "hf_repo": "miner/cpm",
        "revision": "a" * 40,
        "base_id": b.base_id,
        "kind": "lora",
        "manifest_sha256": "b" * 64,
        "weights_sha256": "c" * 64,
    }
    return Work(
        run_id="r",
        lease_id="l",
        lease_expires_at=None,
        kind=kind,
        submission=sub if kind == "submission" else None,
        base={"base_id": b.base_id, "hf_repo": b.repo, "revision": b.revision},
        season=1,
        seeds=[2**64 - 1],
        generate_seeds=[-1],
        bench_version=18,
        gpu_sku=None,
        budget_tokens=2048,
        tool_k=8,
        floor_margin=0.01,
    )


@pytest.fixture
def fake_score(monkeypatch, tmp_path):
    """Stub the heavy scoring path; each test sets the failure it needs."""
    from cpm_scorer import score

    calls = {}
    monkeypatch.setattr(cli, "_generate_bin", lambda *_a, **_k: tmp_path / "gen")
    monkeypatch.setattr(cli, "fetch_hf", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(score, "screen_dir", lambda *_a, **_k: ("lora", "b" * 64))
    loaded = types.SimpleNamespace(weights_sha256="c" * 64)
    monkeypatch.setattr(score, "load_scorer", lambda *_a, **_k: loaded)

    def run(*a, **k):
        calls["run"] = (a, k)
        return {"ok": True}

    monkeypatch.setattr(score, "run", run)
    return types.SimpleNamespace(score=score, calls=calls, loaded=loaded)


@pytest.mark.parametrize(
    "target,exc,kind",
    [
        ("fetch_hf", OSError("network"), "model_load"),
        ("fetch_hf", ScreeningError("forbidden_file", "x.py"), "invalid_model"),
        ("screen_dir", ScreeningError("lora_rank", "r=128"), "invalid_model"),
        ("load_scorer", ScreeningError("architecture_mismatch", "x"), "invalid_model"),
        ("load_scorer", RuntimeError("cuda init"), "model_load"),
    ],
)
def test_serve_fault_kinds(fake_score, monkeypatch, tmp_path, target, exc, kind):
    from cpm_scorer.serve import WorkFault

    owner = cli if target == "fetch_hf" else fake_score.score

    def fail(*_a, **_k):
        raise exc

    monkeypatch.setattr(owner, target, fail)
    fn = cli.make_score_fn(_serve_args(tmp_path))
    with pytest.raises(WorkFault) as e:
        fn(_work())
    assert e.value.error_kind == kind


def test_serve_weights_mismatch_and_generate_seeds(fake_score, tmp_path):
    from cpm_scorer.serve import WorkFault

    fn = cli.make_score_fn(_serve_args(tmp_path))
    assert fn(_work()) == {"ok": True}
    args, kwargs = fake_score.calls["run"]
    assert args[1] == [2**64 - 1] and kwargs["generate_seeds"] == [-1]
    fake_score.loaded.weights_sha256 = "d" * 64
    with pytest.raises(WorkFault) as e:
        fn(_work())
    assert e.value.error_kind == "invalid_model"


def test_serve_reference_scores_the_base(fake_score, monkeypatch, tmp_path):
    seen = {}

    def load(source, *_a, **_k):
        seen["source"] = source
        return fake_score.loaded

    def no_download(*_a, **_k):
        raise AssertionError("reference runs download nothing")

    monkeypatch.setattr(fake_score.score, "load_scorer", load)
    monkeypatch.setattr(cli, "fetch_hf", no_download)
    fn = cli.make_score_fn(_serve_args(tmp_path))
    assert fn(_work("reference")) == {"ok": True}
    assert seen["source"].kind == "base" and seen["source"].path is None
