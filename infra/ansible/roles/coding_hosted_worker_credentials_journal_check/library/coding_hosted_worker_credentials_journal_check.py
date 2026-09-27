#!/usr/bin/python
"""Redacted first-run log spot-check for the native Coding worker credentials.

Root runs this module once, after the first materialization, through a no_log
task. It takes no secret parameter. It opens the three worker credential files
at their fixed path through pinned directory descriptors (``O_NOFOLLOW`` from
``/``), derives search needles from the values in memory, and scans:

* the whole systemd journal in export format, which carries every field of every
  entry (``MESSAGE`` and also ``_CMDLINE``, ``SYSLOG_IDENTIFIER`` and the rest);
* the plain and gzip-rotated text logs in ``/var/log`` an rsyslog or sudo
  configuration may write;
* Ansible remote temp locations, where a module file carrying arguments would
  persist if pipelining were ever off or a run were interrupted.

It looks for forbidden names (controller input variables, runtime key names,
credential file names, role input and capture names, the confirmation, and the
write module's AnsiballZ file name) and for value fingerprints: each raw value,
its JSON-escaped, doubly JSON-escaped, backslash-doubled, repr, URL-encoded,
hex and base64 (every byte alignment, standard and URL-safe) forms, and the
MD5, SHA-1, SHA-256 and SHA-512 hex digests of every value and every file.

It returns only integers and booleans per source and per check class. It never
returns, logs or writes a matching line, a value, a needle or a digest, keeps
the stream in memory only, and reports an internal failure by exception class.
"""

import base64
import errno
import gzip
import hashlib
import json
import os
import pwd
import stat
import subprocess
import urllib.parse

DOCUMENTATION = r"""
module: coding_hosted_worker_credentials_journal_check
short_description: Count credential names and value fingerprints in host logs
description:
  - Reads the worker credential files through pinned descriptors, derives needles
    in memory, and scans the journal, text logs and Ansible temp locations.
    Returns only per-class counts and booleans, never a value, line or digest.
options:
  private_dir:
    description: Absolute worker private directory holding the three files.
    type: str
    required: true
  owner:
    description: The worker account that must own the files.
    type: str
    required: true
  journal_argv:
    description: Absolute argv that writes the whole journal in export format.
    type: list
    elements: str
    required: true
  log_dir:
    description: Directory of text logs to scan, not recursively.
    type: str
    required: true
  log_prefixes:
    description: File name prefixes of the text logs to scan.
    type: list
    elements: str
    required: true
  temp_dirs:
    description: Ansible remote temp directories, or shared temp directories.
    type: list
    elements: str
    required: true
  home_parent:
    description: Directory whose children's .ansible/tmp directories are scanned.
    type: str
    required: true
"""

NAMES = ("hippius-environment.json", "image-storage.json", "provider-key")
PRIVATE = "private"
HIPPIUS_VALUE_KEYS = (
    "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY",
    "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY",
    "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY",
    "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY",
    "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY",
)
IMAGE_VALUE_KEYS = ("access_key", "secret_key")

FORBIDDEN_NAMES = {
    "env_name": (
        "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY",
        "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY",
        "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY",
        "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY",
        "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY",
        "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY",
        "DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY",
        "DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY",
        "DITTO_CODING_WORKER_PROVIDER_KEY",
    ),
    "runtime_key_name": (
        *HIPPIUS_VALUE_KEYS,
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY",
    ),
    "file_name": (
        "hippius-environment.json",
        "image-storage.json",
        "private/provider-key",
    ),
    "input_name": (
        "coding_hosted_worker_credentials_enabled",
        "coding_hosted_worker_credentials_confirmation",
        "coding_hosted_worker_credentials_source_revision",
        "coding_hosted_worker_credentials_gate_confirmation",
        "coding_hosted_worker_credentials_gate_source_revision",
        "coding_hosted_worker_credentials_secrets",
        "coding_hosted_worker_credentials_documents",
        "MATERIALIZE NATIVE CODING WORKER CREDENTIALS",
    ),
    "module_temp_file": ("AnsiballZ_coding_hosted_worker_credentials_write",),
}
VALUE_CLASSES = ("value_raw", "value_encoded", "value_digest")
CLASSES = (*VALUE_CLASSES, *FORBIDDEN_NAMES)
# Short needles would match unrelated bytes; a value shorter than this is
# counted, not searched.
MINIMUM_RAW = 8
MINIMUM_ENCODED = 12
CHUNK = 4 * 1024 * 1024
MAX_TEMP_FILE = 64 * 1024 * 1024
MAX_TEMP_DEPTH = 4
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Unsafe(Exception):
    """A value-free diagnostic: names and states only."""


