"""Public OCI fixtures only; real Docker metadata test is explicitly opt-in."""

import contextlib
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra/ansible/roles/coding_hosted_image/files/image-bundle.py"
spec = importlib.util.spec_from_file_location("hosted_image", SCRIPT)
assert spec is not None and spec.loader is not None
POLICY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(POLICY)
REVISION = "1" * 40
REPO = "coding-hosted-fixture.invalid/public/image"


def configuration():
    return {
        "Entrypoint": POLICY.ENTRYPOINT,
        "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"],
        "Labels": {
            POLICY.PREFIX + "coding-supervisor-contract": "1",
            POLICY.PREFIX + "coding-test-driver-profile": POLICY.PROFILE,
            "org.opencontainers.image.revision": REVISION,
        },
    }


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, raw in entries:
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    return output.getvalue()


def fixture_entries(config=None, alter_manifest=None):
    files = {}

    def blob(raw, media):
        digest = hashlib.sha256(raw).hexdigest()
        files["blobs/sha256/" + digest] = raw
        return {"mediaType": media, "digest": "sha256:" + digest, "size": len(raw)}

    layer = blob(tar_bytes([]), "application/vnd.oci.image.layer.v1.tar")
    config = blob(
        POLICY.json_bytes(
            {
                "architecture": "amd64",
                "os": "linux",
                "config": configuration() if config is None else config,
                "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
            }
        ),
        POLICY.CONFIG_TYPE,
    )
    manifest = {
        "schemaVersion": 2,
        "mediaType": POLICY.MANIFEST_TYPE,
        "config": config,
        "layers": [layer],
    }
    if alter_manifest:
        alter_manifest(manifest)
    selected = blob(POLICY.json_bytes(manifest), POLICY.MANIFEST_TYPE)
    files["index.json"] = POLICY.json_bytes(
        {"schemaVersion": 2, "manifests": [selected]}
    )
    files["oci-layout"] = POLICY.json_bytes({"imageLayoutVersion": "1.0.0"})
    return files


def prepared(tmp_path, repository=REPO, profile=POLICY.PROFILE):
    source, output, manifest = (
        tmp_path / n for n in ("source.tar", "image.tar", "approval.json")
    )
    config = configuration()
    config["Labels"][POLICY.PREFIX + "coding-test-driver-profile"] = profile
    source.write_bytes(tar_bytes(fixture_entries(config).items()))
    approval = POLICY.prepare(source, output, manifest, repository, REVISION)
    raw = manifest.read_bytes()
    return output, raw, hashlib.sha256(raw).hexdigest(), approval


def test_roundtrip_preserves_manifest_and_config_identity(tmp_path):
    output, raw, sha, approval = prepared(tmp_path)
    with output.open("rb") as stream:
        assert POLICY.verify(stream, raw, sha) == approval
        assert stream.tell() == 0
    assert approval["image_ref"].startswith(REPO + "@sha256:")
    assert approval["weight_eligible"] is False
    assert (
        len(
            {
                approval["archive_sha256"],
                approval["config_digest"][7:],
                approval["image_ref"].split(":")[-1],
            }
        )
        == 3
    )


def test_prepare_never_overwrites_existing_output(tmp_path):
    output, _, _, _ = prepared(tmp_path)
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        POLICY.prepare(
            tmp_path / "source.tar", output, tmp_path / "new.json", REPO, REVISION
        )
    assert output.read_bytes() == before


