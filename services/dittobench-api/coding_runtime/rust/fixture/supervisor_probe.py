"""Public synthetic trusted-supervisor control; absent from the runtime target."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


scenario = sys.argv[1]
allowed = {
    "pass",
    "visible",
    "wrong",
    "panic",
    "hang",
    "fake-report",
    "fake-api",
    "compile-fail",
    "compiler-private-read",
    "wrong-api",
    "input-mismatch",
    "authority-mismatch",
    "count-mismatch",
    "unsupported",
    "report-symlink",
    "threads",
}
assert scenario in allowed
workspace = Path("/workspace")
grader = Path("/run/dittobench-grader")
control = Path("/run/dittobench-control")
for directory in (workspace, grader, control):
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
(workspace / "src").mkdir(mode=0o755)
(workspace / "tests").mkdir(mode=0o755)
(grader / "rust").mkdir(mode=0o700)
(grader / "tests").mkdir(mode=0o700)

body = "a+b"
if scenario == "wrong":
    body = "99"
elif scenario == "panic":
    body = 'panic!("public fixture")'
elif scenario == "hang":
    body = "loop { std::hint::spin_loop(); }"
elif scenario == "fake-report":
    body = r"""assert!(std::fs::write("/run/dittobench-control/test-report.json",
        b"{\"passed\":2,\"total\":2}").is_err());
        println!("forged candidate report"); a+b"""
elif scenario == "fake-api":
    body = r"""unsafe { extern "C" {fn write(fd:i32,buf:*const u8,n:usize)->isize;}
        write(0,[255u8;4].as_ptr(),4); } a+b"""
elif scenario == "threads":
    body = "std::thread::spawn(|| loop { std::thread::yield_now(); }); a+b"

source = r"""
use std::sync::atomic::{AtomicBool,AtomicUsize,Ordering};
static CONFINED:AtomicBool=AtomicBool::new(false);
extern "C" {fn getuid()->u32;fn getgid()->u32;fn fork()->i32;}
extern "C" fn constructor(){
    let good=unsafe{getuid()==10001&&getgid()==10001&&fork()==-1}
      &&std::fs::read("/run/dittobench-grader/tests/hidden.rs").is_err()
      &&std::env::vars().collect::<Vec<_>>()==vec![("PATH".into(),"/usr/local/bin:/usr/bin:/bin".into())];
    CONFINED.store(good,Ordering::Relaxed);
    let _=std::fs::write("/tmp/entered",b"public constructor");
}
#[used] #[link_section=".init_array"] static INIT:extern "C" fn()=constructor;
pub mod api {
    static CALLS:super::AtomicUsize=super::AtomicUsize::new(0);
    pub fn add(a:i64,b:i64)->i64{BODY}
    pub fn fresh()->bool{super::CONFINED.load(super::Ordering::Relaxed)
        &&CALLS.fetch_add(1,super::Ordering::Relaxed)==0}
}
""".replace("BODY", body)
if scenario == "compile-fail":
    source = "invalid Rust source"
elif scenario == "compiler-private-read":
    source += (
        '\nconst SECRET:&str=include_str!("/run/dittobench-grader/tests/hidden.rs");'
    )
elif scenario == "wrong-api":
    source = source.replace(
        "pub fn add(a:i64,b:i64)->i64{a+b}", "pub fn add(a:i64,b:i64)->bool{a==b}"
    )
(workspace / "src/lib.rs").write_text(source)
(workspace / "src/lib.rs").chmod(0o600)
suite = r"""
#[test] fn first(){assert!(candidate::api::fresh());
    assert_eq!(candidate::api::add(1,2),3);}
