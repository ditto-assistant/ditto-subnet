#!/usr/bin/env python3
"""Offline native enforcement evidence format, store and verifier.

This tool encodes, retains and verifies native enforcement evidence records and
checks a curator-signed approval against their review. It runs no probes,
contacts no host, daemon or service, opens no custody paths and never mints
approval. It checks Peyton's detached curator signature offline; the host's
``native.py`` does not, and consumes an approval by the digest its operator
supplies (see the host-side follow-up in the evidence doc).
"""

from __future__ import annotations

import argparse
import base64
import builtins
import contextlib
import errno
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

RECORD_SCHEMA = "dittobench-coding-native-enforcement-evidence-v1"
CATALOG_SCHEMA = "dittobench-coding-native-enforcement-catalog-v1"
REVIEW_SCHEMA = "dittobench-coding-native-evidence-review-v1"
VERIFICATION_SCHEMA = "dittobench-coding-native-evidence-verification-v1"
CUSTODY_SCHEMA = "dittobench-coding-native-custody-binding-v1"
CHECK_SCHEMA = "dittobench-coding-native-evidence-approval-check-v1"
PREFLIGHT_SCHEMA = "dittobench-coding-native-host-preflight-v2"
APPROVAL_SCHEMA = "dittobench-coding-native-controls-approval-v2"
EXECUTION_PROFILE_SCHEMA = "dittobench-coding-hosted-authoring-profile-v2"
GRADING_PROFILE_SCHEMA = "dittobench-coding-hosted-grading-profile-v2"
CONNECTIVITY_SCHEMAS = (
    "dittobench-coding-hosted-connectivity-v2",
    "dittobench-coding-hosted-connectivity-v3",
)

# Collection order is fixed and records never overlap: each claims an idle host.
KINDS = (
    "network_enforcement",
    "resource_enforcement",
    "preexec_confinement",
    "cleanup_recovery",
)
EVIDENCE = ("host_preflight", *KINDS, "private_input_custody")
LANGUAGES = ("go", "node", "python", "rust")
ROUTER_NAMESPACES = ("host", "rootless-netns")
COVERAGE = "same_boot"
NOT_COVERED = ("daemon_restart_recovery", "reboot_recovery")
PROFILE_INPUTS = (
    "connectivity_profile_sha256",
    "execution_profile_sha256",
    "grading_profile_sha256",
)
# Peyton, 2026-09-15: every collection record falls within six hours of the
# approval's issued_at, on one machine and one boot.
FRESHNESS_SECONDS = 21600
# A record's pre-collection preflight is at most this old when collection starts.
PRE_COLLECTION_PREFLIGHT_MAX_AGE_SECONDS = 900
# Versioned tolerances. A change is a new version in both the catalog and here.
TOLERANCES = {
    "version": "dittobench-coding-native-enforcement-tolerances-v1",
    "cpu_usage_max_permille_of_quota": 1150,
    "memory_peak_max_permille_of_limit": 1000,
    "pids_max_permille_of_limit": 1000,
    "nofile_max_permille_of_limit": 1000,
    "scratch_max_permille_of_limit": 1000,
    "log_max_permille_of_limit": 1000,
    "timeout_elapsed_max_permille_of_deadline": 1100,
    # Lower bounds: the limit event must happen at or near the limit, so an
    # idle or crashed burner never counts as enforcement.
    "cpu_usage_min_permille_of_quota": 750,
    "memory_peak_min_permille_of_limit": 900,
    "pids_min_permille_of_limit": 1000,
    "nofile_min_permille_of_limit": 990,
    "scratch_min_permille_of_limit": 950,
    "log_min_permille_of_limit": 500,
}
# Where each resource container's limits come from. Runtime constants are the
# sandbox (--ulimit nofile=1024, --log-opt max-size=8m) and executor (24 KiB
# model-visible output, Rust /out carve-out) values in services/dittobench-api.
RESOURCE_CONTAINERS = {
    "harness": {
        "profile": "execution_profile_sha256",
        "scratch": "full",
        "nofile_limit": 1024,
        "log_limit_bytes": 8388608,
    },
    "executor_authoring": {
        "profile": "execution_profile_sha256",
        "scratch": "executor",
        "nofile_limit": 1024,
        "log_limit_bytes": 24576,
    },
    "executor_grading": {
        "profile": "grading_profile_sha256",
        "scratch": "executor",
        "nofile_limit": 1024,
        "log_limit_bytes": 24576,
    },
}
BIND_SOURCES = (
    "memory_limit_bytes",
    "cpu_quota_millis",
    "pids_limit",
    "scratch_limit_bytes",
    "nofile_limit",
    "log_limit_bytes",
    "hidden_command_timeout_ms",
    "visible_command_timeout_ms",
)
# Every hosted grading test group's approved command timeout is evidenced.
COMMAND_TIMEOUT_SOURCES = {
    "hidden_command_timeout_ms": "hidden",
    "visible_command_timeout_ms": "visible",
}
BIND_FIELDS = {
    "profile_equal": "profile",
    "bounded": "limit",
    "supervisor_timeout": "deadline_ms",
    "zero_retained": "limit",
}
# Peyton, 2026-09-15: hosted grading intentionally retains zero bytes of
# candidate output, so its log probe is an exact zero-byte assertion, not a
# per-mille floor. The candidate must have emitted at least the bound limit,
# so an idle or crashed writer, which also retains nothing, never counts.
ZERO_RETAINED_PROBE = "executor_grading.log_bound"
# Rootless Docker maps container uid 0 to the daemon user and uid c >= 1 to
# subordinate id start + c - 1.
SUBORDINATE_MIN_START = 100000
SUBORDINATE_MIN_COUNT = 65536
ENDPOINT_DOMAIN = b"dittobench-coding-native-endpoint-v1"
# The endpoint set is the connectivity profile without its per-issue fields
# (schema, issued/expiry times and the fixed shadow flags), so a probe profile
# and the canary's own later profile reproduce the same digest.
ENDPOINT_SET_SCHEMA = "dittobench-coding-native-endpoint-set-v1"
ENDPOINT_SET_DIGEST_SCHEMA = "dittobench-coding-native-endpoint-set-digest-v1"
HOSTED_TEST_GROUPS = ("hidden", "visible")
# Network phases observed while the connectivity profile is unexpired, and the
# phase that must reach its expiry.
NETWORK_PRE_EXPIRY_PHASES = ("active", "stop_rollback")
NETWORK_EXPIRY_PHASE = "expiry"
PIN_NAMES = (
    "connectivity_endpoint_set_sha256",
    "execution_profile_sha256",
    "grading_profile_sha256",
)

CATALOG_FILE = (
    "services/dittobench-api/internal/codingenforcement/catalog/catalog-v1.json"
)
FIXTURE_ROOT = "services/dittobench-api/internal/codingenforcement/fixtures"
TOOL_FILES = {
    "catalog_sha256": CATALOG_FILE,
    "collector_sha256": "infra/scripts/collect-coding-native-enforcement.py",
    "evidence_tool_sha256": "infra/scripts/coding-native-evidence.py",
}
# The preflight's own tool list (infra/scripts/inspect-coding-native-host.py).
PREFLIGHT_TOOL_FILES = (
    "infra/scripts/inspect-coding-native-host.py",
    "infra/scripts/build-coding-native-release.py",
    "infra/ansible/roles/coding_hosted_image/files/image-bundle.py",
    "infra/ansible/roles/coding_hosted_runtime/files/runtime-bundle.py",
    "infra/ansible/roles/coding_hosted/files/host-policy.py",
)
PREFLIGHT_PENDING = [
    "live_network_and_expiry_enforcement",
    "live_resource_limit_enforcement",
    "candidate_preexec_confinement",
    "candidate_cleanup_and_interruption",
]
NATIVE_BINDING_FILE = "services/dittobench-api/coding_runtime/qualification/native.py"
NATIVE_RUNNER_FILE = "services/dittobench-api/coding_runtime/qualification/run.py"
DEFAULT_OPENSSL = Path("/usr/bin/openssl")

MAX_OBJECT = 1 << 20
MAX_TOOL = 8 << 20
MAX_PROFILE = 64 << 10
MAX_CONNECTIVITY = 16384
MAX_APPROVAL = 65536
MAX_PUBLIC_KEY = 4096
SIGNATURE_BYTES = 64
MAX_DEPTH = 16
MAX_INTEGER_TEXT = 20
INT64_MAX = (1 << 63) - 1

SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
BOOT_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
KERNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,127}")
PROBE_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}")
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
# Observed strings are short lowercase names such as interface names. Digits
# cannot lead, so no address or host:port can be recorded.
OBSERVED_STRING = re.compile(r"[a-z][a-z0-9_-]{0,31}")
RELATIVE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
SURROGATE = re.compile("[\\ud800-\\udfff]")