@pytest.mark.parametrize("profile", ["python-call-ast-v1", "python-call-ast-v2"])
def test_driver_profile_is_bound_to_archive_and_loaded_image(tmp_path, profile):
    output, raw, sha, approval = prepared(tmp_path, profile=profile)
    assert approval["driver_profile"] == profile
    with output.open("rb") as stream:
        assert POLICY.verify(stream, raw, sha) == approval
    config = configuration()
    config["Labels"][POLICY.PREFIX + "coding-test-driver-profile"] = profile
    loaded = [
        {
            "RepoDigests": [approval["image_ref"]],
            "Id": approval["config_digest"],
            "Descriptor": {"digest": approval["image_ref"].split("@")[1]},
            "Os": "linux",
            "Architecture": "amd64",
            "Config": config,
        }
    ]
    POLICY.validate_loaded(loaded, approval)
    other = "python-call-ast-v1" if profile.endswith("v2") else "python-call-ast-v2"
    altered = POLICY.json_bytes({**approval, "driver_profile": other})
    with (
        output.open("rb") as stream,
        pytest.raises(ValueError, match="approval fields"),
    ):
        POLICY.verify(stream, altered, hashlib.sha256(altered).hexdigest())
    config["Labels"][POLICY.PREFIX + "coding-test-driver-profile"] = other
    with pytest.raises(ValueError, match="loaded driver profile"):
        POLICY.validate_loaded(loaded, approval)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Entrypoint", ["/bin/sh"]),
        ("Env", ["TOKEN=secret"]),
        ("Env", ["HTTPS_PROXY=http://proxy"]),
        ("User", "1000"),
        ("Volumes", {"/tmp": {}}),
        ("Cmd", ["sh"]),
        ("Healthcheck", {"Test": ["CMD", "true"]}),
        ("OnBuild", ["RUN true"]),
        ("WorkingDir", "/tmp"),
        ("ExposedPorts", {"80/tcp": {}}),
    ],
)
def test_rejects_unsafe_image_defaults(field, value):
    config = configuration()
    config[field] = value
    with pytest.raises(ValueError):
        POLICY.graph(io.BytesIO(tar_bytes(fixture_entries(config).items())), REVISION)


@pytest.mark.parametrize(
    "label,value",
    [
        (POLICY.PREFIX + "coding-supervisor-fixture", "true"),
        (POLICY.PREFIX + "coding-supervisor-contract", "2"),
        (POLICY.PREFIX + "coding-test-driver-profile", "node"),
        ("org.opencontainers.image.revision", "2" * 40),
    ],
)
def test_rejects_wrong_image_labels(label, value):
    config = configuration()
    config["Labels"][label] = value
    with pytest.raises(ValueError):
        POLICY.graph(io.BytesIO(tar_bytes(fixture_entries(config).items())), REVISION)


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["layers"][0].update(size=1),
        lambda m: m["layers"][0].update(urls=["https://external.invalid/layer"]),
        lambda m: m["layers"][0].update(
            mediaType="application/vnd.oci.image.layer.nondistributable.v1.tar"
        ),
        lambda m: m.update(subject={}),
        lambda m: m.update(layers=[]),
    ],
)
def test_rejects_invalid_manifest_graph(change):
    with pytest.raises(ValueError):
        POLICY.graph(
            io.BytesIO(tar_bytes(fixture_entries(alter_manifest=change).items())),
            REVISION,
        )


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate",
        "traversal",
        "extra_blob",
        "tamper",
        "trailer",
        "truncated",
        "pax",
        "symlink",
    ],
)
def test_rejects_archive_attacks(attack):
    files = fixture_entries()
    entries = list(files.items())
    if attack == "duplicate":
        entries.append(entries[0])
    elif attack == "traversal":
        entries.append(("../outside", b"bad"))
    elif attack == "extra_blob":
        entries.append(
            ("blobs/sha256/" + hashlib.sha256(b"extra").hexdigest(), b"extra")
        )
    elif attack == "tamper":
        entries[0] = (entries[0][0], b"tampered")
    archive = tar_bytes(entries)
    if attack == "trailer":
        archive += tar_bytes(entries)
    elif attack == "truncated":
        archive = archive[:1024]
    elif attack in ("pax", "symlink"):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
            item = tarfile.TarInfo("alias")
            if attack == "pax":
                item.pax_headers = {"path": "index.json"}
            else:
                item.type, item.linkname = tarfile.SYMTYPE, "index.json"
            tar.addfile(item)
        archive = output.getvalue()
    with pytest.raises(ValueError):
        POLICY.graph(io.BytesIO(archive), REVISION)


