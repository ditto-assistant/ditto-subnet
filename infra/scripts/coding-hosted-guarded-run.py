#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "ansible-core==2.21.2",
#     "google-auth==2.58.0",
#     "requests==2.34.2",
# ]
# ///
"""Guarded entry point for the native Coding host's secret-handling playbooks.

This file is shared, byte for byte, by every branch that adds a guarded
operation. Each operation is a data file under ``infra/ansible/guarded-runs/``;
adding one never edits this file or any shared registry.

Usage, from any directory of a clean checkout at the reviewed revision:

    uv run --locked --script infra/scripts/coding-hosted-guarded-run.py \
        OPERATION REVIEWED_REVISION

The guard accepts exactly those two arguments and nothing else. It:

* verifies that HEAD is the 40-hex reviewed revision, that the tracked tree is
  clean, and that every file ansible loads from ``infra/ansible`` (plus this
  script and its lock) is byte-identical to that revision, with no untracked or
  ignored file beside them;
* refuses an environment carrying ANSIBLE_*, _ANSIBLE_*, LD_*, DYLD_*, a
  code-loading PYTHON* variable, a relative PATH entry, the entry-point marker
  or an input the operation forbids;
* validates every protected input environment variable the operation's role
  reads, rejecting template syntax and structural problems, and reports only
  the variable name and a failure class, never a value;
* builds the JSON boolean enable flag, the exact confirmation and the revision
  itself and passes only those (and pattern-validated non-secret inputs) as
  extra vars;
* runs ansible-core 2.21.2 from this script's own locked environment with a
  fixed argv (fixed playbook, inventory and --limit), a constructed allowlist
  environment, fixed ANSIBLE_* hardening settings, stdin closed and the marker
  the playbooks require.

The marker is an accident guard only: anyone who can run ansible-playbook can
set it. Direct ansible-playbook invocation is unsupported.
"""

# ruff: noqa: E402
import sys

# Run as a script, Python puts the script's directory (symlinks resolved) first
# on sys.path, ahead of the standard library, so an untracked json.py or
# ansible/ beside the script would be imported before the checkout is verified.
# Drop that entry, whatever path spelling it has, before any other import.
if __name__ == "__main__" and not sys.flags.safe_path and sys.path:
    del sys.path[0]
sys.path[:] = [entry for entry in sys.path if entry not in ("", ".")]

import os  # already loaded from the standard library by site at start-up

