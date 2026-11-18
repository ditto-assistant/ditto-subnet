"""Run the public DittoBench generator (`cmd/generate`) for a season seed.

The binary is either given (`--generate-bin`, the pinned build on the scorer
host) or built with `go build` from `research/dittobench-datagen` into a cache
directory keyed by the SHA-256 of the generator sources.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cpm_scorer.screening import sha256_file

DATAGEN_REL = Path("research/dittobench-datagen")
RUN_SIZE = "full"


def default_cache_dir() -> Path:
    root = os.environ.get("CPM_SCORER_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
        "cpm-scorer",
    )
    return Path(root)


def find_datagen_dir() -> Path:
    env = os.environ.get("CPM_DATAGEN_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        cand = parent / DATAGEN_REL
        if (cand / "go.mod").is_file():
            return cand
    raise FileNotFoundError(
        "research/dittobench-datagen not found; pass --generate-bin or set "
        "CPM_DATAGEN_DIR"
    )


def source_digest(datagen_dir: Path) -> str:
    """SHA-256 over every file in the generator tree (relative path + bytes)."""
    h = hashlib.sha256()
    files = sorted(
        p for p in datagen_dir.rglob("*") if p.is_file() and ".git" not in p.parts
    )
    for p in files:
        rel = p.relative_to(datagen_dir).as_posix().encode()
        h.update(len(rel).to_bytes(4, "little") + rel)
        h.update(sha256_file(p).encode())
    return h.hexdigest()


def build_generate(datagen_dir: Path, cache_dir: Path) -> Path:
    digest = source_digest(datagen_dir)[:16]
    out = cache_dir / "bin" / f"generate-{digest}"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    if not shutil.which("go"):
        raise FileNotFoundError("go toolchain not found; pass --generate-bin")
    tmp = out.with_suffix(".tmp")
    subprocess.run(
        ["go", "build", "-trimpath", "-o", str(tmp), "./cmd/generate"],
        cwd=datagen_dir,
        check=True,
        env={**os.environ, "CGO_ENABLED": "0"},
    )
    tmp.replace(out)
    return out


def generator_seed_arg(seed: int) -> str:
    """`cmd/generate -seed` is an int64 flag. Season seeds are uint64
    (contract §4.1), so seeds >= 2**63 pass as their two's-complement int64."""
    if not 0 <= seed < 2**64:
        if -(2**63) <= seed < 0:
            return str(seed)
        raise ValueError(f"seed out of range: {seed}")
    return str(seed - 2**64 if seed >= 2**63 else seed)


def generate(bin_path: Path, bench_version: int, seed: int, cache_dir: Path) -> Path:
    """Generate (or reuse) the artifact for one seed. Cached per binary hash."""
    bin_digest = sha256_file(bin_path)[:16]
    out = cache_dir / "artifacts" / bin_digest / f"v{bench_version}-{seed}.json"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
    os.close(fd)
    try:
        subprocess.run(
            [
                str(bin_path),
                "-bench-version",
                str(bench_version),
                "-seed",
                generator_seed_arg(seed),
                "-run-size",
                RUN_SIZE,
                "-out",
                tmp,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        Path(tmp).replace(out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out