PRECONDITIONS = {
    "custody_active": False,
    "custody_socket_present": False,
    "daemon_containers": 0,
    "daemon_job_networks": 0,
    "worker_active": False,
}
RESIDUE = {
    "containers": 0,
    "custody_socket_present": False,
    "job_networks": 0,
    "processes": 0,
    "volumes": 0,
    "worker_active": False,
}
RECORD_KEYS = {
    "schema",
    "kind",
    "coverage",
    "not_covered",
    "tolerances_version",
    "host",
    "release",
    "pre_collection_preflight_sha256",
    "inputs",
    "endpoints",
    "tools",
    "preconditions",
    "phases",
    "residue",
    "started_at_unix",
    "completed_at_unix",
}
HOST_KEYS = {
    "machine_id_sha256",
    "boot_id",
    "kernel_release",
    "daemon_identity_sha256",
    "subordinate_ids",
    "router_namespace",
}
SUBORDINATE_KEYS = {"uid_start", "uid_count", "gid_start", "gid_count"}
RELEASE_KEYS = {
    "source_revision",
    "release_manifest_sha256",
    "runtime_archive_sha256",
    "image_approval_sha256",
}
TOOL_KEYS = {*TOOL_FILES, "fixtures_sha256"}
PHASE_KEYS = {"name", "started_at_unix", "completed_at_unix", "probes"}
PROBE_KEYS = {"id", "language", "endpoint_sha256", "expect", "observed", "matched"}
ENDPOINT_KEYS = {"role", "endpoint_sha256"}
PREFLIGHT_KEYS = {
    "schema",
    "source_revision",
    "release_manifest_sha256",
    "runtime_archive_sha256",
    "image_approval_sha256",
    "config_sha256",
    "host",
    "daemon_identity_sha256",
    "tool_sha256",
    "nft_snapshot_sha256",
    "checked_at_unix",
    "host_preflight_passed",
    "pending_host_qualification",
    "runtime_qualification",
    "private_execution_ready",
    "shadow_only",
    "weight_eligible",
}
CUSTODY_KEYS = {
    "schema",
    "custody_evidence_sha256",
    "machine_id_sha256",
    "boot_id",
    "bound_at_unix",
}
REVIEW_KEYS = {
    "schema",
    "approval_generated",
    "catalog_sha256",
    "tolerances_version",
    "coverage",
    "not_covered",
    "verification",
    "consistency",
    "verified",
}
REVIEW_PASS_KEYS = {
    *REVIEW_KEYS,
    "evidence_sha256",
    "host",
    "release",
    "inputs",
    "endpoints",
    "endpoint_counts",
    "endpoint_set_sha256",
    "window",
}
WINDOW_KEYS = {
    "earliest_started_at_unix",
    "latest_completed_at_unix",
    "host_preflight_checked_at_unix",
    "custody_bound_at_unix",
}
CATALOG_KEYS = {
    "schema",
    "record_schema",
    "review_schema",
    "languages",
    "router_namespaces",
    "coverage",
    "not_covered",
    "freshness_max_seconds",
    "pre_collection_preflight_max_age_seconds",
    "resource_containers",
    "tolerances",
    "outcomes",
    "kinds",
}
EXPECT_KEYS = {
    "outcome_in": {"type", "accept"},
    "exact": {"type", "value"},
    "profile_equal": {"type"},
    "bounded": {"type", "tolerance", "floor"},
    "supervisor_timeout": {"type", "tolerance"},
    "control": {"type", "result"},
    "subordinate_ids": {"type", "uid", "gid"},
    "zero_retained": {"type"},
}
SCOPES = ("host", "language", "trusted_endpoint", "router_endpoint", "proxy_endpoint")
SCOPE_ROLES = {
    "trusted_endpoint": "trusted",
    "router_endpoint": "router",
    "proxy_endpoint": "refusing_proxy",
}
EXECUTION_PROFILE_KEYS = {"schema", "image_digest", "resource_policy", "budgets"}
GRADING_PROFILE_KEYS = {
    "schema",
    "image_digest",
    "grader_contract_sha256",
    "grader_bundle_sha256",
    "resource_policy",
    "build",
    "test_groups",
    "execution_timeout",
}
RESOURCE_POLICY_KEYS = {
    "CandidateLimits",
    "ProtectedLimits",
    "MaxCombinedDiskBytes",
    "MemoryLimitBytes",
    "ScratchLimitBytes",
    "PidsLimit",
    "CPUQuotaMillis",
}
LIMIT_KEYS = {
    "MaxBundleBytes",
    "MaxWorkspaceBytes",
    "MaxFileBytes",
    "MaxPatchBytes",
    "MaxEntries",
    "MaxToolCalls",
    "MaxReadBytes",
    "MaxResponseBytes",
    "MaxSearchResults",
    "MaxReplayCacheBytes",
    "MaxTranscriptBytes",
}
BUDGET_KEYS = {
    "model_input_tokens",
    "model_output_tokens",
    "workspace_tool_calls",
    "wall_time_seconds",
}
CONNECTIVITY_KEYS = {
    "schema",
    "shadow_only",
    "weight_eligible",
    "issued_at_unix",
    "expires_at_unix",
    "trusted_tcp",
    "trusted_dns",
    "trusted_loopback_tcp",
    "candidate_tcp",
}
SPKI_ED25519_PREFIX = bytes.fromhex("302a300506032b6570032100")
PEM_BEGIN = b"-----BEGIN PUBLIC KEY-----\n"
PEM_END = b"\n-----END PUBLIC KEY-----\n"
RENAME_NOREPLACE = 1


class Refusal(ValueError):
    """A named, source-free reason a record, review or approval is refused."""


# Shape errors a hostile record could still trigger are refusals, never crashes.
MALFORMED = (
    TypeError,
    KeyError,
    AttributeError,
    IndexError,
    RecursionError,
    ValueError,
)


def reason(error: Exception) -> str:
    return str(error) if isinstance(error, Refusal) else "evidence is malformed"


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def is_digest(value: object) -> bool:
    return (
        type(value) is str and SHA256.fullmatch(value) is not None and value != "0" * 64
    )


def is_int(value: object, minimum: int = 0, maximum: int = INT64_MAX) -> bool:
    return type(value) is int and minimum <= value <= maximum


