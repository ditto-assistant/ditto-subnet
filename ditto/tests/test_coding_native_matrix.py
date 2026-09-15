"""Synthetic native bindings and fake engine calls; never run private controls."""

import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import re
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
ROOT = Path(__file__).parents[2]
DAEMON_VECTOR = json.loads(
    (
        ROOT / "services/dittobench-api/internal/codingenforcement/catalog/testdata/"
        "daemon-identity-vector-v1.json"
    ).read_bytes()
)


def approval():
    return {
        "schema": "dittobench-coding-native-controls-approval-v3",
        "purpose": "private-compatibility-once",
        "source_revision": SOURCE,
        "release_manifest_sha256": "1" * 64,
        "plan_sha256": "2" * 64,
        "helper_sha256": "3" * 64,
        "runner_sha256": "4" * 64,
        "binding_sha256": "5" * 64,
        "evidence_tool_sha256": "d" * 64,
        "curator_signing_key_sha256": NATIVE.CURATOR_SIGNING_KEY_SHA256,
        "machine_id_sha256": "6" * 64,
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "daemon_identity": copy.deepcopy(DAEMON_VECTOR["identity"]),
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
        "profile_pins": dict.fromkeys(NATIVE.PROFILE_PINS, "e" * 64),
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
        ("schema", "dittobench-coding-native-controls-approval-v2"),
        ("evidence_tool_sha256", "0" * 64),
        ("curator_signing_key_sha256", "short"),
        ("daemon_identity", {}),
        ("daemon_identity", None),
        ("profile_pins", {}),
        ("profile_pins", {"execution_profile_sha256": "e" * 64}),
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
        "schema": "dittobench-coding-native-release-set-v3",
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
    binding.signature_sha = "f" * 64
    binding.deadline, binding.consumed = 2000, False
    binding.daemon = DAEMON_VECTOR["identity_sha256"]
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
        ["--native-approval-signature", "/unread/approval.sig"],
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


def test_local_mode_cannot_select_native_or_remote_engine(monkeypatch, tmp_path):
    client = tmp_path / "client"
    client.mkdir(mode=0o700)
    monkeypatch.setenv("SYNTHETIC_SECRET", "must-not-inherit")
    monkeypatch.setattr(RUNNER.platform, "node", lambda: "synthetic-local")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert (
        RUNNER.local_engine_environment(client)["DOCKER_HOST"]
        == "unix:///var/run/docker.sock"
    )
    monkeypatch.setenv("DOCKER_CONTEXT", "native")
    with pytest.raises(ValueError):
        RUNNER.local_engine_environment(client)
    monkeypatch.delenv("DOCKER_CONTEXT")
    monkeypatch.setenv("DOCKER_HOST", "ssh://native")
    with pytest.raises(ValueError):
        RUNNER.local_engine_environment(client)
    monkeypatch.delenv("DOCKER_HOST")
    assert "SYNTHETIC_SECRET" not in RUNNER.local_engine_environment(client)
    assert RUNNER.local_engine_environment(client)["DOCKER_CONFIG"] == str(client)
    (client / "config.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        RUNNER.local_engine_environment(client)
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


@pytest.mark.parametrize("batch_failure", [False, True])
def test_native_runner_wiring_retains_manifest_authority_without_real_docker(
    monkeypatch, tmp_path, batch_failure
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
    submitted = []
    if batch_failure:
        # Both controls have finished when wait returns: inspect the successful
        # one first to reproduce replenishment before the second one's failure.
        class ImmediatePool:
            def __init__(self, *, max_workers):
                assert max_workers == 2

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def submit(self, function, item):
                future = RUNNER.concurrent.futures.Future()
                submitted.append(future)
                if len(submitted) == 2:
                    future.set_exception(RuntimeError("synthetic control failure"))
                else:
                    future.set_result(function(item))
                return future

        monkeypatch.setattr(
            RUNNER.concurrent.futures, "ThreadPoolExecutor", ImmediatePool
        )
        monkeypatch.setattr(
            RUNNER.concurrent.futures,
            "wait",
            lambda pending, **_kwargs: (
                sorted(pending, key=submitted.index),
                set(),
            ),
        )
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
            "2" if batch_failure else "1",
            "--private-native-controls-once",
            "--native-approval",
            "/unused/approval",
            "--native-approval-signature",
            "/unused/approval.sig",
            "--native-release-index",
            "/unused/release",
        ],
    )
    previous = os.umask(0o077)
    try:
        if batch_failure:
            with pytest.raises(RuntimeError, match="synthetic control failure"):
                RUNNER.main()
            assert len(submitted) == 2, "failure must prevent replacement controls"
            assert not (destination / "summary.json").exists()
            assert (destination / "case-0000-1.json").exists()
            return
        else:
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


# ---------------------------------------------------------------------------
# B5 PR 3a: on-host curator signature and daemon identity (Peyton, 2026-09-15)

EVIDENCE_TESTS = importlib.import_module(
    "ditto.tests.test_coding_native_enforcement_evidence"
)
Curator = EVIDENCE_TESTS.Curator
needs_openssl = EVIDENCE_TESTS.needs_openssl
EXPECTED = {
    "source": SOURCE,
    "plan_sha": "2" * 64,
    "helper_sha": "3" * 64,
    "controls": 32,
    "jobs": 2,
}


class HostApproval:
    """A synthetic curator, a copied verifier checkout and private files."""

    def __init__(self, tmp_path, monkeypatch):
        self.directory = tmp_path / "private"
        self.directory.mkdir(mode=0o700)
        self.curator = Curator(tmp_path / "curator", "curator")
        checkout = tmp_path / "checkout"
        verifier = checkout / NATIVE.VERIFIER
        verifier.parent.mkdir(parents=True)
        verifier.write_bytes((ROOT / NATIVE.VERIFIER).read_bytes())
        verifier.chmod(0o644)
        self.verifier_sha = NATIVE.sha(verifier.read_bytes())
        self.reads = []
        real_read = NATIVE.read_private

        def read_private(path, maximum):
            self.reads.append(path)
            return real_read(path, maximum)

        monkeypatch.setattr(NATIVE, "ROOT", checkout)
        monkeypatch.setattr(NATIVE, "OPENSSL", Path(EVIDENCE_TESTS.OPENSSL_BIN))
        monkeypatch.setattr(NATIVE, "protected_parents", lambda _path: None)
        monkeypatch.setattr(NATIVE, "read_private", read_private)
        monkeypatch.setattr(
            NATIVE, "CURATOR_SIGNING_PUBLIC_KEY", self.curator.public.read_bytes()
        )
        monkeypatch.setattr(
            NATIVE, "CURATOR_SIGNING_KEY_SHA256", self.curator.key_sha256
        )

    def value(self, **overrides):
        value = approval()
        value.update(
            curator_signing_key_sha256=self.curator.key_sha256,
            evidence_tool_sha256=self.verifier_sha,
        )
        value.update(overrides)
        return value

    def write(self, value=None, *, raw=None, signer=None, name="approval"):
        body = raw if raw is not None else NATIVE.canonical(value or self.value())
        path = self.directory / f"{name}.json"
        path.write_bytes(body)
        path.chmod(0o600)
        signature = (signer or self.curator).sign(body, self.directory / f"{name}.sig")
        signature.chmod(0o600)
        return path, signature

    def authorize(self, path, signature):
        return NATIVE.authorize(path, signature, now=1000, **EXPECTED)


@pytest.fixture
def host_approval(tmp_path, monkeypatch):
    return HostApproval(tmp_path, monkeypatch)


@needs_openssl
def test_host_verifies_the_curator_signature_over_the_canonical_approval(
    host_approval,
):
    path, signature = host_approval.write()
    value, approval_sha, signature_sha = host_approval.authorize(path, signature)
    assert value == host_approval.value()
    assert approval_sha == NATIVE.sha(path.read_bytes())
    assert signature_sha == NATIVE.sha(signature.read_bytes())
    # One read each: the verified bytes are the only approval ever parsed.
    assert host_approval.reads == [path, signature]


@needs_openssl
def test_host_refuses_a_bad_signature(host_approval):
    path, signature = host_approval.write()
    raw = bytearray(signature.read_bytes())
    raw[10] ^= 1
    signature.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="does not verify"):
        host_approval.authorize(path, signature)
    # A validly signed approval edited afterwards is refused too.
    path, signature = host_approval.write()
    path.write_bytes(path.read_bytes().replace(b'"controls":32', b'"controls":64'))
    with pytest.raises(ValueError, match="does not verify"):
        host_approval.authorize(path, signature)


