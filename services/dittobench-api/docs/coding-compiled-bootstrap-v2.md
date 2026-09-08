# Compiled candidate pre-exec confinement

This layer launches a sealed Linux amd64 candidate executable with confinement
already installed before any candidate instruction, including Go package `init`
and native ELF constructors. It is a prerequisite for Go/Rust API bridges, not
a trusted assertion driver, compiler sandbox, selectable image or private task
qualification. No catalog, host, key service or reward gate is activated.

## Boundary and one-time handoff

Installing a filter in a generated program's `main` is too late: linked candidate
initializers may already have run. The trusted parent instead starts the immutable
C bootstrap with a verified executable descriptor and private socketpair.

1. The container-side parent requires Linux amd64, root, no-new-privileges, effective
   KILL/SETGID/SETUID and no privileges beyond those plus the existing executor's
   optional CHOWN/DAC_OVERRIDE. Inheritable and ambient capabilities must be empty.
   Missing termination authority is rejected before spawning.
2. The parent opens a root-owned mode-0555 regular ELF file with one link under
   protected canonical directories. It verifies the expected SHA-256 on that same
   descriptor, with a 256 MiB bound and stable size/mtime/ctime. Paths are not
   reopened to select the candidate executable.
   The Go integration and Python container helper can instead use a root-owned anonymous executable FD
   sealed against write/grow/shrink and further seal changes. The bootstrap checks
   those seals whenever the link count is zero; an arbitrary unlinked file or
   writable memory file is not accepted. This keeps writable scratch mounts
   non-executable while allowing one explicitly verified compiled program.
3. The immutable bootstrap verifies its descriptor and root-parent private Unix
   socket peer. It proves it has exactly one thread, drops all real/effective/saved
   UID/GID values to the non-root identity, clears groups, requires empty candidate
   capabilities, disables dumpability, and sets no-new-privileges.
4. It installs the filter before candidate loading, then sends its seccomp listener
   to the trusted parent using a single bounded SCM_RIGHTS handoff. It closes its
   listener and handoff socket before attempting `execveat` on the verified FD.
5. The parent checks the handoff PID/FD/UID/GID and the initial kernel notification's
   PID, architecture, syscall, descriptor and flags. It validates the kernel cookie
   and sends exactly one CONTINUE response. It never reads target memory or performs
   target-requested operations. It then closes the last listener descriptor.
6. Later `execveat` calls receive ENOSYS from the kernel because no listener exists;
   ordinary `execve` receives EPERM. No untrusted notification is serviced. Both
   filter-installation APIs are blocked so candidate code cannot install a new
   listener or replace this policy. There is no alternate executable or fallback.

The CONTINUE handoff is confined to the **trusted bootstrap phase**. It is not a
general syscall authorization service for untrusted code. At that point the only
thread is immutable bootstrap code, arguments are fixed by that code, the target
FD is verified/read-only, and nondumpability plus distinct credentials prevent
untrusted same-UID memory mutation. No candidate code, signal handler or dynamic
candidate library has run. The trusted image, bootstrap, parent and kernel are
part of the trusted computing base.

This distinction matters because [Linux explicitly warns against using seccomp
notifications for security decisions on untrusted syscall arguments](https://man7.org/linux/man-pages/man2/seccomp_unotify.2.html).
Do not reuse the helper to inspect attacker pointers, authorize mutable paths,
continue arbitrary candidate requests or emulate privileged operations. Any such
change requires a different security design and review.

## Policy after execution

The inherited filter denies process creation, executable replacement, process-group
escape, namespace changes, ptrace/process-memory operations, pidfd-based interference
and io_uring. Other architectures and x32 are killed. `clone3` returns ENOSYS;
only same-thread-group `clone` calls are allowed. Candidate-created threads inherit
the filter, keeping whole-process kill/reap meaningful.

Go's scheduler needs self-directed signals for asynchronous preemption. `kill`,
`tgkill` and queued thread-group signals are therefore allowed only when their
PID/TGID is the original candidate's exact process ID. Foreign/group signal forms
are denied. No process creation, namespace change or later exec can change the
bound thread-group identity. This differs intentionally from the interpreter
profiles that deny all signal syscalls.

Rootless container isolation, network denial, read-only root, protected root-owned
0700 grader/control directories, bounded memory/CPU/PIDs and outer process-group
cleanup remain mandatory. The helper is not a host sandbox and must never be used
to run candidate code directly on an ordinary host.

## Caller responsibilities

`launcher.launch()` returns a process after its initial exec was authorized under
the filter. It does not assert that program initialization succeeded, that its API
is correct, or that a test passed. The caller still owns the API protocol,
assertions, expected values, runtime/output deadlines, termination and final report.
Keep private tests and expected answers outside the candidate binary and process.

The Python helper accepts either its original protected path or a sealed
descriptor, which it duplicates before checking mode/owner/link count, seals and
digest. An optional `api_socket` must be a same-parent AF_UNIX stream; it becomes
candidate stdin without passing other API descriptors. The caller retains
ownership of its original descriptor/socket and must close them and verify
candidate termination. Socket closure is not process-cleanup authority.

Compiler execution is not covered. A future Go/Rust driver must compile only
candidate-authorized source and bridge material in a separately constrained phase,
terminate that phase, and seal the resulting file before launch. Private repository
source can remain within the authorized Platform execution boundary. Do not link
hidden test oracles into candidate binaries; reference implementations belong only
in separate authorized qualification controls, never miner candidate builds.
Never reuse this execution filter as
a compiler policy: it deliberately forbids the subprocesses compilers need.

Launch failures close descriptors and kill/wait for the child. The listener is
closed even if the initial grant fails. Callers must also invoke `terminate()` on
successful launches before accepting completion. A missing feature or invalid
handoff is not grounds to retry without confinement or report a synthetic success.

## Evidence and limits

The Dockerfile is a public **fixture-only** image, pinned to Go 1.26.6, Rust 1.98.0
and the existing Python 3.13.14 base digest. It lacks a production supervisor
contract/driver and carries the fixture label. Its synthetic Go file has a build
ignore tag so repository-wide `go test ./...` cannot execute its `init` on a host.

Real-container probes verify:

- Linux notification ABI sizes and ioctl encodings;
- failed digest, descriptor, handoff and cancelled-grant cases never entering the
  candidate constructor and leaving no unreaped child;
- missing parent termination authority rejected before candidate entry;
- C/Rust constructors and Go `init` running under confinement;
- denied fork, exec, later `execveat`, filter replacement, private-file access and
  foreign signals; a fixed PATH-only candidate environment without inherited credentials;
- new-thread inheritance and Go asynchronous preemption with one scheduler P.

```bash
uv run pytest -q ditto/tests/test_coding_compiled_bootstrap.py
docker build -f services/dittobench-api/Dockerfile.coding-compiled-bootstrap \
  -t coding-compiled-bootstrap:synthetic .
bash scripts/test-coding-compiled-bootstrap.sh coding-compiled-bootstrap:synthetic
```

These results do not qualify a private Go/Rust suite, approve a native runtime
image, deploy a host or demonstrate the live private shadow canary. Those remain
separate acceptance gates after the language-specific trusted graders are built.