def same(left: object, right: object) -> bool:
    """JSON equality with exact types: False is not 0 and 1 is not True."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        assert isinstance(right, dict)
        return left.keys() == right.keys() and all(
            same(item, right[key]) for key, item in left.items()
        )
    if isinstance(left, list):
        assert isinstance(right, list)
        return len(left) == len(right) and all(
            same(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def closed(value: object, keys: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict, f"{label} must be an object")
    assert isinstance(value, dict)
    require(set(value) == keys, f"{label} keys are not the closed set")
    return value


# ---------------------------------------------------------------------------
# Canonical JSON


def _reject_float(_text: str) -> None:
    raise Refusal("JSON numbers must be integers")


def _reject_constant(_text: str) -> None:
    raise Refusal("JSON numbers must be finite integers")


def _bounded_int(text: str) -> int:
    # int() on thousands of digits is slow and raises past Python's limit.
    require(len(text) <= MAX_INTEGER_TEXT, "integer is outside int64")
    return int(text)


def parse_json(raw: bytes, label: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, f"{label} has a duplicate key")
            result[key] = value
        return result

    require(type(raw) is bytes, f"{label} must be bytes")
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=unique,
            parse_float=_reject_float,
            parse_int=_bounded_int,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise Refusal(f"{label} is not valid JSON") from None


def _canonical_value(value: object, depth: int) -> None:
    require(depth <= MAX_DEPTH, "canonical JSON is nested too deeply")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        require(-(1 << 63) <= value <= INT64_MAX, "integer is outside int64")
        return
    if type(value) is str:
        require(SURROGATE.search(value) is None, "string has a lone surrogate")
        return
    if type(value) is list:
        for item in value:
            _canonical_value(item, depth + 1)
        return
    require(type(value) is dict, "canonical JSON holds only JSON values")
    assert isinstance(value, dict)
    for key, item in value.items():
        require(type(key) is str, "object keys must be strings")
        _canonical_value(key, depth + 1)
        _canonical_value(item, depth + 1)


def canonical_bytes(value: object) -> bytes:
    """Native-qualification form: sorted keys, compact, ASCII-escaped, no newline.

    This is the ``json.dumps(value, sort_keys=True, separators=(",", ":"))`` form
    that ``run.py``, ``prepare.py``, the image/release tools and the connectivity
    rollout digest already hash. ``ensure_ascii`` escapes every non-ASCII
    character, including U+2028 and U+2029, as a lowercase escape. Floats, NaN
    and non-int64 integers are refused so the Go encoder produces identical bytes.
    """

    _canonical_value(value, 0)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def canonical_sha256(value: object) -> str:
    return sha256(canonical_bytes(value))


def parse_canonical(raw: bytes, label: str) -> Any:
    value = parse_json(raw, label)
    require(canonical_bytes(value) == raw, f"{label} is not canonical JSON")
    return value


# ---------------------------------------------------------------------------
# Paths, store and reviewed checkout


def _protected_ancestors(path: Path, label: str) -> None:
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode), f"{label} ancestor is not a directory")
        require(info.st_uid in (0, os.geteuid()), f"{label} ancestor has another owner")
        require(
            not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX),
            f"{label} ancestor is writable by others",
        )


def _rename_noreplace(directory: int, source: str, target: str) -> bool | None:
    """renameat2(RENAME_NOREPLACE): True renamed, False exists, None unsupported."""

    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.renameat2
    except (OSError, AttributeError):
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(
        directory, os.fsencode(source), directory, os.fsencode(target), RENAME_NOREPLACE
    ):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            return False
        if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            return None
        raise OSError(code, os.strerror(code))
    return True


class Store:
    """Owner-only 0700 directory of 0400 objects named by their SHA-256."""

    def __init__(self, path: Path) -> None:
        require(path.is_absolute(), "store path must be absolute")
        try:
            require(path.resolve() == path, "store path must be canonical")
            _protected_ancestors(path, "store")
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise Refusal("store is not an existing directory") from None
        info = os.fstat(fd)
        if not (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700
        ):
            os.close(fd)
            raise Refusal("store must be mode 0700 and owned by the invoking user")
        self.fd = fd

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def recover(self, digest: str) -> None:
        """Finish an interrupted link-then-unlink retain for this digest.

        A crash between linking the digest name and unlinking the temporary
        leaves two links to one complete fsynced object. Only a partial of this
        digest, owned by us and on the same inode, is removed.
        """

        try:
            info = os.stat(digest, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 2
            and info.st_uid == os.geteuid()
        ):
            return
        for name in os.listdir(self.fd):
            if not name.startswith(f".partial-{digest}-"):
                continue
            partial = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (
                stat.S_ISREG(partial.st_mode)
                and partial.st_uid == os.geteuid()
                and (partial.st_dev, partial.st_ino) == (info.st_dev, info.st_ino)
            ):
                os.unlink(name, dir_fd=self.fd)
                os.fsync(self.fd)
                return

    def put(self, raw: bytes) -> str:
        digest = sha256(raw)
        self.recover(digest)
        temporary = f".partial-{digest}-{secrets.token_hex(8)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o400,
            dir_fd=self.fd,
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o400)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            renamed = _rename_noreplace(self.fd, temporary, digest)
            if renamed is None:
                try:
                    os.link(
                        temporary,
                        digest,
                        src_dir_fd=self.fd,
                        dst_dir_fd=self.fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    renamed = False
                else:
                    os.unlink(temporary, dir_fd=self.fd)
                    renamed = True
            require(renamed, "object is already retained")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        return digest

    def get(self, digest: object, maximum: int = MAX_OBJECT) -> bytes:
        require(is_digest(digest), "object digest is malformed")
        assert isinstance(digest, str)
        self.recover(digest)
        try:
            fd = os.open(
                digest,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.fd,
            )
        except OSError:
            raise Refusal("object is not retained") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o400,
                "retained object is not a private single-link file",
            )
            require(info.st_size <= maximum, "retained object exceeds its bound")
            raw = stream.read(maximum + 1)
        require(sha256(raw) == digest, "retained object does not match its digest")
        return raw


def _not_writable(info: os.stat_result, label: str) -> None:
    require(info.st_uid in (0, os.geteuid()), f"{label} has another owner")
    require(not info.st_mode & 0o022, f"{label} is writable by others")


class Checkout:
    """Reads tool bytes from a reviewed checkout without links or shared writes."""

    def __init__(self, path: Path) -> None:
        require(path.is_absolute(), "checkout path must be absolute")
        try:
            require(path.resolve() == path, "checkout path must be canonical")
            _protected_ancestors(path, "checkout")
            info = path.lstat()
        except OSError:
            raise Refusal("checkout is not an existing directory") from None
        require(stat.S_ISDIR(info.st_mode), "checkout is not a directory")
        _not_writable(info, "checkout")
        self.root = path

    def _path(self, relative: str) -> Path:
        require(
            RELATIVE_PATH.fullmatch(relative) is not None
            and ".." not in relative.split("/"),
            "checkout path is malformed",
        )
        current = self.root
        for part in relative.split("/"):
            current = current / part
            try:
                info = current.lstat()
            except OSError:
                raise Refusal(f"checkout is missing {relative}") from None
            require(not stat.S_ISLNK(info.st_mode), f"checkout {relative} is a link")
            _not_writable(info, f"checkout {relative}")
        return current

    def read(self, relative: str) -> bytes:
        path = self._path(relative)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode), f"checkout {relative} is not a file")
            _not_writable(info, f"checkout {relative}")
            require(info.st_size <= MAX_TOOL, f"checkout {relative} is too large")
            return stream.read(MAX_TOOL + 1)

    def file_sha256(self, relative: str) -> str:
        return sha256(self.read(relative))

    def tree_sha256(self, relative: str) -> str:
        """SHA-256 of the canonical sorted [{path, sha256}] list under a root."""

        root = self._path(relative)
        require(
            stat.S_ISDIR(root.lstat().st_mode), f"checkout {relative} is not a tree"
        )
        entries: list[dict[str, str]] = []

        def walk(directory: Path, prefix: str) -> None:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
            for entry in children:
                name = f"{prefix}{entry.name}"
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    _not_writable(info, f"fixture {name}")
                    walk(Path(entry.path), name + "/")
                else:
                    require(stat.S_ISREG(info.st_mode), f"fixture {name} is not a file")
                    entries.append(
                        {"path": name, "sha256": self.file_sha256(f"{relative}/{name}")}
                    )

        walk(root, "")
        require(bool(entries), "fixture tree is empty")
        entries.sort(key=lambda entry: entry["path"])
        return canonical_sha256(entries)

    def tools(self) -> dict[str, str]:
        result = {name: self.file_sha256(path) for name, path in TOOL_FILES.items()}
        result["fixtures_sha256"] = self.tree_sha256(FIXTURE_ROOT)
        return result


def read_input(path: Path, maximum: int, label: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Refusal(f"{label} is missing") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode), f"{label} is not a regular file")
        require(info.st_size <= maximum, f"{label} is too large")
        return stream.read(maximum + 1)


# ---------------------------------------------------------------------------
# Approved profile documents


def _go_canonical(value: object, raw: bytes, label: str) -> None:
    """Go codingcontract canonical bytes: sorted compact JSON plus a newline.

    Profiles are ASCII without HTML characters, so Go's HTML escaping and
    Platform's coding_canonical_json_bytes agree on these bytes.
    """

    require(
        raw.isascii() and not re.search(rb"[<>&\x00-\x1f\x7f]", raw[:-1]),
        f"{label} has characters outside its canonical form",
    )
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    require((encoded + "\n").encode() == raw, f"{label} is not canonical JSON")


def _resource_policy(value: object, label: str) -> dict[str, Any]:
    policy = closed(value, RESOURCE_POLICY_KEYS, f"{label} resource policy")
    for name in ("CandidateLimits", "ProtectedLimits"):
        limits = closed(policy[name], LIMIT_KEYS, f"{label} limits")
        require(
            all(is_int(item, 1) for item in limits.values()),
            f"{label} limits are malformed",
        )
    require(
        is_int(policy["MaxCombinedDiskBytes"], 1, 8 << 30)
        and is_int(policy["MemoryLimitBytes"], 256 << 20, 64 << 30)
        and is_int(policy["ScratchLimitBytes"], 1, 8 << 30)
        and is_int(policy["PidsLimit"], 1, 4096)
        and is_int(policy["CPUQuotaMillis"], 100, 64000),
        f"{label} resource policy is outside hard bounds",
    )
    return policy


def parse_execution_profile(raw: bytes) -> dict[str, Any]:
    value = closed(
        parse_json(raw, "execution profile"),
        EXECUTION_PROFILE_KEYS,
        "execution profile",
    )
    _go_canonical(value, raw, "execution profile")
    require(
        value["schema"] == EXECUTION_PROFILE_SCHEMA
        and type(value["image_digest"]) is str
        and value["image_digest"].startswith("sha256:")
        and is_digest(value["image_digest"][7:]),
        "execution profile identity is malformed",
    )
    policy = _resource_policy(value["resource_policy"], "execution profile")
    budgets = closed(value["budgets"], BUDGET_KEYS, "execution profile budgets")
    require(
        all(is_int(item, 1) for item in budgets.values())
        and budgets["wall_time_seconds"] <= 3600,
        "execution profile budgets are malformed",
    )
    return {"sha256": sha256(raw), "policy": policy}


def _command_timeout_ms(value: object, label: str) -> int:
    command = closed(value, {"ID", "Argv", "Timeout"}, f"{label} command")
    require(
        type(command["ID"]) is str
        and IDENTIFIER.fullmatch(command["ID"]) is not None
        and type(command["Argv"]) is list
        and 0 < len(command["Argv"]) <= 64
        and all(type(item) is str for item in command["Argv"])
        and is_int(command["Timeout"], 1_000_000, 600_000_000_000)
        and command["Timeout"] % 1_000_000 == 0,
        f"{label} command is malformed",
    )
    return command["Timeout"] // 1_000_000


def parse_grading_profile(raw: bytes) -> dict[str, Any]:
    value = parse_json(raw, "grading profile")
    # Main still carries test_manifest_sha256; the hosted-v2 profile work drops it.
    require(
        type(value) is dict
        and set(value)
        in (GRADING_PROFILE_KEYS, GRADING_PROFILE_KEYS | {"test_manifest_sha256"}),
        "grading profile keys are not the closed set",
    )
    _go_canonical(value, raw, "grading profile")
    require(
        value["schema"] == GRADING_PROFILE_SCHEMA
        and type(value["image_digest"]) is str
        and value["image_digest"].startswith("sha256:")
        and is_digest(value["image_digest"][7:])
        and is_int(value["execution_timeout"], 1_000_000, 3_600_000_000_000)
        and value["execution_timeout"] % 1_000_000 == 0,
        "grading profile identity is malformed",
    )
    policy = _resource_policy(value["resource_policy"], "grading profile")
    build = closed(value["build"], {"Required", "Command"}, "grading profile build")
    require(type(build["Required"]) is bool, "grading profile build is malformed")
    _command_timeout_ms(build["Command"], "grading profile build")
    groups = value["test_groups"]
    require(
        type(groups) is list and len(groups) == len(HOSTED_TEST_GROUPS),
        "grading profile test groups are malformed",
    )
    timeouts = {}
    for group, name in zip(groups, HOSTED_TEST_GROUPS, strict=True):
        group = closed(group, {"Group", "Command", "ExpectedTotal"}, "test group")
        require(
            same(group["Group"], name) and is_int(group["ExpectedTotal"], 1, 1_000_000),
            "grading profile test groups are malformed",
        )
        timeouts[name] = _command_timeout_ms(group["Command"], "grading test group")
    return {"sha256": sha256(raw), "policy": policy, "group_timeouts_ms": timeouts}


def _endpoint_sha256(endpoint_set: str, label: str, address: str, port: int) -> str:
    return sha256(
        b"\x00".join(
            (
                ENDPOINT_DOMAIN,
                endpoint_set.encode(),
                label.encode(),
                f"{address}:{port}".encode(),
            )
        )
    )


def _endpoint_pairs(value: object, label: str, *, rollout: bool) -> list[tuple]:
    """connectivity-policy.py ``pairs``: the deployer's exact endpoint rules."""

    candidate, dns = label == "candidate_tcp", label == "trusted_dns"
    maximum = 8 if candidate and rollout else 2 if candidate or dns else 32
    require(
        type(value) is list and len(value) <= maximum,
        f"connectivity {label} is malformed",
    )
    assert isinstance(value, list)
    pairs: list[tuple[str, int]] = []
    for item in value:
        item = closed(item, {"address", "port"}, f"connectivity {label} entry")
        address, port = item["address"], item["port"]
        require(
            type(address) is str and is_int(port, 1, 65535),
            f"connectivity {label} entry is malformed",
        )
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            raise Refusal(f"connectivity {label} entry is malformed") from None
        require(
            str(ip) == address
            and not (
                ip.is_multicast
                or ip.is_unspecified
                or ip.is_link_local
                or ip.is_reserved
            ),
            f"connectivity {label} entry is malformed",
        )
        if candidate:
            require(
                any(
                    ip in ipaddress.IPv4Network(network)
                    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                )
                and port >= 1024,
                "connectivity candidate is not a private high port",
            )
        if dns:
            require(port == 53, "connectivity trusted_dns entry is malformed")
        require((address, port) not in pairs, f"connectivity {label} repeats")
        pairs.append((address, port))
    return sorted(pairs)


def parse_connectivity_profile(raw: bytes) -> dict[str, Any]:
    """The worker connectivity profile; only derived digests ever leave here.

    Structure follows connectivity-policy.py ``policy`` except its wall-clock
    window, which is checked against the evidence timestamps instead.
    """

    value = closed(
        parse_json(raw, "connectivity profile"),
        CONNECTIVITY_KEYS,
        "connectivity profile",
    )
    issued, expires = value["issued_at_unix"], value["expires_at_unix"]
    require(
        type(value["schema"]) is str
        and value["schema"] in CONNECTIVITY_SCHEMAS
        and value["shadow_only"] is True
        and value["weight_eligible"] is False
        and type(value["trusted_loopback_tcp"]) is bool
        and is_int(issued, 0)
        and is_int(expires, 1, (1 << 32) - 1)
        and 0 < expires - issued <= 86400,
        "connectivity profile identity is malformed",
    )
    rollout = value["schema"].endswith("v3")
    pairs = {
        label: _endpoint_pairs(value[label], label, rollout=rollout)
        for label in ("trusted_tcp", "trusted_dns", "candidate_tcp")
    }
    require(
        bool(pairs["trusted_tcp"]) and bool(pairs["candidate_tcp"]),
        "connectivity profile lists no trusted or candidate endpoint",
    )
    endpoint_set = {
        "schema": ENDPOINT_SET_SCHEMA,
        "trusted_loopback_tcp": value["trusted_loopback_tcp"],
        **{
            label: [{"address": address, "port": port} for address, port in items]
            for label, items in pairs.items()
        },
    }
    endpoint_set_sha256 = canonical_sha256(endpoint_set)

    def hashes(label: str) -> list[str]:
        return sorted(
            _endpoint_sha256(endpoint_set_sha256, label, *pair) for pair in pairs[label]
        )

    return {
        "sha256": canonical_sha256(value),
        "endpoint_set_sha256": endpoint_set_sha256,
        "issued_at_unix": issued,
        "expires_at_unix": expires,
        "endpoints": {
            "trusted": hashes("trusted_tcp"),
            "trusted_dns": hashes("trusted_dns"),
            "candidate": hashes("candidate_tcp"),
        },
    }


