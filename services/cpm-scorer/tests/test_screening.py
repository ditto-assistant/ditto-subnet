from __future__ import annotations

import copy
import hashlib
import json
import struct

import pytest
import torch
from cpm_scorer import screening as s
from safetensors.torch import save_file


def _manifest(**over) -> dict:
    m = {
        "schema": "ditto-cpm-manifest-v1",
        "track": "model_context",
        "base_id": "qwen3.5-0.8b",
        "kind": "lora",
        "files": [
            {"path": "adapter_config.json", "sha256": "a" * 64, "bytes": 812},
            {"path": "adapter_model.safetensors", "sha256": "b" * 64, "bytes": 1234},
        ],
        "license": "apache-2.0",
        "training_data_attestation": "public DittoBench seeds only",
    }
    m.update(over)
    return m


def _reason(fn, *a, **kw) -> str:
    with pytest.raises(s.ScreeningError) as e:
        fn(*a, **kw)
    return e.value.reason


# --- manifest ----------------------------------------------------------------


def test_valid_lora_manifest():
    m = s.validate_manifest(_manifest())
    assert m.kind == "lora" and m.total_bytes == 812 + 1234


def test_valid_full_manifest():
    files = [
        {"path": "config.json", "sha256": "c" * 64, "bytes": 10},
        {"path": "model-00001-of-00002.safetensors", "sha256": "d" * 64, "bytes": 5},
        {"path": "model-00002-of-00002.safetensors", "sha256": "e" * 64, "bytes": 5},
        {"path": "model.safetensors.index.json", "sha256": "f" * 64, "bytes": 5},
    ]
    assert s.validate_manifest(_manifest(kind="full", files=files)).kind == "full"


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"schema": "v0"}, "invalid_manifest"),
        ({"track": "router"}, "invalid_manifest"),
        ({"base_id": "llama"}, "base_not_allowed"),
        ({"kind": "merge"}, "invalid_manifest"),
        ({"license": "cc-by-nc"}, "license"),
        ({"training_data_attestation": ""}, "invalid_manifest"),
        ({"extra": 1}, "invalid_manifest"),
        ({"files": []}, "invalid_manifest"),
        ({"base_id": ["qwen3.5-0.8b"]}, "base_not_allowed"),
        ({"license": {"spdx": "mit"}}, "license"),
    ],
)
def test_manifest_rejections(over, reason):
    assert _reason(s.validate_manifest, _manifest(**over)) == reason


@pytest.mark.parametrize(
    "path,reason",
    [
        ("../adapter_config.json", "invalid_manifest"),
        ("/etc/passwd.json", "invalid_manifest"),
        ("model.py", "forbidden_file"),
        ("pytorch_model.bin", "forbidden_file"),
        ("cpm-manifest.json", "invalid_manifest"),
    ],
)
def test_manifest_file_path_rejections(path, reason):
    m = _manifest()
    m["files"].append({"path": path, "sha256": "0" * 64, "bytes": 1})
    assert _reason(s.validate_manifest, m) == reason


def test_manifest_hash_and_kind_rules():
    m = _manifest()
    m["files"][0]["sha256"] = "XYZ"
    assert _reason(s.validate_manifest, m) == "invalid_manifest"
    m = _manifest()
    m["files"] = m["files"][:1]
    assert _reason(s.validate_manifest, m) == "invalid_manifest"
    m = _manifest()
    m["files"].append({"path": "extra.safetensors", "sha256": "0" * 64, "bytes": 1})
    assert _reason(s.validate_manifest, m) == "invalid_manifest"
    dup = _manifest()
    dup["files"].append(dict(dup["files"][0]))
    assert _reason(s.validate_manifest, dup) == "invalid_manifest"


def test_manifest_size_cap():
    m = _manifest()
    m["files"][1]["bytes"] = s.MAX_TOTAL_BYTES
    assert _reason(s.validate_manifest, m) == "too_large"


def test_manifest_sha256_is_exact_bytes():
    raw = b'{"a": 1}\n'
    assert s.manifest_sha256(raw) == hashlib.sha256(raw).hexdigest()


def test_verify_files(tmp_path):
    (tmp_path / "adapter_config.json").write_bytes(b"{}")
    (tmp_path / "adapter_model.safetensors").write_bytes(b"xyz")
    m = _manifest()
    m["files"] = [
        {
            "path": "adapter_config.json",
            "sha256": hashlib.sha256(b"{}").hexdigest(),
            "bytes": 2,
        },
        {
            "path": "adapter_model.safetensors",
            "sha256": hashlib.sha256(b"xyz").hexdigest(),
            "bytes": 3,
        },
    ]
    s.verify_files(tmp_path, s.validate_manifest(m))
    (tmp_path / "adapter_model.safetensors").write_bytes(b"xyw")
    assert _reason(s.verify_files, tmp_path, s.validate_manifest(m)) == "hash_mismatch"
    (tmp_path / "adapter_model.safetensors").unlink()
    assert _reason(s.verify_files, tmp_path, s.validate_manifest(m)) == "missing_file"