# Refused before any further import. ANSIBLE_* covers ANSIBLE_CONFIG,
# KEEP_REMOTE_FILES, DEBUG, VERBOSITY, LOG_PATH, callback, strategy, plugin and
# library paths, REMOTE_TEMP and every other setting. The rest change what this
# interpreter, OpenSSL, glibc, uv or gcloud load: PYTHONPATH would shadow the
# next import and OPENSSL_CONF is read by the first hashlib import, so they are
# refused here, before either can happen.
REFUSED_ENV_PREFIXES = (
    "ANSIBLE_",
    "_ANSIBLE_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "OPENSSL_",
    "GCONV_",
    "GLIBC_",
    "CLOUDSDK_PYTHON",
)
REFUSED_ENV_NAMES = frozenset(
    {
        "DITTO_CODING_HOSTED_GUARDED_RUN",
        # Choose or download the interpreter itself; UV_PYTHON_INSTALL_DIR only
        # says where uv keeps managed interpreters and is allowed.
        "UV_PYTHON",
        "UV_PYTHON_INSTALL_MIRROR",
        "UV_PYPY_INSTALL_MIRROR",
        "UV_PYTHON_DOWNLOADS_JSON_URL",
        "UV_NO_VERIFY_HASHES",
        "UV_INSECURE_HOST",
        "UV_CONFIG_FILE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
HARMLESS_PYTHON_ENV = frozenset(
    {
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONHASHSEED",
    }
)


def _printable(name: str) -> str:
    """Show a name as is only when it cannot carry terminal escapes."""
    if name and all("!" <= c <= "~" for c in name):
        return name
    return ascii(name)


def refused_environment_names(environ) -> list[str]:
    return sorted(
        name
        for name in environ
        if name in REFUSED_ENV_NAMES
        or (name.startswith(REFUSED_ENV_PREFIXES) and name not in HARMLESS_PYTHON_ENV)
    )


if __name__ == "__main__":
    _early = refused_environment_names(os.environ)
    if _early:
        sys.stderr.write("coding-hosted-guarded-run: refused; nothing was run.\n")
        for _name in _early:
            sys.stderr.write(f"  - environment: {_printable(_name)} must be unset\n")
        sys.exit(2)

import hashlib
import json
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "ditto-coding-hosted-guarded-run/v1"
ANSIBLE_CORE_VERSION = "2.21.2"
SCRIPT_RELATIVE = "infra/scripts/coding-hosted-guarded-run.py"
LOCK_RELATIVE = SCRIPT_RELATIVE + ".lock"
ANSIBLE_RELATIVE = "infra/ansible"
SPEC_DIRECTORY = "guarded-runs"
# Every file under these is hashed against the reviewed tree: ansible loads from
# infra/ansible, and the guard and its lock live in infra/scripts.
VERIFIED_DIRECTORIES = ("infra/ansible", "infra/scripts")
INVENTORY = "inventory/gcp.yml"
MARKER_ENV = "DITTO_CODING_HOSTED_GUARDED_RUN"
# Plugin search paths are pointed here. It lies inside the verified tree, which
# may hold no untracked file, so it can never exist while a run is allowed.
NO_PLUGINS = ".guarded-run-no-plugins"

OPERATION_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
ROLE_VAR_RE = re.compile(r"coding_hosted_[a-z0-9_]+")
INPUT_ENV_RE = re.compile(r"DITTO_CODING_[A-Z0-9_]+")
LIMIT_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PLAYBOOK_RE = re.compile(r"playbooks/gcp-coding-hosted-[a-z0-9-]+[.]yml")
CONFIRMATION_RE = re.compile(r"[A-Z]+(?: [A-Z]+)*")

# ansible-core 2.21 treats a string as a possible template when it contains a
# variable, block or comment start string, or starts with a '#jinja2:' header,
# which can redefine those delimiters (and line statement prefixes) for the rest
# of the string. The header check is case-insensitive and positionless on
# purpose: it costs nothing and covers any later change to where it is honoured.
TEMPLATE_START_STRINGS = ("{{", "{%", "{#")
TEMPLATE_OVERRIDE_HEADER = "#jinja2"

# Variables copied from the operator's environment into ansible's. Everything
# else is dropped. The DITTO_CODING_* inputs an operation declares are added
# separately, after validation.
PASSTHROUGH_ENV = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "TERM",
        "TMPDIR",
        "NO_COLOR",
        "SSH_AUTH_SOCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # The ssh login name the group vars read.
        "GCP_OSLOGIN_USER",
        # How the IAP ProxyCommand's gcloud finds its configuration and account.
        "CLOUDSDK_CONFIG",
        "CLOUDSDK_ACTIVE_CONFIG_NAME",
        "CLOUDSDK_CORE_ACCOUNT",
        "CLOUDSDK_CORE_PROJECT",
    }
)
# LC_* for the UTF-8 locale ansible requires. Nothing else passes by prefix.
PASSTHROUGH_PREFIXES = ("LC_",)

PLUGIN_PATH_ENV = (
    "ANSIBLE_ACTION_PLUGINS",
    "ANSIBLE_BECOME_PLUGINS",
    "ANSIBLE_CACHE_PLUGINS",
    "ANSIBLE_CALLBACK_PLUGINS",
    "ANSIBLE_CLICONF_PLUGINS",
    "ANSIBLE_CONNECTION_PLUGINS",
    "ANSIBLE_DOC_FRAGMENT_PLUGINS",
    "ANSIBLE_FILTER_PLUGINS",
    "ANSIBLE_HTTPAPI_PLUGINS",
    "ANSIBLE_INVENTORY_PLUGINS",
    "ANSIBLE_LIBRARY",
    "ANSIBLE_LOOKUP_PLUGINS",
    "ANSIBLE_MODULE_UTILS",
    "ANSIBLE_NETCONF_PLUGINS",
    "ANSIBLE_STRATEGY_PLUGINS",
    "ANSIBLE_TERMINAL_PLUGINS",
    "ANSIBLE_TEST_PLUGINS",
    "ANSIBLE_VARS_PLUGINS",
)