def load_profiles(paths: dict[str, Path | None]) -> dict[str, dict[str, Any]]:
    parsers: dict[str, tuple[Callable[[bytes], dict[str, Any]], int]] = {
        "connectivity_profile_sha256": (parse_connectivity_profile, MAX_CONNECTIVITY),
        "execution_profile_sha256": (parse_execution_profile, MAX_PROFILE),
        "grading_profile_sha256": (parse_grading_profile, MAX_PROFILE),
    }
    result = {}
    for name, path in paths.items():
        if path is not None:
            parser, maximum = parsers[name]
            label = name.removesuffix("_sha256").replace("_", " ")
            result[name] = parser(read_input(path, maximum, label))
    return result


# ---------------------------------------------------------------------------
# Probe catalog and expectation semantics


def _validate_expect(expect: object, outcomes: set[str], label: str) -> None:
    require(type(expect) is dict and "type" in expect, f"{label} expect is malformed")
    assert isinstance(expect, dict)
    kind = expect["type"]
    require(type(kind) is str and kind in EXPECT_KEYS, f"{label} expect type unknown")
    closed(expect, EXPECT_KEYS[kind], f"{label} expect")
    if kind == "outcome_in":
        accept = expect["accept"]
        require(
            type(accept) is list
            and bool(accept)
            and all(type(item) is str for item in accept)
            and accept == sorted(set(accept))
            and set(accept) <= outcomes,
            f"{label} accepted outcomes are malformed",
        )
    elif kind == "exact":
        value = expect["value"]
        require(type(value) is dict and bool(value), f"{label} exact value malformed")
        for item in value.values():
            require(
                is_int(item)
                or type(item) is bool
                or (
                    type(item) is list
                    and all(
                        type(entry) is str and OBSERVED_STRING.fullmatch(entry)
                        for entry in item
                    )
                ),
                f"{label} exact value malformed",
            )
    elif kind == "bounded":
        tolerance, floor = expect["tolerance"], expect["floor"]
        require(
            type(tolerance) is str
            and "_max_permille_of_" in tolerance
            and tolerance in TOLERANCES
            and same(floor, tolerance.replace("_max_", "_min_"))
            and floor in TOLERANCES,
            f"{label} tolerance is unknown",
        )
        ceiling, lower = TOLERANCES[tolerance], TOLERANCES[floor]
        assert isinstance(ceiling, int) and isinstance(lower, int)
        require(1 <= lower <= ceiling, f"{label} tolerance floor exceeds its ceiling")
    elif kind == "supervisor_timeout":
        require(
            same(expect["tolerance"], "timeout_elapsed_max_permille_of_deadline"),
            f"{label} tolerance is unknown",
        )
    elif kind == "control":
        require(
            type(expect["result"]) is str
            and expect["result"] in ("all_pass", "some_fail", "timeout"),
            f"{label} result unknown",
        )
    elif kind == "subordinate_ids":
        require(
            is_int(expect["uid"], 1, SUBORDINATE_MIN_COUNT - 1)
            and is_int(expect["gid"], 1, SUBORDINATE_MIN_COUNT - 1),
            f"{label} candidate ids are malformed",
        )


def _validate_bind(probe: dict[str, Any], kind: str) -> None:
    bind = probe["bind"]
    require(type(bind) is dict, f"{probe['id']} bind is malformed")
    zero_retained = probe["expect"]["type"] == "zero_retained"
    require(
        zero_retained == (probe["id"] == ZERO_RETAINED_PROBE)
        and (not zero_retained or kind == "resource_enforcement"),
        f"{probe['id']} zero-byte retention fits only grading output",
    )
    field = BIND_FIELDS.get(probe["expect"]["type"])
    if kind != "resource_enforcement" or field is None:
        require(not bind, f"{probe['id']} must not bind limits")
        return
    container = probe["id"].split(".")[0]
    require(container in RESOURCE_CONTAINERS, f"{probe['id']} container is unknown")
    closed(bind, {field}, f"{probe['id']} bind")
    source = bind[field]
    require(
        type(source) is str and source in BIND_SOURCES,
        f"{probe['id']} bind source is unknown",
    )
    group = COMMAND_TIMEOUT_SOURCES.get(source)
    require(
        (group is not None) == (probe["expect"]["type"] == "supervisor_timeout"),
        f"{probe['id']} bind source does not fit its type",
    )
    require(
        group is None
        or RESOURCE_CONTAINERS[container]["profile"] == "grading_profile_sha256",
        f"{probe['id']} has no approved command timeout",
    )
    require(
        group is None or probe["id"] == f"{container}.supervisor_timeout.{group}",
        f"{probe['id']} names another test group",
    )
    require(
        not zero_retained or source == "log_limit_bytes",
        f"{probe['id']} zero-byte retention must bind the output limit",
    )


def load_catalog(raw: bytes) -> dict[str, Any]:
    catalog = closed(parse_json(raw, "catalog"), CATALOG_KEYS, "catalog")
    require(same(catalog["schema"], CATALOG_SCHEMA), "catalog schema is unknown")
    require(
        same(catalog["record_schema"], RECORD_SCHEMA), "catalog record schema differs"
    )
    require(
        same(catalog["review_schema"], REVIEW_SCHEMA), "catalog review schema differs"
    )
    require(same(catalog["languages"], list(LANGUAGES)), "catalog languages differ")
    require(
        same(catalog["router_namespaces"], list(ROUTER_NAMESPACES)),
        "catalog router namespaces differ",
    )
    require(same(catalog["coverage"], COVERAGE), "catalog coverage differs")
    require(
        same(catalog["not_covered"], list(NOT_COVERED)), "catalog not_covered differs"
    )
    require(
        same(catalog["freshness_max_seconds"], FRESHNESS_SECONDS),
        "catalog freshness differs",
    )
    require(
        same(
            catalog["pre_collection_preflight_max_age_seconds"],
            PRE_COLLECTION_PREFLIGHT_MAX_AGE_SECONDS,
        ),
        "catalog preflight age differs",
    )
    require(
        same(catalog["resource_containers"], RESOURCE_CONTAINERS),
        "catalog resource containers differ",
    )
    require(same(catalog["tolerances"], TOLERANCES), "catalog tolerances differ")
    outcomes = catalog["outcomes"]
    require(
        type(outcomes) is list
        and all(type(item) is str and NAME.fullmatch(item) for item in outcomes)
        and outcomes == sorted(set(outcomes)),
        "catalog outcomes are malformed",
    )
    kinds = catalog["kinds"]
    require(type(kinds) is dict and set(kinds) == set(KINDS), "catalog kinds differ")
    for kind in KINDS:
        entry = closed(
            kinds[kind], {"inputs", "endpoint_roles", "phases", "probes"}, kind
        )
        require(
            type(entry["inputs"]) is list
            and all(
                type(name) is str and name in PROFILE_INPUTS for name in entry["inputs"]
            )
            and entry["inputs"] == sorted(set(entry["inputs"])),
            f"{kind} inputs are malformed",
        )
        roles = entry["endpoint_roles"]
        require(type(roles) is dict, f"{kind} endpoint roles are malformed")
        for role, bounds in roles.items():
            bounds = closed(bounds, {"min", "max"}, f"{kind} endpoint role")
            require(
                NAME.fullmatch(role) is not None
                and is_int(bounds["min"], 0)
                and is_int(bounds["max"], max(1, bounds["min"])),
                f"{kind} endpoint role is malformed",
            )
        phases = entry["phases"]
        require(
            type(phases) is list
            and bool(phases)
            and all(type(name) is str and NAME.fullmatch(name) for name in phases)
            and len(set(phases)) == len(phases),
            f"{kind} phases are malformed",
        )
        probes = entry["probes"]
        require(type(probes) is list and bool(probes), f"{kind} probes are empty")
        seen = set()
        used_phases = set()
        for probe in probes:
            probe = closed(
                probe, {"id", "phase", "scope", "expect", "bind"}, f"{kind} probe"
            )
            require(
                type(probe["id"]) is str
                and PROBE_ID.fullmatch(probe["id"]) is not None
                and probe["id"] not in seen,
                f"{kind} probe id is malformed or repeated",
            )
            seen.add(probe["id"])
            require(
                type(probe["phase"]) is str and probe["phase"] in phases,
                f"{probe['id']} phase is unknown",
            )
            used_phases.add(probe["phase"])
            require(
                type(probe["scope"]) is str and probe["scope"] in SCOPES,
                f"{probe['id']} scope is unknown",
            )
            require(
                probe["scope"] not in SCOPE_ROLES
                or SCOPE_ROLES[probe["scope"]] in roles,
                f"{probe['id']} needs {probe['scope']} roles",
            )
            _validate_expect(probe["expect"], set(outcomes), probe["id"])
            _validate_bind(probe, kind)
        require(used_phases == set(phases), f"{kind} has a phase without probes")
    network_phases = kinds["network_enforcement"]["phases"]
    require(
        all(name in network_phases for name in NETWORK_PRE_EXPIRY_PHASES)
        and NETWORK_EXPIRY_PHASE in network_phases,
        "catalog network phases lack the expiry window",
    )
    timeouts = {
        probe["bind"].get("deadline_ms")
        for probe in kinds["resource_enforcement"]["probes"]
        if probe["expect"]["type"] == "supervisor_timeout"
    }
    require(
        timeouts == set(COMMAND_TIMEOUT_SOURCES),
        "catalog does not evidence every grading test group timeout",
    )
    require(
        any(
            probe["id"] == ZERO_RETAINED_PROBE
            and probe["expect"]["type"] == "zero_retained"
            for probe in kinds["resource_enforcement"]["probes"]
        ),
        "catalog grading output retention is not an exact zero-byte assertion",
    )
    return catalog