def test_scan_repo_files():
    s.scan_repo_files(["README.md", "adapter_config.json", "tokenizer.json"])
    for bad in ("modeling_x.py", "sub/pytorch_model.bin", "w.PKL", "opt.pt"):
        assert _reason(s.scan_repo_files, ["README.md", bad]) == "forbidden_file"


def test_remote_code_config():
    s.check_config_no_remote_code({"model_type": "qwen3_5_text"}, "config.json")
    assert (
        _reason(s.check_config_no_remote_code, {"auto_map": {}}, "c") == "remote_code"
    )


# --- safetensors and weights_sha256 ---------------------------------------------


def _tensors() -> dict[str, torch.Tensor]:
    g = torch.Generator().manual_seed(0)
    return {
        "b.weight": torch.randn(3, 4, generator=g).to(torch.bfloat16),
        "a.weight": torch.randn(5, generator=g),
        "c.idx": torch.arange(6, dtype=torch.int64).reshape(2, 3),
    }


def _expected_stream(tensors: dict[str, torch.Tensor]) -> str:
    """Independent re-statement of the canonical stream (module docstring)."""
    names = {torch.bfloat16: "BF16", torch.float32: "F32", torch.int64: "I64"}
    h = hashlib.sha256()
    for name in sorted(tensors):
        t = tensors[name].contiguous()
        raw = t.view(torch.uint8).numpy().tobytes() if t.dim() else b""
        nb, db = name.encode(), names[t.dtype].encode()
        h.update(struct.pack("<I", len(nb)) + nb + struct.pack("<I", len(db)) + db)
        h.update(struct.pack("<I", t.dim()))
        for d in t.shape:
            h.update(struct.pack("<Q", d))
        h.update(struct.pack("<Q", len(raw)) + raw)
    return h.hexdigest()


def test_weights_sha256_matches_spec(tmp_path):
    t = _tensors()
    p = tmp_path / "model.safetensors"
    save_file(t, str(p))
    assert s.weights_sha256([p]) == _expected_stream(t)


def test_weights_sha256_is_shard_and_metadata_invariant(tmp_path):
    t = _tensors()
    one = tmp_path / "model.safetensors"
    save_file(t, str(one), metadata={"format": "pt"})
    a = tmp_path / "model-00001-of-00002.safetensors"
    b = tmp_path / "model-00002-of-00002.safetensors"
    save_file({"c.idx": t["c.idx"]}, str(a))
    save_file({"b.weight": t["b.weight"], "a.weight": t["a.weight"]}, str(b))
    assert (
        s.weights_sha256([one]) == s.weights_sha256([a, b]) == s.weights_sha256([b, a])
    )


def test_weights_sha256_is_sensitive(tmp_path):
    t = _tensors()
    base = tmp_path / "base.safetensors"
    save_file(t, str(base))
    ref = s.weights_sha256([base])
    variants = []
    v = copy.deepcopy(t)
    v["a.weight"][0] += 1
    variants.append(v)
    v = copy.deepcopy(t)
    v["a.weight"] = v["a.weight"].to(torch.float16)
    variants.append(v)
    v = copy.deepcopy(t)
    v["b.weight"] = v["b.weight"].reshape(4, 3)
    variants.append(v)
    v = copy.deepcopy(t)
    v["z.weight"] = v.pop("a.weight")
    variants.append(v)
    for i, v in enumerate(variants):
        p = tmp_path / f"v{i}.safetensors"
        save_file(v, str(p))
        assert s.weights_sha256([p]) != ref, i


def test_duplicate_tensor_across_shards(tmp_path):
    t = _tensors()
    a, b = tmp_path / "a.safetensors", tmp_path / "b.safetensors"
    save_file(t, str(a))
    save_file({"a.weight": t["a.weight"]}, str(b))
    assert _reason(s.weights_sha256, [a, b]) == "bad_safetensors"


def test_corrupt_safetensors(tmp_path):
    p = tmp_path / "x.safetensors"
    p.write_bytes(struct.pack("<Q", 10**12) + b"{}")
    assert _reason(s.read_safetensors_header, p) == "bad_safetensors"
    header = json.dumps(
        {"w": {"dtype": "F32", "shape": [4], "data_offsets": [0, 8]}}
    ).encode()
    p.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 16)
    assert _reason(s.read_safetensors_header, p) == "bad_safetensors"