SPEC_KEYS = frozenset(
    {
        "schema",
        "operation",
        "playbook",
        "limit",
        "enabled_var",
        "confirmation_var",
        "confirmation",
        "revision_var",
        "nonsecret_env_vars",
        "secret_env",
        "distinct_secret_values",
        "forbidden_env",
        "forbidden_env_prefixes",
    }
)
NONSECRET_KEYS = frozenset({"env", "var", "pattern", "max_length"})
SECRET_KEYS = frozenset({"name", "charset", "prefix", "min_length", "max_length"})
CHARSETS = frozenset({"printable_ascii", "single_line"})


class Refusal(Exception):
    """A refusal whose message is fixed text plus names, never a value."""

    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


@dataclass(frozen=True)
class SecretInput:
    name: str
    charset: str
    prefix: str
    min_length: int
    max_length: int


@dataclass(frozen=True)
class NonsecretInput:
    env: str
    var: str
    pattern: re.Pattern[str]
    max_length: int


@dataclass(frozen=True)
class Spec:
    operation: str
    playbook: str
    limit: str
    enabled_var: str
    confirmation_var: str
    confirmation: str
    revision_var: str
    nonsecret_env_vars: tuple[NonsecretInput, ...]
    secret_env: tuple[SecretInput, ...]
    distinct_secret_values: bool
    forbidden_env: frozenset[str]
    forbidden_env_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    extra_vars: dict[str, Any]


USAGE = (
    "usage: uv run --locked --script infra/scripts/coding-hosted-guarded-run.py "
    "OPERATION REVIEWED_REVISION"
)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_arguments(argv: Sequence[str]) -> tuple[str, str]:
    if len(argv) != 2 or any(not isinstance(a, str) for a in argv):
        raise Refusal(["expected exactly OPERATION and REVIEWED_REVISION"])
    operation, revision = argv
    reasons = []
    for label, value in (("operation", operation), ("revision", revision)):
        if value.startswith("-"):
            reasons.append(f"{label}: options are not accepted")
    if not reasons:
        if len(operation) > 64 or not OPERATION_RE.fullmatch(operation):
            reasons.append("operation: not a lowercase hyphenated name")
        if not REVISION_RE.fullmatch(revision):
            reasons.append("revision: not 40 lowercase hex characters")
    if reasons:
        raise Refusal(reasons)
    return operation, revision


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Refusal([f"spec: duplicate key {key!r}"])
        result[key] = value
    return result


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal([f"spec: {reason}"])


def _int_field(obj: Mapping[str, Any], key: str, low: int, high: int) -> int:
    value = obj.get(key)
    _require(
        type(value) is int and low <= value <= high,
        f"{key} must be an integer in [{low}, {high}]",
    )
    return int(value)


