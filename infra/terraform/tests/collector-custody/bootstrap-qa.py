"""Run rendered custody bootstrap in disposable Debian13 root containers.

No cloud credentials, metadata, real Secret Manager or custody key generation.
Git fetch uses a local bare fixture containing the exact source commit; only
its advertised main ref is synthetic until the PR merges. Dependency install,
root/runuser transitions, source checks and launcher are real. SDK derivation
uses a universally public BIP39 fixture through a no-generation adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/sn118-collector")
STATE = Path("/var/lib/sn118-collector-key-ceremony")
ORIGIN = "https://github.com/ditto-assistant/ditto-subnet.git"
ROLE = sys.argv[1]
REVISION = sys.argv[2]
assert ROLE in ("registration", "transfer") and len(REVISION) == 40
ADDRESSES = ["5" + char * 47 for char in "BCDEF"]


def run(*args, success=True, env=None):
    result = subprocess.run(args, text=True, capture_output=True, env=env)
    if success and result.returncode:
        # Bootstrap happens before key material; no custody secrets exist here.
        print(result.stdout[-5000:])
        print(result.stderr[-5000:])
        raise AssertionError(f"command failed: {args[0]} exit {result.returncode}")
    if not success:
        assert result.returncode != 0, args[0]
    return result


def install_shims():
    # No external metadata. Exact production command receives bootstrap tags.
    Path("/usr/local/bin/curl").write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == '
        "http://metadata.google.internal/computeMetadata/v1/instance/tags ]]; then\n"
        f"    printf '%s\\n' '[\"collector-{ROLE}-bootstrap\"]'; exit 0\n"
        "  fi\n"
        "done\n"
        'exec /usr/bin/curl "$@"\n'
    )
    Path("/usr/local/bin/git").write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == fetch ]]; then\n'
        "    if [[ -f /qa/fail-fetch ]]; then exit 73; fi\n"
        "    exec /usr/bin/git -c "
        "url.file:///fixture/repo.git.insteadOf="
        'https://github.com/ditto-assistant/ditto-subnet.git "$@"\n'
        "  fi\n"
        "done\n"
        'exec /usr/bin/git "$@"\n'
    )
    for path in ("/usr/local/bin/curl", "/usr/local/bin/git"):
        Path(path).chmod(0o755)


def main():
    assert os.geteuid() == 0
    assert 'VERSION_ID="13"' in Path("/etc/os-release").read_text()
    assert run("uname", "-m").stdout.strip() == "x86_64"
    Path("/qa").mkdir(exist_ok=True)
    install_shims()
    run(
        "/usr/bin/git",
        "config",
        "--system",
        "--add",
        "safe.directory",
        "/fixture/repo.git",
    )
    template = Path(
        "/fixture/source/infra/terraform/stacks/gcp-platform/files/collector-custody-startup.sh.tpl"
    ).read_text()
    replacements = {
        "role": ROLE,
        "project": "test-project",
        "git_revision": REVISION,
        "offline_addresses": " ".join(ADDRESSES),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("${" + key + "}", value)
    rendered = rendered.replace("$${", "${")
    bootstrap = Path("/qa/rendered.sh")
    bootstrap.write_text(rendered)
    run("bash", "-n", str(bootstrap))
    print(
        json.dumps(
            {
                "role": ROLE,
                "source": REVISION,
                "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            }
        ),
        flush=True,
    )

    # Reproduce first-fetch interruption after production remote add. Retry must
    # retain correct origin and continue; this fails on original unconditional add.
    Path("/qa/fail-fetch").touch()
    interrupted = run("bash", str(bootstrap), success=False)
    assert interrupted.returncode == 73
    Path("/qa/fail-fetch").unlink()
    origin = run(
        "runuser",
        "-u",
        "collector-bootstrap",
        "--",
        "git",
        "-C",
        str(ROOT),
        "remote",
        "get-url",
        "origin",
    ).stdout.strip()
    assert origin == ORIGIN
    assert not (STATE / "ready").exists()
    public_state = STATE / "qa-public-state"
    public_state.write_text("public recovery sentinel\n")
    sentinel = hashlib.sha256(public_state.read_bytes()).hexdigest()

    # Wrong existing origin must refuse and preserve state, not reset/fetch.
    run(
        "runuser",
        "-u",
        "collector-bootstrap",
        "--",
        "git",
        "-C",
        str(ROOT),
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/wrong.git",
    )
    run("bash", str(bootstrap), success=False)
    assert hashlib.sha256(public_state.read_bytes()).hexdigest() == sentinel
    run(
        "runuser",
        "-u",
        "collector-bootstrap",
        "--",
        "git",
        "-C",
        str(ROOT),
        "remote",
        "set-url",
        "origin",
        ORIGIN,
    )

    run("bash", str(bootstrap))
    assert (STATE / "ready").read_text().strip() == REVISION
    assert ROOT.stat().st_uid == 0 and ROOT.stat().st_mode & 0o077 == 0
    assert all(p.lstat().st_uid == 0 for p in ROOT.rglob("*") if not p.is_symlink())
    assert STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o077 == 0
    assert hashlib.sha256(public_state.read_bytes()).hexdigest() == sentinel
    assert run("id", "collector-bootstrap", success=False).returncode != 0
    assert not (ROOT / "bootstrap-venv").exists()
    wrapper = Path("/usr/local/sbin/collector-delegate-ceremony")
    assert wrapper.stat().st_uid == 0 and wrapper.stat().st_mode & 0o077 == 0
    run("bash", str(bootstrap))  # Ready path: no reinstall, ownership change or key.

    # SDK imports and deterministic public fixture derivation, no generate call.
    py = ROOT / ".venv/bin/python"
    sdk = run(
        str(py),
        "-I",
        "-c",
        "import importlib.metadata; from bittensor_wallet import Keypair; "
        "assert importlib.metadata.version('bittensor') == '10.5.0'; "
        "assert importlib.metadata.version('bittensor-wallet') == '4.1.1'; "
        "k=Keypair.create_from_mnemonic(' '.join(['abandon']*23+['art'])); "
        "print(k.ss58_address)",
    )
    assert len(sdk.stdout.strip()) == 48 and sdk.stdout.strip().startswith("5")
    print(
        json.dumps(
            {
                "sdk": "10.5.0",
                "wallet": "4.1.1",
                "public_fixture_address": sdk.stdout.strip(),
            }
        ),
        flush=True,
    )
    original_link = os.readlink(py)
    py.unlink()
    (ROOT / ".venv/bin/python-real").symlink_to(original_link)
    py.write_text(
        "#!/bin/bash\nexec /opt/sn118-collector/.venv/bin/python-real -I "
        "/fixture/source/infra/terraform/tests/collector-custody/"
        'launcher-qa.py "$@"\n'
    )
    py.chmod(0o700)
    env = {**os.environ, "QA_ROLE": ROLE}
    generated = json.loads(run(str(wrapper), "generate", env=env).stdout)
    verified = json.loads(run(str(wrapper), "verify", env=env).stdout)
    assert generated["address"] == verified["address"] == sdk.stdout.strip()
    assert generated["status"] == "stored"
    assert verified["status"] == "independently-rederived"
    print(
        json.dumps(
            {
                "role": ROLE,
                "launcher_generate_verify": "pass",
                "custody_keys_generated": 0,
            }
        ),
        flush=True,
    )
    run("bash", str(bootstrap))
    assert hashlib.sha256(public_state.read_bytes()).hexdigest() == sentinel
    run(str(wrapper), "unexpected", success=False, env=env)

    # Dirty/wrong pinned source cannot be treated as ready or silently reset.
    source = ROOT / "scripts/collector_delegate_key.py"
    source.write_text(source.read_text() + "\n# public QA dirty sentinel\n")
    dirty = hashlib.sha256(source.read_bytes()).hexdigest()
    run("bash", str(bootstrap), success=False)
    run(str(wrapper), "verify", success=False, env=env)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == dirty
    assert hashlib.sha256(public_state.read_bytes()).hexdigest() == sentinel
    # No ready marker, different existing HEAD: refuse before replacing source.
    (STATE / "ready").unlink()
    run(
        "useradd",
        "--system",
        "--home-dir",
        str(ROOT),
        "--shell",
        "/usr/sbin/nologin",
        "collector-bootstrap",
    )
    run(
        "git",
        "-C",
        str(ROOT),
        "-c",
        "user.name=Public QA",
        "-c",
        "user.email=qa@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "public QA wrong existing source",
    )
    wrong = run("git", "-C", str(ROOT), "rev-parse", "HEAD").stdout.strip()
    run("chown", "-R", "collector-bootstrap:collector-bootstrap", str(ROOT))
    run("bash", str(bootstrap), success=False)
    assert (
        run(
            "runuser",
            "-u",
            "collector-bootstrap",
            "--",
            "git",
            "-C",
            str(ROOT),
            "rev-parse",
            "HEAD",
        ).stdout.strip()
        == wrong
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == dirty
    assert hashlib.sha256(public_state.read_bytes()).hexdigest() == sentinel
    print(
        json.dumps(
            {
                "role": ROLE,
                "bootstrap_retry_ready_wrong_origin_wrong_source": "pass",
                "cloud_calls": 0,
                "custody_keys_generated": 0,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
