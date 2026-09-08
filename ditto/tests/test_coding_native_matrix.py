"""Synthetic native bindings and fake engine calls; never run private controls."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto.tests.test_coding_hosted_image_import import POLICY as IMAGE
from ditto.tests.test_coding_qualification import RUNNER, image

RUNNER_FILE = RUNNER.__file__
assert RUNNER_FILE is not None
SPEC = importlib.util.spec_from_file_location(
    "native_matrix", Path(RUNNER_FILE).with_name("native.py")
)
assert SPEC is not None and SPEC.loader is not None
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)
SOURCE = "b" * 40


def approval():
    return {
        "schema": "dittobench-coding-native-controls-approval-v2",
        "purpose": "private-compatibility-once",
        "source_revision": SOURCE,
        "release_manifest_sha256": "1" * 64,
        "plan_sha256": "2" * 64,
        "helper_sha256": "3" * 64,
        "runner_sha256": "4" * 64,
        "binding_sha256": "5" * 64,
        "machine_id_sha256": "6" * 64,
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "issued_at_unix": 900,
        "expires_at_unix": 2000,
        "controls": 32,
        "max_jobs": 2,
        "images": {
            lang: {
                "image_ref": f"coding-runtime.invalid/{lang}/runtime@sha256:"
                + "c" * 64,
                "config_digest": "sha256:" + "a" * 64,
                "approval_sha256": "7" * 64,
                "driver_profile": profile,
            }
            for lang, profile in NATIVE.PROFILES.items()
        },
        "evidence_sha256": dict.fromkeys(NATIVE.EVIDENCE, "8" * 64),
        "shadow_only": True,
        "weight_eligible": False,
    }


def validate(value):
    return NATIVE.policy(
        value,
        source=SOURCE,
        plan_sha="2" * 64,
        helper_sha="3" * 64,
        controls=32,
        jobs=2,
        now=1000,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "canary"),
        ("source_revision", "f" * 40),
        ("plan_sha256", "a" * 64),
        ("helper_sha256", "a" * 64),
        ("controls", True),
        ("controls", 400),
        ("max_jobs", 1),
        ("max_jobs", True),
        ("issued_at_unix", 1001),
        ("expires_at_unix", 1000),
        ("expires_at_unix", 90000),
        ("shadow_only", 1),
        ("weight_eligible", True),
        ("machine_id_sha256", "0" * 64),
        ("boot_id", "wrong"),
        ("evidence_sha256", {}),
        ("images", {}),
        ("extra", True),
    ],
)
def test_native_approval_is_exact_bounded_and_not_an_activation(field, value):
    assert validate(approval()) == approval()
    changed = approval()
    changed[field] = value
    with pytest.raises(ValueError):
        validate(changed)


def test_release_index_and_each_image_pin_must_match():
    value = approval()
    release = {
        "schema": "dittobench-coding-native-release-set-v2",
        "source_revision": SOURCE,
        "images": copy.deepcopy(value["images"]),
        "independent_approval_required": True,
        "native_imported": False,
        "runtime_qualification": False,
        "canary_completed": False,
        "shadow_only": True,
        "weight_eligible": False,
    }
    NATIVE.release_policy(release, value)
    for field in ("config_digest", "image_ref", "approval_sha256", "driver_profile"):
        changed = copy.deepcopy(release)
        changed["images"]["rust"][field] = "wrong"
        with pytest.raises(ValueError):
            NATIVE.release_policy(changed, value)
    with pytest.raises(ValueError):
        NATIVE.release_policy({**release, "native_imported": True}, value)


def bare_binding(monkeypatch):
    binding = NATIVE.Binding.__new__(NATIVE.Binding)
    binding.value, binding.approval_sha = approval(), "9" * 64
    binding.deadline, binding.daemon, binding.consumed = 2000, "synthetic-daemon", False
    binding.image_policy = IMAGE
    monkeypatch.setattr(binding, "check_current", lambda: None)
    return binding


@pytest.mark.parametrize("language", sorted(NATIVE.PROFILES))
def test_native_image_binding_uses_manifest_not_local_config(monkeypatch, language):
    binding = bare_binding(monkeypatch)
    inspected = image(language)
    reference = binding.value["images"][language]["image_ref"]
    inspected.update(
        {"RepoDigests": [reference], "Descriptor": {"digest": "sha256:" + "c" * 64}}
    )
    assert binding.select_image(language, reference, inspected) == (
        reference,
        "sha256:" + "c" * 64,
    )
    with pytest.raises(ValueError):
        binding.select_image(language, inspected["Id"], inspected)
    inspected["Descriptor"]["digest"] = inspected["Id"]
    with pytest.raises(ValueError):
        binding.select_image(language, reference, inspected)


def test_single_use_is_independent_of_output_directory_and_partial_marker(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(NATIVE, "STATE", tmp_path)
    monkeypatch.setattr(NATIVE, "private", lambda *_args, **_kwargs: None)
    binding = bare_binding(monkeypatch)
    binding.consume()
    marker = tmp_path / (binding.approval_sha + ".consumed")
    assert marker.read_text() == binding.approval_sha + "\n"
    assert marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        binding.consume()
    another = bare_binding(monkeypatch)
    with pytest.raises(FileExistsError):
        another.consume()
    marker.write_bytes(b"")
    with pytest.raises(FileExistsError):
        another.consume()


def test_failed_consumption_retains_a_tombstone(monkeypatch, tmp_path):
    monkeypatch.setattr(NATIVE, "STATE", tmp_path)
    monkeypatch.setattr(NATIVE, "private", lambda *_args, **_kwargs: None)

    def failed(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(NATIVE.os, "fsync", failed)
    binding = bare_binding(monkeypatch)
    with pytest.raises(OSError):
        binding.consume()
    assert (tmp_path / (binding.approval_sha + ".consumed")).exists()
    with pytest.raises(FileExistsError):
        bare_binding(monkeypatch).consume()


def test_control_deadline_reserves_cleanup_time(monkeypatch):
    binding = bare_binding(monkeypatch)
    binding.consumed = True
    monkeypatch.setattr(NATIVE.time, "time", lambda: 1000)
    monkeypatch.setattr(NATIVE.time, "monotonic", lambda: 1000)
    assert binding.control_timeout() == 210
    binding.deadline = 1100
    assert binding.control_timeout() == 80
    binding.deadline = 1020
    with pytest.raises(ValueError):
        binding.control_timeout()


def test_boot_binding_and_monotonic_expiry_cannot_be_extended(monkeypatch):
    binding = NATIVE.Binding.__new__(NATIVE.Binding)
    binding.value, binding.deadline = approval(), 1200
    machine = b"a" * 32
    binding.value["machine_id_sha256"] = NATIVE.sha(machine)
    clock = {"wall": 1000, "mono": 1000, "boot": binding.value["boot_id"]}
    monkeypatch.setattr(NATIVE.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(NATIVE.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(Path, "read_bytes", lambda _path: machine + b"\n")
    monkeypatch.setattr(Path, "read_text", lambda _path: clock["boot"])
    binding.check_current()
    clock.update(wall=950, mono=1201)
    with pytest.raises(ValueError):
        binding.check_current()
    clock.update(wall=1000, mono=1000, boot="different-boot")
    with pytest.raises(ValueError):
        binding.check_current()


@pytest.mark.parametrize(
    "native_options",
    [
        ["--private-native-controls-once"],
        ["--native-approval", "/unread/approval"],
        ["--native-approval-sha256", "a" * 64],
        ["--native-release-index", "/unread/release"],
    ],
)
def test_partial_native_opt_in_never_falls_back_to_local(monkeypatch, native_options):
    def forbidden(*_args):
        pytest.fail("incomplete opt-in read private input")

    monkeypatch.setattr(RUNNER, "private_document", forbidden)
    monkeypatch.setattr(
        RUNNER.sys,
        "argv",
        [
            "run",
            "--plan",
            "/unread/plan",
            "--images",
            "/unread/images",
            "--corpus",
            "/unread/corpus",
            "--helper",
            "/unread/helper",
            "--checkout",
            "/unread/checkout",
            "--output",
            "/unread/output",
            *native_options,
        ],
    )
    with pytest.raises(SystemExit) as error:
        RUNNER.main()
    assert error.value.code == 2


def test_private_approval_hash_and_duplicate_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(NATIVE, "private", lambda *_args, **_kwargs: None)
    path = tmp_path / "approval.json"
    raw = json.dumps(approval()).encode()
    path.write_bytes(raw)
    assert NATIVE.read_approval(path, NATIVE.sha(raw)) == approval()
    with pytest.raises(ValueError):
        NATIVE.read_approval(path, "f" * 64)
    raw = b'{"schema":1,"schema":2}'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        NATIVE.read_approval(path, NATIVE.sha(raw))


def test_case_inputs_survive_unknown_cleanup(tmp_path):
    with pytest.raises(RuntimeError), RUNNER.retained_case(tmp_path) as directory:
        folder = Path(directory)
        RUNNER.save(folder, "case.json", {"public": "synthetic"})
        raise RuntimeError("cleanup unconfirmed")
    assert json.loads((folder / "case.json").read_bytes()) == {"public": "synthetic"}


def test_private_plan_hashes_exact_loaded_bytes_and_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "plan.json"
    raw = b'{"public":"synthetic"}\n'
    path.write_bytes(raw)
    path.chmod(0o600)
    assert RUNNER.private_document(path) == (
        {"public": "synthetic"},
        RUNNER.digest(raw),
    )
    path.write_bytes(b'{"public":1,"public":2}')
    with pytest.raises(ValueError, match="duplicate"):
        RUNNER.private_document(path)


def test_native_private_paths_and_helper_are_protected(monkeypatch, tmp_path):
    monkeypatch.setattr(NATIVE, "protected_parents", lambda _path: None)
    corpus, output = tmp_path / "corpus", tmp_path / "output"
    corpus.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    helper = tmp_path / "helper"
    helper.write_bytes(b"\x7fELF\x02\x01" + bytes(12) + b"\x3e\x00")
    helper.chmod(0o555)
    plan, images = tmp_path / "plan", tmp_path / "images"
    for path in (plan, images):
        path.write_bytes(b"{}")
        path.chmod(0o600)
    NATIVE.validate_paths(corpus, helper, output, plan, images)
    helper.chmod(0o755)
    with pytest.raises(ValueError):
        NATIVE.validate_paths(corpus, helper, output, plan, images)
    helper.chmod(0o555)
    corpus.chmod(0o755)
    with pytest.raises(ValueError):
        NATIVE.validate_paths(corpus, helper, output, plan, images)


def test_native_parent_symlink_and_shared_ancestor_refused(tmp_path):
    path = tmp_path / "target"
    path.write_bytes(b"public")
    link = tmp_path / "alias"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        NATIVE.protected_parents(link)
    # Native operator storage intentionally does not admit /tmp's writable parent.
    with pytest.raises(ValueError):
        NATIVE.protected_parents(path)


def test_local_mode_cannot_select_native_or_remote_engine(monkeypatch):
    monkeypatch.setattr(RUNNER.platform, "node", lambda: "synthetic-local")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert (
        RUNNER.local_engine_environment()["DOCKER_HOST"]
        == "unix:///var/run/docker.sock"
    )
    monkeypatch.setenv("DOCKER_CONTEXT", "native")
    with pytest.raises(ValueError):
        RUNNER.local_engine_environment()
    monkeypatch.delenv("DOCKER_CONTEXT")
    monkeypatch.setenv("DOCKER_HOST", "ssh://native")
    with pytest.raises(ValueError):
        RUNNER.local_engine_environment()
    RUNNER.local_engine_policy(
        {"Name": "synthetic-local", "DockerRootDir": "/var/lib/docker"}
    )
    for info in (
        {},
        {"Name": "ditto-coding-hosted-v2", "DockerRootDir": "/var/lib/docker"},
        {"Name": "alias", "DockerRootDir": "/var/lib/ditto-coding-hosted/docker"},
    ):
        with pytest.raises(ValueError):
            RUNNER.local_engine_policy(info)


def test_native_runner_wiring_retains_manifest_authority_without_real_docker(
    monkeypatch, tmp_path
):
    corpus = tmp_path / "corpus"
    corpus.mkdir(mode=0o700)
    helper = tmp_path / "helper"
    helper.write_bytes(b"synthetic public helper")
    helper.chmod(0o555)
    cases = [
        {
            "language": lang,
            "group_id": f"private-group-{n:03}",
            "role": role,
            "phase": phase,
        }
        for n, lang in enumerate(NATIVE.PROFILES, 1)
        for role in ("base", "reference")
        for phase in ("visible", "hidden")
    ]
    plan = {
        "schema": "dittobench-private-compatibility-plan-v1",
        "source_sha": SOURCE,
        "replicates": 2,
        "cases": cases,
    }
    refs = {lang: value["image_ref"] for lang, value in approval()["images"].items()}
    for name, value in (("plan.json", plan), ("images.json", refs)):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    calls = []

    class FakeBinding:
        environment = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": "unix:///native-only"}
        consumed = False

        def __init__(self, *_args, **expected):
            assert expected["controls"] == 32 and expected["source"] == SOURCE
            assert (
                expected["helper_sha"]
                == hashlib.sha256(helper.read_bytes()).hexdigest()
            )

        def command(self, args):
            return ["/usr/bin/docker", *args]

        def check_daemon(self, _info):
            calls.append("daemon")

        def select_image(self, _language, reference, _info):
            return reference, "sha256:" + "c" * 64

        def consume(self):
            assert not self.consumed
            self.consumed = True
            calls.append("consume")

        def control_timeout(self):
            assert self.consumed
            return 7

        def provenance(self):
            return {"approval_sha256": "9" * 64}

    def validate_paths(*paths):
        assert paths == (
            corpus,
            helper,
            tmp_path,
            tmp_path / "plan.json",
            tmp_path / "images.json",
        )

    monkeypatch.setattr(
        RUNNER,
        "load_native",
        lambda: SimpleNamespace(Binding=FakeBinding, validate_paths=validate_paths),
    )

    def output(args, **kwargs):
        if args[0] == "git":
            return SOURCE if args[1] == "rev-parse" else ""
        if args[0] == "uname":
            return "synthetic-kernel"
        assert kwargs["env"] == FakeBinding.environment and args[0] == "/usr/bin/docker"
        if args[1] == "info":
            return "{}"
        language = next(lang for lang, ref in refs.items() if ref == args[-1])
        return json.dumps([image(language)])

    monkeypatch.setattr(RUNNER, "output", output)

    def run(args, **kwargs):
        assert kwargs["env"] == FakeBinding.environment
        if args[1:3] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"No such container"
            )
        assert args[1] == "run" and "--pull=never" in args and kwargs["timeout"] == 7
        assert args[-1] in refs.values() and calls[1] == "consume"
        mount = next(arg for arg in args if "dst=/private-control," in arg)
        folder = Path(mount.split("src=", 1)[1].split(",", 1)[0])
        raw = (folder / "case.json").read_bytes()
        assert json.loads(raw)["image_sha256"] == "c" * 64
        return SimpleNamespace(
            returncode=0,
            stderr=b"",
            stdout=json.dumps(
                {
                    "schema": "dittobench-private-compatibility-observation-v1",
                    "case_sha256": RUNNER.digest(raw),
                    "expectation_matched": True,
                    "completed": True,
                    "process_tree_dead": True,
                    "runtime_qualification": False,
                    "production_api_approval": False,
                }
            ).encode(),
        )

    monkeypatch.setattr(RUNNER.subprocess, "run", run)
    destination = tmp_path / "result"
    monkeypatch.setattr(
        RUNNER.sys,
        "argv",
        [
            "run",
            "--plan",
            str(tmp_path / "plan.json"),
            "--images",
            str(tmp_path / "images.json"),
            "--corpus",
            str(corpus),
            "--helper",
            str(helper),
            "--checkout",
            str(NATIVE.ROOT),
            "--output",
            str(destination),
            "--jobs",
            "1",
            "--private-native-controls-once",
            "--native-approval",
            "/unused/approval",
            "--native-approval-sha256",
            "9" * 64,
            "--native-release-index",
            "/unused/release",
        ],
    )
    previous = os.umask(0o077)
    try:
        RUNNER.main()
    finally:
        os.umask(previous)
    summary = json.loads((destination / "summary.json").read_bytes())
    assert summary["native_controls_passed"] is True and summary["controls"] == 32
    assert (
        summary["runtime_qualification"] is False
        and summary["native_host_ready"] is False
    )
    assert summary["image_binding_kind"] == "approved_native_oci_manifest"
    assert calls == ["daemon", "consume", "daemon"]
    assert (
        len(list(destination.glob("case-*"))) == 64
    )  # inputs and observations retained
