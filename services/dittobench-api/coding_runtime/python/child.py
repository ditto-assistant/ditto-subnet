"""Untrusted-side Python API bridge. Its replies are NEVER test evidence."""

import ctypes
import errno
import importlib.util
import json
import os
import platform
import sys

from dittobench_wire import pack, unpack

MAX_MESSAGE = 65536


def confine():
    # Immutable setup runs before importing any candidate code. Linux amd64 only.
    if platform.machine() != "x86_64" or os.geteuid() == 0 or os.getegid() == 0:
        raise RuntimeError("invalid child identity")

    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint32),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    # seccomp_data: nr at 0, arch at 4. Refuse other ABIs including x32.
    instructions = [
        (0x20, 0, 0, 4),
        (0x15, 1, 0, 0xC000003E),
        (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0),
        (0x45, 0, 1, 0x40000000),
        (0x06, 0, 0, 0x80000000),
    ]
    # Permit only clone(CLONE_THREAD): threads share the child's thread group
    # and die with it. clone3 returns ENOSYS so libc can use that checked path.
    instructions.extend(
        [
            (0x15, 0, 4, 56),
            (0x20, 0, 0, 16),
            (0x45, 1, 0, 0x10000),
            (0x06, 0, 0, 0x50000 | errno.EPERM),
            (0x06, 0, 0, 0x7FFF0000),
            (0x15, 0, 1, 435),
            (0x06, 0, 0, 0x50000 | errno.ENOSYS),
        ]
    )
    # fork/vfork, execve, ptrace, kill, setpgid/setsid, tkill/tgkill,
    # unshare/setns, process_vm_*, execveat, io_uring_*, clone3.
    for number in (
        57,
        58,
        59,
        62,
        101,
        109,
        112,
        200,
        234,
        272,
        308,
        310,
        311,
        322,
        425,
        426,
        427,
    ):
        instructions.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x50000 | errno.EPERM)])
    instructions.append((0x06, 0, 0, 0x7FFF0000))
    filters = (Filter * len(instructions))(*(Filter(*i) for i in instructions))
    program = Program(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(38, 1, 0, 0, 0) != 0
        or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0
    ):
        raise RuntimeError("child confinement unavailable")


def main():
    protocol = os.fdopen(os.dup(1), "wb", buffering=0)
    sink = os.open(os.devnull, os.O_WRONLY)
    os.dup2(sink, 1)
    os.close(sink)
    confine()
    protocol.write(b"DITTO-PYTHON-CHILD-READY-V1\n")
    # No protected path, test source, report nonce or expected value arrives here.
    sys.path.insert(0, "/workspace")
    modules, references = {}, {}

    def encode(value):
        # Wrap ALL results, so a candidate dictionary cannot become a protocol ref.
        try:
            return {"kind": "data", "value": pack(value)}
        except ValueError:
            pass
        key = len(references) + 1
        references[key] = value
        return {"kind": "reference", "value": key}

    while line := sys.stdin.buffer.readline(MAX_MESSAGE + 1):
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            return 1
        request = json.loads(line)
        try:
            target = request["target"]
            if "module" in target:
                name = target["module"]
                if name not in modules:
                    spec = importlib.util.spec_from_file_location(
                        name, f"/workspace/{name}.py"
                    )
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[name] = module
                    spec.loader.exec_module(module)
                    modules[name] = module
                value = modules[name]
            else:
                value = references[target["reference"]]
            for part in target["path"]:
                value = getattr(value, part)
            if request["operation"] == "call":
                value = value(*unpack(request["args"]), **unpack(request["kwargs"]))
            result = encode(value)
        except BaseException:
            result = {"kind": "failure"}
        response = (
            json.dumps(
                {"id": request["id"], "result": result},
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        if len(response) > MAX_MESSAGE:
            return 1
        protocol.write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