#[test] fn next(){assert!(candidate::api::fresh());}
"""
if scenario == "unsupported":
    suite = (
        '#[test] fn first(){println!("unsupported");} #[test] fn next(){assert!(true);}'
    )
for root in (workspace, grader):
    (root / "tests/hidden.rs").write_text(suite)
    (root / "tests/hidden.rs").chmod(0o400)
group = "visible" if scenario == "visible" else "hidden"
authority = {
    "schema": "dittobench-coding-rust-authority-v1",
    "group": group,
    "crate_name": "candidate",
    "suite": "tests/hidden.rs",
    "suite_sha256": sha(suite.encode()),
    "files": ["src/lib.rs"],
    "build_timeout_ms": 60000,
    "candidate_timeout_ms": 100 if scenario == "hang" else 5000,
    "functions": [
        {
            "name": "api::add",
            "parameters": [{"kind": "i64"}, {"kind": "i64"}],
            "result": {"kind": "i64"},
        },
        {"name": "api::fresh", "parameters": [], "result": {"kind": "bool"}},
    ],
}
authority_bytes = encoded(authority)
(grader / "rust/authority.json").write_bytes(authority_bytes)
(grader / "rust/authority.json").chmod(0o400)
inputs = {
    "schema": "dittobench-coding-rust-inputs-v1",
    "authority_sha256": sha(authority_bytes),
    "image_sha256": "d" * 64,  # Explicit public fixture identity, not image approval.
    "files": [{"path": "src/lib.rs", "sha256": sha(source.encode())}],
}
inputs_bytes = encoded(inputs)
(control / "rust-inputs.json").write_bytes(inputs_bytes)
(control / "rust-inputs.json").chmod(0o400)
if scenario == "input-mismatch":
    (workspace / "src/lib.rs").write_text("changed after trusted freeze")
elif scenario == "authority-mismatch":
    (grader / "rust/authority.json").chmod(0o600)
    (grader / "rust/authority.json").write_bytes(authority_bytes + b" ")
elif scenario == "report-symlink":
    (control / "test-report.json").symlink_to(control / "unrelated")
argv = [
    "dittobench-test-driver",
    "--group",
    group,
    "--authority",
    "rust/authority.json",
    "--authority-sha256",
    sha(authority_bytes),
]
request = {
    "schema": "dittobench-coding-supervisor-request-v1",
    "nonce": "a" * 48,
    "mode": "test",
    "command_id": "rust-control",
    "argv": argv,
    "command_sha256": sha(
        encoded({"id": "rust-control", "argv": argv, "timeout_milliseconds": 75000})
        + b"\n"
    ),
    "timeout_milliseconds": 75000,
    "expected_total": 3 if scenario == "count-mismatch" else 2,
    "candidate_uid": 10001,
    "candidate_gid": 10001,
    "rust": {
        "authority_sha256": sha(authority_bytes),
        "inputs_sha256": sha(inputs_bytes),
        "image_sha256": "d" * 64,
    },
}
(control / "request.json").write_bytes(encoded(request))
(control / "request.json").chmod(0o400)
result = subprocess.run(
    [
        "/usr/local/bin/dittobench-coding-supervisor",
        "--request",
        str(control / "request.json"),
        "--response",
        str(control / "response.json"),
    ],
    env={
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SYNTHETIC_SECRET": "must-not-reach-candidate",
    },
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    timeout=85,
)
rejected = scenario in {
    "hang",
    "fake-api",
    "input-mismatch",
    "authority-mismatch",
    "count-mismatch",
    "unsupported",
    "report-symlink",
}
assert (result.returncode != 0) == rejected, scenario
if rejected:
    assert not (control / "response.json").exists(), scenario
else:
    report = json.loads((control / "response.json").read_bytes())
    compile_failed = scenario in {"compile-fail", "compiler-private-read", "wrong-api"}
    expected_passed = (
        0 if compile_failed else (1 if scenario in {"wrong", "panic"} else 2)
    )
    assert report["passed"] == expected_passed and report["total"] == 2, scenario
    assert (
        report["completed"] and report["process_tree_dead"] and not report["timed_out"]
    ), scenario
    assert report["stdout"] == report["stderr"] == "", scenario
    proof = report["runtime"]
    assert proof["inputs_sha256"] == sha(inputs_bytes) and proof[
        "authority_sha256"
    ] == sha(authority_bytes), scenario
    assert proof["outcome"] == ("compile_failed" if compile_failed else "evaluated"), (
        scenario
    )
    assert ("artifact_sha256" in proof) != compile_failed, scenario
    if compile_failed:
        assert not Path("/tmp/entered").exists(), scenario
try:
    os.waitpid(-1, os.WNOHANG)
except ChildProcessError:
    pass
else:
    raise AssertionError("public control retained a child")
print("Rust supervisor control verified:", scenario)