@needs_openssl
def test_host_refuses_another_key(host_approval, tmp_path, monkeypatch):
    other = Curator(tmp_path / "other", "other")
    path, signature = host_approval.write(signer=other)
    with pytest.raises(ValueError, match="does not verify"):
        host_approval.authorize(path, signature)
    # Substituting the verification key alone is caught by the pinned identity.
    monkeypatch.setattr(NATIVE, "CURATOR_SIGNING_PUBLIC_KEY", other.public.read_bytes())
    with pytest.raises(ValueError, match="native control approval rejected"):
        host_approval.authorize(path, signature)
    # A validly signed approval that names another key is refused.
    monkeypatch.setattr(
        NATIVE, "CURATOR_SIGNING_PUBLIC_KEY", host_approval.curator.public.read_bytes()
    )
    path, signature = host_approval.write(
        host_approval.value(curator_signing_key_sha256=other.key_sha256)
    )
    with pytest.raises(ValueError, match="native control approval rejected"):
        host_approval.authorize(path, signature)


@needs_openssl
def test_host_refuses_a_missing_or_malformed_signature(host_approval):
    path, signature = host_approval.write()
    signature.unlink()
    with pytest.raises(OSError):
        host_approval.authorize(path, signature)
    path, signature = host_approval.write()
    signature.write_bytes(signature.read_bytes()[:63])
    with pytest.raises(ValueError):
        host_approval.authorize(path, signature)
    signature.write_bytes(b"")
    with pytest.raises(ValueError):
        host_approval.authorize(path, signature)
    path, signature = host_approval.write()
    signature.chmod(0o644)
    with pytest.raises(ValueError):
        host_approval.authorize(path, signature)


