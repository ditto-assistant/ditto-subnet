"""Static screening, seeds and commitment decoding for the model_context lane."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from uuid import UUID

import pytest

from ditto.api_models.model_context import (
    DEFAULT_BASES,
    ModelContextSettings,
    commitment_value,
    generate_seed,
    season_seeds,
    seed_score,
    signature_payload,
)
from ditto.chain.client import decode_commitment_registration
from ditto.model_context_artifacts import (
    ArtifactRejected,
    canonical_weights_sha256,
    check_full,
    parse_manifest,
    read_safetensors,
    screen_files,
)
from ditto.tests.model_context_fixtures import (
    FakeFetcher,
    lora_repo,
    lora_tensors,
    manifest_for,
    safetensors_bytes,
)

_SETTINGS = ModelContextSettings(mode="shadow")
_BASE = DEFAULT_BASES[0]


def _write(tmp: Path, files: dict[str, bytes]) -> dict[str, Path]:
    out = {}
    for name, raw in files.items():
        (tmp / name).write_bytes(raw)
        out[name] = tmp / name
    return out


def test_wire_formats_are_pinned() -> None:
    nonce = UUID("4b0d7e8e-5f3c-4c4e-9a8b-1e2f3a4b5c6d")
    assert (
        signature_payload(
            hotkey="5Hot",
            hf_repo="miner/cpm",
            revision="a" * 40,
            manifest_sha256="b" * 64,
            ts=1_700_000_000,
            nonce=nonce,
        )
        == (
            f"ditto-model-context-v1:5Hot:miner/cpm:{'a' * 40}:{'b' * 64}:"
            f"1700000000:{nonce}"
        ).encode()
    )
    assert commitment_value("c" * 64) == f"dcpm1:{'c' * 64}"


def test_season_seeds_follow_the_contract_formula() -> None:
    block_hash = "0x" + "ab" * 32
    seeds = season_seeds(3, block_hash)
    assert len(seeds) == 3
    for k, seed in enumerate(seeds):
        digest = hashlib.sha256(f"dcpm-season:3:{k}:{block_hash}".encode()).digest()
        assert seed == int.from_bytes(digest[:8], "big")
        assert 0 <= seed < 1 << 64
    assert season_seeds(4, block_hash) != seeds


def test_generate_seed_is_int64_twos_complement() -> None:
    """``cmd/generate -seed`` is a signed int64 (contract #3572 clarification)."""
    assert generate_seed(0) == 0
    assert generate_seed((1 << 63) - 1) == (1 << 63) - 1
    assert generate_seed(1 << 63) == -(1 << 63)
    assert generate_seed((1 << 64) - 1) == -1
    for bad in (-1, 1 << 64):
        with pytest.raises(ValueError):
            generate_seed(bad)


# Shared with services/cpm-scorer (PR #3574): its ``weights_sha256`` returns
# this digest for these exact tensors, computed by running that module.
WEIGHTS_VECTOR = {
    "b.weight": ("BF16", [3, 4], bytes(range(24))),
    "a.weight": ("F32", [5], bytes(range(100, 120))),
}
WEIGHTS_VECTOR_SHA256 = (
    "cb56f3dee2cc79b6c1cf0fe95d932f3baea83da0aec9763640fecf3c449289dd"
)


def test_weights_hash_matches_the_cpm_scorer_vector(tmp_path: Path) -> None:
    raw = safetensors_bytes(WEIGHTS_VECTOR)  # type: ignore[arg-type]
    path = _write(tmp_path, {"v.safetensors": raw})["v.safetensors"]
    assert canonical_weights_sha256([read_safetensors(path)]) == WEIGHTS_VECTOR_SHA256

    # And the layout, restated independently: u32 lengths and ndim, u64 dims
    # and byte count, tensors in name order.
    digest = hashlib.sha256()
    for name in sorted(WEIGHTS_VECTOR):
        dtype, shape, data = WEIGHTS_VECTOR[name]
        digest.update(struct.pack("<I", len(name)) + name.encode())
        digest.update(struct.pack("<I", len(dtype)) + dtype.encode())
        digest.update(struct.pack("<I", len(shape)))
        for dim in shape:
            digest.update(struct.pack("<Q", dim))
        digest.update(struct.pack("<Q", len(data)) + data)
    assert digest.hexdigest() == WEIGHTS_VECTOR_SHA256


def test_seed_score_formula() -> None:
    assert seed_score(1.0, 0.0, 1.0) == pytest.approx(1.0)
    assert seed_score(0.5, 0.5, 0.25) == pytest.approx(0.6 * 0.5 * 0.75 + 0.1)


def test_manifest_rules() -> None:
    text, _ = lora_repo()
    manifest = parse_manifest(text, _SETTINGS)
    assert manifest.kind == "lora" and manifest.base_id == "qwen3.5-0.8b"

    bad = json.loads(text)
    bad["license"] = "llama3"
    with pytest.raises(ArtifactRejected, match="license"):
        parse_manifest(json.dumps(bad), _SETTINGS)
    bad = json.loads(text)
    bad["base_id"] = "llama-3-8b"
    with pytest.raises(ArtifactRejected, match="not allowlisted"):
        parse_manifest(json.dumps(bad), _SETTINGS)
    bad = json.loads(text)
    bad["schema"] = "ditto-cpm-manifest-v0"
    with pytest.raises(ArtifactRejected, match="schema"):
        parse_manifest(json.dumps(bad), _SETTINGS)
    bad = json.loads(text)
    bad["files"].append({"path": "train.py", "sha256": "0" * 64, "bytes": 1})
    with pytest.raises(ArtifactRejected, match="exactly"):
        parse_manifest(json.dumps(bad), _SETTINGS)
    bad = json.loads(text)
    bad["files"][0]["path"] = "../adapter_config.json"
    with pytest.raises(ArtifactRejected, match="plain root file"):
        parse_manifest(json.dumps(bad), _SETTINGS)
    big = json.loads(text)
    big["files"][1]["bytes"] = _SETTINGS.max_total_bytes
    with pytest.raises(ArtifactRejected, match="cap"):
        parse_manifest(json.dumps(big), _SETTINGS)
    with pytest.raises(ArtifactRejected, match="repeats a key"):
        parse_manifest('{"schema": 1, "schema": 2}', _SETTINGS)
    with pytest.raises(ArtifactRejected, match="JSON"):
        parse_manifest("not json", _SETTINGS)

    full = manifest_for(
        {"config.json": b"{}", "model.safetensors": b"x", "model.bin": b"y"},
        kind="full",
    )
    with pytest.raises(ArtifactRejected, match="may list only"):
        parse_manifest(full, _SETTINGS)


def test_safetensors_header_sanity(tmp_path: Path) -> None:
    good = safetensors_bytes({"w": ("BF16", [2, 3], None)})
    parsed = read_safetensors(
        _write(tmp_path, {"a.safetensors": good})["a.safetensors"]
    )
    assert [t.name for t in parsed.tensors] == ["w"]

    header = json.dumps(
        {"w": {"dtype": "BF16", "shape": [2, 3], "data_offsets": [0, 10]}}
    ).encode()
    short = struct.pack("<Q", len(header)) + header + b"\0" * 10
    with pytest.raises(ArtifactRejected, match="byte size"):
        read_safetensors(_write(tmp_path, {"b.safetensors": short})["b.safetensors"])

    header = json.dumps(
        {"w": {"dtype": "I64", "shape": [1], "data_offsets": [0, 8]}}
    ).encode()
    ints = struct.pack("<Q", len(header)) + header + b"\0" * 8
    with pytest.raises(ArtifactRejected, match="dtype"):
        read_safetensors(_write(tmp_path, {"c.safetensors": ints})["c.safetensors"])

    with pytest.raises(ArtifactRejected, match="header length"):
        read_safetensors(
            _write(tmp_path, {"d.safetensors": struct.pack("<Q", 1 << 40)})[
                "d.safetensors"
            ]
        )
    trailing = good + b"\0\0"
    with pytest.raises(ArtifactRejected, match="covers"):
        read_safetensors(_write(tmp_path, {"e.safetensors": trailing})["e.safetensors"])


def test_weights_hash_ignores_order_sharding_and_metadata(tmp_path: Path) -> None:
    tensors = lora_tensors(4)
    names = list(tensors)
    one = safetensors_bytes(tensors, metadata={"format": "pt"})
    reordered = safetensors_bytes(tensors, order=list(reversed(names)))
    half = len(names) // 2
    shard_a = safetensors_bytes({n: tensors[n] for n in names[:half]})
    shard_b = safetensors_bytes({n: tensors[n] for n in names[half:]})
    paths = _write(
        tmp_path,
        {
            "one.safetensors": one,
            "re.safetensors": reordered,
            "a.safetensors": shard_a,
            "b.safetensors": shard_b,
        },
    )
    digest = canonical_weights_sha256([read_safetensors(paths["one.safetensors"])])
    assert digest == canonical_weights_sha256(
        [read_safetensors(paths["re.safetensors"])]
    )
    assert digest == canonical_weights_sha256(
        [
            read_safetensors(paths["b.safetensors"]),
            read_safetensors(paths["a.safetensors"]),
        ]
    )
    other = safetensors_bytes(lora_tensors(4, salt="tweak"))
    paths.update(_write(tmp_path, {"other.safetensors": other}))
    assert digest != canonical_weights_sha256(
        [read_safetensors(paths["other.safetensors"])]
    )
    with pytest.raises(ArtifactRejected, match="two files"):
        canonical_weights_sha256([read_safetensors(paths["one.safetensors"])] * 2)


def _screen_lora(tmp_path: Path, **kwargs: object) -> object:
    text, files = lora_repo(**kwargs)  # type: ignore[arg-type]
    manifest = parse_manifest(text, _SETTINGS)
    return screen_files(manifest, _write(tmp_path, files), base=_BASE, max_lora_rank=64)


def test_lora_rank_cap_and_target_modules(tmp_path: Path) -> None:
    result = _screen_lora(tmp_path)
    assert result.detail["rank"] == 4  # type: ignore[attr-defined]
    with pytest.raises(ArtifactRejected, match="rank 65 is above"):
        _screen_lora(tmp_path, rank=65)
    with pytest.raises(ArtifactRejected, match="not allowed"):
        _screen_lora(tmp_path, target_modules=["lm_head"], tensor_modules=("q_proj",))
    with pytest.raises(ArtifactRejected, match="targets 'embed_tokens'"):
        _screen_lora(
            tmp_path, target_modules=["q_proj"], tensor_modules=("embed_tokens",)
        )
    with pytest.raises(ArtifactRejected, match="remote code"):
        _screen_lora(tmp_path, extra_config={"auto_map": {"x": "y"}})
    with pytest.raises(ArtifactRejected, match="modules_to_save"):
        _screen_lora(tmp_path, extra_config={"modules_to_save": ["lm_head"]})
    # Qwen3.5's linear-attention projections are targetable.
    _screen_lora(tmp_path, tensor_modules=("in_proj_qkv", "out_proj"))


def test_full_architecture_and_parameter_count(tmp_path: Path) -> None:
    base = _BASE.model_copy(update={"parameters": 1_000})
    shard = safetensors_bytes({"w": ("BF16", [10, 100], None)})
    config = json.dumps({"architectures": base.architectures}).encode()
    paths = _write(tmp_path, {"config.json": config, "model.safetensors": shard})
    detail = check_full(
        paths["config.json"],
        [read_safetensors(paths["model.safetensors"])],
        index_path=None,
        base=base,
    )
    assert detail["parameters"] == 1_000
    with pytest.raises(ArtifactRejected, match="within 1%"):
        check_full(
            paths["config.json"],
            [read_safetensors(paths["model.safetensors"])],
            index_path=None,
            base=base.model_copy(update={"parameters": 2_000}),
        )
    paths.update(_write(tmp_path, {"config.json": b'{"architectures": ["Llama"]}'}))
    with pytest.raises(ArtifactRejected, match="architectures"):
        check_full(
            paths["config.json"],
            [read_safetensors(paths["model.safetensors"])],
            index_path=None,
            base=base,
        )


async def test_fake_fetcher_streams_in_chunks() -> None:
    fetcher = FakeFetcher()
    fetcher.put("m/r", "a" * 40, {"f": b"0123456789abcdef"})
    chunks = [c async for c in fetcher.stream("m/r", "a" * 40, "f")]
    assert b"".join(chunks) == b"0123456789abcdef" and len(chunks) > 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {
                "deposit": 0,
                "block": 1234,
                "info": {"fields": [{"Raw7": "0x" + b"dcpm1:x".hex()}]},
            },
            (1234, b"dcpm1:x"),
        ),
        (
            {"deposit": 0, "block": 9, "info": {"fields": [[{"Raw2": "6869"}]]}},
            (9, b"hi"),
        ),
        (
            {"block": 5, "info": {"fields": [{"Raw2": [104, 105]}]}},
            (5, b"hi"),
        ),
        ({"block": 5, "info": {"fields": [{"Sha256": "0x00"}]}}, (5, b"")),
        ({"block": 5, "info": {"fields": []}}, (5, b"")),
        (None, None),
        ({"info": {}}, None),
    ],
)
def test_decode_commitment_registration(
    value: object, expected: tuple[int, bytes] | None
) -> None:
    assert decode_commitment_registration(value) == expected
