//! Trusted pre-exec bootstrap adapter. Use only inside a qualified private
//! executor container; this module does not construct its mount/network/cgroup.
use crate::{
    artifact::Artifact,
    bridge,
    evaluator::{ApiError, ApiFactory, CandidateApi, Signature},
    value::Value,
    wire::Reply,
    wire_unix::Channel,
};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io,
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
        unix::{
            fs::{MetadataExt, OpenOptionsExt},
            net::UnixStream,
            process::CommandExt,
        },
    },
    path::Path,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

const BOOTSTRAP: &str = "/usr/local/libexec/dittobench-compiled-bootstrap";
const UID: u32 = 10001;
const GID: u32 = 10001;
const CLEANUP: Duration = Duration::from_secs(5);
type Result<T> = std::result::Result<T, ApiError>;
fn transport<T>(_: T) -> ApiError {
    ApiError::Transport
}

/// Fixed candidate identity and bounded session wall clock. The outer container
/// must additionally enforce memory/PID/CPU/output mount bounds and no network.
#[derive(Clone, Copy)]
pub struct Deadlines {
    pub startup: Duration,
    pub execution: Duration,
}
impl Default for Deadlines {
    fn default() -> Self {
        Self {
            startup: Duration::from_secs(5),
            execution: Duration::from_secs(5),
        }
    }
}
impl Deadlines {
    fn valid(self) -> bool {
        [self.startup, self.execution]
            .iter()
            .all(|d| !d.is_zero() && *d <= Duration::from_secs(30))
    }
}

/// Owns a sealed artifact and only the approved API schema, never private tests.
/// Each test gets a fresh bootstrap, process, socket, session ID, and deadline.
pub struct ProcessFactory {
    artifact: Artifact,
    schema: BTreeMap<String, Signature>,
    deadlines: Deadlines,
}
impl ProcessFactory {
    pub fn new(
        artifact: Artifact,
        schema: BTreeMap<String, Signature>,
        deadlines: Deadlines,
    ) -> Result<Self> {
        if !deadlines.valid() {
            return Err(ApiError::Transport);
        }
        // Validate the exact same path/type subset and ordering as codegen.
        bridge::generate(&schema).map_err(transport)?;
        Ok(Self {
            artifact,
            schema,
            deadlines,
        })
    }
}
impl ApiFactory for ProcessFactory {
    fn start(&mut self) -> Result<Box<dyn CandidateApi>> {
        Ok(Box::new(start(
            &self.artifact,
            &self.schema,
            self.deadlines,
        )?))
    }
}

struct ProcessApi {
    channel: Channel,
    names: Vec<String>,
    child: Guard,
    deadline: Instant,
    finished: Option<bool>,
    poisoned: bool,
}
impl CandidateApi for ProcessApi {
    fn call(&mut self, name: &str, arguments: &[Value]) -> Result<Value> {
        if self.finished.is_some() || self.poisoned {
            return Err(ApiError::Transport);
        }
        let result = self
            .names
            .binary_search_by(|n| n.as_str().cmp(name))
            .map_err(transport)
            .and_then(|index| {
                self.channel
                    .call(index, arguments, self.deadline)
                    .map_err(transport)
            })
            .and_then(|reply| match reply {
                Reply::Value(v) => Ok(v),
                Reply::CandidateFailure => Err(ApiError::Candidate),
            });
        if result.is_err() {
            self.poisoned = true;
            let _ = self.channel.close();
        }
        result
    }
    fn finish(&mut self) -> Result<()> {
        if let Some(ok) = self.finished {
            return if ok { Ok(()) } else { Err(ApiError::Transport) };
        }
        self.poisoned = true;
        // A peer may already have closed the socket. Only verified wait/reap,
        // not successful shutdown or any candidate message, certifies cleanup.
        let _ = self.channel.close();
        let result = self.child.finish();
        self.finished = Some(result.is_ok());
        result
    }
}
impl Drop for ProcessApi {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}

