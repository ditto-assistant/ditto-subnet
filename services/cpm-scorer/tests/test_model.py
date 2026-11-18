"""Model path on a tiny randomly initialised Qwen3.5 (no downloads)."""

from __future__ import annotations

import json

import pytest
import torch
import torch.nn.functional as F
from cpm_scorer import model as m
from cpm_scorer import score
from cpm_scorer.bases import BASES
from cpm_scorer.labels import export_records
from cpm_scorer.screening import (
    check_full,
    check_lora,
    read_headers,
    read_safetensors_header,
)
from cpm_scorer.tasks import build_tasks
from peft import LoraConfig, get_peft_model
from tiny import tiny_model, tiny_tokenizer

WORDS = [
    "hello",
    "world",
    "dentist",
    "Alvarez",
    "Brown",
    "poll",
    "group",
    "send",
    "chat",
    "vendors",
]


@pytest.fixture(scope="module", autouse=True)
def deterministic():
    m.configure_determinism("cpu")


@pytest.fixture
def tok():
    return tiny_tokenizer(WORDS)


def test_shift_conv_matches_reference_conv1d():
    g = torch.Generator().manual_seed(1)
    x = torch.randn(3, 12, 9, generator=g)
    w = torch.randn(12, 4, generator=g)
    b = torch.randn(12, generator=g)
    ref = F.silu(F.conv1d(x, w.unsqueeze(1), b, padding=3, groups=12)[..., :9])
    out = m.shift_causal_conv1d(x, w, b, activation="silu")
    torch.testing.assert_close(out, ref, rtol=1e-5, atol=1e-5)


def test_scores_are_deterministic_and_padding_invariant(tok):
    scorer = m.RelevanceScorer(tiny_model(len(tok)), tok, "cpu")
    rng = torch.Generator().manual_seed(2)
    pairs = []
    for i in range(70):  # three batches, ragged lengths
        n = int(torch.randint(1, 12, (1,), generator=rng))
        idx = torch.randint(0, len(WORDS), (n,), generator=rng).tolist()
        pairs.append((f"hello {i % 3}", " ".join(WORDS[j] for j in idx)))
    a, tokens = scorer.score(pairs)
    b, _ = scorer.score(pairs)
    assert a == b and len(a) == 70 and tokens > 0
    assert score.scores_digest(a) == score.scores_digest(b)
    # A pair scored alone (no padding) matches its padded in-batch score.
    alone, _ = scorer.score([pairs[5]])
    assert alone[0] == pytest.approx(a[5], abs=1e-4)


def test_score_is_yes_minus_no_logit(tok):
    net = tiny_model(len(tok))
    scorer = m.RelevanceScorer(net, tok, "cpu")
    (s,), _ = scorer.score([("hello", "world")])
    text = scorer.render("hello", "world")
    assert text.endswith("<think> </think> ")
    ids = torch.tensor([tok(text, add_special_tokens=False)["input_ids"]])
    with torch.no_grad():
        logits = net(input_ids=ids).logits[0, -1]
    (yes,) = tok.encode("yes", add_special_tokens=False)
    (no,) = tok.encode("no", add_special_tokens=False)
    assert s == pytest.approx(float(logits[yes] - logits[no]), abs=1e-4)


def test_truncation(tok):
    scorer = m.RelevanceScorer(tiny_model(len(tok)), tok, "cpu")
    text = scorer.render(" ".join(["hello"] * 300), " ".join(["world"] * 1000))
    assert text.count("hello") == m.QUERY_MAX_TOKENS
    assert text.count("world") == m.CANDIDATE_MAX_TOKENS


def test_truncation_cache_is_bounded_oldest_first(tok, monkeypatch):
    monkeypatch.setattr(m, "TRUNC_CACHE_MAX", 3)
    scorer = m.RelevanceScorer(tiny_model(len(tok)), tok, "cpu")
    for word in ("hello", "world", "poll", "group"):
        assert scorer.truncate(word, 5) == word
    assert list(scorer._trunc_cache) == [("world", 5), ("poll", 5), ("group", 5)]
    scorer.truncate("world", 5)  # a hit does not reorder or grow
    assert len(scorer._trunc_cache) == 3


