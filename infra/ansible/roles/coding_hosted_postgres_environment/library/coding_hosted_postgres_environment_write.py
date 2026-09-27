#!/usr/bin/python
"""Write one reader's PostgreSQL environment copy through pinned directory fds.

The reader account owns, and can write, its home directory, and its rootless
user manager keeps running, so the account can swap `private` (or the copy
itself) for a symlink at any moment, including mid-run. A path-based `copy`/`file`
task run as root would then follow the swap: it would chown and chmod the link
target to the account and write the password inside it, and a follow=false stat
of the final path would still see an account-owned regular file and pass.

This module never resolves the path as a string. It opens every component from
`/` with `O_NOFOLLOW` and `O_DIRECTORY`, verifies the reader home and the
`private` directory through those descriptors (creating `private` with
`mkdirat`+`fchown`+`fchmod`, then re-opening `O_NOFOLLOW` to verify), writes the
document to an `O_CREAT|O_EXCL` temp inside the pinned `private` fd with
`fchown`/`fchmod`/`fsync`, and `renameat`s it into place within that same fd. A
temp left by any failure is unlinked. The document is a `no_log` argument, so the
module never writes its invocation to the target's journal, and the task is
`no_log`.
"""

import contextlib
import errno
import hashlib
import os
import pwd
import secrets
import stat

DOCUMENTATION = r"""
module: coding_hosted_postgres_environment_write
short_description: Write one PostgreSQL environment copy through pinned fds
description:
  - Opens every path component with O_NOFOLLOW, verifies the reader-owned home
    and private directories on the descriptors, and atomically renames an
    exclusive temp file into the pinned private directory.
options:
  path:
    description: Absolute, normalised path ending in private/postgres-environment.json.
    type: str
    required: true
  owner:
    description: The reader account that must own the home, private directory and file.
    type: str
    required: true
  content:
    description: The exact bytes to write. Never logged.
    type: str
    required: true
    no_log: true
"""

NAME = "postgres-environment.json"
PRIVATE = "private"
MODE = 0o600
PRIVATE_MODE = 0o700
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeCopy(Exception):
    """The copy or one of its parents is not safe to write."""


