"""Platform's structural sketch must equal the scorer's Go ``astfp`` output."""

from __future__ import annotations

import hashlib
import io
import tarfile

from ditto.api_server.structural_fingerprint import compute_structural_fingerprint

# Mirrors ``parityFixture`` in services/dittobench-api/internal/astfp/astfp_test.go
# byte-for-byte, including invalid UTF-8 and Unicode that Go and Python classify
# differently. Change both or neither.
PARITY_FIXTURE: dict[str, bytes] = {
    "src/main.rs": (
        b'use std::io;\nfn main() {\n    let r#type = "q\\"s";\n'
        b'    match r#type.len() { 0 => println!("empty"), '
        b'n => println!("{}", n + 1) }\n}\n'
        b"struct Pair<T> { a: T, b: T }\nimpl<T: Copy> Pair<T> { "
        b"fn swap(&self) -> Self { Pair { a: self.b, b: self.a } } }\n"
    ),
    "agent/run.py": (
        b"# comment\nimport os\n\ndef run(x):\n    return x == 1 and 'a' or \"b\" \xff "
        + "İd ٣٤ ?? :: \x1c\n".encode()
    ),
    "web/app.ts": b"const a = `t ${x}`; let b = a?.c ?? 1_000.5e3; /* open",
    "tool.sh": b"#!/bin/sh\necho 'x'\n",
    "README.md": b"not source",
    "Dockerfile": b"FROM scratch\n",
}
PARITY_FIXTURE_DIGEST = (
    "f965993f10fcb6c650a7bd23c1e71382113f6034d51d77b8bab15e64acaf593b"
)


def _tar_gz(files: dict[str, bytes], prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _digest(sketch: dict) -> str:
    joined = ",".join(sketch["m"])
    payload = f"{sketch['v']}:{sketch['k']}:{sketch['card']}:{joined}"
    return hashlib.sha256(payload.encode()).hexdigest()


def test_matches_scorer_sketch_for_root_and_nested_build_contexts() -> None:
    for prefix in ("", "harness/"):
        sketch = compute_structural_fingerprint(_tar_gz(PARITY_FIXTURE, prefix))
        assert sketch is not None
        assert _digest(sketch) == PARITY_FIXTURE_DIGEST


def test_unbuildable_or_unreadable_archive_has_no_signal() -> None:
    no_dockerfile = {k: v for k, v in PARITY_FIXTURE.items() if k != "Dockerfile"}
    assert compute_structural_fingerprint(_tar_gz(no_dockerfile)) is None
    assert compute_structural_fingerprint(b"not a tarball") is None
