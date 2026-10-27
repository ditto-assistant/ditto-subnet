"""Exercise the real updater with a fake registry and service manager."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
OLD = "b" * 40
DIGEST = "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "c" * 64


@pytest.mark.parametrize("fault", ["none", "signature", "identity", "startup"])
def test_signed_activation_and_failure_preserve_last_good(
    tmp_path: Path, fault: str
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    release_root = tmp_path / "scorer"
    previous = release_root / "releases" / OLD
    previous.mkdir(parents=True)
    (previous / "challenge-scorer").touch()
    (release_root / "current").symlink_to(previous)
    (release_root / "last-good").symlink_to(previous)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    identity = {
        "revision": "d" * 40 if fault == "identity" else REVISION,
        "update_protocol": 1,
        "max_cases": 80,
        "builder_image": "docker.io/library/golang@sha256:" + "1" * 64,
        "runner_image": "docker.io/library/debian@sha256:" + "2" * 64,
    }
    binary = bundle / "challenge-scorer"
    binary.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(identity) + "'\n")
    binary.chmod(0o755)
    (bundle / "updater.sh").write_text("#!/bin/sh\nexit 0\n")
    shim = tools / "shim"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        + "import os, sys, pathlib, shutil\n"
        + "name=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]\n"
        + "root=pathlib.Path(os.environ['GENERATOR_SCORER_RELEASE_ROOT'])\n"
        + "fault=os.environ['TEST_FAULT']\n"
        + "with open(os.environ['TEST_LOG'],'a') as log:\n"
        + " log.write(name+' '+repr(args)+'\\n')\n"
        + "if name=='cosign': sys.exit(1 if fault=='signature' else 0)\n"
        + "if name=='flock' or name=='sleep': sys.exit(0)\n"
        + "if name=='docker':\n"
        + " if args[0]=='image':\n"
        + "  fmt=args[args.index('--format')+1]\n"
        + f"  digest={DIGEST!r}; revision={REVISION!r}\n"
        + "  value=digest if 'RepoDigests' in fmt else revision\n"
        + "  print('1' if 'update-protocol' in fmt else value)\n"
        + " elif args[0]=='create': print('fake-container')\n"
        + " elif args[0]=='cp':\n"
        + "  shutil.copytree(os.environ['TEST_BUNDLE'],args[2],dirs_exist_ok=True)\n"
        + " sys.exit(0)\n"
        + "if name=='systemctl':\n"
        + " if args[0]=='show':\n"
        + "  print('0' if (root/'signaled').exists() and fault=='startup' else '42')\n"
        + " if args[0]=='kill': (root/'signaled').touch()\n"
        + " sys.exit(0)\n"
        + "if name=='readlink':\n"
        + " if args[-1].startswith('/proc/'):\n"
        + f"  revision={REVISION!r}; old={OLD!r}\n"
        + "  ready=(root/'signaled').exists() and fault!='startup'\n"
        + "  directory=root/'releases'/(revision if ready else old)\n"
        + "  print(directory/'challenge-scorer')\n"
        + " else: print(pathlib.Path(args[-1]).resolve())\n"
    )
    shim.chmod(0o755)
    for name in ("docker", "cosign", "flock", "systemctl", "readlink", "sleep"):
        (tools / name).symlink_to(shim)
    # GNU mv -T is needed by the production Linux host. macOS CI runs this
    # shim only for its spelling; the filesystem operations remain real.
    mv = tools / "mv"
    mv.write_text(
        "#!/usr/bin/env python3\nimport os,sys,shutil\n"
        "args=[a for a in sys.argv[1:] if not a.startswith('-')]\n"
        "if os.path.islink(args[0]): os.replace(*args)\n"
        "else: shutil.move(*args)\n"
    )
    mv.chmod(0o755)
    env = os.environ | {
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "GENERATOR_SCORER_RELEASE_ROOT": str(release_root),
        "GENERATOR_SCORER_UPDATER_PATH": str(tmp_path / "installed-updater"),
        "GENERATOR_SCORER_ADOPTION_POLLS": "5",
        "GENERATOR_SCORER_STARTUP_SECONDS": "0",
        "TEST_BUNDLE": str(bundle),
        "TEST_FAULT": fault,
        "TEST_LOG": str(tmp_path / "calls"),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/generator-scorer-auto-update.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == (0 if fault == "none" else 1), result.stderr
    expected = release_root / "releases" / (REVISION if fault == "none" else OLD)
    assert (release_root / "current").resolve() == expected
    assert (release_root / "last-good").resolve() == expected
    calls = (tmp_path / "calls").read_text()
    assert "SIGTERM" not in calls
    if fault in ("signature", "identity"):
        assert "systemctl" not in calls
    else:
        assert "SIGUSR1" in calls
    if fault == "none":
        assert (release_root / "active-descriptor").read_text().strip() == DIGEST
        assert (tmp_path / "installed-updater").exists()
    if fault == "signature":
        assert "create" not in calls

    if fault == "startup":
        assert (release_root / "failed-candidate").read_text().strip() == DIGEST
        assert calls.count("sleep ['5']") < 5
        (tmp_path / "calls").write_text("")
        retry = subprocess.run(
            ["bash", str(ROOT / "scripts/generator-scorer-auto-update.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert retry.returncode == 0, retry.stderr
        assert "systemctl" not in (tmp_path / "calls").read_text()
        assert (release_root / "current").resolve() == previous