@needs_openssl
def test_host_refuses_a_signed_but_noncanonical_approval(host_approval):
    raw = json.dumps(host_approval.value(), indent=2, sort_keys=True).encode()
    path, signature = host_approval.write(raw=raw)
    with pytest.raises(ValueError, match="not canonical"):
        host_approval.authorize(path, signature)
    duplicate = NATIVE.canonical(host_approval.value())[:-1] + b',"schema":"x"}'
    path, signature = host_approval.write(raw=duplicate)
    with pytest.raises(ValueError):
        host_approval.authorize(path, signature)


@needs_openssl
def test_host_refuses_a_verifier_other_than_the_signed_one(host_approval):
    path, signature = host_approval.write(
        host_approval.value(evidence_tool_sha256="c" * 64)
    )
    with pytest.raises(ValueError, match="native control approval rejected"):
        host_approval.authorize(path, signature)
    verifier = NATIVE.ROOT / NATIVE.VERIFIER
    verifier.chmod(0o664)
    path, signature = host_approval.write()
    with pytest.raises(ValueError):
        host_approval.authorize(path, signature)


@needs_openssl
def test_host_refuses_a_signed_approval_for_another_invocation(host_approval):
    path, signature = host_approval.write(host_approval.value(controls=16))
    with pytest.raises(ValueError, match="native control approval rejected"):
        host_approval.authorize(path, signature)
    path, signature = host_approval.write(host_approval.value(issued_at_unix=1001))
    with pytest.raises(ValueError, match="native control approval rejected"):
        host_approval.authorize(path, signature)


def test_hash_only_invocation_is_refused_before_reading_anything(monkeypatch):
    def forbidden(*_args):
        pytest.fail("a hash-only invocation read private input")

    monkeypatch.setattr(RUNNER, "private_document", forbidden)
    base = [
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
        "--private-native-controls-once",
        "--native-approval",
        "/unread/approval",
        "--native-release-index",
        "/unread/release",
    ]
    for extra in (
        ["--native-approval-sha256", "a" * 64],
        ["--native-approval-sha", "a" * 64],
        # A prefix of --native-approval-signature is not accepted as one.
        ["--native-approval-sig", "/unread/signature"],
        [],
    ):
        monkeypatch.setattr(RUNNER.sys, "argv", [*base, *extra])
        with pytest.raises(SystemExit) as error:
            RUNNER.main()
        assert error.value.code == 2
    assert "native_approval_sha256" not in Path(RUNNER_FILE).read_text()
    assert "expected_sha" not in inspect.signature(NATIVE.Binding).parameters