def test_check_finite(tmp_path):
    ok = tmp_path / "ok.safetensors"
    save_file({"w": torch.ones(3), "i": torch.arange(3)}, str(ok))
    s.check_finite([ok])
    for bad_val in (float("nan"), float("inf")):
        p = tmp_path / "bad.safetensors"
        w = torch.ones(3, dtype=torch.bfloat16)
        w[1] = bad_val
        save_file({"w": w}, str(p))
        assert _reason(s.check_finite, [p]) == "non_finite"


# --- LoRA and full -------------------------------------------------------------


def _lora_cfg(**over) -> dict:
    cfg = {
        "peft_type": "LORA",
        "r": 16,
        "lora_alpha": 32,
        "target_modules": ["q_proj", "v_proj", "in_proj_qkv"],
        "modules_to_save": None,
    }
    cfg.update(over)
    return cfg


def _lora_tensors(tmp_path, rank=16, module="q_proj") -> dict:
    p = tmp_path / "adapter_model.safetensors"
    prefix = f"base_model.model.model.layers.0.self_attn.{module}"
    save_file(
        {
            f"{prefix}.lora_A.weight": torch.zeros(rank, 8),
            f"{prefix}.lora_B.weight": torch.zeros(8, rank),
        },
        str(p),
    )
    return s.read_safetensors_header(p)


def test_lora_ok(tmp_path):
    s.check_lora(_lora_cfg(), _lora_tensors(tmp_path))
    s.check_lora(_lora_cfg(r=64), _lora_tensors(tmp_path, rank=64))


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"r": 65}, "lora_rank"),
        ({"r": 0}, "lora_rank"),
        ({"rank_pattern": {"q_proj": 128}}, "lora_rank"),
        ({"rank_pattern": ["q_proj"]}, "lora_rank"),
        ({"rank_pattern": "q_proj"}, "lora_rank"),
        ({"target_modules": ["q_proj", "embed_tokens"]}, "lora_target"),
        ({"target_modules": ".*"}, "lora_target"),
        ({"target_modules": []}, "lora_target"),
        ({"modules_to_save": ["lm_head"]}, "lora_config"),
        ({"peft_type": "IA3"}, "lora_config"),
        ({"use_dora": True}, "lora_config"),
        ({"auto_map": {"x": "y"}}, "remote_code"),
    ],
)
def test_lora_config_rejections(tmp_path, over, reason):
    assert _reason(s.check_lora, _lora_cfg(**over), _lora_tensors(tmp_path)) == reason


def test_lora_tensor_rejections(tmp_path):
    # Config says r=16 but the tensors carry rank 96.
    assert (
        _reason(s.check_lora, _lora_cfg(), _lora_tensors(tmp_path, rank=96))
        == "lora_rank"
    )
    assert (
        _reason(s.check_lora, _lora_cfg(), _lora_tensors(tmp_path, module="lm_head"))
        == "lora_target"
    )
    p = tmp_path / "x.safetensors"
    save_file({"model.embed_tokens.weight": torch.zeros(2, 2)}, str(p))
    assert (
        _reason(s.check_lora, _lora_cfg(), s.read_safetensors_header(p))
        == "lora_target"
    )


def test_check_full(tmp_path):
    base = {"a.weight": (100, 10), "b.weight": (10,)}
    p = tmp_path / "model.safetensors"
    save_file({"a.weight": torch.zeros(100, 10), "b.weight": torch.zeros(10)}, str(p))
    s.check_full(s.read_safetensors_header(p), base)
    save_file({"a.weight": torch.zeros(100, 10), "c.weight": torch.zeros(10)}, str(p))
    assert (
        _reason(s.check_full, s.read_safetensors_header(p), base)
        == "architecture_mismatch"
    )
    save_file({"a.weight": torch.zeros(100, 12), "b.weight": torch.zeros(10)}, str(p))
    assert _reason(s.check_full, s.read_safetensors_header(p), base) == "param_count"
    save_file({"a.weight": torch.zeros(10, 100), "b.weight": torch.zeros(10)}, str(p))
    assert (
        _reason(s.check_full, s.read_safetensors_header(p), base)
        == "architecture_mismatch"
    )


def test_check_full_config():
    base = {"model_type": "qwen3_5_text", "architectures": ["Qwen3_5ForCausalLM"]}
    s.check_full_config(dict(base), base)
    assert (
        _reason(s.check_full_config, {**base, "model_type": "llama"}, base)
        == "architecture_mismatch"
    )
    assert _reason(s.check_full_config, {**base, "auto_map": {}}, base) == "remote_code"


def test_lora_null_rank_pattern_is_accepted(tmp_path):
    s.check_lora(_lora_cfg(rank_pattern=None), _lora_tensors(tmp_path))