def test_lora_save_screen_and_merge(tok, tmp_path):
    base = tiny_model(len(tok), seed=0)
    cfg = LoraConfig(
        r=4,
        lora_alpha=8,
        target_modules=["q_proj", "v_proj", "in_proj_qkv", "down_proj"],
    )
    peft_model = get_peft_model(tiny_model(len(tok), seed=0), cfg)
    with torch.no_grad():
        for name, p in peft_model.named_parameters():
            if "lora_B" in name:
                p.copy_(torch.randn_like(p) * 0.5)
    peft_model.save_pretrained(tmp_path)
    adapter_cfg = json.loads((tmp_path / "adapter_config.json").read_text())
    # The real peft layout must pass the screening rules.
    check_lora(
        adapter_cfg, read_safetensors_header(tmp_path / "adapter_model.safetensors")
    )
    assert score.detect_kind(tmp_path) == "lora"
    kind, msha = score.screen_dir(tmp_path, "qwen3.5-0.8b")
    assert kind == "lora" and msha is None

    pairs = [("hello", "world dentist"), ("poll", "send group")]
    base_scores, _ = m.RelevanceScorer(base, tok, "cpu").score(pairs)
    merged = m.apply_lora(tiny_model(len(tok), seed=0), tmp_path)
    merged_scores, _ = m.RelevanceScorer(merged, tok, "cpu").score(pairs)
    assert merged_scores != base_scores
    ids = torch.tensor(
        [
            tok(
                m.RelevanceScorer(merged, tok, "cpu").render(*pairs[0]),
                add_special_tokens=False,
            )["input_ids"]
        ]
    )
    with torch.no_grad():
        logits = peft_model(input_ids=ids).logits[0, -1]
    (yes,) = tok.encode("yes", add_special_tokens=False)
    (no,) = tok.encode("no", add_special_tokens=False)
    assert merged_scores[0] == pytest.approx(float(logits[yes] - logits[no]), abs=1e-4)


def test_full_finetune_roundtrip(tok, tmp_path):
    tuned = tiny_model(len(tok), seed=7)
    tuned.save_pretrained(tmp_path)
    paths = sorted(tmp_path.glob("model*.safetensors"))
    assert paths and score.detect_kind(tmp_path) == "full"
    base = tiny_model(len(tok), seed=0)
    headers = {k: info for k, (_, info) in read_headers(paths).items()}
    check_full(headers, m.base_tensor_shapes(base))
    loaded = m.apply_full(base, paths)
    for k, v in tuned.state_dict().items():
        assert torch.equal(loaded.state_dict()[k], v), k
    # The tied head follows the loaded embeddings.
    assert (
        loaded.lm_head.weight.data_ptr() == loaded.model.embed_tokens.weight.data_ptr()
    )


def _loaded(tok, kind="base"):
    src = score.ModelSource(BASES["qwen3.5-0.8b"], kind, None, "tiny")
    return score.LoadedScorer(
        m.RelevanceScorer(tiny_model(len(tok)), tok, "cpu"), src, None
    )


def test_score_seed_on_fixture(tok, tiny_artifact):
    tasks = build_tasks(export_records(tiny_artifact, "v18-5"))
    rec, raw = score.score_seed(
        _loaded(tok).scorer, tasks, 2048, 8, log=lambda _m: None
    )
    # 2 memory cases (4 + 3 candidates, each including the tool prerequisite
    # pair tp1) + 1 tool case x 3 tools
    assert rec["pairs_scored"] == len(raw) == 4 + 3 + 3
    # The budget fits everything and the catalog has < 8 tools. m1's relevant
    # id "missing" is not a candidate, so m1 recall is 1/2 and the mean 0.75:
    # recall counts every relevant id, candidate or not (contract §4.3).
    assert rec["memory_recall_at_b"] == 0.75 and rec["tool_recall_at_k"] == 1.0
    assert 0.0 <= rec["distractor_rate"] <= 1.0
    assert 0.0 <= rec["score"] <= 1.0
    tight, _ = score.score_seed(_loaded(tok).scorer, tasks, 5, 1, log=lambda _m: None)
    assert tight["memory_recall_at_b"] < 1.0
    assert tight["tool_recall_at_k"] in (0.0, 0.5)  # top-1 of a 2-tool target


def test_run_output_shape(tok, tiny_artifact, tmp_path, monkeypatch):
    art = tmp_path / "art.json"
    art.write_text(json.dumps(tiny_artifact))
    gen = tmp_path / "generate"
    gen.write_bytes(b"fake")
    monkeypatch.setattr(score, "generate", lambda *_a, **_k: art)
    out = score.run(_loaded(tok), [42, 7], 18, gen, tmp_path, log=lambda _m: None)
    assert out["cpm_score_version"] == 1
    assert set(out["components"]) == {
        "memory_recall_at_b",
        "distractor_rate",
        "tool_recall_at_k",
    }
    assert [s["seed"] for s in out["per_seed"]] == [42, 7]
    assert out["determinism"] == {"rerun_seed": 42, "identical": True}
    assert out["score"] == pytest.approx(sum(s["score"] for s in out["per_seed"]) / 2)
    assert out["tokens"]["pairs_scored"] == 20
    json.dumps(out)  # JSON-serialisable
