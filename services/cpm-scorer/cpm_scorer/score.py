"""The scoring path shared by `cpm-scorer score` and `cpm-scorer serve`."""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cpm_scorer import metrics
from cpm_scorer.bases import Base
from cpm_scorer.generate import generate
from cpm_scorer.labels import export_records
from cpm_scorer.screening import (
    MANIFEST_NAME,
    MAX_TOTAL_BYTES,
    ScreeningError,
    check_config_no_remote_code,
    check_finite,
    check_full,
    check_full_config,
    check_lora,
    read_headers,
    read_safetensors_header,
    scan_repo_files,
    sha256_file,
    validate_manifest,
    verify_files,
    weights_sha256,
)
from cpm_scorer.tasks import SeedTasks, build_tasks

Log = Callable[[str], None]


def stderr_log(msg: str) -> None:
    print(
        f"[cpm-scorer {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True
    )


@dataclass(frozen=True)
class ModelSource:
    """What to score: the unmodified base, or a LoRA / full directory."""

    base: Base
    kind: str  # "base" | "lora" | "full"
    path: Path | None = None
    label: str = ""


def list_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and ".git" not in p.relative_to(root).parts
        and ".cache" not in p.relative_to(root).parts
    )


def detect_kind(root: Path) -> str:
    if (root / "adapter_config.json").is_file():
        return "lora"
    if (root / "config.json").is_file() and any(root.glob("model*.safetensors")):
        return "full"
    raise ScreeningError(
        "invalid_model_dir",
        f"{root}: need adapter_config.json (lora) or config.json + "
        "model*.safetensors (full)",
    )


def weight_paths(root: Path, kind: str) -> list[Path]:
    if kind == "lora":
        return [root / "adapter_model.safetensors"]
    return sorted(root.glob("model*.safetensors"))


def screen_dir(
    root: Path, base_id: str, expected_manifest_sha: str | None = None
) -> tuple[str, str | None]:
    """Static screening of a downloaded or local model directory.

    With a `cpm-manifest.json` the manifest is validated, its base must match,
    and every listed file is size- and hash-checked. Without one (local
    practice) the kind is detected from the files. Returns (kind,
    manifest_sha256 or None).
    """
    scan_repo_files(list_files(root))
    mpath = root / MANIFEST_NAME
    msha = None
    if mpath.is_file():
        raw = mpath.read_bytes()
        msha = hashlib.sha256(raw).hexdigest()
        if expected_manifest_sha and msha != expected_manifest_sha:
            raise ScreeningError("hash_mismatch", "cpm-manifest.json")
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise ScreeningError("invalid_manifest", str(e)) from None
        manifest = validate_manifest(obj)
        if manifest.base_id != base_id:
            raise ScreeningError(
                "base_mismatch", f"manifest {manifest.base_id} != {base_id}"
            )
        verify_files(root, manifest)
        listed = {f for f in manifest.paths() if f.endswith(".safetensors")}
        present = {q.relative_to(root).as_posix() for q in root.rglob("*.safetensors")}
        if present != listed:
            raise ScreeningError(
                "unlisted_file",
                f"safetensors not in manifest: {sorted(present - listed)}",
            )
        kind = manifest.kind
    elif expected_manifest_sha:
        raise ScreeningError("missing_file", MANIFEST_NAME)
    else:
        kind = detect_kind(root)
    if kind == "lora":
        cfg = json.loads((root / "adapter_config.json").read_text(encoding="utf-8"))
        check_lora(cfg, read_safetensors_header(root / "adapter_model.safetensors"))
    else:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check_config_no_remote_code(cfg, "config.json")
    paths = weight_paths(root, kind)
    if sum(p.stat().st_size for p in paths) > MAX_TOTAL_BYTES:
        raise ScreeningError("too_large", "weights exceed 6 GiB")
    check_finite(paths)
    return kind, msha


def scores_digest(scores: Sequence[float]) -> str:
    """SHA-256 of the scores as float32 little-endian, for bitwise compare."""
    return hashlib.sha256(struct.pack(f"<{len(scores)}f", *scores)).hexdigest()


@dataclass
class LoadedScorer:
    scorer: Any
    source: ModelSource
    weights_sha256: str | None


def load_scorer(
    source: ModelSource,
    device: str,
    cache_dir: str | None = None,
    log: Log = stderr_log,
) -> LoadedScorer:
    from cpm_scorer import model as m

    m.configure_determinism(device)
    base_dir = m.download_base(source.base, cache_dir=cache_dir)
    log(f"base {source.base.repo}@{source.base.revision} at {base_dir}")
    tok = m.load_tokenizer(base_dir)
    net = m.load_base_model(base_dir)
    wsha = None
    if source.kind == "lora":
        assert source.path is not None
        wsha = weights_sha256(weight_paths(source.path, "lora"))
        net = m.apply_lora(net, source.path)
    elif source.kind == "full":
        assert source.path is not None
        paths = weight_paths(source.path, "full")
        base_cfg = net.config.to_dict()
        check_full_config(
            json.loads((source.path / "config.json").read_text(encoding="utf-8")),
            base_cfg,
        )
        headers = {k: info for k, (_, info) in read_headers(paths).items()}
        check_full(headers, m.base_tensor_shapes(net))
        wsha = weights_sha256(paths)
        net = m.apply_full(net, paths)
    return LoadedScorer(m.RelevanceScorer(net, tok, device), source, wsha)


