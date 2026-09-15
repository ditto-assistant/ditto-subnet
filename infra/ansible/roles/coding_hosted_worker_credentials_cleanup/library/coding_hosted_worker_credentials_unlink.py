#!/usr/bin/python
"""Remove the native Coding worker credential files through pinned directory fds.

Root runs this module. It opens every component of the worker private directory
from ``/`` with ``O_NOFOLLOW`` and ``O_DIRECTORY`` so the worker account cannot
swap a parent for a symlink between inspection and removal, verifies the reader
home and private directory on their descriptors, and removes each of the three
fixed names with ``unlinkat`` relative to the pinned directory, refusing any
that is a symlink, directory, hard link or another account's file. It also
removes and reports any leftover ``.<name>.*.tmp`` a partial write may have left,
so no partial secret temporary is retained. It never resolves a path as a string
and never removes a directory. It returns only filenames and states; removal is
not revocation.
"""

import errno
import os
import pwd
import re
import stat

DOCUMENTATION = r"""
module: coding_hosted_worker_credentials_unlink
short_description: Remove the worker credential files without following links
description:
  - Opens every path component with O_NOFOLLOW and removes the three fixed names
    and any leftover temporaries with unlinkat relative to the pinned directory.
options:
  private_dir:
    description: Absolute, normalised worker private directory.
    type: str
    required: true
  owner:
    description: The worker account that must own the home, private dir and files.
    type: str
    required: true
"""

NAMES = ("hippius-environment.json", "image-storage.json", "provider-key")
PRIVATE = "private"
_TMP = re.compile(
    r"^\.(hippius-environment\.json|image-storage\.json|provider-key)\.[0-9]+\.[0-9a-f]+\.tmp$"
)
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


def _require_reader_dir(fd, uid, label):
    # Cleanup requires the expected owner and no group/other write, but --
    # unlike the write module -- does not require mode 0700, so a directory
    # left at a wrong mode can still be cleaned up. The observed mode is
    # reported for the operator to reconcile; see docs for this asymmetry.
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise Unsafe(f"the {label} is not a directory")
    if info.st_uid != uid:
        raise Unsafe(f"the {label} is not owned by the worker")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Unsafe(f"the {label} is writable by group or others")
    return info


def _open_private(private_dir, uid):
    components = _split(private_dir)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    mode = None
    try:
        for index, component in enumerate(components):
            try:
                child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                return None, None
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise Unsafe("a private directory component is a symlink") from None
                raise
            os.close(fd)
            fd = child
            if index == len(components) - 2:
                _require_reader_dir(fd, uid, "worker home")
        info = _require_reader_dir(fd, uid, "private directory")
        mode = format(stat.S_IMODE(info.st_mode), "04o")
    except BaseException:
        os.close(fd)
        raise
    return fd, mode


def _unlink_name(dir_fd, name, uid, check_mode):
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Unsafe(f"{name} is a symlink, directory or special file")
    if info.st_nlink != 1:
        raise Unsafe(f"{name} has more than one hard link")
    if info.st_uid != uid:
        raise Unsafe(f"{name} is owned by another account")
    if check_mode:
        return "would_remove"
    os.unlink(name, dir_fd=dir_fd)
    return "removed"


def _remove_leftover_temps(dir_fd, uid, check_mode):
    removed = []
    for entry in os.listdir(dir_fd):
        if not _TMP.match(entry):
            continue
        try:
            info = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid:
            raise Unsafe(f"{entry} is not a removable worker temporary")
        if not check_mode:
            os.unlink(entry, dir_fd=dir_fd)
        removed.append(entry)
    return sorted(removed)


def remove_all(private_dir, owner_uid, *, check_mode=False):
    dir_fd, dir_mode = _open_private(private_dir, owner_uid)
    if dir_fd is None:
        return {
            "changed": False,
            "removed": [],
            "already_absent": list(NAMES),
            "refused": [],
            "not_attempted": [],
            "leftover_temps": [],
            "private_dir_present": False,
            "private_dir_mode": None,
        }
    removed = []
    already_absent = []
    refused = []
    not_attempted = []
    temps = []
    try:
        for index, name in enumerate(NAMES):
            try:
                state = _unlink_name(dir_fd, name, owner_uid, check_mode)
            except Unsafe as error:
                # Report every path: what was removed, what refused and why, and
                # what was not attempted after the refusal. Do not continue.
                refused.append(f"{name}: {error}")
                not_attempted = list(NAMES[index + 1 :])
                break
            (already_absent if state == "absent" else removed).append(name)
        else:
            temps = _remove_leftover_temps(dir_fd, owner_uid, check_mode)
            if not check_mode:
                os.fsync(dir_fd)
                for name in NAMES:
                    try:
                        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    refused.append(f"{name}: still present after removal")
    finally:
        os.close(dir_fd)
    return {
        "changed": bool(removed or temps),
        "removed": removed,
        "already_absent": already_absent,
        "refused": refused,
        "not_attempted": not_attempted,
        "leftover_temps": temps,
        "private_dir_present": True,
        "private_dir_mode": dir_mode,
    }


def main():
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "private_dir": {"type": "str", "required": True},
            "owner": {"type": "str", "required": True},
        },
        supports_check_mode=True,
    )
    try:
        account = pwd.getpwnam(module.params["owner"])
    except KeyError:
        module.fail_json(msg="Refused: the worker account does not exist.", removed=[])
    empty = {
        "removed": [],
        "already_absent": [],
        "not_attempted": list(NAMES),
        "leftover_temps": [],
    }
    try:
        result = remove_all(
            module.params["private_dir"], account.pw_uid, check_mode=module.check_mode
        )
    except Unsafe as error:
        # A directory-level refusal (symlinked component, wrong owner): nothing
        # was removed. Report every list so the operator sees the full picture.
        module.fail_json(
            msg=f"Refused: {error}; nothing was removed.", refused=[str(error)], **empty
        )
    except OSError as error:
        module.fail_json(
            msg=f"Failed: {os.strerror(error.errno or 0)}; reconcile by hand.",
            refused=["unexpected error"],
            **empty,
        )
    # A per-name refusal is a failure that still reports what was removed first.
    if result["refused"]:
        module.fail_json(msg="Refused: " + "; ".join(result["refused"]), **result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
