"""Generate v1 protocol regression inputs outside the shipped public dataset."""

from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

from dittobench_coding_datagen.compiler import compile_practice

SOURCE = Path(__file__).parent / "fixtures/legacy-practice-source.json"
_directories: list[TemporaryDirectory[str]] = []


@cache
def legacy_practice_pack() -> Path:
    directory = TemporaryDirectory(prefix="coding-v1-protocol-tests-")
    _directories.append(directory)
    pack = Path(directory.name) / "pack"
    compile_practice(SOURCE, pack)
    return pack