def test_the_curator_key_is_pinned_in_source_not_selected_at_runtime():
    assert list(inspect.signature(NATIVE.authorize).parameters) == [
        "approval_path",
        "signature_path",
        "now",
        "expected",
    ]
    source = Path(NATIVE.__file__).read_text()
    assert "os.environ" not in source and "getenv" not in source
    namespace = {"__name__": "verifier", "__builtins__": __builtins__}
    raw = (ROOT / NATIVE.VERIFIER).read_bytes()
    exec(compile(raw, NATIVE.VERIFIER, "exec", dont_inherit=True), namespace)
    key = namespace["curator_public_key"](NATIVE.CURATOR_SIGNING_PUBLIC_KEY)
    assert NATIVE.sha(key) == NATIVE.CURATOR_SIGNING_KEY_SHA256
    # Stage 3 custody record: the registry identity of Peyton's offline key.
    assert NATIVE.CURATOR_SIGNING_KEY_SHA256 == (
        "aa7e1d820f2cfea52932c21629c0f51d3b1f9c2362b218e7a8759e32fd8b2220"
    )


def test_no_native_or_evidence_tool_can_mint_an_approval():
    for relative in (
        NATIVE.VERIFIER,
        "services/dittobench-api/coding_runtime/qualification/native.py",
        "services/dittobench-api/coding_runtime/qualification/run.py",
        "infra/scripts/inspect-coding-native-host.py",
    ):
        source = (ROOT / relative).read_text()
        for forbidden in ('"-sign"', "genpkey", "private_key", "Ed25519PrivateKey"):
            assert forbidden not in source, (relative, forbidden)


def test_native_daemon_identity_matches_the_shared_go_vector():
    info = DAEMON_VECTOR["info"]
    identity = NATIVE.daemon_identity(copy.deepcopy(info))
    assert NATIVE.canonical(identity).decode() == DAEMON_VECTOR["identity_canonical"]
    assert NATIVE.daemon_identity_sha256(identity) == DAEMON_VECTOR["identity_sha256"]
    assert str(NATIVE.SOCKET) == DAEMON_VECTOR["socket_path"]
    for key, value in DAEMON_VECTOR["volatile"].items():
        assert NATIVE.daemon_identity({**info, key: value}) == identity, key
    for key, value in DAEMON_VECTOR["binding"].items():
        assert NATIVE.daemon_identity({**info, key: value}) != identity, key
    for key, value in DAEMON_VECTOR["refused"].items():
        with pytest.raises(ValueError):
            NATIVE.daemon_identity({**info, key: value})
        with pytest.raises(ValueError):
            NATIVE.daemon_identity({k: v for k, v in info.items() if k != key})
    for socket_path in ("", "relative.sock", "//run/docker.sock", "/run/../x.sock"):
        with pytest.raises(ValueError):
            NATIVE.daemon_identity(copy.deepcopy(info), socket_path)


def daemon_binding(monkeypatch, *, peer_uid=None):
    binding = bare_binding(monkeypatch)
    binding.daemon = None
    binding.image_policy = SimpleNamespace(validate_daemon=lambda _info: None)
    uid = os.geteuid() if peer_uid is None else peer_uid
    monkeypatch.setattr(NATIVE, "socket_peer_uid", lambda: uid)
    return binding


def test_daemon_identity_must_equal_the_approved_identity(monkeypatch):
    binding = daemon_binding(monkeypatch)
    binding.check_daemon(copy.deepcopy(DAEMON_VECTOR["info"]))
    assert binding.daemon == DAEMON_VECTOR["identity_sha256"]
    assert binding.provenance()["daemon_identity_sha256"] == binding.daemon
    for key, value in DAEMON_VECTOR["binding"].items():
        other = daemon_binding(monkeypatch)
        with pytest.raises(ValueError):
            other.check_daemon({**DAEMON_VECTOR["info"], key: value})
        assert other.daemon is None
    # The daemon cannot change between the checks before and after the matrix.
    binding.value["daemon_identity"]["server_version"] = "29.1.4"
    with pytest.raises(ValueError):
        binding.check_daemon({**DAEMON_VECTOR["info"], "ServerVersion": "29.1.4"})


