"""miners/cpm-starter-kit/train_lora.py on the tiny model (no downloads)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys

import pytest
import torch
from conftest import REPO_ROOT
from cpm_scorer import model as m
from cpm_scorer import score
from cpm_scorer.labels import export_records
from cpm_scorer.screening import (
    MANIFEST_NAME,
    ScreeningError,
    check_lora,
    read_safetensors_header,
    validate_manifest,
    verify_files,
)
from cpm_scorer.tasks import build_tasks
from peft import LoraConfig, get_peft_model
from tiny import tiny_model, tiny_tokenizer

TRAIN = REPO_ROOT / "miners" / "cpm-starter-kit" / "train_lora.py"
WORDS = ["hello", "world", "dentist", "Alvarez", "Brown", "poll", "send", "group"]


@pytest.fixture(scope="module")
def kit():
    if not TRAIN.is_file():
        pytest.skip("miners/cpm-starter-kit not in this checkout")
    spec = importlib.util.spec_from_file_location("train_lora", TRAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through it
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_groups_have_positives_and_hard_negatives(kit, tiny_artifact):
    tasks = build_tasks(export_records(tiny_artifact, "v18-5"))
    groups = kit.groups_for_seed(tasks, random.Random(0), group_size=3)
    assert groups and all(any(g.labels) for g in groups)
    assert all(len(g.texts) <= 3 for g in groups)
    by_query = {g.query: g for g in groups}
    dentist = by_query["Who is my dentist?"]
    # p2 is relevant; p1 carries the "Dr. Brown" distractor and comes first.
    assert dentist.labels[0] is True
    assert dentist.texts[1] == tasks.pair_text["p1"]
    tool = by_query["Send the poll"]
    # One positive (group_size // 2); the other target is never a negative.
    assert tool.labels == (True, False)
    assert tool.texts == ("send_poll: Send a poll.", "delete_all: Dangerous.")


def test_listwise_loss(kit):
    s = torch.tensor([2.0, 0.0, 0.0])
    loss = kit.listwise_loss(s, [True, False, False])
    assert float(loss) == pytest.approx(float(torch.logsumexp(s, 0) - 2.0), abs=1e-6)
    assert float(kit.listwise_loss(s, [True, True, True])) == pytest.approx(0.0)


def test_one_step_trains_and_writes_a_valid_manifest(kit, tiny_artifact, tmp_path):
    tok = tiny_tokenizer(WORDS)
    net = tiny_model(len(tok))
    render = m.RelevanceScorer(net, tok, "cpu").render
    net = get_peft_model(
        net, LoraConfig(r=4, lora_alpha=8, target_modules=["q_proj", "in_proj_qkv"])
    )
    net.train()
    tasks = build_tasks(export_records(tiny_artifact, "v18-5"))
    (g, *_) = kit.groups_for_seed(tasks, random.Random(0), group_size=3)
    opt = torch.optim.AdamW([p for p in net.parameters() if p.requires_grad], lr=1e-2)
    inner = net.get_base_model()
    before = kit.listwise_loss(kit.group_scores(inner, tok, render, g, "cpu"), g.labels)
    before.backward()
    assert any(
        p.grad is not None and p.grad.abs().sum() > 0
        for n, p in net.named_parameters()
        if "lora_" in n
    )
    opt.step()
    net.save_pretrained(tmp_path)
    msha = kit.write_manifest(tmp_path, "qwen3.5-0.8b", "apache-2.0", "tiny test")
    raw = (tmp_path / MANIFEST_NAME).read_bytes()
    manifest = validate_manifest(json.loads(raw))
    verify_files(tmp_path, manifest)
    check_lora(
        json.loads((tmp_path / "adapter_config.json").read_text()),
        read_safetensors_header(tmp_path / "adapter_model.safetensors"),
    )
    assert msha == hashlib.sha256(raw).hexdigest()
    # The scorer's directory screening accepts it and pins the manifest hash.
    assert score.screen_dir(tmp_path, "qwen3.5-0.8b", msha) == ("lora", msha)
    for args, reason in [
        (("qwen3.5-0.8b", "0" * 64), "hash_mismatch"),
        (("qwen3.5-2b",), "base_mismatch"),
    ]:
        with pytest.raises(ScreeningError) as e:
            score.screen_dir(tmp_path, *args)
        assert e.value.reason == reason
    extra = tmp_path / "model-extra.safetensors"
    extra.write_bytes((tmp_path / "adapter_model.safetensors").read_bytes())
    with pytest.raises(ScreeningError) as e:
        score.screen_dir(tmp_path, "qwen3.5-0.8b")
    assert e.value.reason == "unlisted_file"


def test_accum_window_sizes_cover_a_partial_final_window(kit):
    sizes = [kit.accum_window_size(i, 10, 4) for i in range(1, 11)]
    assert sizes == [4, 4, 4, 4, 4, 4, 4, 4, 2, 2]
    # Each optimiser step sums its window's scaled losses to a mean (weight 1).
    for start in (0, 4, 8):
        window = sizes[start : start + 4]
        assert sum(1 / s for s in window) == pytest.approx(1.0)
    assert [kit.accum_window_size(i, 3, 1) for i in (1, 2, 3)] == [1, 1, 1]
    assert [kit.accum_window_size(i, 3, 8) for i in (1, 2, 3)] == [3, 3, 3]
    with pytest.raises(ValueError):
        kit.accum_window_size(0, 3, 2)


@pytest.mark.parametrize(
    "argv,msg",
    [
        (["--rank", "65"], "--rank must be in 1..64"),
        (["--rank", "0"], "--rank must be in 1..64"),
        (["--targets", "q_proj,embed_tokens"], "not allowed: ['embed_tokens']"),
        (["--targets", ","], "at least one module"),
        (["--accum", "0"], "--accum must be at least 1"),
    ],
)
def test_bad_lora_args_fail_before_any_work(kit, monkeypatch, capsys, argv, msg):
    def boom(*_a, **_k):
        pytest.fail("must fail before generating data")

    monkeypatch.setattr(kit, "generate_records", boom)
    monkeypatch.setattr(kit, "load_export", boom)
    with pytest.raises(SystemExit) as e:
        kit.main(["--seeds", "101", "--out", "/tmp/unused", *argv])
    assert e.value.code == 2 and msg in capsys.readouterr().err
