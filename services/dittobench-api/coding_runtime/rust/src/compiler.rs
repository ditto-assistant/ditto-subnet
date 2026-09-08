//! Fixed offline compiler lifecycle inside an exclusively leased, qualified
//! executor container. No private suite bytes are accepted by this module.
use crate::{
    artifact::Artifact,
    process::parent_status,
    workspace::{CompilerRecipe, StagedInputs},
};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io,
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::{
            fs::{FileExt, MetadataExt, OpenOptionsExt},
            process::CommandExt,
        },
    },
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompileError {
    Profile,
    Toolchain,
    Inputs,
    Compilation,
    Deadline,
    Cleanup,
    Output,
}
type Result<T> = std::result::Result<T, CompileError>;

/// From the trusted image verifier, not candidate claims or hashes discovered
/// from the mutable workspace. Image authentication remains supervisor-owned.
#[derive(Clone, Copy)]
pub struct ToolchainIdentity {
    pub image_sha256: [u8; 32],
    pub compiler_sha256: [u8; 32],
    pub bridge_library_sha256: [u8; 32],
}
/// Constructed only after both compiler groups were killed/reaped and the exact
/// output was sealed/read back. This commitment is not an external signature.
pub struct Build {
    artifact: Artifact,
    digest: [u8; 32],
}
impl Build {
    pub fn sha256(&self) -> [u8; 32] {
        self.digest
    }
    pub fn artifact_sha256(&self) -> [u8; 32] {
        self.artifact.sha256()
    }
    pub fn into_artifact(self) -> Artifact {
        self.artifact
    }
}

fn image_file(path: &str, expected: [u8; 32]) -> Result<File> {
    // Image-owned symlinks (rustc/cc installations) may resolve only through
    // protected root-owned paths. The qualified image itself is immutable.
    for entry in Path::new(path).ancestors().skip(1) {
        let m = fs::symlink_metadata(entry).map_err(|_| CompileError::Toolchain)?;
        if !m.is_dir() || m.uid() != 0 || m.mode() & 0o7022 != 0 {
            return Err(CompileError::Toolchain);
        }
    }
    let path = fs::canonicalize(path).map_err(|_| CompileError::Toolchain)?;
    for entry in path.ancestors().skip(1) {
        let m = fs::symlink_metadata(entry).map_err(|_| CompileError::Toolchain)?;
        if !m.is_dir() || m.uid() != 0 || m.mode() & 0o7022 != 0 {
            return Err(CompileError::Toolchain);
        }
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| CompileError::Toolchain)?;
    let info = file.metadata().map_err(|_| CompileError::Toolchain)?;
    if !info.is_file()
        || info.uid() != 0
        || info.mode() & 0o7022 != 0
        || info.nlink() != 1
        || !(64..=256 << 20).contains(&info.size())
    {
        return Err(CompileError::Toolchain);
    }
    let mut hash = Sha256::new();
    let mut buffer = [0; 65536];
    let mut offset = 0;
    while offset < info.size() {
        let count = ((info.size() - offset) as usize).min(buffer.len());
        file.read_exact_at(&mut buffer[..count], offset)
            .map_err(|_| CompileError::Toolchain)?;
        hash.update(&buffer[..count]);
        offset += count as u64;
    }
    let actual: [u8; 32] = hash.finalize().into();
    if actual != expected {
        return Err(CompileError::Toolchain);
    }
    Ok(file)
}

fn output_mount() -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open("/out")
        .map_err(|_| CompileError::Output)?;
    let m = file.metadata().map_err(|_| CompileError::Output)?;
    if m.uid() != 10001 || m.gid() != 10001 || m.mode() & 0o7777 != 0o700 {
        return Err(CompileError::Output);
    }
    let mut fs: libc::statfs = unsafe { std::mem::zeroed() };
    let mut vfs: libc::statvfs = unsafe { std::mem::zeroed() };
    let flags = libc::ST_NOSUID | libc::ST_NODEV | libc::ST_NOEXEC;
    if unsafe { libc::fstatfs(file.as_raw_fd(), &mut fs) } != 0
        || fs.f_type != libc::TMPFS_MAGIC
        || unsafe { libc::fstatvfs(file.as_raw_fd(), &mut vfs) } != 0
        || vfs.f_flag & flags != flags
        || vfs
            .f_blocks
            .checked_mul(vfs.f_frsize)
            .is_none_or(|size| size == 0 || size > 128 << 20)
        || unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0
    {
        return Err(CompileError::Output);
    }
    // Only a fresh /out is accepted. Failure retains partial output and prevents
    // a retry from silently reusing it. Cleanup belongs to container disposal.
    if fs::read_dir(format!("/proc/self/fd/{}", file.as_raw_fd()))
        .map_err(|_| CompileError::Output)?
        .next()
        .is_some()
    {
        return Err(CompileError::Output);
    }
    Ok(file)
}

