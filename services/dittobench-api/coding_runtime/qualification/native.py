"""Consume an independently approved native control invocation; never mint one.

This grants one compatibility run, not competition admission or qualification.
Host enforcement evidence is independently reviewed, not inferred from metadata.
"""

import hashlib
import importlib.util
import json
import os
import platform
import pwd
import re
import stat
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HOME_DIR = Path("/var/lib/ditto-coding-hosted")
STATE = HOME_DIR / "qualification"
SOCKET = Path("/run/ditto-coding-hosted/docker.sock")
PROFILES = {
    "python": "python-call-ast-v2",
    "node": "node-call-ast-v2",
    "go": "go-call-ast-v1",
    "rust": "rust-call-ast-v1",
}
EVIDENCE = {
    "host_preflight",
    "network_enforcement",
    "resource_enforcement",
    "preexec_confinement",
    "cleanup_recovery",
    "private_input_custody",
}


def require(condition):
    if not condition:
        raise ValueError("native control approval rejected")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value))
    require(value != "0" * 64)


def protected_parents(path):
    require(path.is_absolute() and path.resolve() == path)
    for parent in path.parents:
        info = parent.stat()
        require(info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022)


def private(path, *, directory=False):
    protected_parents(path)
    info = path.lstat()
    require(info.st_uid == os.geteuid())
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600))
    if not directory:
        require(info.st_nlink == 1)


def validate_paths(corpus, helper, output_parent, plan, images):
    for path in (corpus, output_parent):
        require(not path.is_relative_to(ROOT))
        private(path, directory=True)
    for path in (plan, images):
        require(not path.is_relative_to(ROOT))
        private(path)
    protected_parents(helper)
    info = helper.lstat()
    require(info.st_uid in (0, os.geteuid()) and stat.S_ISREG(info.st_mode))
    require(info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o555)
    with helper.open("rb") as stream:
        header = stream.read(20)
    require(header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00")


def read_approval(path, expected_sha):
    digest(expected_sha)
    private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
        require(0 < before.st_size <= 65536)
        raw = stream.read(65537)
        after = os.fstat(fd)
        require(
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        )
    require(sha(raw) == expected_sha)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    require(type(value) is dict)
    return value


def policy(value, *, source, plan_sha, helper_sha, controls, jobs, now):
    require(type(controls) is int and type(jobs) is int and jobs in (1, 2, 4))
    require(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "purpose",
            "source_revision",
            "release_manifest_sha256",
            "plan_sha256",
            "helper_sha256",
            "runner_sha256",
            "binding_sha256",
            "machine_id_sha256",
            "boot_id",
            "issued_at_unix",
            "expires_at_unix",
            "controls",
            "max_jobs",
            "images",
            "evidence_sha256",
            "shadow_only",
            "weight_eligible",
        }
    )
    require(value["schema"] == "dittobench-coding-native-controls-approval-v2")
    require(value["purpose"] == "private-compatibility-once")
    require(value["shadow_only"] is True and value["weight_eligible"] is False)
    require(
        type(source) is str
        and re.fullmatch(r"[0-9a-f]{40}", source)
        and source != "0" * 40
    )
    require(value["source_revision"] == source)
    for name in (
        "release_manifest_sha256",
        "plan_sha256",
        "helper_sha256",
        "runner_sha256",
        "binding_sha256",
        "machine_id_sha256",
    ):
        digest(value[name])
    require(value["plan_sha256"] == plan_sha and value["helper_sha256"] == helper_sha)
    require(
        type(value["controls"]) is int
        and value["controls"] == controls
        and 1 <= controls <= 1024
    )
    require(
        type(value["max_jobs"]) is int
        and value["max_jobs"] in (1, 2, 4)
        and jobs <= value["max_jobs"]
    )
    issued, expires = value["issued_at_unix"], value["expires_at_unix"]
    require(type(issued) is int and type(expires) is int and issued <= now < expires)
    require(0 < expires - issued <= 86400)
    require(
        type(value["boot_id"]) is str
        and re.fullmatch(
            r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", value["boot_id"]
        )
    )
    require(
        type(value["evidence_sha256"]) is dict
        and set(value["evidence_sha256"]) == EVIDENCE
    )
    for checksum in value["evidence_sha256"].values():
        digest(checksum)
    require(type(value["images"]) is dict and set(value["images"]) == set(PROFILES))
    for language, expected in value["images"].items():
        require(
            type(expected) is dict
            and set(expected)
            == {"image_ref", "config_digest", "approval_sha256", "driver_profile"}
        )
        prefix = f"coding-runtime.invalid/{language}/runtime@sha256:"
        require(
            type(expected["image_ref"]) is str
            and expected["image_ref"].startswith(prefix)
        )
        digest(expected["image_ref"][len(prefix) :])
        require(
            type(expected["config_digest"]) is str
            and expected["config_digest"].startswith("sha256:")
        )
        digest(expected["config_digest"][7:])
        digest(expected["approval_sha256"])
        require(expected["driver_profile"] == PROFILES[language])
    return value


