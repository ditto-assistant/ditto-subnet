"""Public qualification-tool contracts; no private corpus or candidate execution."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PATH = ROOT / "services/dittobench-api/coding_runtime/qualification/run.py"
SPEC = importlib.util.spec_from_file_location("coding_qualification", PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def image(language):
    env = "PATH=/usr/local/bin:/usr/bin:/bin"
    if language == "go":
        env = "PATH=/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin"
    return {
        "Id": "sha256:" + "a" * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "Env": [env],
            "Entrypoint": ["/usr/local/bin/dittobench-coding-supervisor"],
            "WorkingDir": "/workspace" if language in ("go", "rust") else "",
            "Labels": {
                "org.opencontainers.image.revision": "b" * 40,
                RUNNER.PREFIX + "coding-supervisor-contract": "1",
                RUNNER.PREFIX + "coding-test-driver-profile": RUNNER.PROFILES[language],
            },
        },
    }


@pytest.mark.parametrize("language", sorted(RUNNER.PROFILES))
def test_image_profile_source_and_runtime_target_are_exact(language):
    value = image(language)
    assert RUNNER.image_policy(value, language, "b" * 40) == value["Id"]
    for field, bad in (
        ("Env", ["SECRET=value"]),
        ("Entrypoint", ["/bin/sh"]),
        ("Volumes", {"/private": {}}),
        ("Cmd", ["sh"]),
        ("Healthcheck", {"Test": ["CMD", "true"]}),
        ("User", "10001"),
    ):
        altered = copy.deepcopy(value)
        altered["Config"][field] = bad
        with pytest.raises(ValueError):
            RUNNER.image_policy(altered, language, "b" * 40)
    value["Config"]["Labels"][RUNNER.PREFIX + "coding-supervisor-fixture"] = "true"
    with pytest.raises(ValueError):
        RUNNER.image_policy(value, language, "b" * 40)
    with pytest.raises(ValueError):
        RUNNER.image_policy(image(language), language, "c" * 40)


def test_container_envelope_has_no_network_or_host_socket():
    for language in RUNNER.PROFILES:
        args = RUNNER.envelope(language)
        assert args[args.index("--network") + 1] == "none"
        assert "--read-only" in args and "no-new-privileges" in args
        assert "--privileged" not in args and not any("docker.sock" in a for a in args)
        assert sum(a.startswith("/out:") for a in args) == (language == "rust")
        assert "--memory-swap" in args and "--pids-limit" in args


def test_repeat_projection_keeps_semantic_results_but_not_nonce_bound_bytes():
    first = {
        "passed": 2,
        "total": 2,
        "case_sha256": "a",
        "response_sha256": "b",
        "supervisor_response": {"nonce": "first"},
    }
    second = {**first, "response_sha256": "c", "supervisor_response": {"nonce": "next"}}
    assert RUNNER.stable(first) == RUNNER.stable(second)
    assert RUNNER.stable(first) != RUNNER.stable({**second, "passed": 1})


def test_private_report_creation_is_exclusive(tmp_path):
    RUNNER.save(tmp_path, "record.json", {"runtime_qualification": False})
    assert (tmp_path / "record.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        RUNNER.save(tmp_path, "record.json", {"runtime_qualification": True})
