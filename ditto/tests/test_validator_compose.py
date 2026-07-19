"""Integration tests for the Compose wrapper's verified dittobench-api pin."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
COMPOSE_WRAPPER = ROOT / "scripts/validator-compose.sh"


FAKE_WRAPPER_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["compose", "version", "--short"]:
    print("2.40.0")
elif args == ["info"] or args == ["buildx", "version"]:
    pass
elif args[:1] == ["compose"]:
    with open(os.environ["FAKE_COMPOSE_CAPTURE"], "w") as handle:
        json.dump(
            {
                "args": args,
                "image": os.environ.get("DITTO_SUBNET_IMAGE"),
                "wallets_dir": os.environ.get("DITTO_BITTENSOR_WALLETS_DIR"),
            },
            handle,
        )
else:
    raise SystemExit("unhandled wrapper docker command: " + repr(args))
"""


FAKE_WRAPPER_GIT = r"""#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if "remote" in args and "get-url" in args:
    print("https://github.com/ditto-assistant/dittobench-api.git")
elif "rev-parse" in args:
    if args[-1] == "FETCH_HEAD":
        print(
            os.environ.get(
                "FAKE_DITTOBENCH_REF_CHECKSUM",
                os.environ["FAKE_DITTOBENCH_CHECKSUM"],
            )
        )
    else:
        print(os.environ["FAKE_DITTOBENCH_CHECKSUM"])
elif "merge-base" in args:
    if os.environ.get("FAKE_DITTOBENCH_IS_MAIN", "true") != "true":
        raise SystemExit(1)
elif "ls-tree" in args:
    records = [b"100644 Dockerfile"]
    executable = os.environ.get("FAKE_EXECUTABLE_PATH")
    if executable:
        records.append(b"100755 " + executable.encode())
    sys.stdout.buffer.write(b"\0".join(records) + b"\0")
elif "fetch" in args:
    if os.environ.get("FAKE_DITTOBENCH_FETCH_FAIL") == "true":
        raise SystemExit(1)
elif (
    "status" in args
    or "cat-file" in args
    or "checkout" in args
):
    pass
else:
    raise SystemExit("unhandled wrapper git command: " + repr(args))
"""


def _wrapper_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "wrapper-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    git = fake_bin / "git"
    docker.write_text(FAKE_WRAPPER_DOCKER)
    git.write_text(FAKE_WRAPPER_GIT)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    git.chmod(git.stat().st_mode | stat.S_IXUSR)
    checksum_match = re.search(
        r"checksum=([0-9a-f]{40})", (ROOT / "docker-compose.yml").read_text()
    )
    assert checksum_match is not None
    checksum = checksum_match.group(1)
    cache = tmp_path / "cache"
    checkout = cache / "dittobench-api" / checksum
    (checkout / ".git").mkdir(parents=True)
    (checkout / "Dockerfile").write_text("FROM scratch\n")
    capture = tmp_path / "compose-capture.json"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DITTO_SUBNET_BUILD_CACHE": str(cache),
        "FAKE_COMPOSE_CAPTURE": str(capture),
        "FAKE_DITTOBENCH_CHECKSUM": checksum,
        "HOME": str(tmp_path / "operator-home"),
    }
    return env, capture


def test_compose_config_forwards_wallets_dir(tmp_path: Path) -> None:
    env, capture = _wrapper_env(tmp_path)

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "config", "--quiet"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = json.loads(capture.read_text())
    assert captured["wallets_dir"] == str(tmp_path / "operator-home/.bittensor/wallets")


def test_compose_wrapper_repairs_git_tracked_executable_modes(
    tmp_path: Path,
) -> None:
    env, _ = _wrapper_env(tmp_path)
    env.pop("DITTOBENCH_ALLOW_UNMERGED_SMOKE", None)
    env.pop("SUBTENSOR_NETWORK", None)
    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    executable = (
        Path(env["DITTO_SUBNET_BUILD_CACHE"]) / "dittobench-api" / checksum / "run.sh"
    )
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o644)
    env["FAKE_EXECUTABLE_PATH"] = "run.sh"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert executable.stat().st_mode & stat.S_IXUSR


def test_compose_wrapper_rejects_checksum_that_is_not_on_main(tmp_path: Path) -> None:
    env, _ = _wrapper_env(tmp_path)
    env["FAKE_DITTOBENCH_IS_MAIN"] = "false"

    result = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "is not in refs/heads/main history" in result.stderr


def test_compose_wrapper_caches_successful_main_ancestry(tmp_path: Path) -> None:
    env, _ = _wrapper_env(tmp_path)

    first = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert first.returncode == 0, first.stderr

    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    marker = (
        Path(env["DITTO_SUBNET_BUILD_CACHE"])
        / "dittobench-api"
        / f"{checksum}.ref-verified"
    )
    assert marker.read_text() == f"refs/heads/main {checksum}\n"

    # A later invocation trusts the immutable cached evidence and does not
    # repeat a fetch or ancestry command (the fake would fail either now).
    env["FAKE_DITTOBENCH_IS_MAIN"] = "false"
    env["FAKE_DITTOBENCH_FETCH_FAIL"] = "true"
    second = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert second.returncode == 0, second.stderr


def test_compose_wrapper_allows_exact_unmerged_ref_only_for_local_smoke(
    tmp_path: Path,
) -> None:
    env, _ = _wrapper_env(tmp_path)
    checksum = env["FAKE_DITTOBENCH_CHECKSUM"]
    env["DITTOBENCH_BUILD_CONTEXT"] = (
        "https://github.com/ditto-assistant/dittobench-api.git?"
        "ref=refs/heads/test/unmerged-smoke&"
        f"checksum={checksum}"
    )

    denied = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert denied.returncode == 1
    assert "require local network smoke opt-in" in denied.stderr

    for partial_opt_in in (
        {"DITTOBENCH_ALLOW_UNMERGED_SMOKE": "true"},
        {"SUBTENSOR_NETWORK": "local"},
    ):
        partially_allowed = subprocess.run(
            [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
            env={**env, **partial_opt_in},
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert partially_allowed.returncode == 1
        assert "require local network smoke opt-in" in partially_allowed.stderr

    smoke_env = {
        **env,
        "DITTOBENCH_ALLOW_UNMERGED_SMOKE": "true",
        "SUBTENSOR_NETWORK": "local",
    }
    wrong_tip = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env={
            **smoke_env,
            "FAKE_DITTOBENCH_REF_CHECKSUM": "f" * 40,
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert wrong_tip.returncode == 1
    assert "is not the current" in wrong_tip.stderr

    allowed = subprocess.run(
        [str(COMPOSE_WRAPPER), "build", "dittobench-api"],
        env=smoke_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
