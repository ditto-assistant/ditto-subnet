#!/usr/bin/python
"""Write the native Coding worker credential files through pinned directory fds.

Root runs this module. It receives the three file contents as a single no_log
dict parameter, so ansible redacts them from the target's module-invocation log
(the journal records VALUE_SPECIFIED_IN_NO_LOG_PARAMETER, not the secrets) and
from any -vvv controller output; nothing is passed in argv or on stdin. The
module never resolves the destination path as a string: it opens every component
from ``/`` with ``O_NOFOLLOW`` and ``O_DIRECTORY`` so the worker account cannot
swap a parent for a symlink between the role's inspection and the write, checks
the reader home and private directory on their descriptors, writes each file to
a tracked temporary with ``O_CREAT|O_EXCL|O_NOFOLLOW`` relative to the pinned
directory, ``fchown``/``fchmod``/``fsync``s it, renames every temporary into
place only after all three are written, and re-verifies each result on its own
descriptor. Every temporary it creates is unlinked on any failure. It returns
only non-secret metadata: filenames, mode, link count, size, owner — never a
value or a digest.
"""

import contextlib
import errno
import hashlib
import os
import pwd
import stat

DOCUMENTATION = r"""
module: coding_hosted_worker_credentials_write
short_description: Write the worker credential files without following links
description:
  - Opens every path component with O_NOFOLLOW, writes each file to a temporary
    with O_CREAT|O_EXCL and renames it into place only after all temporaries are
    written, then re-verifies each on its descriptor. Prints no value or digest.
options:
  private_dir:
    description: Absolute, normalised worker private directory.
    type: str
    required: true
  owner:
    description: The worker account that must own the home, private dir and files.
    type: str
    required: true
  source_revision:
    description: Non-secret reviewed source revision, echoed in the result.
    type: str
    required: false
    default: ""
  documents:
    description: Exactly the three fixed filenames mapped to their file contents.
    type: dict
    required: true
    no_log: true
"""

NAMES = ("hippius-environment.json", "image-storage.json", "provider-key")
PRIVATE = "private"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Unsafe(Exception):
    """A value-free diagnostic: names and states only."""


def _split(private_dir):
    if not isinstance(private_dir, str) or not private_dir.startswith("/"):
        raise Unsafe("the private directory path is not absolute")
    if os.path.normpath(private_dir) != private_dir or "//" in private_dir:
        raise Unsafe("the private directory path is not normalised")
    components = private_dir.split("/")[1:]
    if len(components) < 2 or components[-1] != PRIVATE:
        raise Unsafe("the private directory path does not end in /private")
    return components


def _require_reader_dir(fd, uid, gid, label, *, private):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise Unsafe(f"the {label} is not a directory")
    if info.st_uid != uid:
        raise Unsafe(f"the {label} is not owned by the worker")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Unsafe(f"the {label} is writable by group or others")
    if private and (info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o700):
        raise Unsafe("the private directory is not the worker's mode 0700 directory")


def _open_private(private_dir, uid, gid):
    components = _split(private_dir)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, component in enumerate(components):
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise Unsafe("a private directory component is a symlink") from None
                raise
            os.close(fd)
            fd = child
            if index == len(components) - 2:
                _require_reader_dir(fd, uid, gid, "worker home", private=False)
        _require_reader_dir(fd, uid, gid, "private directory", private=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _require_replaceable(dir_fd, name, uid):
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise Unsafe(f"{name} exists and is not a regular file")
    if info.st_nlink != 1:
        raise Unsafe(f"{name} exists with more than one hard link")
    if info.st_uid != uid:
        raise Unsafe(f"{name} exists and is owned by another account")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise Unsafe(f"{name} exists and is not mode 0600; reconcile it by hand")


def _write_temp(dir_fd, tmp, content, uid, gid):
    # The caller tracks `tmp` before this runs, so a create that then fails to
    # fill (for example a write past a file-size ulimit) leaves a tracked temp
    # the caller unlinks, never an orphaned partial secret.
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        written = 0
        view = memoryview(content)
        while written < len(content):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify(dir_fd, name, content, uid, gid):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Unsafe(f"{name} is not a regular file after rename")
        if info.st_nlink != 1:
            raise Unsafe(f"{name} has more than one hard link after rename")
        if info.st_uid != uid or info.st_gid != gid:
            raise Unsafe(f"{name} is not owned by the worker after rename")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise Unsafe(f"{name} is not mode 0600 after rename")
        if info.st_size != len(content):
            raise Unsafe(f"{name} size differs after rename")
        body = b""
        while chunk := os.read(fd, 65536):
            body += chunk
    finally:
        os.close(fd)
    if hashlib.sha256(body).digest() != hashlib.sha256(content).digest():
        raise Unsafe(f"{name} content differs after rename")
    return {
        "name": name,
        "mode": "0600",
        "nlink": info.st_nlink,
        "size": info.st_size,
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def write_all(private_dir, owner_uid, owner_gid, contents, *, check_mode=False):
    dir_fd = _open_private(private_dir, owner_uid, owner_gid)
    temps = {}
    renamed = []
    try:
        for name in NAMES:
            _require_replaceable(dir_fd, name, owner_uid)
        if check_mode:
            return {"changed": True, "files": [], "check_mode": True}
        for name in NAMES:
            # Track the temp name before the create/write, so a failure part-way
            # through writing still leaves a tracked temp the finally unlinks.
            tmp = f".{name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
            temps[name] = tmp
            _write_temp(dir_fd, tmp, contents[name], owner_uid, owner_gid)
        try:
            for name in NAMES:
                os.rename(temps[name], name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                renamed.append(name)
                del temps[name]
            os.fsync(dir_fd)
        except OSError:
            not_renamed = [n for n in NAMES if n not in renamed]
            raise Unsafe(
                "partial write: replaced="
                + repr(renamed)
                + " unchanged_or_unknown="
                + repr(not_renamed)
                + "; reconcile by hand"
            ) from None
        return {
            "changed": True,
            "files": [
                _verify(dir_fd, n, contents[n], owner_uid, owner_gid) for n in NAMES
            ],
        }
    finally:
        for tmp in temps.values():
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
        os.close(dir_fd)


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "private_dir": {"type": "str", "required": True},
            "owner": {"type": "str", "required": True},
            "source_revision": {"type": "str", "required": False, "default": ""},
            "documents": {"type": "dict", "required": True, "no_log": True},
        },
        supports_check_mode=True,
    )
    documents = module.params["documents"]
    if set(documents) != set(NAMES):
        module.fail_json(
            msg="Refused: documents must carry exactly the three fixed filenames."
        )
    contents = {}
    for name, value in documents.items():
        if not isinstance(value, str) or not value:
            module.fail_json(msg=f"Refused: {name} content is empty or not a string.")
        contents[name] = value.encode("utf-8")
    try:
        account = pwd.getpwnam(module.params["owner"])
    except KeyError:
        module.fail_json(msg="Refused: the worker account does not exist.")
    try:
        result = write_all(
            module.params["private_dir"],
            account.pw_uid,
            account.pw_gid,
            contents,
            check_mode=module.check_mode,
        )
    except Unsafe as error:
        module.fail_json(msg=f"Refused: {error}.")
    except OSError as error:
        module.fail_json(
            msg=f"Failed: {os.strerror(error.errno or 0)}; reconcile by hand."
        )
    result["source_revision"] = module.params["source_revision"]
    module.exit_json(**result)


if __name__ == "__main__":
    main()