# ---------------------------------------------------------------------------
# Reading the credential files
# ---------------------------------------------------------------------------


def _open_private(private_dir):
    if not isinstance(private_dir, str) or not private_dir.startswith("/"):
        raise Unsafe("the private directory path is not absolute")
    if os.path.normpath(private_dir) != private_dir:
        raise Unsafe("the private directory path is not normalised")
    components = private_dir.split("/")[1:]
    if len(components) < 2 or components[-1] != PRIVATE:
        raise Unsafe("the private directory path does not end in /private")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in components:
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise Unsafe("a private directory component is a symlink") from None
                if error.errno == errno.ENOENT:
                    raise Unsafe("the private directory does not exist") from None
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_documents(private_dir, owner_uid):
    """Return the three files' bytes, requiring regular single-link worker files."""
    dir_fd = _open_private(private_dir)
    documents = {}
    try:
        for name in NAMES:
            try:
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
                )
            except OSError as error:
                if error.errno == errno.ENOENT:
                    raise Unsafe(f"{name} is absent") from None
                if error.errno == errno.ELOOP:
                    raise Unsafe(f"{name} is a symlink") from None
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise Unsafe(f"{name} is not a regular single-link file")
                if info.st_uid != owner_uid:
                    raise Unsafe(f"{name} is not owned by the worker")
                if info.st_size > 1024 * 1024:
                    raise Unsafe(f"{name} is larger than a credential file")
                chunks = []
                while chunk := os.read(fd, 65536):
                    chunks.append(chunk)
                documents[name] = b"".join(chunks)
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return documents


def extract_values(documents):
    """Return the eight credential values from the three documents."""
    try:
        hippius = json.loads(documents["hippius-environment.json"])
        image = json.loads(documents["image-storage.json"])
    except ValueError:
        raise Unsafe("a credential document is not JSON") from None
    if not isinstance(hippius, dict) or not isinstance(image, dict):
        raise Unsafe("a credential document is not a JSON object")
    values = []
    for document, keys, label in (
        (hippius, HIPPIUS_VALUE_KEYS, "hippius-environment.json"),
        (image, IMAGE_VALUE_KEYS, "image-storage.json"),
    ):
        for key in keys:
            value = document.get(key)
            if not isinstance(value, str) or not value:
                raise Unsafe(f"{label} lacks an expected credential field")
            values.append(value.encode("utf-8"))
    provider = documents["provider-key"]
    if not provider:
        raise Unsafe("provider-key is empty")
    values.append(provider)
    return values


# ---------------------------------------------------------------------------
# Needles
# ---------------------------------------------------------------------------