def evaluate(
    expect: dict[str, Any],
    observed: object,
    subordinate: dict[str, int],
    outcomes: set[str],
) -> bool:
    """Recompute ``matched`` from the catalog expectation; refuse bad shapes."""

    kind = expect["type"]
    if kind == "outcome_in":
        value = closed(observed, {"outcome"}, "observed")
        require(
            type(value["outcome"]) is str and value["outcome"] in outcomes,
            "observed outcome is unknown",
        )
        return value["outcome"] in expect["accept"]
    if kind == "exact":
        expected = expect["value"]
        value = closed(observed, set(expected), "observed")
        for key, item in value.items():
            if type(expected[key]) is list:
                require(
                    type(item) is list
                    and all(
                        type(entry) is str and OBSERVED_STRING.fullmatch(entry)
                        for entry in item
                    ),
                    "observed list is malformed",
                )
            else:
                require(
                    type(item) is type(expected[key])
                    and (type(item) is bool or is_int(item)),
                    "observed value is malformed",
                )
        return same(value, expected)
    if kind == "profile_equal":
        value = closed(observed, {"cgroup", "profile"}, "observed")
        require(
            is_int(value["cgroup"]) and is_int(value["profile"]),
            "observed value is malformed",
        )
        return value["profile"] >= 1 and value["cgroup"] == value["profile"]
    if kind == "zero_retained":
        value = closed(
            observed, {"emitted_bytes", "limit", "retained_bytes"}, "observed"
        )
        require(
            all(is_int(item) for item in value.values()), "observed value is malformed"
        )
        return (
            value["limit"] >= 1
            and value["emitted_bytes"] >= value["limit"]
            and value["retained_bytes"] == 0
        )
    if kind == "bounded":
        value = closed(observed, {"enforced", "limit", "measured"}, "observed")
        require(
            type(value["enforced"]) is bool
            and is_int(value["limit"])
            and is_int(value["measured"]),
            "observed value is malformed",
        )
        permille, floor = TOLERANCES[expect["tolerance"]], TOLERANCES[expect["floor"]]
        assert isinstance(permille, int) and isinstance(floor, int)
        return (
            value["enforced"] is True
            and value["limit"] >= 1
            and value["measured"] >= 1
            and value["measured"] * 1000 <= value["limit"] * permille
            and value["measured"] * 1000 >= value["limit"] * floor
        )
    if kind == "supervisor_timeout":
        value = closed(
            observed,
            {"deadline_ms", "elapsed_ms", "exit_code", "live_processes", "test_group"},
            "observed",
        )
        require(
            all(is_int(value[name]) for name in value if name != "test_group")
            and value["exit_code"] <= 255
            and type(value["test_group"]) is str
            and value["test_group"] in HOSTED_TEST_GROUPS,
            "observed value is malformed",
        )
        permille = TOLERANCES[expect["tolerance"]]
        assert isinstance(permille, int)
        return (
            value["exit_code"] == 124
            and value["live_processes"] == 0
            and value["deadline_ms"] >= 1
            and value["elapsed_ms"] >= value["deadline_ms"]
            and value["elapsed_ms"] * 1000 <= value["deadline_ms"] * permille
        )
    if kind == "control":
        value = closed(
            observed, {"passed", "suite_sha256", "timed_out", "total"}, "observed"
        )
        require(
            is_int(value["passed"])
            and is_int(value["total"])
            and value["passed"] <= value["total"]
            and is_digest(value["suite_sha256"])
            and type(value["timed_out"]) is bool,
            "observed value is malformed",
        )
        if expect["result"] == "timeout":
            return value["timed_out"] is True
        # A suite of fewer than two tests, or a wrong control that passes no
        # test, is what a crashed grader also reports; neither proves anything.
        if value["timed_out"] or value["total"] < 2:
            return False
        if expect["result"] == "all_pass":
            return value["passed"] == value["total"]
        return 1 <= value["passed"] < value["total"]
    if kind == "subordinate_ids":
        value = closed(observed, {"host_gid", "host_uid"}, "observed")
        require(
            is_int(value["host_uid"]) and is_int(value["host_gid"]),
            "observed value is malformed",
        )
        return (
            expect["uid"] < subordinate["uid_count"]
            and expect["gid"] < subordinate["gid_count"]
            and value["host_uid"] == subordinate["uid_start"] + expect["uid"] - 1
            and value["host_gid"] == subordinate["gid_start"] + expect["gid"] - 1
        )
    raise Refusal("expect type is unknown")