@pytest.mark.parametrize(
    "field,value",
    [
        ("archive_sha256", "0" * 64),
        ("config_digest", "sha256:" + "0" * 64),
        ("image_ref", REPO + "@sha256:" + "0" * 64),
        ("source_revision", "2" * 40),
        ("schema", "v1"),
        ("shadow_only", False),
        ("weight_eligible", True),
        ("shadow_only", 1),
    ],
)
def test_approval_drift_rejected_even_when_approval_hash_matches(
    tmp_path, field, value
):
    output, _, _, approval = prepared(tmp_path)
    approval[field] = value
    raw = POLICY.json_bytes(approval)
    with output.open("rb") as stream, pytest.raises(ValueError):
        POLICY.verify(stream, raw, hashlib.sha256(raw).hexdigest())


def test_independent_approval_hash_required(tmp_path):
    output, raw, _, _ = prepared(tmp_path)
    with output.open("rb") as stream, pytest.raises(ValueError, match="approval SHA"):
        POLICY.verify(stream, raw, "0" * 64)


def test_duplicate_json_keys_rejected():
    with pytest.raises(ValueError, match="duplicate JSON"):
        POLICY.json_object(b'{"image_ref":"first","image_ref":"second"}')


def test_streaming_expansion_is_bounded(monkeypatch):
    monkeypatch.setattr(POLICY, "MAX_EXPANDED", 1024)
    with pytest.raises(ValueError, match="expanded layers"):
        POLICY.graph(io.BytesIO(tar_bytes(fixture_entries().items())), REVISION)


def test_gzip_layer_diff_id_verification():
    files = fixture_entries()
    index = json.loads(files["index.json"])
    old_name = "blobs/sha256/" + index["manifests"][0]["digest"][7:]
    manifest = json.loads(files.pop(old_name))
    layer = manifest["layers"][0]
    compressed = gzip.compress(files.pop("blobs/sha256/" + layer["digest"][7:]))
    compressed_sha = hashlib.sha256(compressed).hexdigest()
    files["blobs/sha256/" + compressed_sha] = compressed
    layer.update(
        digest="sha256:" + compressed_sha,
        size=len(compressed),
        mediaType="application/vnd.oci.image.layer.v1.tar+gzip",
    )
    raw = POLICY.json_bytes(manifest)
    manifest_sha = hashlib.sha256(raw).hexdigest()
    files["blobs/sha256/" + manifest_sha] = raw
    index["manifests"][0].update(digest="sha256:" + manifest_sha, size=len(raw))
    files["index.json"] = POLICY.json_bytes(index)
    POLICY.graph(io.BytesIO(tar_bytes(files.items())), REVISION)


