#!/usr/bin/python
"""Remove one reader's PostgreSQL environment copy through pinned directory fds.

The reader home and its private directory are owned, and writable, by the
unprivileged reader account, so that account could swap a path component for a
symlink between the role's inspection and the removal. A path-based unlink run
as root would then follow the swapped parent. This module never resolves the
path as a string: it opens every component from ``/`` with ``O_NOFOLLOW`` and
``O_DIRECTORY``, checks the two reader-owned directories and the file through
those descriptors, and removes the single entry with ``unlinkat`` relative to
the pinned private directory. ``unlinkat`` without ``AT_REMOVEDIR`` cannot
remove a directory and never follows a symlink, so a later swap can at most
remove the reader's own directory entry, never a target elsewhere.
"""

import errno
import os
import pwd
import stat

DOCUMENTATION = r"""
module: coding_hosted_postgres_environment_unlink
short_description: Remove one PostgreSQL environment copy without following links
description:
  - Opens every path component with O_NOFOLLOW and removes the file with
    unlinkat relative to the pinned private directory.
  - Refuses a linked or non-directory parent, a reader home or private directory
    not owned by the reader or writable by group or others, and anything but a
    regular single-link file owned by the reader.
options:
  path:
    description: Absolute, normalised path ending in private/postgres-environment.json.
    type: str
    required: true
  owner:
    description: The reader account that must own the home, private directory and file.
    type: str
    required: true
"""

NAME = "postgres-environment.json"
PRIVATE = "private"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeCopy(Exception):
    """The copy or one of its parents is not safe to remove."""


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


def _require_reader_directory(fd, owner_uid, label):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeCopy(f"the {label} directory is not a directory")
    if info.st_uid != owner_uid:
        raise UnsafeCopy(f"the {label} directory is not owned by the reader")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UnsafeCopy(f"the {label} directory is writable by group or others")


def open_private_directory(path, owner_uid):
    """Pin the reader's private directory, or return None when a parent is absent.

    Every component is opened relative to its already-opened parent, so no
    component is ever resolved through a symlink.
    """
    directories = split_target(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    pinned = None
    try:
        for index, component in enumerate(directories):
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise UnsafeCopy(
                        "a parent is a symlink or not a directory"
                    ) from None
                raise
            os.close(fd)
            fd = child
            if index == len(directories) - 2:
                _require_reader_directory(fd, owner_uid, "reader home")
        _require_reader_directory(fd, owner_uid, PRIVATE)
        pinned = fd
        return pinned
    finally:
        if pinned is None:
            os.close(fd)


def lstat_entry(dir_fd):
    """Inspect the copy's own directory entry without following it."""
    return os.stat(NAME, dir_fd=dir_fd, follow_symlinks=False)


def unlink_in(dir_fd, owner_uid, check_mode=False):
    """Remove the single copy entry from a pinned private directory."""
    try:
        info = lstat_entry(dir_fd)
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeCopy("the copy is a symlink, directory or special file")
    if info.st_nlink != 1:
        raise UnsafeCopy("the copy has more than one hard link")
    if info.st_uid != owner_uid:
        raise UnsafeCopy("the copy is not owned by the reader")
    if check_mode:
        return "would_remove"
    try:
        # unlinkat(2) without AT_REMOVEDIR: never a directory, never a link target.
        os.unlink(NAME, dir_fd=dir_fd)
    except IsADirectoryError:
        raise UnsafeCopy("the copy became a directory before removal") from None
    except FileNotFoundError:
        raise UnsafeCopy("the copy disappeared before removal") from None
    try:
        lstat_entry(dir_fd)
    except FileNotFoundError:
        return "removed"
    raise UnsafeCopy("a new entry replaced the copy during removal")


def remove_copy(path, owner_uid, check_mode=False):
    """Return absent, would_remove or removed; raise UnsafeCopy otherwise."""
    dir_fd = open_private_directory(path, owner_uid)
    if dir_fd is None:
        return "absent"
    try:
        return unlink_in(dir_fd, owner_uid, check_mode=check_mode)
    finally:
        os.close(dir_fd)


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "path": {"type": "str", "required": True},
            "owner": {"type": "str", "required": True},
        },
        supports_check_mode=True,
    )
    path = module.params["path"]
    try:
        owner_uid = pwd.getpwnam(module.params["owner"]).pw_uid
    except KeyError:
        module.fail_json(msg=f"Refused {path}: the reader account does not exist.")
    try:
        state = remove_copy(path, owner_uid, check_mode=module.check_mode)
    except UnsafeCopy as error:
        module.fail_json(msg=f"Refused {path}: {error}; nothing was removed there.")
    except OSError as error:
        module.fail_json(
            msg=f"Failed {path}: {os.strerror(error.errno or 0)}; reconcile by hand."
        )
    module.exit_json(
        changed=state in ("removed", "would_remove"), state=state, path=path
    )


if __name__ == "__main__":
    main()