def _base64_fragments(value):
    """Encoded bytes that depend only on ``value``, at every stream alignment."""
    fragments = []
    for pad in range(3):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(b"\0" * pad + value)
            start = (pad * 4 + 2) // 3
            end = ((pad + len(value)) // 3) * 4
            fragments.append(encoded[start:end])
    return fragments


def _encoded_forms(value):
    text = value.decode("utf-8", "surrogateescape")
    json_escaped = json.dumps(text)[1:-1]
    forms = [
        json_escaped,
        json.dumps(json_escaped)[1:-1],
        text.replace("\\", "\\\\"),
        repr(text)[1:-1],
        urllib.parse.quote(text, safe=""),
        urllib.parse.quote_plus(text, safe=""),
    ]
    encoded = [f.encode("utf-8", "surrogateescape") for f in forms]
    encoded += [value.hex().encode(), value.hex().upper().encode()]
    encoded += _base64_fragments(value)
    return encoded


def _digests(payload):
    return [
        hashlib.new(algorithm, payload).hexdigest().encode()
        for algorithm in ("md5", "sha1", "sha256", "sha512")
    ]


def build_needles(documents, values):
    """Map each check class to its unique needles, and count skipped values."""
    needles = {name: set() for name in CLASSES}
    too_short = 0
    for value in values:
        if len(value) < MINIMUM_RAW:
            too_short += 1
        else:
            needles["value_raw"].add(value)
        for form in _encoded_forms(value):
            if len(form) >= MINIMUM_ENCODED and form != value:
                needles["value_encoded"].add(form)
        needles["value_digest"].update(_digests(value))
    for document in documents.values():
        needles["value_digest"].update(_digests(document))
    for name, literals in FORBIDDEN_NAMES.items():
        needles[name].update(literal.encode() for literal in literals)
    raw = needles["value_raw"]
    # A form identical to another class's needle is counted once, in the
    # stronger class.
    needles["value_encoded"] -= raw
    needles["value_digest"] -= raw | needles["value_encoded"]
    return {name: sorted(found) for name, found in needles.items()}, too_short


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


class Scanner:
    """Count needle occurrences across chunk boundaries, keeping only a tail."""

    def __init__(self, needles):
        self._needles = [(name, n) for name, group in needles.items() for n in group]
        self._keep = max((len(n) for _, n in self._needles), default=1) - 1
        self._tail = b""
        self.counts = dict.fromkeys(CLASSES, 0)
        self.bytes = 0

    def feed(self, chunk):
        if not chunk:
            return
        self.bytes += len(chunk)
        buffer = self._tail + chunk
        tail_length = len(self._tail)
        for name, needle in self._needles:
            # Only matches that end inside the new chunk; earlier ones were
            # counted when their last byte arrived.
            start = max(0, tail_length - len(needle) + 1)
            self.counts[name] += buffer.count(needle, start)
        self._tail = buffer[-self._keep :] if self._keep else b""

    def scan(self, stream, chunk_size=CHUNK):
        while chunk := stream.read(chunk_size):
            self.feed(chunk)
        self.reset_boundary()

    def reset_boundary(self):
        # Separate sources never form a match across their boundary.
        self._tail = b""


def _merge(target, counts):
    for name, count in counts.items():
        target[name] += count


def scan_journal(argv, needles):
    source = {"scanned": False, "bytes": 0, "counts": dict.fromkeys(CLASSES, 0)}
    if not argv or not all(isinstance(a, str) for a in argv) or argv[0][:1] != "/":
        raise Unsafe("the journal command is not an absolute argv")
    scanner = Scanner(needles)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "SYSTEMD_COLORS": "0"},
        close_fds=True,
    )
    try:
        scanner.scan(process.stdout)
    finally:
        process.stdout.close()
        returncode = process.wait()
    source["scanned"] = returncode == 0
    source["bytes"] = scanner.bytes
    source["counts"] = scanner.counts
    return source


def _open_regular(dir_fd, name, max_size=None):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (max_size and info.st_size > max_size):
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def scan_text_logs(log_dir, prefixes, needles):
    source = {"files": 0, "bytes": 0, "counts": dict.fromkeys(CLASSES, 0)}
    try:
        dir_fd = os.open(log_dir, _DIR_FLAGS)
    except OSError:
        return source
    try:
        scanner = Scanner(needles)
        for name in sorted(os.listdir(dir_fd)):
            if not any(name.startswith(prefix) for prefix in prefixes):
                continue
            handle = _open_regular(dir_fd, name)
            if handle is None:
                continue
            with handle:
                stream = (
                    gzip.GzipFile(fileobj=handle) if name.endswith(".gz") else handle
                )
                try:
                    scanner.scan(stream)
                except (OSError, EOFError):
                    # A truncated rotation still counted what it held.
                    scanner.reset_boundary()
            source["files"] += 1
        source["bytes"] = scanner.bytes
        source["counts"] = scanner.counts
    finally:
        os.close(dir_fd)
    return source