struct Group {
    child: Child,
    cleaned: bool,
    signalled: bool,
}
impl Group {
    fn exited(&self) -> Result<Option<bool>> {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        // WNOWAIT retains the leader PID until the final group signal, so no
        // numeric PID can be recycled before kill(-pgid).
        if unsafe {
            libc::waitid(
                libc::P_PID,
                self.child.id(),
                &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        } != 0
        {
            return Err(CompileError::Cleanup);
        }
        if unsafe { info.si_pid() } == 0 {
            return Ok(None);
        }
        // A signal (including OOM/resource termination) is not evidence that
        // the candidate's source failed compilation normally.
        if info.si_code != libc::CLD_EXITED {
            return Err(CompileError::Profile);
        }
        Ok(Some(unsafe { info.si_status() } == 0))
    }
    fn cleanup(&mut self) -> Result<()> {
        if self.cleaned {
            return Ok(());
        }
        let pid = self.child.id() as i32;
        if !self.signalled {
            if unsafe { libc::kill(-pid, libc::SIGKILL) } != 0
                && io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
            {
                return Err(CompileError::Cleanup);
            }
            self.signalled = true;
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let mut status = 0;
            let reaped = unsafe { libc::waitpid(-pid, &mut status, libc::WNOHANG) };
            if reaped > 0 {
                continue;
            }
            if reaped < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::ECHILD) {
                return Err(CompileError::Cleanup);
            }
            if reaped < 0
                && unsafe { libc::kill(-pid, 0) } == -1
                && io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
            {
                self.cleaned = true;
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(CompileError::Cleanup);
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
    fn run(&mut self, deadline: Instant) -> Result<()> {
        let result = loop {
            if Instant::now() >= deadline {
                break Err(CompileError::Deadline);
            }
            match self.exited() {
                Ok(Some(true)) => break Ok(()),
                Ok(Some(false)) => break Err(CompileError::Compilation),
                Err(e) => break Err(e),
                Ok(None) => {}
            }
            std::thread::sleep(Duration::from_millis(2));
        };
        self.cleanup()?; // cleanup failure overrides compiler success/failure
        result
    }
}
impl Drop for Group {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

fn run(compiler: &File, cwd: RawFd, arguments: &[&str], deadline: Instant) -> Result<()> {
    if Instant::now() >= deadline {
        return Err(CompileError::Deadline);
    }
    let mut command = Command::new(format!("/proc/self/fd/{}", compiler.as_raw_fd()));
    command
        .args(arguments)
        .env_clear()
        .envs(CompilerRecipe::ENVIRONMENT.iter().copied())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    unsafe {
        command.pre_exec(move || {
            libc::umask(0o077);
            if libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, 4u32) != 0
                || libc::fchdir(cwd) != 0
                || libc::setsid() < 0
                || libc::setgroups(0, std::ptr::null()) != 0
                || libc::setresgid(10001, 10001, 10001) != 0
                || libc::setresuid(10001, 10001, 10001) != 0
                || libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
            {
                return Err(io::Error::last_os_error());
            }
            for (resource, maximum) in [
                (libc::RLIMIT_CORE, 0),
                (libc::RLIMIT_FSIZE, 128 << 20),
                (libc::RLIMIT_AS, 2 << 30),
                (libc::RLIMIT_CPU, 60),
                (libc::RLIMIT_NOFILE, 256),
            ] {
                let limit = libc::rlimit {
                    rlim_cur: maximum,
                    rlim_max: maximum,
                };
                if libc::setrlimit(resource, &limit) != 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }
    let mut group = Group {
        child: command.spawn().map_err(|_| CompileError::Profile)?,
        cleaned: false,
        signalled: false,
    };
    group.run(deadline)
}

/// Compile once in an exclusively leased single-threaded trusted parent. The
/// qualified container owns network isolation, immutable tools, bounded /tmp,
/// PID/memory/CPU cgroups, and whole-container teardown on any cleanup failure.
/// Fixed trusted compiler/linker binaries may spawn helpers; no Cargo, build
/// script, external proc macro, candidate executable, or user flags are invoked.
pub fn compile(
    inputs: &StagedInputs,
    identity: ToolchainIdentity,
    timeout: Duration,
) -> Result<Build> {
    if timeout.is_zero()
        || timeout > Duration::from_secs(120)
        || unsafe { libc::getuid() } != 0
        || unsafe { libc::geteuid() } != 0
        || [
            identity.image_sha256,
            identity.compiler_sha256,
            identity.bridge_library_sha256,
        ]
        .contains(&[0; 32])
    {
        return Err(CompileError::Profile);
    }
    parent_status(&fs::read_to_string("/proc/self/status").map_err(|_| CompileError::Profile)?)
        .map_err(|_| CompileError::Profile)?;
    if fs::read_dir("/proc/self/task")
        .map_err(|_| CompileError::Profile)?
        .count()
        != 1
        || unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0
    {
        return Err(CompileError::Profile);
    }
    let metadata = inputs
        .directory_fd()
        .try_clone_to_owned()
        .map_err(|_| CompileError::Inputs)?;
    let directory = File::from(metadata);
    let info = directory.metadata().map_err(|_| CompileError::Inputs)?;
    if !info.is_dir() || info.uid() != 0 || info.mode() & 0o7777 != 0o555 {
        return Err(CompileError::Inputs);
    }
    let compiler = image_file(CompilerRecipe::PROGRAM, identity.compiler_sha256)?;
    let _bridge = image_file(
        "/opt/deps/libcoding_rust_suite.rlib",
        identity.bridge_library_sha256,
    )?;
    let output = output_mount()?;
    let deadline = Instant::now() + timeout;
    for arguments in [CompilerRecipe::LIBRARY, CompilerRecipe::BRIDGE] {
        run(&compiler, directory.as_raw_fd(), arguments, deadline)?;
    }
    let fd = unsafe {
        libc::openat(
            output.as_raw_fd(),
            c"candidate".as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(CompileError::Output);
    }
    let source = unsafe { File::from_raw_fd(fd) };
    let artifact = Artifact::seal_output(&source, 10001).map_err(|_| CompileError::Output)?;
    let mut hash = Sha256::new();
    hash.update(b"coding-rust-completed-build-v1\0");
    for digest in [
        identity.image_sha256,
        identity.compiler_sha256,
        identity.bridge_library_sha256,
        inputs.sha256(),
        CompilerRecipe::sha256(),
        artifact.sha256(),
    ] {
        hash.update(digest);
    }
    Ok(Build {
        artifact,
        digest: hash.finalize().into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn trusted(arguments: &[&str]) -> Group {
        let mut command = Command::new("/bin/sh");
        command.args(arguments);
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        Group {
            child: command.spawn().unwrap(),
            cleaned: false,
            signalled: false,
        }
    }
    #[test]
    fn compiler_success_and_nonzero_exit_always_reap() {
        for (script, expected) in [
            ("exit 0", Ok(())),
            ("exit 7", Err(CompileError::Compilation)),
        ] {
            let mut group = trusted(&["-c", script]);
            let pid = group.child.id();
            assert_eq!(group.run(Instant::now() + Duration::from_secs(2)), expected);
            assert!(group.cleaned);
            group.cleanup().unwrap();
            assert_eq!(
                unsafe { libc::waitpid(pid as i32, std::ptr::null_mut(), libc::WNOHANG) },
                -1
            );
        }
    }
    #[test]
    fn compiler_timeout_kills_and_reaps_before_returning() {
        let mut group = trusted(&["-c", "exec /bin/sleep 30"]);
        assert_eq!(
            group.run(Instant::now() + Duration::from_millis(20)),
            Err(CompileError::Deadline)
        );
        assert!(group.cleaned);
    }
    #[test]
    fn signal_is_not_mislabeled_as_a_source_compilation_error() {
        let mut group = trusted(&["-c", "kill -KILL $$"]);
        assert_eq!(
            group.run(Instant::now() + Duration::from_secs(2)),
            Err(CompileError::Profile)
        );
        assert!(group.cleaned);
    }
    #[test]
    fn dropping_an_unfinished_group_still_reaps() {
        let group = trusted(&["-c", "exec /bin/sleep 30"]);
        let pid = group.child.id();
        drop(group);
        assert_eq!(
            unsafe { libc::waitpid(pid as i32, std::ptr::null_mut(), libc::WNOHANG) },
            -1
        );
        assert_eq!(
            io::Error::last_os_error().raw_os_error(),
            Some(libc::ECHILD)
        );
    }
}