def test_import_checks_before_load_and_attests_only_after_inspect(
    tmp_path, monkeypatch
):
    output, raw, sha, approval = prepared(tmp_path)
    monkeypatch.setattr(POLICY, "HOME_DIR", tmp_path)
    client = tmp_path / "client"
    client.mkdir()
    monkeypatch.setattr(POLICY, "CLIENT", client)
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(POLICY.os, "getegid", lambda: 1001)
    monkeypatch.setattr(POLICY.os, "getgroups", lambda: [1001])
    monkeypatch.setattr(
        POLICY.pwd,
        "getpwnam",
        lambda _: type("User", (), {"pw_uid": 1001, "pw_gid": 1001})(),
    )
    monkeypatch.setattr(POLICY, "private", lambda *_a: None)
    original_stat = os.fstat

    def lock_stat(fd):
        original = original_stat(fd)
        # Test process need not own a real native-host account.
        return type(
            "Stat", (), {"st_mode": original.st_mode, "st_uid": 1001, "st_nlink": 1}
        )()

    monkeypatch.setattr(POLICY.os, "fstat", lock_stat)

    @contextlib.contextmanager
    def protected(path):
        with path.open("rb") as stream:
            yield stream

    monkeypatch.setattr(POLICY, "protected_file", protected)
    calls = []
    fail_inspect = False

    def docker(args, **kwargs):
        calls.append(args)
        if args[0] == "info":
            return POLICY.json_bytes(daemon())
        if args[:2] == ["image", "load"]:
            assert kwargs["stream"].tell() == 0
            assert kwargs["stream"].read() == output.read_bytes()
            assert kwargs["timeout"] == 900
            return b"loaded"
        assert args == ["image", "inspect", approval["image_ref"]]
        return POLICY.json_bytes(
            [
                {
                    "RepoDigests": [] if fail_inspect else [approval["image_ref"]],
                    "Id": approval["config_digest"],
                    "Descriptor": {"digest": approval["image_ref"].split("@")[1]},
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": configuration(),
                }
            ]
        )

    monkeypatch.setattr(POLICY, "docker", docker)
    manifest = tmp_path / "approval.json"
    assert manifest.read_bytes() == raw
    result = POLICY.import_image(output, manifest, sha)
    assert [c[0] for c in calls] == ["info", "image", "image", "info"]
    assert result["qualification_required"] is True
    assert result["private_execution_ready"] is False
    calls.clear()
    with pytest.raises(ValueError, match="approval SHA"):
        POLICY.import_image(output, manifest, "0" * 64)
    assert not calls
    fail_inspect = True
    with pytest.raises(ValueError, match="repository digest"):
        POLICY.import_image(output, manifest, sha)
    assert len(calls) == 3  # no retry or success receipt after failed inspection