def _is_ansible_entry(name):
    return name.startswith(("ansible", ".ansible", "AnsiballZ_"))


def _scan_tree(dir_fd, scanner, source, depth):
    for name in sorted(os.listdir(dir_fd)):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and depth < MAX_TEMP_DEPTH:
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
            except OSError:
                continue
            try:
                _scan_tree(child, scanner, source, depth + 1)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            handle = _open_regular(dir_fd, name, MAX_TEMP_FILE)
            if handle is None:
                continue
            with handle:
                scanner.scan(handle)
            source["files"] += 1


def scan_ansible_temp(temp_dirs, home_parent, needles):
    source = {
        "entries": 0,
        "files": 0,
        "bytes": 0,
        "counts": dict.fromkeys(CLASSES, 0),
    }
    scanner = Scanner(needles)
    # Everything inside an .ansible/tmp directory is Ansible's; in a shared temp
    # directory only Ansible-named entries are.
    locations = [
        (path, None if path.endswith("/.ansible/tmp") else _is_ansible_entry)
        for path in temp_dirs
    ]
    try:
        parent_fd = os.open(home_parent, _DIR_FLAGS)
    except OSError:
        parent_fd = None
    if parent_fd is not None:
        try:
            for home in sorted(os.listdir(parent_fd)):
                locations.append((f"{home_parent}/{home}/.ansible/tmp", None))
        finally:
            os.close(parent_fd)
    for path, accept in locations:
        # Every ancestor is opened without following links; a missing or linked
        # location has nothing Ansible left behind.
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            for component in [c for c in path.split("/") if c]:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
        except OSError:
            os.close(fd)
            continue
        try:
            for name in sorted(os.listdir(fd)):
                if accept is not None and not accept(name):
                    continue
                source["entries"] += 1
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        _scan_tree(child, scanner, source, 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    handle = _open_regular(fd, name, MAX_TEMP_FILE)
                    if handle is not None:
                        with handle:
                            scanner.scan(handle)
                        source["files"] += 1
        finally:
            os.close(fd)
    source["bytes"] = scanner.bytes
    source["counts"] = scanner.counts
    return source


def spot_check(params, owner_uid):
    documents = read_documents(params["private_dir"], owner_uid)
    values = extract_values(documents)
    needles, too_short = build_needles(documents, values)
    del documents, values
    journal = scan_journal(params["journal_argv"], needles)
    text_logs = scan_text_logs(params["log_dir"], params["log_prefixes"], needles)
    temp = scan_ansible_temp(params["temp_dirs"], params["home_parent"], needles)
    del needles
    totals = dict.fromkeys(CLASSES, 0)
    for source in (journal, text_logs, temp):
        _merge(totals, source["counts"])
    found = {name: count > 0 for name, count in totals.items()}
    clean = (
        journal["scanned"]
        and not any(found.values())
        and temp["entries"] == 0
        and too_short == 0
    )
    return {
        "changed": False,
        "sources": {"journal": journal, "text_logs": text_logs, "ansible_temp": temp},
        "totals": totals,
        "found": found,
        "values_checked": 8,
        "values_below_minimum_length": too_short,
        "clean": bool(clean),
    }


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "private_dir": {"type": "str", "required": True},
            "owner": {"type": "str", "required": True},
            "journal_argv": {"type": "list", "elements": "str", "required": True},
            "log_dir": {"type": "str", "required": True},
            "log_prefixes": {"type": "list", "elements": "str", "required": True},
            "temp_dirs": {"type": "list", "elements": "str", "required": True},
            "home_parent": {"type": "str", "required": True},
        },
        supports_check_mode=False,
    )
    try:
        owner_uid = pwd.getpwnam(module.params["owner"]).pw_uid
    except KeyError:
        module.fail_json(msg="Refused: the worker account does not exist.")
    try:
        result = spot_check(module.params, owner_uid)
    except Unsafe as error:
        module.fail_json(msg=f"Refused: {error}.")
    except Exception as error:  # never echo a message that could carry bytes
        module.fail_json(msg=f"Failed: {type(error).__name__}; nothing was reported.")
    module.exit_json(**result)


if __name__ == "__main__":
    main()