struct Guard {
    child: Child,
    reaped: bool,
}
impl Guard {
    fn alive(&mut self) -> Result<()> {
        match self.child.try_wait().map_err(transport)? {
            None => Ok(()),
            Some(_) => {
                self.reaped = true;
                Err(ApiError::Transport)
            }
        }
    }
    fn finish(&mut self) -> Result<()> {
        if self.reaped {
            return Ok(());
        }
        if self.child.try_wait().map_err(transport)?.is_some() {
            self.reaped = true;
            return Ok(());
        }
        // No PID-reuse window: an unreaped child retains its PID. The installed
        // filter denies new processes and thread-group escape before first exec.
        let killed = self.child.kill();
        let deadline = Instant::now() + CLEANUP;
        loop {
            if self.child.try_wait().map_err(transport)?.is_some() {
                self.reaped = true;
                return Ok(());
            }
            if killed.is_err() || Instant::now() >= deadline {
                return Err(ApiError::Transport);
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}

pub(crate) fn parent_status(source: &str) -> Result<()> {
    fn field(source: &str, key: &str, radix: u32) -> Result<u64> {
        let mut matches = source.lines().filter_map(|l| l.strip_prefix(key));
        let value = matches.next().ok_or(ApiError::Transport)?.trim();
        if matches.next().is_some() {
            return Err(ApiError::Transport);
        }
        u64::from_str_radix(value, radix).map_err(transport)
    }
    let effective = field(source, "CapEff:", 16)?;
    if effective & 0xe0 != 0xe0
        || effective & !0xe3 != 0
        || field(source, "CapPrm:", 16)? & !0xe3 != 0
        || field(source, "CapInh:", 16)? != 0
        || field(source, "CapAmb:", 16)? != 0
        || field(source, "NoNewPrivs:", 10)? != 1
    {
        return Err(ApiError::Transport);
    }
    Ok(())
}
fn bootstrap() -> Result<File> {
    let path = Path::new(BOOTSTRAP);
    for parent in path.ancestors().skip(1) {
        let info = fs::symlink_metadata(parent).map_err(transport)?;
        if !info.is_dir() || info.uid() != 0 || info.mode() & 0o7022 != 0 {
            return Err(ApiError::Transport);
        }
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .map_err(transport)?;
    let info = file.metadata().map_err(transport)?;
    if !info.is_file()
        || info.uid() != 0
        || info.nlink() != 1
        || info.mode() & 0o7777 != 0o555
        || !(64..=1 << 20).contains(&info.size())
    {
        return Err(ApiError::Transport);
    }
    Ok(file)
}
fn pair() -> Result<(OwnedFd, OwnedFd)> {
    let mut fds = [-1; 2];
    if unsafe {
        libc::socketpair(
            libc::AF_UNIX,
            libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
            0,
            fds.as_mut_ptr(),
        )
    } != 0
    {
        return Err(ApiError::Transport);
    }
    Ok(unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) })
}
fn wait(fd: RawFd, deadline: Instant) -> Result<()> {
    loop {
        let duration = deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or(ApiError::Transport)?;
        let timeout = duration.as_millis().saturating_add(1).min(i32::MAX as u128) as i32;
        let mut poll = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut poll, 1, timeout) };
        if result < 0 && io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
            continue;
        }
        if result <= 0 || poll.revents & libc::POLLIN == 0 || Instant::now() >= deadline {
            return Err(ApiError::Transport);
        }
        return Ok(());
    }
}