def test_daemon_socket_must_be_served_by_the_native_principal(monkeypatch):
    binding = daemon_binding(monkeypatch, peer_uid=os.geteuid() + 1)
    with pytest.raises(ValueError):
        binding.check_daemon(copy.deepcopy(DAEMON_VECTOR["info"]))
    assert binding.daemon is None


def test_daemon_check_refuses_a_different_boot(monkeypatch):
    binding = daemon_binding(monkeypatch)

    def other_boot():
        raise ValueError("native control approval rejected")

    monkeypatch.setattr(binding, "check_current", other_boot)
    with pytest.raises(ValueError):
        binding.check_daemon(copy.deepcopy(DAEMON_VECTOR["info"]))
    assert binding.daemon is None


def test_ambient_docker_environment_never_selects_the_daemon(monkeypatch):
    for name, value in (
        ("DOCKER_HOST", "tcp://198.51.100.7:2375"),
        ("DOCKER_CONTEXT", "attacker"),
        ("DOCKER_CONFIG", "/tmp/attacker"),
        ("DOCKER_CERT_PATH", "/tmp/attacker"),
    ):
        monkeypatch.setenv(name, value)
    environment = NATIVE.native_environment()
    assert environment == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "DOCKER_HOST": f"unix://{NATIVE.SOCKET}",
        "DOCKER_CONFIG": str(NATIVE.HOME_DIR / "empty-client"),
    }
    # The approved identity names the fixed socket, so a daemon reached any
    # other way cannot present it.
    assert approval()["daemon_identity"]["socket_path"] == str(NATIVE.SOCKET)
    with pytest.raises(ValueError):
        NATIVE.daemon_identity_policy(
            {**approval()["daemon_identity"], "socket_path": "/run/other.sock"}
        )


def protected_runner_copy(tmp_path, monkeypatch, native=None):
    """run.py beside a native.py copy; ancestors are checked separately."""
    directory = tmp_path / "qualification"
    directory.mkdir()
    runner = directory / "run.py"
    runner.write_bytes(Path(RUNNER_FILE).read_bytes())
    path = directory / "native.py"
    real = Path(RUNNER_FILE).with_name("native.py").read_bytes()
    path.write_bytes(real if native is None else native)
    path.chmod(0o644)
    monkeypatch.setattr(RUNNER, "__file__", str(runner))
    monkeypatch.setattr(RUNNER, "protected_ancestors", lambda _path: None)
    return path