def load_spec(ansible_dir: Path, operation: str) -> Spec:
    path = ansible_dir / SPEC_DIRECTORY / f"{operation}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Refusal(
            [f"operation: no spec {SPEC_DIRECTORY}/{operation}.json"]
        ) from None
    except (OSError, UnicodeDecodeError):
        raise Refusal([f"operation: spec {operation}.json is unreadable"]) from None
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError:
        raise Refusal([f"spec: {operation}.json is not valid JSON"]) from None
    _require(isinstance(raw, dict), "top level must be an object")
    _require(set(raw) == SPEC_KEYS, "keys must be exactly the v1 schema keys")
    _require(raw["schema"] == SCHEMA, "unsupported schema")
    _require(raw["operation"] == operation, "operation must equal the file name")
    for key in ("playbook", "limit", "confirmation"):
        _require(isinstance(raw[key], str), f"{key} must be a string")
    _require(bool(PLAYBOOK_RE.fullmatch(raw["playbook"])), "playbook path")
    _require(bool(LIMIT_RE.fullmatch(raw["limit"])), "limit must be one host name")
    _require(
        bool(CONFIRMATION_RE.fullmatch(raw["confirmation"]))
        and len(raw["confirmation"]) <= 128,
        "confirmation must be upper-case words",
    )
    role_vars = []
    for key in ("enabled_var", "confirmation_var", "revision_var"):
        _require(
            isinstance(raw[key], str) and bool(ROLE_VAR_RE.fullmatch(raw[key])),
            f"{key} must be a coding_hosted_* variable name",
        )
        role_vars.append(raw[key])

    nonsecret: list[NonsecretInput] = []
    _require(isinstance(raw["nonsecret_env_vars"], list), "nonsecret_env_vars list")
    for item in raw["nonsecret_env_vars"]:
        _require(
            isinstance(item, dict) and set(item) == NONSECRET_KEYS,
            "nonsecret_env_vars entries need exactly env, var, pattern, max_length",
        )
        _require(
            isinstance(item["env"], str) and bool(INPUT_ENV_RE.fullmatch(item["env"])),
            "nonsecret env must be a DITTO_CODING_* name",
        )
        _require(
            isinstance(item["var"], str) and bool(ROLE_VAR_RE.fullmatch(item["var"])),
            "nonsecret var must be a coding_hosted_* variable name",
        )
        _require(isinstance(item["pattern"], str), "nonsecret pattern must be text")
        try:
            pattern = re.compile(item["pattern"])
        except re.error:
            raise Refusal(["spec: nonsecret pattern does not compile"]) from None
        max_length = _int_field(item, "max_length", 1, 256)
        nonsecret.append(NonsecretInput(item["env"], item["var"], pattern, max_length))
        role_vars.append(item["var"])
    _require(len(set(role_vars)) == len(role_vars), "extra var names must differ")

    secrets: list[SecretInput] = []
    _require(isinstance(raw["secret_env"], list), "secret_env must be a list")
    for item in raw["secret_env"]:
        _require(
            isinstance(item, dict) and set(item) == SECRET_KEYS,
            "secret_env entries need exactly the v1 keys",
        )
        _require(
            isinstance(item["name"], str)
            and bool(INPUT_ENV_RE.fullmatch(item["name"])),
            "secret env must be a DITTO_CODING_* name",
        )
        _require(item["charset"] in CHARSETS, "secret charset")
        _require(
            isinstance(item["prefix"], str)
            and all("!" <= c <= "~" for c in item["prefix"])
            and not any(m in item["prefix"] for m in TEMPLATE_START_STRINGS),
            "secret prefix must be printable ASCII",
        )
        min_length = _int_field(item, "min_length", 1, 65536)
        max_length = _int_field(item, "max_length", min_length, 65536)
        secrets.append(
            SecretInput(
                item["name"], item["charset"], item["prefix"], min_length, max_length
            )
        )

    _require(
        isinstance(raw["distinct_secret_values"], bool), "distinct_secret_values bool"
    )
    for key in ("forbidden_env", "forbidden_env_prefixes"):
        _require(
            isinstance(raw[key], list)
            and all(
                isinstance(n, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", n)
                for n in raw[key]
            ),
            f"{key} must list environment variable names",
        )
    input_names = [s.name for s in secrets] + [n.env for n in nonsecret]
    _require(len(set(input_names)) == len(input_names), "input env names must differ")
    forbidden = frozenset(raw["forbidden_env"])
    forbidden_prefixes = tuple(raw["forbidden_env_prefixes"])
    _require(
        not any(
            name in forbidden or name.startswith(forbidden_prefixes)
            for name in input_names
        ),
        "an input env name is also forbidden",
    )
    return Spec(
        operation=operation,
        playbook=raw["playbook"],
        limit=raw["limit"],
        enabled_var=raw["enabled_var"],
        confirmation_var=raw["confirmation_var"],
        confirmation=raw["confirmation"],
        revision_var=raw["revision_var"],
        nonsecret_env_vars=tuple(nonsecret),
        secret_env=tuple(secrets),
        distinct_secret_values=raw["distinct_secret_values"],
        forbidden_env=forbidden,
        forbidden_env_prefixes=forbidden_prefixes,
    )


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def contains_template_syntax(value: str) -> bool:
    return any(marker in value for marker in TEMPLATE_START_STRINGS) or (
        TEMPLATE_OVERRIDE_HEADER in value.lower()
    )


def _has_surrogate(value: str) -> bool:
    # os.environ decodes undecodable bytes to lone surrogates.
    return any("\ud800" <= c <= "\udfff" for c in value)


def classify_secret(value: str | None, spec: SecretInput) -> str | None:
    """Return a failure class for a protected input, or None. Never raises."""
    if value is None:
        return "missing"
    if value == "":
        return "empty"
    if _has_surrogate(value):
        return "invalid_encoding"
    if contains_template_syntax(value):
        return "template_syntax"
    if spec.charset == "printable_ascii":
        if any(not ("!" <= c <= "~") for c in value):
            return "not_printable_ascii_without_whitespace"
    else:
        # Cc includes C0, DEL and C1 (NEL); Cf, Zl and Zp are invisible format
        # and line or paragraph separators YAML or splitlines treat as breaks.
        if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
            return "control_character"
        if value != value.strip():
            return "surrounding_whitespace"
    if len(value) < spec.min_length:
        return "too_short"
    if len(value) > spec.max_length:
        return "too_long"
    if spec.prefix and (
        not value.startswith(spec.prefix) or len(value) <= len(spec.prefix)
    ):
        return "missing_prefix"
    return None


def classify_nonsecret(value: str | None, spec: NonsecretInput) -> str | None:
    if value is None:
        return "missing"
    if value == "":
        return "empty"
    if _has_surrogate(value):
        return "invalid_encoding"
    if contains_template_syntax(value):
        return "template_syntax"
    if len(value) > spec.max_length:
        return "too_long"
    if not spec.pattern.fullmatch(value):
        return "pattern_mismatch"
    return None


def check_base_environment(environ: Mapping[str, str]) -> None:
    """Refuse settings that change what git, python or ansible load or log."""
    reasons = [
        f"environment: {_printable(name)} must be unset"
        for name in refused_environment_names(dict(environ))
    ]
    path = environ.get("PATH", "")
    if not path:
        reasons.append("environment: PATH is empty")
    elif any(not entry.startswith("/") for entry in path.split(":")):
        reasons.append("environment: PATH has an empty or relative entry")
    if not environ.get("HOME", "").startswith("/"):
        reasons.append("environment: HOME must be an absolute path")
    login = environ.get("GCP_OSLOGIN_USER")
    if login is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", login):
        reasons.append("environment: GCP_OSLOGIN_USER is not a login name")
    # Passed-through values reach ansible too (group vars read GCP_OSLOGIN_USER),
    # so none may carry template syntax, whatever ansible does with lookups.
    for name in sorted(environ):
        if name in PASSTHROUGH_ENV or name.startswith(PASSTHROUGH_PREFIXES):
            value = environ[name]
            if _has_surrogate(value) or contains_template_syntax(value):
                reasons.append(
                    f"environment: {_printable(name)}: template_syntax_or_encoding"
                )
    if reasons:
        raise Refusal(reasons)


def check_inputs(environ: Mapping[str, str], spec: Spec) -> None:
    """Validate the operation's inputs, reporting only names and classes."""
    reasons: list[str] = []
    for name in sorted(environ):
        if name in spec.forbidden_env or name.startswith(spec.forbidden_env_prefixes):
            reasons.append(
                f"environment: {_printable(name)} is forbidden for {spec.operation}"
            )
    for secret in spec.secret_env:
        failure = classify_secret(environ.get(secret.name), secret)
        if failure:
            reasons.append(f"input: {secret.name}: {failure}")
    for item in spec.nonsecret_env_vars:
        failure = classify_nonsecret(environ.get(item.env), item)
        if failure:
            reasons.append(f"input: {item.env}: {failure}")
    if spec.distinct_secret_values and not reasons:
        seen: dict[str, str] = {}
        for secret in spec.secret_env:
            value = environ[secret.name]
            if value in seen:
                reasons.append(
                    f"input: {secret.name}: duplicate_value (same as {seen[value]})"
                )
            else:
                seen[value] = secret.name
    if reasons:
        raise Refusal(reasons)


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def _git_runner(root: Path, environ: Mapping[str, str]) -> Callable[..., bytes]:
    git = shutil.which("git", path=environ.get("PATH", ""))
    if not git or not os.path.isabs(git):
        raise Refusal(["checkout: git is not on PATH"])
    # Only these reach git: no GIT_* variable, system or global config can
    # redirect the repository, index, objects or replace refs.
    git_env = {
        "HOME": environ.get("HOME", "/nonexistent"),
        "PATH": environ.get("PATH", ""),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }

    def run(*args: str) -> bytes:
        completed = subprocess.run(
            [
                git,
                "-C",
                str(root),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *args,
            ],
            env=git_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise Refusal([f"checkout: git {args[0]} failed"])
        return completed.stdout

    return run


def _blob_digest(data: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest()


def verify_checkout(root: Path, revision: str, environ: Mapping[str, str]) -> None:
    git = _git_runner(root, environ)
    toplevel = git("rev-parse", "--show-toplevel").decode().strip()
    if Path(toplevel).resolve() != root.resolve():
        raise Refusal(["checkout: the guard is not at the repository root"])
    head = git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if head != revision:
        raise Refusal(["checkout: HEAD is not the reviewed revision"])
    object_format = git("rev-parse", "--show-object-format").decode().strip()
    if object_format not in ("sha1", "sha256"):
        raise Refusal(["checkout: unsupported object format"])
    reasons: list[str] = []
    if git("status", "--porcelain=v1", "-z", "--untracked-files=no"):
        reasons.append("checkout: tracked files differ from HEAD")

    # status trusts the index's stat cache, assume-unchanged and skip-worktree
    # bits and clean filters, so every file ansible can load is hashed here,
    # without filters, against the reviewed tree itself.
    listing = git(
        "ls-tree", "-r", "-z", "--full-tree", revision, "--", *VERIFIED_DIRECTORIES
    )
    expected: dict[str, tuple[str, str]] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind, oid = meta.decode().split(" ")
        if kind != "blob":
            raise Refusal(["checkout: a submodule is inside the verified tree"])
        expected[name.decode("utf-8", "surrogateescape")] = (mode, oid)
    for required in (SCRIPT_RELATIVE, LOCK_RELATIVE):
        if required not in expected:
            raise Refusal([f"checkout: {required} is not in the reviewed revision"])

    tracked_directories = {
        str(parent)
        for relative in expected
        for parent in Path(relative).parents
        if str(parent) != "."
    }

    def unwalkable(_error: OSError) -> None:
        # os.walk skips a directory it cannot list, yet ansible can still open a
        # known path inside an execute-only one, so any listing error refuses.
        raise Refusal(["checkout: a directory in the verified tree is unreadable"])

    actual: set[str] = set()
    for verified in VERIFIED_DIRECTORIES:
        top = root / verified
        if top.is_symlink() or not top.is_dir():
            raise Refusal([f"checkout: {verified} is not a real directory"])
        for directory, dirnames, filenames in os.walk(top, onerror=unwalkable):
            relative_directory = os.path.relpath(directory, root)
            if relative_directory not in tracked_directories:
                reasons.append(
                    "checkout: untracked or ignored directory "
                    + _printable(relative_directory)
                )
            if not os.access(directory, os.R_OK | os.X_OK):
                raise Refusal(
                    ["checkout: a directory in the verified tree is unreadable"]
                )
            for name in list(dirnames):
                if os.path.islink(os.path.join(directory, name)):
                    filenames.append(name)
                    dirnames.remove(name)
            for name in filenames:
                actual.add(os.path.relpath(os.path.join(directory, name), root))
    for relative in sorted(actual - set(expected)):
        reasons.append(f"checkout: untracked or ignored file {_printable(relative)}")
    for relative in sorted(set(expected) - actual):
        reasons.append(f"checkout: missing file {_printable(relative)}")
    for relative in sorted(actual & set(expected)):
        mode, oid = expected[relative]
        path = root / relative
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(path))
            matches = mode == "120000"
        elif stat.S_ISREG(info.st_mode):
            data = path.read_bytes()
            executable = bool(info.st_mode & stat.S_IXUSR)
            matches = mode == ("100755" if executable else "100644")
        else:
            data, matches = b"", False
        if not matches or _blob_digest(data, object_format) != oid:
            reasons.append(
                f"checkout: {_printable(relative)} differs from the reviewed revision"
            )
    if reasons:
        raise Refusal(reasons)


# ---------------------------------------------------------------------------
# Ansible runtime and invocation
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def verify_ansible_runtime(root: Path) -> str:
    """Require exactly the locked distributions in this interpreter's own venv.

    uv run --locked --script installs the lock beside this script. Without
    --locked, with --with, or under another interpreter the installed set or a
    version differs, and the run is refused. Code a --with package ran at start
    up is detected here, not prevented.
    """
    import importlib.metadata

    if sys.prefix == sys.base_prefix:
        raise Refusal(["runtime: run through uv run --locked --script"])
    try:
        lock = tomllib.loads((root / LOCK_RELATIVE).read_text(encoding="utf-8"))
        locked = {
            _normalise(package["name"]): package["version"]
            for package in lock["package"]
        }
    except (OSError, ValueError, KeyError, TypeError):
        raise Refusal(["runtime: the script lock is unreadable"]) from None
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = _normalise(distribution.metadata["Name"] or "")
        if name in installed and installed[name] != distribution.version:
            raise Refusal([f"runtime: {name} is installed twice"])
        installed[name] = distribution.version
    reasons = []
    for name in sorted(set(installed) - set(locked)):
        reasons.append(f"runtime: {name} is not in the script lock")
    for name, version in sorted(locked.items()):
        if installed.get(name, version) != version:
            reasons.append(f"runtime: {name} is not the locked version")
    if locked.get("ansible-core") != ANSIBLE_CORE_VERSION:
        reasons.append(
            f"runtime: the lock must pin ansible-core {ANSIBLE_CORE_VERSION}"
        )
    if reasons:
        raise Refusal(reasons)
    try:
        import ansible  # type: ignore[import-not-found]
        import ansible.release  # type: ignore[import-not-found]
    except ImportError:
        raise Refusal(["runtime: ansible-core is not installed"]) from None
    try:
        # The google.cloud.gcp_compute inventory plugin needs both; without them
        # ansible parses no inventory and would match no host.
        import google.auth  # type: ignore[import-not-found]  # noqa: F401
        import requests  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        raise Refusal(["runtime: google-auth and requests are not installed"]) from None
    location = Path(ansible.__file__).resolve()
    if not location.is_relative_to(Path(sys.prefix).resolve()):
        raise Refusal(["runtime: ansible-core is not from the locked environment"])
    if ansible.release.__version__ != ANSIBLE_CORE_VERSION:
        raise Refusal([f"runtime: ansible-core must be {ANSIBLE_CORE_VERSION}"])
    return sys.executable


def build_invocation(
    root: Path,
    spec: Spec,
    revision: str,
    environ: Mapping[str, str],
    python: str,
) -> Invocation:
    ansible_dir = root / ANSIBLE_RELATIVE
    extra_vars: dict[str, Any] = {
        spec.enabled_var: True,
        spec.confirmation_var: spec.confirmation,
        spec.revision_var: revision,
    }
    for item in spec.nonsecret_env_vars:
        extra_vars[item.var] = environ[item.env]
    for value in extra_vars.values():
        if isinstance(value, str) and contains_template_syntax(value):
            raise Refusal(["internal: a constructed extra var has template syntax"])

    env = {
        name: value
        for name, value in environ.items()
        if name in PASSTHROUGH_ENV or name.startswith(PASSTHROUGH_PREFIXES)
    }
    for secret in spec.secret_env:
        env[secret.name] = environ[secret.name]
    no_plugins = str(ansible_dir / NO_PLUGINS)
    env |= dict.fromkeys(PLUGIN_PATH_ENV, no_plugins)
    env |= {
        "ANSIBLE_CONFIG": str(ansible_dir / "ansible.cfg"),
        "ANSIBLE_ROLES_PATH": str(ansible_dir / "roles"),
        "ANSIBLE_KEEP_REMOTE_FILES": "False",
        "ANSIBLE_DEBUG": "False",
        "ANSIBLE_VERBOSITY": "0",
        "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "False",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        # An inventory that fails to parse, or a --limit that matches no host,
        # must fail the run instead of skipping every play and exiting 0.
        "ANSIBLE_INVENTORY_UNPARSED_FAILED": "True",
        "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED": "True",
        "ANSIBLE_HOST_PATTERN_MISMATCH": "error",
        MARKER_ENV: spec.operation,
    }
    argv = [
        python,
        "-I",
        "-m",
        "ansible.cli.playbook",
        "-i",
        str(ansible_dir / INVENTORY),
        "--limit",
        spec.limit,
        "-e",
        json.dumps(extra_vars, sort_keys=True),
        str(ansible_dir / spec.playbook),
    ]
    return Invocation(argv=argv, env=env, cwd=str(ansible_dir), extra_vars=extra_vars)


def _default_runner(invocation: Invocation) -> int:
    os.umask(0o077)
    # A fresh ssh ControlPath directory per run, so no master connection left
    # by an earlier ssh or ansible session is reused.
    with tempfile.TemporaryDirectory(prefix="guarded-run-ssh-") as control:
        completed = subprocess.run(
            invocation.argv,
            env={**invocation.env, "ANSIBLE_SSH_CONTROL_PATH_DIR": control},
            cwd=invocation.cwd,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    return completed.returncode


def run(
    argv: Sequence[str],
    environ: Mapping[str, str],
    *,
    root: Path,
    runner: Callable[[Invocation], int] = _default_runner,
    runtime: Callable[[Path], str] = verify_ansible_runtime,
    out: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
) -> int:
    try:
        operation, revision = parse_arguments(argv)
        check_base_environment(environ)
        verify_checkout(root, revision, environ)
        spec = load_spec(root / ANSIBLE_RELATIVE, operation)
        check_inputs(environ, spec)
        if (root / ANSIBLE_RELATIVE / NO_PLUGINS).exists():
            raise Refusal(["checkout: the empty plugin path exists"])
        python = runtime(root)
        invocation = build_invocation(root, spec, revision, environ, python)
    except Refusal as refusal:
        out("coding-hosted-guarded-run: refused; nothing was run.")
        for reason in refusal.reasons:
            out(f"  - {reason}")
        out(USAGE)
        return 2
    out(
        f"coding-hosted-guarded-run: operation={spec.operation} "
        f"revision={revision} playbook={spec.playbook} limit={spec.limit}"
    )
    out(
        "coding-hosted-guarded-run: validated inputs (values not shown): "
        + (", ".join(s.name for s in spec.secret_env) or "none")
    )
    out(
        "coding-hosted-guarded-run: extra vars: "
        + json.dumps(invocation.extra_vars, sort_keys=True)
    )
    return runner(invocation)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        return run(sys.argv[1:], dict(os.environ), root=root)
    except KeyboardInterrupt:
        print("coding-hosted-guarded-run: interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # never echo a message or a traceback
        print(
            f"coding-hosted-guarded-run: internal error ({type(error).__name__});"
            " nothing further was run.",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