def resolve_bind(
    source: str,
    container: str,
    language: str,
    observed: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> int:
    """The approved value a resource probe must report, never the record's own."""

    spec = RESOURCE_CONTAINERS[container]
    if source in ("nofile_limit", "log_limit_bytes"):
        value = spec[source]
        assert isinstance(value, int)
        return value
    profile_name = spec["profile"]
    assert isinstance(profile_name, str)
    require(profile_name in profiles, f"record needs the {profile_name} document")
    profile = profiles[profile_name]
    policy = profile["policy"]
    if source == "memory_limit_bytes":
        return policy["MemoryLimitBytes"]
    if source == "cpu_quota_millis":
        return policy["CPUQuotaMillis"]
    if source == "pids_limit":
        return policy["PidsLimit"]
    if source == "scratch_limit_bytes":
        limit = policy["ScratchLimitBytes"]
        if spec["scratch"] == "executor" and language == "rust":
            return limit - min(limit // 2, 128 << 20)
        return limit
    require(source in COMMAND_TIMEOUT_SOURCES, "bind source is unknown")
    group = COMMAND_TIMEOUT_SOURCES[source]
    require(same(observed["test_group"], group), f"test_group is not {group}")
    return profile["group_timeouts_ms"][group]


# ---------------------------------------------------------------------------
# Host preflight and custody binding


def parse_preflight(raw: bytes, checkout: Checkout | None) -> dict[str, Any]:
    """The verbatim ``inspect-coding-native-host.py`` stdout.

    With a reviewed checkout, its tool hashes must match that checkout too.
    """

    value = closed(parse_json(raw, "host preflight"), PREFLIGHT_KEYS, "host preflight")
    require(
        (json.dumps(value, sort_keys=True) + "\n").encode() == raw,
        "host preflight is not verbatim preflight stdout",
    )
    require(same(value["schema"], PREFLIGHT_SCHEMA), "host preflight schema is unknown")
    require(value["host_preflight_passed"] is True, "host preflight did not pass")
    require(
        same(value["pending_host_qualification"], PREFLIGHT_PENDING),
        "host preflight pending list differs",
    )
    require(
        value["runtime_qualification"] is False
        and value["private_execution_ready"] is False
        and value["shadow_only"] is True
        and value["weight_eligible"] is False,
        "host preflight readiness flags differ",
    )
    require(
        type(value["source_revision"]) is str
        and REVISION.fullmatch(value["source_revision"]) is not None,
        "host preflight source revision is malformed",
    )
    for key in (
        "release_manifest_sha256",
        "runtime_archive_sha256",
        "config_sha256",
        "daemon_identity_sha256",
        "nft_snapshot_sha256",
    ):
        require(is_digest(value[key]), f"host preflight {key} is malformed")
    images = value["image_approval_sha256"]
    require(
        type(images) is dict
        and set(images) == set(LANGUAGES)
        and all(is_digest(item) for item in images.values()),
        "host preflight image approvals are malformed",
    )
    host = closed(
        value["host"], {"machine_id_sha256", "boot_id", "kernel_release"}, "host"
    )
    require(
        is_digest(host["machine_id_sha256"])
        and type(host["boot_id"]) is str
        and BOOT_ID.fullmatch(host["boot_id"]) is not None
        and type(host["kernel_release"]) is str
        and KERNEL.fullmatch(host["kernel_release"]) is not None,
        "host preflight host binding is malformed",
    )
    require(is_int(value["checked_at_unix"], 1), "host preflight time is malformed")
    tools = value["tool_sha256"]
    require(
        type(tools) is dict and set(tools) == set(PREFLIGHT_TOOL_FILES),
        "host preflight tool list differs",
    )
    for path, digest in tools.items():
        require(is_digest(digest), "host preflight tool hash is malformed")
        require(
            checkout is None or digest == checkout.file_sha256(path),
            "host preflight tool hash differs from the reviewed checkout",
        )
    return value


def parse_custody_binding(raw: bytes) -> dict[str, Any]:
    value = closed(
        parse_canonical(raw, "custody binding"), CUSTODY_KEYS, "custody binding"
    )
    require(same(value["schema"], CUSTODY_SCHEMA), "custody binding schema is unknown")
    require(
        is_digest(value["custody_evidence_sha256"])
        and is_digest(value["machine_id_sha256"])
        and type(value["boot_id"]) is str
        and BOOT_ID.fullmatch(value["boot_id"]) is not None
        and is_int(value["bound_at_unix"], 1),
        "custody binding is malformed",
    )
    return value


# ---------------------------------------------------------------------------
# Records


def parse_record_envelope(raw: bytes) -> dict[str, Any]:
    """Catalog-free structure: canonical bytes, schema, closed keys and kind."""

    value = closed(parse_canonical(raw, "record"), RECORD_KEYS, "record")
    require(same(value["schema"], RECORD_SCHEMA), "record schema is unknown")
    require(
        type(value["kind"]) is str and value["kind"] in KINDS, "record kind is unknown"
    )
    return value


def _host(value: object, catalog: dict[str, Any]) -> dict[str, Any]:
    host = closed(value, HOST_KEYS, "record host")
    require(
        is_digest(host["machine_id_sha256"])
        and is_digest(host["daemon_identity_sha256"])
        and type(host["boot_id"]) is str
        and BOOT_ID.fullmatch(host["boot_id"]) is not None
        and type(host["kernel_release"]) is str
        and KERNEL.fullmatch(host["kernel_release"]) is not None,
        "record host binding is malformed",
    )
    require(
        type(host["router_namespace"]) is str
        and host["router_namespace"] in catalog["router_namespaces"],
        "record router namespace is unknown",
    )
    ids = closed(host["subordinate_ids"], SUBORDINATE_KEYS, "subordinate ids")
    for prefix in ("uid", "gid"):
        start, count = ids[f"{prefix}_start"], ids[f"{prefix}_count"]
        require(
            is_int(start, SUBORDINATE_MIN_START)
            and is_int(count, SUBORDINATE_MIN_COUNT)
            and start + count <= 1 << 32,
            "subordinate ids are malformed",
        )
    return host


def _release(value: object) -> dict[str, Any]:
    release = closed(value, RELEASE_KEYS, "record release")
    require(
        type(release["source_revision"]) is str
        and REVISION.fullmatch(release["source_revision"]) is not None
        and release["source_revision"] != "0" * 40
        and is_digest(release["release_manifest_sha256"])
        and is_digest(release["runtime_archive_sha256"]),
        "record release binding is malformed",
    )
    images = release["image_approval_sha256"]
    require(
        type(images) is dict
        and set(images) == set(LANGUAGES)
        and all(is_digest(item) for item in images.values()),
        "record image approvals are malformed",
    )
    return release


def _endpoints(
    value: object, entry: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    """Endpoints must be exactly the hashes derived from the connectivity profile."""

    require(type(value) is list, "record endpoints are malformed")
    assert isinstance(value, list)
    roles = entry["endpoint_roles"]
    grouped: dict[str, list[str]] = {role: [] for role in roles}
    keys = []
    for item in value:
        endpoint = closed(item, ENDPOINT_KEYS, "record endpoint")
        require(
            type(endpoint["role"]) is str and endpoint["role"] in roles,
            "record endpoint role is not allowed",
        )
        require(is_digest(endpoint["endpoint_sha256"]), "endpoint hash is malformed")
        grouped[endpoint["role"]].append(endpoint["endpoint_sha256"])
        keys.append((endpoint["role"], endpoint["endpoint_sha256"]))
    require(keys == sorted(keys), "record endpoints are not sorted")
    hashes = [key[1] for key in keys]
    require(len(set(hashes)) == len(hashes), "record endpoint hash is repeated")
    for role, bounds in roles.items():
        require(
            bounds["min"] <= len(grouped[role]) <= bounds["max"],
            f"record needs {bounds['min']}..{bounds['max']} {role} endpoints",
        )
    if not roles:
        return grouped
    name = "connectivity_profile_sha256"
    require(name in profiles, f"record needs the {name} document")
    derived = profiles[name]["endpoints"]
    require(
        same(grouped["trusted"], derived["trusted"])
        and same(grouped["trusted_dns"], derived["trusted_dns"]),
        "record trusted endpoints differ from the connectivity profile",
    )
    # Peyton, 2026-09-15: the refusing proxy runs beside the router, so the
    # evidenced candidate list is exactly those two distinct endpoints.
    require(
        len(derived["candidate"]) == 2
        and sorted(grouped["router"] + grouped["refusing_proxy"])
        == derived["candidate"],
        "record router and proxy differ from the connectivity profile",
    )
    return grouped


Instance = tuple[str, str | None, str | None]


def _required_instances(
    entry: dict[str, Any], endpoints: dict[str, list[str]]
) -> dict[Instance, dict[str, Any]]:
    """Router and proxy probes name their endpoint, trusted probes each one."""

    required: dict[Instance, dict[str, Any]] = {}
    for probe in entry["probes"]:
        if probe["scope"] == "host":
            required[(probe["id"], None, None)] = probe
        elif probe["scope"] == "language":
            for language in LANGUAGES:
                required[(probe["id"], language, None)] = probe
        else:
            for endpoint in endpoints[SCOPE_ROLES[probe["scope"]]]:
                required[(probe["id"], None, endpoint)] = probe
    return required


def _order_key(probe: dict[str, Any]) -> tuple[str, str, str]:
    return (probe["id"], probe["language"] or "", probe["endpoint_sha256"] or "")


def _controls(observations: dict[Instance, Any]) -> None:
    """Pass, wrong and hang controls come from one suite per language image."""

    for language in LANGUAGES:
        passed = observations[("control.pass", language, None)]
        wrong = observations[("control.wrong", language, None)]
        hang = observations[("control.hang", language, None)]
        require(
            same(passed["suite_sha256"], wrong["suite_sha256"])
            and same(passed["suite_sha256"], hang["suite_sha256"]),
            f"{language} controls come from different suites",
        )
        require(
            same(passed["total"], wrong["total"]),
            f"{language} wrong control ran a different test count",
        )


def _connectivity_window(
    phases: list[dict[str, Any]], started: int, connectivity: dict[str, Any]
) -> None:
    """Network phases fall inside and then past the evidenced profile's window."""

    issued, expires = connectivity["issued_at_unix"], connectivity["expires_at_unix"]
    require(
        issued <= started,
        "connectivity profile was issued after network collection started",
    )
    by_name = {phase["name"]: phase for phase in phases}
    for name in NETWORK_PRE_EXPIRY_PHASES:
        require(
            by_name[name]["completed_at_unix"] < expires,
            f"network {name} phase does not end before the connectivity expiry",
        )
    require(
        by_name[NETWORK_EXPIRY_PHASE]["completed_at_unix"] >= expires,
        "network expiry phase ends before the connectivity expiry",
    )


def verify_record(
    raw: bytes,
    *,
    catalog: dict[str, Any],
    tools: dict[str, str],
    store: Store,
    checkout: Checkout,
    profiles: dict[str, dict[str, Any]],
    host_preflight: dict[str, Any],
    host_preflight_sha256: str,
) -> dict[str, Any]:
    """Verify one record completely; return its binding summary or refuse."""

    record = parse_record_envelope(raw)
    kind = record["kind"]
    entry = catalog["kinds"][kind]
    require(
        same(record["coverage"], COVERAGE)
        and same(record["not_covered"], list(NOT_COVERED)),
        "record claims coverage beyond the same boot",
    )
    require(
        same(record["tolerances_version"], TOLERANCES["version"]),
        "record tolerances version differs",
    )
    host = _host(record["host"], catalog)
    release = _release(record["release"])
    inputs = closed(record["inputs"], set(entry["inputs"]), "record inputs")
    for name, value in inputs.items():
        require(is_digest(value), "record input malformed")
        require(name in profiles, f"record needs the {name} document")
        require(
            same(value, profiles[name]["sha256"]),
            f"record {name} differs from the supplied document",
        )
    endpoints = _endpoints(record["endpoints"], entry, profiles)
    require(
        same(closed(record["tools"], TOOL_KEYS, "record tools"), tools),
        "record tool hashes differ from the reviewed checkout",
    )
    require(same(record["preconditions"], PRECONDITIONS), "record preconditions failed")
    require(same(record["residue"], RESIDUE), "record residue is not empty")
    started, completed = record["started_at_unix"], record["completed_at_unix"]
    require(
        is_int(started, 1) and is_int(completed, 1), "record timestamps are malformed"
    )

    phases = record["phases"]
    require(type(phases) is list, "record phases are malformed")
    require(
        same(
            [phase.get("name") if type(phase) is dict else None for phase in phases],
            entry["phases"],
        ),
        "record phases differ from the catalog order",
    )
    required = _required_instances(entry, endpoints)
    outcomes = set(catalog["outcomes"])
    seen: set[Instance] = set()
    observations: dict[Instance, Any] = {}
    unmatched: list[str] = []
    cursor = started
    for phase in phases:
        phase = closed(phase, PHASE_KEYS, "record phase")
        require(
            is_int(phase["started_at_unix"], 1)
            and is_int(phase["completed_at_unix"], 1)
            and cursor <= phase["started_at_unix"] <= phase["completed_at_unix"],
            "record phase timestamps are out of order",
        )
        cursor = phase["completed_at_unix"]
        probes = phase["probes"]
        require(type(probes) is list, "record probes are malformed")
        for probe in probes:
            probe = closed(probe, PROBE_KEYS, "record probe")
            require(
                (probe["language"] is None or probe["language"] in LANGUAGES)
                and (
                    probe["endpoint_sha256"] is None
                    or is_digest(probe["endpoint_sha256"])
                ),
                "record probe scope is malformed",
            )
            key = (probe["id"], probe["language"], probe["endpoint_sha256"])
            if not (type(probe["id"]) is str and key in required):
                # Only a well-formed id is echoed; records never inject text.
                named = type(probe["id"]) is str and PROBE_ID.fullmatch(probe["id"])
                raise Refusal(
                    f"record has unexpected probe {probe['id']}"
                    if named
                    else "record has an unexpected probe"
                )
            require(key not in seen, f"record repeats probe {probe['id']}")
            seen.add(key)
            definition = required[key]
            require(
                same(definition["phase"], phase["name"]),
                f"probe {probe['id']} is in the wrong phase",
            )
            require(
                same(probe["expect"], definition["expect"]),
                f"probe {probe['id']} expectation differs from the catalog",
            )
            require(type(probe["matched"]) is bool, "record matched is not a boolean")
            try:
                matched = evaluate(
                    definition["expect"],
                    probe["observed"],
                    host["subordinate_ids"],
                    outcomes,
                )
            except Refusal as error:
                raise Refusal(f"probe {probe['id']} {error}") from None
            for field, source in definition["bind"].items():
                try:
                    expected = resolve_bind(
                        source,
                        probe["id"].split(".")[0],
                        probe["language"],
                        probe["observed"],
                        profiles,
                    )
                except Refusal as error:
                    raise Refusal(f"probe {probe['id']} {error}") from None
                require(
                    same(probe["observed"][field], expected),
                    f"probe {probe['id']} {field} differs from the approved profile",
                )
            require(
                probe["matched"] is matched,
                f"probe {probe['id']} matched value is misreported",
            )
            observations[key] = probe["observed"]
            if not matched:
                unmatched.append(probe["id"])
        require(
            [_order_key(probe) for probe in probes]
            == sorted(_order_key(probe) for probe in probes),
            "record probes are not sorted",
        )
    require(cursor <= completed, "record completed before its last phase")
    missing = sorted({key[0] for key in required.keys() - seen})
    require(not missing, f"record is missing probe {missing[0] if missing else ''}")
    require(not unmatched, f"probe {unmatched[0] if unmatched else ''} did not match")
    if kind == "preexec_confinement":
        _controls(observations)
    if kind == "network_enforcement":
        _connectivity_window(phases, started, profiles["connectivity_profile_sha256"])

    require(
        record["pre_collection_preflight_sha256"] != host_preflight_sha256,
        "host preflight must be a separate post-collection preflight",
    )
    pre = parse_preflight(
        store.get(record["pre_collection_preflight_sha256"]), checkout
    )
    for preflight, label in ((pre, "pre-collection"), (host_preflight, "host")):
        require(
            same(preflight["host"]["machine_id_sha256"], host["machine_id_sha256"]),
            f"{label} preflight machine differs",
        )
        require(
            same(preflight["host"]["boot_id"], host["boot_id"]),
            f"{label} preflight boot differs",
        )
        require(
            same(preflight["host"]["kernel_release"], host["kernel_release"]),
            f"{label} preflight kernel differs",
        )
        require(
            same(preflight["daemon_identity_sha256"], host["daemon_identity_sha256"]),
            f"{label} preflight daemon differs",
        )
        for name in RELEASE_KEYS:
            require(
                same(preflight[name], release[name]),
                f"{label} preflight {name} differs",
            )
    for name in ("nft_snapshot_sha256", "config_sha256"):
        require(
            same(pre[name], host_preflight[name]),
            f"pre-collection preflight {name} differs from the host preflight",
        )
    require(
        pre["checked_at_unix"] <= started,
        "pre-collection preflight is later than collection",
    )
    require(
        started - pre["checked_at_unix"] <= PRE_COLLECTION_PREFLIGHT_MAX_AGE_SECONDS,
        "pre-collection preflight is too old",
    )
    require(
        host_preflight["checked_at_unix"] >= completed,
        "host preflight is earlier than the last collection record",
    )
    require(
        completed - started <= FRESHNESS_SECONDS,
        "record spans more than the freshness window",
    )
    return {
        "kind": kind,
        "host": host,
        "release": release,
        "inputs": inputs,
        "endpoints": record["endpoints"],
        "tools": record["tools"],
        "started_at_unix": started,
        "completed_at_unix": completed,
    }


def consistency(summaries: list[dict[str, Any]], moments: list[int]) -> None:
    """One machine, boot, daemon, release and router mode; ordered, fresh records."""

    if not summaries:
        return
    first = summaries[0]
    shared_inputs: dict[str, str] = {}
    for summary in summaries:
        require(
            same(
                summary["host"]["router_namespace"], first["host"]["router_namespace"]
            ),
            "records disagree on the router namespace",
        )
        require(
            same(summary["host"]["subordinate_ids"], first["host"]["subordinate_ids"]),
            "records disagree on subordinate ids",
        )
        require(same(summary["host"], first["host"]), "records disagree on the host")
        require(
            same(summary["release"], first["release"]), "records disagree on release"
        )
        require(same(summary["tools"], first["tools"]), "records disagree on tools")
        for name, value in summary["inputs"].items():
            require(
                same(shared_inputs.setdefault(name, value), value),
                f"records disagree on {name}",
            )
    kinds = [summary["kind"] for summary in summaries]
    require(len(set(kinds)) == len(kinds), "a record kind is repeated")
    ordered = sorted(summaries, key=lambda summary: KINDS.index(summary["kind"]))
    for previous, following in zip(ordered, ordered[1:], strict=False):
        require(
            previous["completed_at_unix"] < following["started_at_unix"],
            f"{following['kind']} overlaps or precedes {previous['kind']}",
        )
    moments = [
        *moments,
        *(item["started_at_unix"] for item in summaries),
        *(item["completed_at_unix"] for item in summaries),
    ]
    require(
        max(moments) - min(moments) <= FRESHNESS_SECONDS,
        "evidence spans more than the freshness window",
    )


# ---------------------------------------------------------------------------
# verify and review


def _context(checkout: Checkout) -> tuple[dict[str, Any], dict[str, str]]:
    catalog = load_catalog(checkout.read(CATALOG_FILE))
    tools = checkout.tools()
    # The running verifier must itself be the reviewed evidence tool.
    require(
        sha256(Path(__file__).read_bytes()) == tools["evidence_tool_sha256"],
        "running verifier differs from the reviewed checkout",
    )
    return catalog, tools


def verify(
    store: Store,
    checkout: Checkout,
    preflight_sha: str,
    record_shas: list[str],
    profiles: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    catalog, tools = _context(checkout)
    host_preflight = parse_preflight(store.get(preflight_sha), checkout)
    results = []
    summaries = []
    for digest in record_shas:
        try:
            summary = verify_record(
                store.get(digest),
                catalog=catalog,
                tools=tools,
                store=store,
                checkout=checkout,
                profiles=profiles,
                host_preflight=host_preflight,
                host_preflight_sha256=preflight_sha,
            )
        except (Refusal, *MALFORMED) as error:
            results.append(
                {"record_sha256": digest, "verified": False, "failure": reason(error)}
            )
        else:
            summaries.append(summary)
            results.append({"record_sha256": digest, "verified": True, "failure": None})
    try:
        require(bool(record_shas), "no records were supplied")
        require(len(set(record_shas)) == len(record_shas), "a record is repeated")
        consistency(summaries, [host_preflight["checked_at_unix"]])
        consistent: dict[str, Any] = {"verified": True, "failure": None}
    except (Refusal, *MALFORMED) as error:
        consistent = {"verified": False, "failure": reason(error)}
    ok = consistent["verified"] and all(item["verified"] for item in results)
    return (
        {
            "schema": VERIFICATION_SCHEMA,
            "approval_generated": False,
            "catalog_sha256": tools["catalog_sha256"],
            "coverage": COVERAGE,
            "not_covered": list(NOT_COVERED),
            "records": results,
            "consistency": consistent,
            "verified": ok,
        },
        ok,
    )


def review(
    store: Store,
    checkout: Checkout,
    selection: dict[str, str],
    profiles: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Assemble the review. It carries no digest map unless everything verified."""

    require(set(selection) == set(EVIDENCE), "review needs all six evidence digests")
    require(set(profiles) == set(PROFILE_INPUTS), "review needs all three profiles")
    catalog, tools = _context(checkout)
    verification: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    host_preflight: dict[str, Any] | None = None
    custody: dict[str, Any] | None = None
    try:
        host_preflight = parse_preflight(
            store.get(selection["host_preflight"]), checkout
        )
        verification["host_preflight"] = {"verified": True, "failure": None}
    except (Refusal, *MALFORMED) as error:
        verification["host_preflight"] = {"verified": False, "failure": reason(error)}
    for kind in KINDS:
        try:
            require(host_preflight is not None, "host preflight did not verify")
            assert host_preflight is not None
            raw = store.get(selection[kind])
            require(
                same(parse_record_envelope(raw)["kind"], kind),
                f"record is not {kind}",
            )
            summaries[kind] = verify_record(
                raw,
                catalog=catalog,
                tools=tools,
                store=store,
                checkout=checkout,
                profiles=profiles,
                host_preflight=host_preflight,
                host_preflight_sha256=selection["host_preflight"],
            )
            verification[kind] = {"verified": True, "failure": None}
        except (Refusal, *MALFORMED) as error:
            verification[kind] = {"verified": False, "failure": reason(error)}
    try:
        custody = parse_custody_binding(store.get(selection["private_input_custody"]))
        verification["private_input_custody"] = {"verified": True, "failure": None}
    except (Refusal, *MALFORMED) as error:
        verification["private_input_custody"] = {
            "verified": False,
            "failure": reason(error),
        }

    window: dict[str, int] | None = None
    try:
        require(
            len(set(selection.values())) == len(selection),
            "an evidence digest is reused for two evidence items",
        )
        require(
            host_preflight is not None and custody is not None and len(summaries) == 4,
            "not every evidence item verified",
        )
        assert host_preflight is not None and custody is not None
        ordered = [summaries[kind] for kind in KINDS]
        host = ordered[0]["host"]
        require(
            same(custody["machine_id_sha256"], host["machine_id_sha256"]),
            "custody binding machine differs",
        )
        require(
            same(custody["boot_id"], host["boot_id"]), "custody binding boot differs"
        )
        consistency(
            ordered, [host_preflight["checked_at_unix"], custody["bound_at_unix"]]
        )
        window = {
            "earliest_started_at_unix": min(s["started_at_unix"] for s in ordered),
            "latest_completed_at_unix": max(s["completed_at_unix"] for s in ordered),
            "host_preflight_checked_at_unix": host_preflight["checked_at_unix"],
            "custody_bound_at_unix": custody["bound_at_unix"],
        }
        consistent: dict[str, Any] = {"verified": True, "failure": None}
    except (Refusal, *MALFORMED) as error:
        consistent = {"verified": False, "failure": reason(error)}

    ok = consistent["verified"] and all(
        item["verified"] for item in verification.values()
    )
    result: dict[str, Any] = {
        "schema": REVIEW_SCHEMA,
        "approval_generated": False,
        "catalog_sha256": tools["catalog_sha256"],
        "tolerances_version": TOLERANCES["version"],
        "coverage": COVERAGE,
        "not_covered": list(NOT_COVERED),
        "verification": verification,
        "consistency": consistent,
        "verified": ok,
    }
    if ok:
        assert window is not None
        first = summaries[KINDS[0]]
        endpoints = summaries["network_enforcement"]["endpoints"]
        result["evidence_sha256"] = dict(selection)
        result["host"] = first["host"]
        result["release"] = first["release"]
        result["inputs"] = {name: profiles[name]["sha256"] for name in PROFILE_INPUTS}
        result["endpoints"] = endpoints
        result["endpoint_set_sha256"] = profiles["connectivity_profile_sha256"][
            "endpoint_set_sha256"
        ]
        result["endpoint_counts"] = {
            role: sum(1 for item in endpoints if item["role"] == role)
            for role in catalog["kinds"]["network_enforcement"]["endpoint_roles"]
        }
        result["window"] = window
    return result, ok


# ---------------------------------------------------------------------------
# check-approval


def curator_public_key(raw: bytes) -> bytes:
    """Strict PEM SubjectPublicKeyInfo Ed25519 key; returns the raw 32 bytes."""

    require(len(raw) <= MAX_PUBLIC_KEY, "curator public key is too large")
    require(
        raw.startswith(PEM_BEGIN) and raw.endswith(PEM_END),
        "curator public key is not a PEM public key",
    )
    body = raw[len(PEM_BEGIN) : -len(PEM_END)]
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError:
        raise Refusal("curator public key is not a PEM public key") from None
    require(
        len(der) == 44
        and der.startswith(SPKI_ED25519_PREFIX)
        and base64.b64encode(der) == body,
        "curator public key must be Ed25519",
    )
    return der[len(SPKI_ED25519_PREFIX) :]


def trusted_executable(path: Path) -> None:
    """A root-owned executable that no other user can replace or edit."""

    require(path.is_absolute(), "openssl path must be absolute")
    try:
        require(path.resolve() == path, "openssl path must be canonical")
        info = path.lstat()
        parents = [parent.lstat() for parent in path.parents]
    except OSError:
        raise Refusal("openssl is missing") from None
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
        "openssl must be a root-owned file not writable by others",
    )
    require(
        all(
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == 0
            and not parent.st_mode & 0o022
            for parent in parents
        ),
        "openssl ancestors must be root-owned and not writable by others",
    )


def _write_private(directory: Path, name: str, raw: bytes) -> Path:
    path = directory / name
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def verify_ed25519(openssl: Path, key: bytes, message: bytes, signature: bytes) -> None:
    """Detached Ed25519 (RFC 8032) over exact bytes, through OpenSSL 3.

    This is the curator algorithm ``load_curator_signing_public_key`` and
    ``verify_profile_approval`` use: a PEM Ed25519 public key and a raw 64-byte
    signature over the exact document bytes. The infra Python has neither
    ``cryptography`` nor PyNaCl, so a trusted root-owned OpenSSL 3 checks them.
    OpenSSL sees only the validated key, signature and message, written into a
    fresh owner-only directory and re-read unchanged afterwards.
    """

    require(len(key) == 32, "curator public key must be Ed25519")
    require(len(signature) == SIGNATURE_BYTES, "curator signature is malformed")
    trusted_executable(openssl)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    pem = PEM_BEGIN + base64.b64encode(SPKI_ED25519_PREFIX + key) + PEM_END
    directory = Path(tempfile.mkdtemp(prefix="native-evidence-"))
    try:
        info = directory.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "signature workspace is not private",
        )
        _protected_ancestors(directory, "signature workspace")
        inputs = {
            "curator.pem": pem,
            "approval.json": message,
            "approval.sig": signature,
        }
        paths = {
            name: _write_private(directory, name, raw) for name, raw in inputs.items()
        }
        try:
            version = subprocess.run(
                [str(openssl), "version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=environment,
                cwd=directory,
                timeout=30,
                check=False,
            )
            require(
                version.returncode == 0 and version.stdout.startswith(b"OpenSSL 3."),
                "OpenSSL 3 is required to verify the curator signature",
            )
            result = subprocess.run(
                [
                    str(openssl),
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    str(paths["curator.pem"]),
                    "-rawin",
                    "-in",
                    str(paths["approval.json"]),
                    "-sigfile",
                    str(paths["approval.sig"]),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=environment,
                cwd=directory,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise Refusal("curator signature could not be checked") from None
        for name, raw in inputs.items():
            require(
                read_input(paths[name], len(raw), "signature input") == raw,
                "signature inputs changed while OpenSSL ran",
            )
        require(
            result.returncode == 0
            and result.stdout == b"Signature Verified Successfully\n",
            "curator signature does not verify",
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def load_native_policy(checkout: Checkout, expected_sha256: object) -> Callable:
    """native.py compiled from exactly the hashed bytes; no loader or pyc cache."""

    source = checkout.read(NATIVE_BINDING_FILE)
    require(
        same(expected_sha256, sha256(source)),
        "approval binding hash differs from the reviewed native.py",
    )
    path = str(checkout.root / NATIVE_BINDING_FILE)
    namespace: dict[str, Any] = {
        "__name__": "native_controls_policy",
        "__file__": path,
        "__builtins__": builtins,
    }
    exec(compile(source, path, "exec", dont_inherit=True), namespace)
    policy = namespace.get("policy")
    require(callable(policy), "native.py has no policy")
    assert callable(policy)
    return policy


def parse_review(raw: bytes) -> dict[str, Any]:
    value = parse_canonical(raw, "review")
    require(type(value) is dict, "review must be an object")
    require(set(value) == REVIEW_PASS_KEYS, "review did not verify every record")
    require(same(value["schema"], REVIEW_SCHEMA), "review schema is unknown")
    require(value["approval_generated"] is False, "review claims to generate approval")
    require(
        same(value["coverage"], COVERAGE)
        and same(value["not_covered"], list(NOT_COVERED)),
        "review claims coverage beyond the same boot",
    )
    require(value["verified"] is True, "review did not verify")
    evidence = value["evidence_sha256"]
    require(
        type(evidence) is dict
        and set(evidence) == set(EVIDENCE)
        and all(is_digest(item) for item in evidence.values()),
        "review digest map is malformed",
    )
    closed(value["window"], WINDOW_KEYS, "review window")
    return value


def check_approval(
    *,
    store: Store,
    checkout: Checkout,
    profiles: dict[str, dict[str, Any]],
    profile_pins: dict[str, str],
    review_raw: bytes,
    approval_raw: bytes,
    signature: bytes,
    public_key_raw: bytes,
    curator_signing_key_sha256: str,
    openssl: Path,
) -> dict[str, Any]:
    require(is_digest(curator_signing_key_sha256), "curator key pin is malformed")
    key = curator_public_key(public_key_raw)
    require(
        sha256(key) == curator_signing_key_sha256,
        "curator public key differs from the pinned identity",
    )
    require(0 < len(approval_raw) <= MAX_APPROVAL, "approval size is out of bounds")
    verify_ed25519(openssl, key, approval_raw, signature)

    claimed = parse_review(review_raw)
    rebuilt, ok = review(store, checkout, claimed["evidence_sha256"], profiles)
    require(ok, "review evidence no longer verifies")
    require(
        canonical_bytes(rebuilt) == review_raw,
        "review differs from the evidence it names",
    )
    # native.policy's closed approval shape cannot carry profile digests. They are
    # bound through the record digests the approval names, and must also equal
    # independently reviewed pins (for example the signed profile approval). The
    # connectivity pin is the endpoint set, which the canary's own later profile
    # reproduces; the evidenced probe profile's full digest stays in the review.
    require(
        set(profile_pins) == set(PIN_NAMES)
        and all(is_digest(item) for item in profile_pins.values()),
        "profile pins are malformed",
    )
    require(
        same(
            rebuilt["endpoint_set_sha256"],
            profile_pins["connectivity_endpoint_set_sha256"],
        ),
        "review connectivity endpoint set differs from the reviewed pin",
    )
    for name in ("execution_profile_sha256", "grading_profile_sha256"):
        require(
            same(rebuilt["inputs"][name], profile_pins[name]),
            f"review {name} differs from the reviewed pin",
        )

    approval = parse_json(approval_raw, "approval")
    require(type(approval) is dict, "approval must be an object")
    require(same(approval.get("schema"), APPROVAL_SCHEMA), "approval schema is unknown")
    require(
        same(approval.get("runner_sha256"), checkout.file_sha256(NATIVE_RUNNER_FILE)),
        "approval runner hash differs from the reviewed run.py",
    )
    policy = load_native_policy(checkout, approval.get("binding_sha256"))
    issued = approval.get("issued_at_unix")
    try:
        policy(
            approval,
            source=approval.get("source_revision"),
            plan_sha=approval.get("plan_sha256"),
            helper_sha=approval.get("helper_sha256"),
            controls=approval.get("controls"),
            jobs=1,
            now=issued,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Refusal("approval is rejected by native.policy") from None
    require(type(issued) is int, "approval is rejected by native.policy")
    assert isinstance(issued, int)

    require(
        same(approval["evidence_sha256"], rebuilt["evidence_sha256"]),
        "approval evidence digests differ from the review",
    )
    host, release = rebuilt["host"], rebuilt["release"]
    require(
        same(approval["machine_id_sha256"], host["machine_id_sha256"]),
        "approval machine differs from the evidence",
    )
    require(same(approval["boot_id"], host["boot_id"]), "approval boot differs")
    require(
        same(approval["source_revision"], release["source_revision"]),
        "approval source revision differs",
    )
    require(
        same(approval["release_manifest_sha256"], release["release_manifest_sha256"]),
        "approval release manifest differs",
    )
    for language in LANGUAGES:
        require(
            same(
                approval["images"][language]["approval_sha256"],
                release["image_approval_sha256"][language],
            ),
            f"approval {language} image approval differs",
        )
    window = rebuilt["window"]
    require(
        issued >= window["latest_completed_at_unix"]
        and issued >= window["host_preflight_checked_at_unix"]
        and issued >= window["custody_bound_at_unix"],
        "approval was issued before its evidence was complete",
    )
    require(
        issued - window["earliest_started_at_unix"] <= FRESHNESS_SECONDS
        and issued - window["custody_bound_at_unix"] <= FRESHNESS_SECONDS,
        "evidence is older than six hours at approval issuance",
    )
    return {
        "schema": CHECK_SCHEMA,
        "approval_generated": False,
        "checked_approval_sha256": sha256(approval_raw),
        "curator_signing_key_sha256": curator_signing_key_sha256,
        "review_sha256": sha256(review_raw),
        "signature_sha256": sha256(signature),
        "inputs": rebuilt["inputs"],
        "endpoint_set_sha256": rebuilt["endpoint_set_sha256"],
        "endpoint_counts": rebuilt["endpoint_counts"],
        "consistent": True,
    }


# ---------------------------------------------------------------------------
# CLI


def _profile_paths(args: argparse.Namespace) -> dict[str, Path | None]:
    return {
        name: getattr(args, name.removesuffix("_sha256")) for name in PROFILE_INPUTS
    }


def _retain(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    raw = read_input(args.input, MAX_OBJECT, "input")
    if args.type == "record":
        parse_record_envelope(raw)
    elif args.type == "custody-binding":
        parse_custody_binding(raw)
    else:
        parse_preflight(raw, None)
    with Store(args.store) as store:
        digest = store.put(raw)
    return {"retained_sha256": digest, "type": args.type}, True


def _verify(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    profiles = load_profiles(_profile_paths(args))
    with Store(args.store) as store:
        return verify(
            store, Checkout(args.checkout), args.host_preflight, args.record, profiles
        )


def _review(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    selection = {name: getattr(args, name) for name in EVIDENCE}
    profiles = load_profiles(_profile_paths(args))
    with Store(args.store) as store:
        return review(store, Checkout(args.checkout), selection, profiles)


def _check(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    signature = read_input(args.signature, SIGNATURE_BYTES, "curator signature")
    profiles = load_profiles(_profile_paths(args))
    pins = {name: getattr(args, name) for name in PIN_NAMES}
    with Store(args.store) as store:
        return (
            check_approval(
                store=store,
                checkout=Checkout(args.checkout),
                profiles=profiles,
                profile_pins=pins,
                review_raw=read_input(args.review, MAX_OBJECT, "review"),
                approval_raw=read_input(args.approval, MAX_APPROVAL, "approval"),
                signature=signature,
                public_key_raw=read_input(
                    args.curator_public_key, MAX_PUBLIC_KEY, "curator public key"
                ),
                curator_signing_key_sha256=args.curator_signing_key_sha256,
                openssl=args.openssl,
            ),
            True,
        )


def _endpoint_set(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    profile = parse_connectivity_profile(
        read_input(args.connectivity_profile, MAX_CONNECTIVITY, "connectivity profile")
    )
    return {
        "schema": ENDPOINT_SET_DIGEST_SCHEMA,
        "connectivity_profile_sha256": profile["sha256"],
        "endpoint_set_sha256": profile["endpoint_set_sha256"],
        "endpoint_counts": {
            name: len(items) for name, items in profile["endpoints"].items()
        },
    }, True


def _profile_arguments(command: argparse.ArgumentParser, *, required: bool) -> None:
    for name in PROFILE_INPUTS:
        flag = "--" + name.removesuffix("_sha256").replace("_", "-")
        command.add_argument(
            flag, dest=name.removesuffix("_sha256"), type=Path, required=required
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    retain = commands.add_parser("retain", help="retain one object in the store")
    retain.add_argument("--store", type=Path, required=True)
    retain.add_argument(
        "--type",
        choices=("record", "host-preflight", "custody-binding"),
        required=True,
    )
    retain.add_argument("--input", type=Path, required=True)
    retain.set_defaults(handler=_retain)

    check = commands.add_parser("verify", help="verify records against a preflight")
    check.add_argument("--store", type=Path, required=True)
    check.add_argument("--checkout", type=Path, required=True)
    check.add_argument("--host-preflight", required=True)
    check.add_argument("--record", action="append", required=True)
    _profile_arguments(check, required=False)
    check.set_defaults(handler=_verify)

    assemble = commands.add_parser("review", help="assemble the evidence review")
    assemble.add_argument("--store", type=Path, required=True)
    assemble.add_argument("--checkout", type=Path, required=True)
    for name in EVIDENCE:
        assemble.add_argument("--" + name.replace("_", "-"), dest=name, required=True)
    _profile_arguments(assemble, required=True)
    assemble.set_defaults(handler=_review)

    approval = commands.add_parser(
        "check-approval", help="check a curator-signed approval against the review"
    )
    approval.add_argument("--store", type=Path, required=True)
    approval.add_argument("--checkout", type=Path, required=True)
    approval.add_argument("--review", type=Path, required=True)
    approval.add_argument("--approval", type=Path, required=True)
    approval.add_argument("--signature", type=Path, required=True)
    approval.add_argument("--curator-public-key", type=Path, required=True)
    approval.add_argument("--curator-signing-key-sha256", required=True)
    _profile_arguments(approval, required=True)
    for name in PIN_NAMES:
        approval.add_argument("--" + name.replace("_", "-"), dest=name, required=True)
    approval.add_argument("--openssl", type=Path, default=DEFAULT_OPENSSL)
    approval.set_defaults(handler=_check)

    endpoint_set = commands.add_parser(
        "endpoint-set",
        help="print a connectivity profile's endpoint-set digest, never its addresses",
    )
    endpoint_set.add_argument("--connectivity-profile", type=Path, required=True)
    endpoint_set.set_defaults(handler=_endpoint_set)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output, ok = args.handler(args)
    except Refusal as error:
        print(f"native enforcement evidence rejected: {error}", file=sys.stderr)
        return 1
    except (OSError, *MALFORMED):
        print("native enforcement evidence rejected", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_bytes(output))
    sys.stdout.buffer.flush()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