def test_docker_uses_fixed_host_clean_environment_and_bounded_output(monkeypatch):
    original = subprocess.Popen
    captured = {}

    def process(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return original(["/usr/bin/python3", "-c", "print('ok')"], **kwargs)

    monkeypatch.setenv("SYNTHETIC_TEST_SECRET", "not-inherited")
    monkeypatch.setattr(POLICY.subprocess, "Popen", process)
    assert POLICY.docker(["info"]) == b"ok\n"
    assert captured["args"] == [
        "/usr/bin/docker",
        "--host",
        str("unix://" + str(POLICY.SOCKET)),
        "--config",
        str(POLICY.CLIENT),
        "info",
    ]
    assert set(captured["kwargs"]["env"]) == {"PATH", "LANG", "LC_ALL"}
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    monkeypatch.setattr(POLICY, "MAX_JSON", 1)
    with pytest.raises(ValueError, match="output exceeds"):
        POLICY.docker(["info"])


def test_docker_timeout_kills_process(monkeypatch):
    original = subprocess.Popen
    processes = []

    def process(_args, **kwargs):
        result = original(
            ["/usr/bin/python3", "-c", "import time; time.sleep(30)"], **kwargs
        )
        processes.append(result)
        return result

    monkeypatch.setattr(POLICY.subprocess, "Popen", process)
    with pytest.raises(ValueError, match="timed out"):
        POLICY.docker(["info"], timeout=0.05)
    assert processes[0].poll() is not None


def test_import_role_is_default_off_and_preserves_retained_state():
    role = SCRIPT.parents[1]
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    assert defaults["coding_hosted_image_import_enabled"] is False
    assert all(
        value == ""
        for name, value in defaults.items()
        if name != "coding_hosted_image_import_enabled"
    )
    source = (role / "tasks/main.yml").read_text()
    tasks = yaml.safe_load(source)
    assert tasks[1]["when"] == "coding_hosted_image_import_enabled | bool"
    assert source.index("Refuse retained staging") < source.index("Create root-owned")
    assert source.index("Verify and import") < source.index("Retain a private receipt")
    for task in tasks[1]["block"]:
        if "ansible.builtin.copy" in task:
            assert task["ansible.builtin.copy"]["force"] is False
    for forbidden in (
        "coding_executor",
        "docker pull",
        "docker run",
        "state: restarted",
        "state: absent",
    ):
        assert forbidden not in source
    policy = json.loads(
        (
            ROOT / "infra/ansible/roles/coding_hosted/files/daemon-policy.json"
        ).read_text()
    )
    assert policy["features"] == {"containerd-snapshotter": True}
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-image.yml" in workflow


def daemon():
    return {
        "OSType": "linux",
        "Architecture": "x86_64",
        "SecurityOptions": ["name=rootless"],
        "Labels": ["io.heyditto.dittobench.isolated=true"],
        "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
        "DockerRootDir": str(POLICY.HOME_DIR / "docker"),
        "CgroupDriver": "systemd",
        "CgroupVersion": "2",
        "Containers": 0,
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
    }


def test_daemon_accepts_only_native_metadata():
    POLICY.validate_daemon(daemon())


@pytest.mark.parametrize(
    "field,value",
    [
        ("SecurityOptions", []),
        ("SecurityOptions", ["name=not-rootless"]),
        ("Labels", []),
        ("DriverStatus", []),
        ("DockerRootDir", "/var/lib/docker"),
        ("Containers", 1),
        ("Containers", False),
        ("PidsLimit", False),
        ("CgroupDriver", "cgroupfs"),
        ("CgroupVersion", "1"),
    ],
)
def test_daemon_rejects_unqualified_or_busy_host(field, value):
    info = daemon()
    info[field] = value
    with pytest.raises(ValueError):
        POLICY.validate_daemon(info)


def test_import_refuses_other_identity_before_docker(monkeypatch):
    monkeypatch.setattr(POLICY.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        POLICY.pwd,
        "getpwnam",
        lambda _: type("User", (), {"pw_uid": 1001, "pw_gid": 1001})(),
    )
    monkeypatch.setattr(
        POLICY, "docker", lambda *_a, **_k: pytest.fail("Docker reached")
    )
    with pytest.raises(ValueError, match="dedicated"):
        POLICY.import_image(Path("/no"), Path("/no"), "0" * 64)


def test_loaded_digest_is_not_interchangeable_with_config_id(tmp_path):
    _, _, _, approval = prepared(tmp_path)
    info = [
        {
            "RepoDigests": [approval["image_ref"]],
            "Id": approval["config_digest"],
            "Descriptor": {"digest": approval["image_ref"].split("@")[1]},
            "Os": "linux",
            "Architecture": "amd64",
            "Config": configuration(),
        }
    ]
    POLICY.validate_loaded(info, approval)
    bad = copy.deepcopy(info)
    bad[0]["RepoDigests"] = [REPO + "@" + approval["config_digest"]]
    with pytest.raises(ValueError, match="repository digest"):
        POLICY.validate_loaded(bad, approval)


@pytest.mark.skipif(
    os.environ.get("CODING_OCI_IMPORT_TEST") != "1",
    reason="explicit local Docker metadata import opt-in",
)
def test_real_containerd_offline_repo_digest(tmp_path):
    # This is a storage-format test, NOT native/rootless host qualification.
    # It never executes a container. Keep its unique public fixture for review.
    repository = "coding-hosted-fixture.invalid/metadata-" + uuid.uuid4().hex + "/image"
    output, _, _, approval = prepared(tmp_path, repository)
    subprocess.run(
        ["docker", "image", "load", "--input", str(output)],
        check=True,
        timeout=60,
        capture_output=True,
    )
    result = subprocess.run(
        ["docker", "image", "inspect", approval["image_ref"]],
        check=True,
        timeout=30,
        capture_output=True,
    )
    POLICY.validate_loaded(json.loads(result.stdout), approval)