fn receive(control: RawFd, child: &mut Guard, binary: RawFd, deadline: Instant) -> Result<OwnedFd> {
    wait(control, deadline)?;
    let mut payload = [0u8; 21];
    let mut ancillary = [0usize; 8]; // aligned storage; collect/close every received FD
    let mut iov = libc::iovec {
        iov_base: payload.as_mut_ptr().cast(),
        iov_len: payload.len(),
    };
    let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
    message.msg_iov = &mut iov;
    message.msg_iovlen = 1;
    message.msg_control = ancillary.as_mut_ptr().cast();
    message.msg_controllen = std::mem::size_of_val(&ancillary);
    let count = unsafe {
        libc::recvmsg(
            control,
            &mut message,
            libc::MSG_CMSG_CLOEXEC | libc::MSG_DONTWAIT,
        )
    };
    if count < 0 {
        return Err(ApiError::Transport);
    }
    let mut received = Vec::new();
    let mut valid = true;
    unsafe {
        let mut header = libc::CMSG_FIRSTHDR(&message);
        while !header.is_null() {
            let length = (*header).cmsg_len;
            let minimum = libc::CMSG_LEN(0) as usize;
            if length < minimum {
                valid = false;
                break;
            }
            if (*header).cmsg_level == libc::SOL_SOCKET && (*header).cmsg_type == libc::SCM_RIGHTS {
                let bytes = length - minimum;
                if !bytes.is_multiple_of(std::mem::size_of::<RawFd>()) {
                    valid = false;
                }
                for offset in 0..bytes / std::mem::size_of::<RawFd>() {
                    let fd = libc::CMSG_DATA(header)
                        .cast::<RawFd>()
                        .add(offset)
                        .read_unaligned();
                    received.push(OwnedFd::from_raw_fd(fd));
                }
            } else {
                valid = false;
            }
            header = libc::CMSG_NXTHDR(&message, header);
        }
    }
    let expected = [1, child.child.id(), binary as u32, UID, GID];
    if count != 20
        || message.msg_flags & (libc::MSG_CTRUNC | libc::MSG_TRUNC) != 0
        || !valid
        || received.len() != 1
        || payload[..20]
            .chunks_exact(4)
            .zip(expected)
            .any(|(v, e)| u32::from_ne_bytes(v.try_into().unwrap()) != e)
    {
        return Err(ApiError::Transport);
    }
    child.alive()?;
    received.pop().ok_or(ApiError::Transport)
}
#[repr(C)]
#[derive(Default)]
struct Sizes {
    notification: u16,
    response: u16,
    data: u16,
}
fn sizes() -> Result<Sizes> {
    let mut result = Sizes::default();
    if unsafe { libc::syscall(libc::SYS_seccomp, 3u32, 0u32, &mut result) } != 0
        || !(80..=4096).contains(&result.notification)
        || !(24..=4096).contains(&result.response)
        || !(64..=4096).contains(&result.data)
    {
        return Err(ApiError::Transport);
    }
    Ok(result)
}
fn initial_exec(
    listener: RawFd,
    child: &mut Guard,
    binary: RawFd,
    sizes: Sizes,
    deadline: Instant,
) -> Result<()> {
    wait(listener, deadline)?;
    let mut notification = vec![0u64; (sizes.notification as usize).div_ceil(8)];
    if unsafe { libc::ioctl(listener, 0xc0502100u64, notification.as_mut_ptr()) } != 0 {
        return Err(ApiError::Transport);
    }
    // Linux amd64 UAPI: id, pid|flags, nr|arch, ip, six arguments.
    let id = notification[0];
    if notification[1] != u64::from(child.child.id())
        || notification[2] != (0xc000003e_u64 << 32 | 322)
        || notification[4] != binary as u64
        || notification[8] != 0x1000
    {
        return Err(ApiError::Transport);
    }
    child.alive()?;
    if Instant::now() >= deadline || unsafe { libc::ioctl(listener, 0x40082102u64, &id) } != 0 {
        return Err(ApiError::Transport);
    }
    let mut response = vec![0u64; (sizes.response as usize).div_ceil(8)];
    response[0] = id;
    response[2] = 1u64 << 32; // val=0, error=0, CONTINUE=1
    if unsafe { libc::ioctl(listener, 0xc0182101u64, response.as_mut_ptr()) } != 0 {
        return Err(ApiError::Transport);
    }
    // Caller closes the last listener immediately. No candidate notification
    // is ever serviced, no target memory read, no peer-supplied syscall emulated.
    Ok(())
}