def release_policy(release, approval):
    require(release.get("schema") == "dittobench-coding-native-release-set-v2")
    require(release.get("source_revision") == approval["source_revision"])
    require(
        release.get("shadow_only") is True and release.get("weight_eligible") is False
    )
    require(release.get("independent_approval_required") is True)
    for field in ("native_imported", "runtime_qualification", "canary_completed"):
        require(release.get(field) is False)
    require(
        type(release.get("images")) is dict and set(release["images"]) == set(PROFILES)
    )
    for language, expected in approval["images"].items():
        require(type(release["images"][language]) is dict)
        require(
            all(
                release["images"][language].get(key) == value
                for key, value in expected.items()
            )
        )


class Binding:
    def __init__(self, path, expected_sha, release_path, **expected):
        started_mono, started_unix = time.monotonic(), time.time()
        self.value = policy(
            read_approval(path, expected_sha), now=started_unix, **expected
        )
        release_policy(
            read_approval(release_path, self.value["release_manifest_sha256"]),
            self.value,
        )
        self.approval_sha = expected_sha
        self.deadline = started_mono + self.value["expires_at_unix"] - started_unix
        user = pwd.getpwnam("ditto-coding-hosted")
        require(os.geteuid() == os.getuid() == user.pw_uid >= 1000)
        require(os.getegid() == os.getgid() == user.pw_gid >= 1000)
        require(set(os.getgroups()) <= {user.pw_gid})
        require(
            platform.node() == "ditto-coding-hosted-v2"
            and platform.system() == "Linux"
            and platform.machine() == "x86_64"
        )
        for path in (HOME_DIR, SOCKET.parent, HOME_DIR / "empty-client", STATE):
            private(path, directory=True)
        info = SOCKET.lstat()
        require(SOCKET.resolve() == SOCKET and stat.S_ISSOCK(info.st_mode))
        require(info.st_uid == user.pw_uid and stat.S_IMODE(info.st_mode) == 0o600)
        require(not list((HOME_DIR / "empty-client").iterdir()))
        # Native diagnostics never use an ambient Docker context or credential file.
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "DOCKER_HOST": f"unix://{SOCKET}",
            "DOCKER_CONFIG": str(HOME_DIR / "empty-client"),
        }
        tool = ROOT / "infra/ansible/roles/coding_hosted_image/files/image-bundle.py"
        for path in (
            Path("/usr/bin/docker"),
            tool,
            Path(__file__),
            Path(__file__).with_name("run.py"),
        ):
            require(path.resolve() == path and path.is_file())
            for candidate in (path, *path.parents):
                info = candidate.stat()
                require(info.st_uid in (0, user.pw_uid) and not info.st_mode & 0o022)
        require(sha(Path(__file__).read_bytes()) == self.value["binding_sha256"])
        require(
            sha(Path(__file__).with_name("run.py").read_bytes())
            == self.value["runner_sha256"]
        )
        spec = importlib.util.spec_from_file_location("native_image_policy", tool)
        assert spec is not None and spec.loader is not None
        self.image_policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.image_policy)
        self.daemon = None
        self.consumed = False
        self.check_current()

    def command(self, arguments):
        return ["/usr/bin/docker", *arguments]

    def check_current(self):
        require(
            time.time() < self.value["expires_at_unix"]
            and time.monotonic() < self.deadline
        )
        machine = Path("/etc/machine-id").read_bytes().strip()
        require(re.fullmatch(rb"[0-9a-f]{32}", machine))
        require(sha(machine) == self.value["machine_id_sha256"])
        require(
            Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            == self.value["boot_id"]
        )

    def check_daemon(self, info):
        self.check_current()
        require(type(info) is dict)
        self.image_policy.validate_daemon(info)
        require(type(info.get("ID")) is str and bool(info["ID"]))
        require(self.daemon is None or self.daemon == info["ID"])
        self.daemon = info["ID"]

    def select_image(self, language, reference, inspected):
        self.check_current()
        require(type(inspected) is dict)
        expected = self.value["images"][language]
        require(reference == expected["image_ref"])
        self.image_policy.validate_loaded(
            [inspected],
            {
                **expected,
                "source_revision": self.value["source_revision"],
            },
        )
        return expected["image_ref"], expected["image_ref"].split("@")[1]

    def consume(self):
        self.check_current()
        require(self.daemon is not None and not self.consumed)
        private(STATE, directory=True)
        # Fixed per-host root, not caller-selected output: another output directory
        # cannot retry this approval. Partial markers also permanently refuse reuse.
        fd = os.open(
            STATE / (self.approval_sha + ".consumed"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write((self.approval_sha + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.consumed = True

    def control_timeout(self):
        require(self.consumed)
        self.check_current()
        remaining = min(
            self.deadline - time.monotonic(),
            self.value["expires_at_unix"] - time.time(),
        )
        require(remaining > 30)
        return min(210, remaining - 20)

    def provenance(self):
        return {
            "approval_sha256": self.approval_sha,
            "release_manifest_sha256": self.value["release_manifest_sha256"],
            "machine_id_sha256": self.value["machine_id_sha256"],
            "boot_id": self.value["boot_id"],
            "evidence_sha256": self.value["evidence_sha256"],
            "daemon_identity_sha256": sha(self.daemon.encode()),
        }