def split_target(path):
    """Return the directory components of an exact copy path, or refuse it."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise UnsafeCopy("the copy path is not absolute")
    if os.path.normpath(path) != path or "//" in path:
        raise UnsafeCopy("the copy path is not normalised")
    components = path.split("/")[1:]
    if len(components) < 3 or components[-2:] != [PRIVATE, NAME]:
        raise UnsafeCopy(
            "the copy path is not <home>/private/postgres-environment.json"
        )
    return components[:-1]


def _require_reader_directory(fd, owner_uid, mode, label):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeCopy(f"the {label} directory is not a directory")
    if info.st_uid != owner_uid:
        raise UnsafeCopy(f"the {label} directory is not owned by the reader")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeCopy(f"the {label} directory is writable by group or others")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise UnsafeCopy(f"the {label} directory is not mode {mode:04o}")


def open_home(path, owner_uid):
    """Pin the reader home directory, opening each component from / with
    O_NOFOLLOW so no component is ever resolved through a symlink."""
    directories = split_target(path)[:-1]  # drop the trailing 'private'
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    pinned = None
    try:
        for index, component in enumerate(directories):
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise UnsafeCopy(
                        "a parent is a symlink or not a directory"
                    ) from None
                raise
            os.close(fd)
            fd = child
            if index == len(directories) - 1:
                # The home is created and owned by the reader bootstraps; its
                # mode is not pinned here, only ownership and no shared write.
                _require_reader_directory(fd, owner_uid, None, "reader home")
        pinned = fd
        return pinned
    finally:
        if pinned is None:
            os.close(fd)


def open_private(home_fd, owner_uid):
    """Return a pinned, verified fd to the reader's private directory, creating
    it 0700 and reader-owned if absent, then re-opening O_NOFOLLOW to verify."""
    try:
        os.mkdir(PRIVATE, mode=PRIVATE_MODE, dir_fd=home_fd)
        created = True
    except FileExistsError:
        created = False
    try:
        fd = os.open(PRIVATE, _DIRECTORY_FLAGS, dir_fd=home_fd)
    except OSError as error:
        if error.errno in (errno.ENOTDIR, errno.ELOOP):
            raise UnsafeCopy(
                "the private path is a symlink or not a directory"
            ) from None
        raise
    try:
        if created:
            os.fchown(fd, owner_uid, owner_uid)
            os.fchmod(fd, PRIVATE_MODE)
        _require_reader_directory(fd, owner_uid, PRIVATE_MODE, PRIVATE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


def write_into(private_fd, owner_uid, content, check_mode=False):
    """Atomically write the document into the pinned private dir; return the
    state and the sha256 of the bytes on disk."""
    if check_mode:
        return "would_write", _digest(content)
    temp = f".{NAME}.{secrets.token_hex(16)}"
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        MODE,
        dir_fd=private_fd,
    )
    try:
        os.fchown(fd, owner_uid, owner_uid)
        os.fchmod(fd, MODE)
        data = content.encode()
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(temp, dir_fd=private_fd)
        raise
    os.close(fd)
    try:
        os.rename(temp, NAME, src_dir_fd=private_fd, dst_dir_fd=private_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp, dir_fd=private_fd)
        raise
    dir_fd = os.open(".", _DIRECTORY_FLAGS, dir_fd=private_fd)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return "written", _verify(private_fd, owner_uid, content)


def _verify(private_fd, owner_uid, content):
    """Re-open the copy through the pinned dir and confirm what is on disk; raise
    UnsafeCopy on any mismatch. Return its sha256."""
    fd = os.open(NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=private_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeCopy("the copy is not a regular file after the write")
        if info.st_nlink != 1:
            raise UnsafeCopy("the copy has more than one hard link after the write")
        if info.st_uid != owner_uid:
            raise UnsafeCopy("the copy is not owned by the reader after the write")
        if stat.S_IMODE(info.st_mode) != MODE:
            raise UnsafeCopy("the copy is not mode 0600 after the write")
        on_disk = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            on_disk += chunk
    finally:
        os.close(fd)
    if on_disk != content.encode():
        raise UnsafeCopy("the copy does not hold the rendered document")
    return _digest(content)


def write_copy(path, owner_uid, content, check_mode=False):
    """Return (state, checksum); raise UnsafeCopy on any unsafe parent or copy."""
    home_fd = open_home(path, owner_uid)
    try:
        private_fd = open_private(home_fd, owner_uid)
    finally:
        os.close(home_fd)
    try:
        return write_into(private_fd, owner_uid, content, check_mode=check_mode)
    finally:
        os.close(private_fd)


def recheck_parents(path, owner_uid):
    """After the write, re-verify the home and private directories from / with
    O_NOFOLLOW, so a parent swapped during the write is caught."""
    home_fd = open_home(path, owner_uid)
    try:
        private_fd = open_private(home_fd, owner_uid)
    finally:
        os.close(home_fd)
    os.close(private_fd)


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "path": {"type": "str", "required": True},
            "owner": {"type": "str", "required": True},
            "content": {"type": "str", "required": True, "no_log": True},
        },
        supports_check_mode=True,
    )
    path = module.params["path"]
    try:
        owner_uid = pwd.getpwnam(module.params["owner"]).pw_uid
    except KeyError:
        module.fail_json(msg=f"Refused {path}: the reader account does not exist.")
    try:
        state, checksum = write_copy(
            path, owner_uid, module.params["content"], check_mode=module.check_mode
        )
        if not module.check_mode:
            recheck_parents(path, owner_uid)
    except UnsafeCopy as error:
        module.fail_json(msg=f"Refused {path}: {error}; nothing was written there.")
    except OSError as error:
        module.fail_json(
            msg=f"Failed {path}: {os.strerror(error.errno or 0)}; reconcile by hand."
        )
    module.exit_json(
        changed=state == "written", state=state, checksum=checksum, path=path
    )


if __name__ == "__main__":
    main()