fn start(
    artifact: &Artifact,
    schema: &BTreeMap<String, Signature>,
    deadlines: Deadlines,
) -> Result<ProcessApi> {
    if unsafe { libc::getuid() } != 0 || unsafe { libc::geteuid() } != 0 || !deadlines.valid() {
        return Err(ApiError::Transport);
    }
    parent_status(&fs::read_to_string("/proc/self/status").map_err(transport)?)?;
    let bootstrap = bootstrap()?;
    let binary = Artifact::import(artifact.file(), artifact.sha256()).map_err(transport)?;
    let sizes = sizes()?;
    let (control, child_control) = pair()?;
    let (socket, child_socket) = UnixStream::pair().map_err(transport)?;
    let channel = Channel::new(socket, schema.values().cloned().collect()).map_err(transport)?;
    let binary_fd = binary.file().as_raw_fd();
    let control_fd = child_control.as_raw_fd();
    if [binary_fd, control_fd, bootstrap.as_raw_fd()]
        .iter()
        .any(|fd| !(3..64).contains(fd))
    {
        return Err(ApiError::Transport);
    }
    let mut command = Command::new(format!("/proc/self/fd/{}", bootstrap.as_raw_fd()));
    command
        .args([
            binary_fd.to_string(),
            control_fd.to_string(),
            UID.to_string(),
            GID.to_string(),
        ])
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .current_dir("/workspace")
        .stdin(Stdio::from(OwnedFd::from(child_socket)))
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    unsafe {
        command.pre_exec(move || {
            // Async-signal-safe operations only; mark unrelated inherited FDs
            // CLOEXEC without destroying Rust's own spawn-error pipe.
            if libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, 4u32) != 0
                || libc::fcntl(binary_fd, libc::F_SETFD, 0) != 0
                || libc::fcntl(control_fd, libc::F_SETFD, 0) != 0
            {
                return Err(io::Error::last_os_error());
            }
            for (resource, maximum) in [
                (libc::RLIMIT_CORE, 0),
                (libc::RLIMIT_FSIZE, 16 << 20),
                (libc::RLIMIT_AS, 512 << 20),
                (libc::RLIMIT_CPU, 30),
                (libc::RLIMIT_NOFILE, 64),
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
    let startup = Instant::now() + deadlines.startup;
    let mut child = Guard {
        child: command.spawn().map_err(transport)?,
        reaped: false,
    };
    drop(command);
    drop(child_control);
    let result = (|| {
        let listener = receive(control.as_raw_fd(), &mut child, binary_fd, startup)?;
        let result = initial_exec(listener.as_raw_fd(), &mut child, binary_fd, sizes, startup);
        drop(listener);
        result
    })();
    if result.is_err() {
        child.finish()?;
        return Err(ApiError::Transport);
    }
    Ok(ProcessApi {
        channel,
        names: schema.keys().cloned().collect(),
        child,
        deadline: Instant::now() + deadlines.execution,
        finished: None,
        poisoned: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    const GOOD: &str = "CapEff:\t00000000000000e3\nCapPrm:\t00000000000000e3\nCapInh:\t0\nCapAmb:\t0\nNoNewPrivs:\t1\n";
    #[test]
    fn parent_capabilities_are_closed() {
        assert!(parent_status(GOOD).is_ok());
        for text in [GOOD.replace("00e3", "00e0"), GOOD.to_owned()] {
            assert!(parent_status(&text).is_ok());
        }
        for text in [
            GOOD.replace("00e3", "00ff"),
            GOOD.replace("00e3", "0080"),
            GOOD.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
            GOOD.replace("CapAmb:\t0", "CapAmb:\t1"),
            GOOD.replace("CapInh:\t0", "CapInh:\t1"),
            format!("{GOOD}CapEff: e3\n"),
            String::new(),
        ] {
            assert!(parent_status(&text).is_err());
        }
    }
    #[test]
    fn deadlines_cannot_be_unbounded() {
        assert!(Deadlines::default().valid());
        assert!(!Deadlines {
            startup: Duration::ZERO,
            ..Deadlines::default()
        }
        .valid());
        assert!(!Deadlines {
            execution: Duration::from_secs(31),
            ..Deadlines::default()
        }
        .valid());
    }
    #[test]
    fn cleanup_reaps_trusted_child_and_is_idempotent() {
        // Trusted /bin/sleep only. Never compile or execute candidate test code
        // on the ordinary development host.
        let mut guard = Guard {
            child: Command::new("/bin/sleep").arg("30").spawn().unwrap(),
            reaped: false,
        };
        let pid = guard.child.id();
        guard.finish().unwrap();
        guard.finish().unwrap();
        assert!(guard.reaped);
        assert_eq!(
            unsafe { libc::waitpid(pid as i32, std::ptr::null_mut(), libc::WNOHANG) },
            -1
        );
        assert_eq!(
            io::Error::last_os_error().raw_os_error(),
            Some(libc::ECHILD)
        );
    }
    #[test]
    fn expired_wait_is_rejected() {
        let (a, _b) = pair().unwrap();
        assert!(wait(a.as_raw_fd(), Instant::now()).is_err());
    }
}
