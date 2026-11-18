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
