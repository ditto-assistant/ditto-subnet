"""Generator build and invocation, with fake `go` and `cmd/generate` scripts."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from cpm_scorer import generate as g


def _script(p: Path, body: str) -> Path:
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture
def datagen(tmp_path) -> Path:
    d = tmp_path / "datagen"
    (d / "cmd" / "generate").mkdir(parents=True)
    (d / "go.mod").write_text("module x\n")
    return d


def _fake_go(tmp_path, monkeypatch, body: str) -> None:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    _script(bindir / "go", body)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _leftovers(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".tmp"))


def test_build_generate_uses_a_unique_temp_and_replaces(tmp_path, monkeypatch, datagen):
    # `go build -trimpath -o OUT ./cmd/generate`: $4 is OUT.
    _fake_go(tmp_path, monkeypatch, 'printf built > "$4"\n')
    cache = tmp_path / "cache"
    out = g.build_generate(datagen, cache)
    assert out.read_text() == "built" and out.parent == cache / "bin"
    assert _leftovers(out.parent) == []
    # A cached build is reused without calling go.
    _fake_go(tmp_path, monkeypatch, "exit 9\n")
    assert g.build_generate(datagen, cache) == out


def test_failed_build_leaves_no_temp(tmp_path, monkeypatch, datagen):
    _fake_go(tmp_path, monkeypatch, 'printf partial > "$4"; exit 1\n')
    cache = tmp_path / "cache"
    with pytest.raises(subprocess.CalledProcessError):
        g.build_generate(datagen, cache)
    assert list((cache / "bin").iterdir()) == []


def _fake_generator(tmp_path) -> tuple[Path, Path]:
    log = tmp_path / "args.log"
    # -bench-version V -seed S -run-size R -out OUT: $4 is S, $8 is OUT.
    gen = _script(
        tmp_path / "generate",
        f'echo "$4" >> "{log}"\nprintf "{{}}" > "$8"\n',
    )
    return gen, log


def test_generate_passes_the_work_items_int64_seed(tmp_path):
    gen, log = _fake_generator(tmp_path)
    out = g.generate(gen, 18, 2**64 - 1, tmp_path / "c", generate_seed=-1)
    assert out.read_text() == "{}" and out.name == f"v18-{2**64 - 1}.json"
    assert log.read_text().split() == ["-1"]
    assert _leftovers(out.parent) == []


@pytest.mark.parametrize("bad", [1, 2**63, True])
def test_generate_rejects_a_seed_pair_that_disagrees(tmp_path, bad):
    gen, log = _fake_generator(tmp_path)
    with pytest.raises(ValueError):
        g.generate(gen, 18, 2**64 - 1, tmp_path / "c", generate_seed=bad)
    assert not log.exists()


def test_generator_seed_arg():
    assert g.generator_seed_arg(42) == "42"
    assert g.generator_seed_arg(2**63) == str(-(2**63))
    assert g.generator_seed_arg(2**64 - 1) == "-1"
    with pytest.raises(ValueError):
        g.generator_seed_arg(2**64)