def test_runner_compiles_the_binding_it_hashes(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("native.py must not load through an import loader")

    path = protected_runner_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", forbidden)
    loaded = RUNNER.load_native()
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert expected == loaded.LOADED_SHA256
    assert loaded.__file__ == str(path)
    assert callable(loaded.authorize)
    assert 'globals().get("LOADED_SHA256")' in path.read_text()


def test_runner_refuses_an_editable_binding_before_compiling_it(monkeypatch, tmp_path):
    # Binding's own ownership checks run inside native.py, too late to refuse a
    # substituted module, so run.py checks before any byte is compiled.
    path = protected_runner_copy(
        tmp_path, monkeypatch, native=b"raise SystemExit('compiled')\n"
    )
    path.chmod(0o664)
    with pytest.raises(ValueError, match="native binding rejected"):
        RUNNER.load_native()
    path.chmod(0o644)
    link = path.with_name("second-link.py")
    os.link(path, link)
    with pytest.raises(ValueError, match="native binding rejected"):
        RUNNER.load_native()
    link.unlink()
    with pytest.raises(SystemExit, match="compiled"):
        RUNNER.load_native()
    # A shared writable ancestor (here /tmp) is refused as well.
    monkeypatch.setattr(RUNNER, "protected_ancestors", PROTECTED_ANCESTORS)
    with pytest.raises(ValueError, match="native binding rejected"):
        RUNNER.load_native()
    PROTECTED_ANCESTORS(Path("/usr/bin/native.py"))


PROTECTED_ANCESTORS = RUNNER.protected_ancestors


@pytest.mark.usefixtures("host_approval")
def test_host_verifier_is_protected_and_single_link_before_it_runs():
    verifier = NATIVE.ROOT / NATIVE.VERIFIER
    verifier.write_bytes(b"raise SystemExit('verifier ran')\n")
    verifier.chmod(0o644)
    with pytest.raises(SystemExit, match="verifier ran"):
        NATIVE.load_verifier()
    link = verifier.with_name("second-link.py")
    os.link(verifier, link)
    with pytest.raises(ValueError, match="native control approval rejected"):
        NATIVE.load_verifier()
    link.unlink()
    refused = []

    def unprotected(path):
        refused.append(path)
        raise ValueError("native control approval rejected")

    NATIVE.protected_parents, real = unprotected, NATIVE.protected_parents
    try:
        with pytest.raises(ValueError, match="native control approval rejected"):
            NATIVE.load_verifier()
    finally:
        NATIVE.protected_parents = real
    assert refused == [verifier]


def test_native_daemon_data_root_is_pinned():
    identity = approval()["daemon_identity"]
    NATIVE.daemon_identity_policy(identity)
    with pytest.raises(ValueError):
        NATIVE.daemon_identity_policy(
            {**identity, "docker_root_dir": "/var/lib/other/docker"}
        )


def test_private_read_refuses_a_file_changed_while_read(monkeypatch, tmp_path):
    monkeypatch.setattr(NATIVE, "private", lambda *_args, **_kwargs: None)
    path = tmp_path / "approval.json"
    path.write_bytes(b"{}")
    real_fstat, calls = os.fstat, []

    def changing(fd):
        info = real_fstat(fd)
        calls.append(fd)
        if len(calls) == 1:
            return info
        fields = list(info)
        return os.stat_result(
            (*fields[:7], info.st_atime, info.st_mtime, info.st_ctime),
            {"st_mtime_ns": info.st_mtime_ns + 1, "st_ctime_ns": info.st_ctime_ns},
        )

    assert NATIVE.read_private(path, 16) == b"{}"
    monkeypatch.setattr(NATIVE.os, "fstat", changing)
    calls.clear()
    with pytest.raises(ValueError):
        NATIVE.read_private(path, 16)


CATALOG_DIR = ROOT / "services/dittobench-api/internal/codingenforcement/catalog"
# Pinned identically in catalog/approval_test.go.
APPROVAL_VECTOR_SHA256 = (
    "c5d49e2f49dede8b95ebe6fd8e9253dd86b1be80e401ac68887cdeff8c2f7ed7"
)


def test_approval_vector_is_canonical_in_both_languages():
    raw = (CATALOG_DIR / "testdata/approval-vector-v3.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == APPROVAL_VECTOR_SHA256
    value = json.loads(raw)
    assert NATIVE.canonical(value) == raw
    NATIVE.policy(
        copy.deepcopy(value),
        source=value["source_revision"],
        plan_sha=value["plan_sha256"],
        helper_sha=value["helper_sha256"],
        controls=value["controls"],
        jobs=1,
        now=value["issued_at_unix"],
    )
    assert value["curator_signing_key_sha256"] == NATIVE.CURATOR_SIGNING_KEY_SHA256
    assert (
        NATIVE.daemon_identity_sha256(value["daemon_identity"])
        == DAEMON_VECTOR["identity_sha256"]
    )
    go = (CATALOG_DIR / "approval.go").read_text()
    assert f'ApprovalSchema = "{NATIVE.APPROVAL_SCHEMA}"' in go
    block = go.split("var ApprovalKeys = []string{", 1)[1].split("}", 1)[0]
    assert sorted(re.findall(r'"([a-z0-9_]+)"', block)) == sorted(value)
    assert (
        f'approvalVectorSHA256 = "{APPROVAL_VECTOR_SHA256}"'
        in (CATALOG_DIR / "approval_test.go").read_text()
    )
    for key in ("daemon_identity", "profile_pins", "evidence_tool_sha256"):
        changed = {k: v for k, v in value.items() if k != key}
        with pytest.raises(ValueError):
            NATIVE.policy(
                changed,
                source=value["source_revision"],
                plan_sha=value["plan_sha256"],
                helper_sha=value["helper_sha256"],
                controls=value["controls"],
                jobs=1,
                now=value["issued_at_unix"],
            )