def score_seed(
    scorer: Any,
    tasks: SeedTasks,
    budget: int,
    tool_k: int,
    log: Log = stderr_log,
) -> tuple[dict[str, Any], list[float]]:
    """Score one seed's tasks. Returns (per-seed record, raw scores in order)."""
    pairs = seed_pairs(tasks)
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if done == total or now - last[0] > 30:
            last[0] = now
            log(f"  scored {done}/{total} pairs")

    raw, input_tokens = scorer.score(pairs, progress=progress)
    pair_ids = sorted({c for t in tasks.memory for c in t.candidates})
    counts = dict(
        zip(
            pair_ids,
            scorer.count_tokens([tasks.pair_text[p] for p in pair_ids]),
            strict=True,
        )
    )
    i = 0
    mem_results = []
    for t in tasks.memory:
        s = dict(zip(t.candidates, raw[i : i + len(t.candidates)], strict=True))
        i += len(t.candidates)
        mem_results.append(
            metrics.memory_case(t.case_id, s, counts, t.relevant, t.spans, budget)
        )
    tool_results = []
    for t in tasks.tools:
        n = len(tasks.tool_names)
        s = dict(zip(tasks.tool_names, raw[i : i + n], strict=True))
        i += n
        tool_results.append(metrics.tool_case(t.case_id, s, t.target, tool_k))
    assert i == len(raw)
    comps = metrics.seed_components(mem_results, tool_results)
    record = {
        **comps,
        "score": metrics.seed_score(comps),
        "memory_cases": len(mem_results),
        "tool_cases": len(tool_results),
        "pairs_scored": len(raw),
        "model_input_tokens": input_tokens,
        "packed_tokens": sum(r.packed_tokens for r in mem_results),
        "packed_tokens_mean": (
            sum(r.packed_tokens for r in mem_results) / len(mem_results)
            if mem_results
            else 0.0
        ),
        "scores_sha256": scores_digest(raw),
    }
    return record, raw


def run(
    loaded: LoadedScorer,
    seeds: Sequence[int],
    bench_version: int,
    generate_bin: Path,
    cache_dir: Path,
    *,
    limit_cases: int | None = None,
    budget: int = metrics.BUDGET_TOKENS,
    tool_k: int = metrics.TOOL_K,
    rerun: bool = True,
    generate_seeds: Sequence[int] | None = None,
    log: Log = stderr_log,
) -> dict[str, Any]:
    """Score `seeds` (uint64). `generate_seeds`, when given, are the int64
    values passed to `cmd/generate`, one per seed and in the same order."""
    if not seeds:
        raise ValueError("at least one seed")
    if generate_seeds is not None and len(generate_seeds) != len(seeds):
        raise ValueError("generate_seeds must match seeds one to one")
    gen_seeds = (
        list(generate_seeds) if generate_seeds is not None else [None] * len(seeds)
    )
    t0 = time.monotonic()
    per_seed = []
    first: tuple[SeedTasks, list[float]] | None = None
    for seed, gen_seed in zip(seeds, gen_seeds, strict=True):
        art = generate(generate_bin, bench_version, seed, cache_dir, gen_seed)
        records = export_records(
            json.loads(art.read_text(encoding="utf-8")), f"v{bench_version}-{seed}"
        )
        tasks = build_tasks(records, limit_cases)
        log(
            f"seed {seed}: {len(tasks.memory)} memory cases, {len(tasks.tools)} tool "
            f"cases x {len(tasks.tool_names)} tools"
        )
        rec, raw = score_seed(loaded.scorer, tasks, budget, tool_k, log)
        rec = {"seed": seed, "dataset_sha256": sha256_file(art), **rec}
        log(f"seed {seed}: score {rec['score']:.6f}")
        per_seed.append(rec)
        if first is None:
            first = (tasks, raw)
    determinism = None
    if rerun and first is not None:
        log(f"determinism: re-running seed {seeds[0]}")
        again, _ = loaded.scorer.score(seed_pairs(first[0]))
        determinism = {
            "rerun_seed": seeds[0],
            "identical": scores_digest(again) == scores_digest(first[1]),
        }
    agg = metrics.aggregate(per_seed)
    import torch

    device = loaded.scorer.device
    return {
        "cpm_score_version": metrics.CPM_SCORE_VERSION,
        "score": agg["score"],
        "components": {
            k: agg[k]
            for k in ("memory_recall_at_b", "distractor_rate", "tool_recall_at_k")
        },
        "per_seed": per_seed,
        "tokens": {
            "model_input_tokens": sum(s["model_input_tokens"] for s in per_seed),
            "pairs_scored": sum(s["pairs_scored"] for s in per_seed),
            "packed_tokens_mean": sum(s["packed_tokens_mean"] for s in per_seed)
            / len(per_seed),
            "budget_tokens": budget,
        },
        "determinism": determinism,
        "model": {
            "base_id": loaded.source.base.base_id,
            "base_revision": loaded.source.base.revision,
            "kind": loaded.source.kind,
            "source": loaded.source.label,
            "weights_sha256": loaded.weights_sha256,
        },
        "bench_version": bench_version,
        "tool_k": tool_k,
        "limit_cases": limit_cases,
        "generator_sha256": sha256_file(generate_bin),
        "device": device,
        "gpu_sku": (
            torch.cuda.get_device_name(torch.device(device).index or 0)
            if device.startswith("cuda")
            else None
        ),
        "dtype": str(loaded.scorer.dtype).removeprefix("torch."),
        "torch": torch.__version__,
        "wall_seconds": round(time.monotonic() - t0, 1),
    }


def seed_pairs(tasks: SeedTasks) -> list[tuple[str, str]]:
    """(query, candidate text) in the fixed order: memory cases then tool
    cases, each by case id, candidates by id."""
    pairs: list[tuple[str, str]] = []
    for t in tasks.memory:
        pairs.extend((t.query, tasks.pair_text[c]) for c in t.candidates)
    for t in tasks.tools:
        pairs.extend((t.query, tasks.tool_text[n]) for n in tasks.tool_names)
    return pairs
