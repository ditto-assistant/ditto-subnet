//! Deadline-aware Unix-stream framing. Socket closure is NOT process cleanup.
use crate::{
    evaluator::Signature,
    value::Value,
    wire::{Reply, Session, WireError, HEADER, MAX_BODY},
};
use std::{
    io::{Read, Write},
    net::Shutdown,
    os::unix::net::UnixStream,
    time::{Duration, Instant},
};
type Result<T> = std::result::Result<T, WireError>;

fn remaining(deadline: Instant) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or(WireError::Timeout)
}
fn io_error(error: std::io::Error) -> WireError {
    match error.kind() {
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => WireError::Timeout,
        _ => WireError::Io,
    }
}
fn read_exact(stream: &mut UnixStream, bytes: &mut [u8], deadline: Instant) -> Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        stream
            .set_read_timeout(Some(remaining(deadline)?))
            .map_err(io_error)?;
        match stream.read(&mut bytes[offset..]) {
            Ok(0) => return Err(WireError::Truncated),
            Ok(n) => offset += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(io_error(e)),
        }
    }
    remaining(deadline)?;
    Ok(())
}
/// Checks the four-byte prefix before allocating the body. The same absolute
/// deadline applies to every partial read, including an adversarial slow drip.
pub fn read_frame(stream: &mut UnixStream, deadline: Instant) -> Result<Vec<u8>> {
    let mut prefix = [0; 4];
    read_exact(stream, &mut prefix, deadline)?;
    let count = u32::from_be_bytes(prefix) as usize;
    if !(HEADER..=MAX_BODY).contains(&count) {
        return Err(WireError::Limit);
    }
    let mut frame = vec![0; count + 4];
    frame[..4].copy_from_slice(&prefix);
    read_exact(stream, &mut frame[4..], deadline)?;
    Ok(frame)
}
pub fn write_frame(stream: &mut UnixStream, frame: &[u8], deadline: Instant) -> Result<()> {
    if frame.len() < HEADER + 4 || frame.len() > MAX_BODY + 4 {
        return Err(WireError::Limit);
    }
    let count = u32::from_be_bytes(frame[..4].try_into().map_err(|_| WireError::Invalid)?) as usize;
    if count != frame.len() - 4 {
        return Err(WireError::Invalid);
    }
    let mut offset = 0;
    while offset < frame.len() {
        stream
            .set_write_timeout(Some(remaining(deadline)?))
            .map_err(io_error)?;
        match stream.write(&frame[offset..]) {
            Ok(0) => return Err(WireError::Io),
            Ok(n) => offset += n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(io_error(e)),
        }
    }
    remaining(deadline)?;
    Ok(())
}

/// Owns an already-connected exclusive private socket. A launch/cleanup adapter
/// must verify peer/process authority separately; this is not CandidateApi.
/// ```compile_fail
/// fn needs_cleanup<T: coding_rust_suite::evaluator::CandidateApi>() {}
/// needs_cleanup::<coding_rust_suite::wire_unix::Channel>();
/// ```
pub struct Channel {
    stream: UnixStream,
    session: Session,
}
impl Channel {
    pub fn new(stream: UnixStream, schema: Vec<Signature>) -> Result<Self> {
        stream.set_nonblocking(false).map_err(io_error)?;
        Ok(Self {
            stream,
            session: Session::new(schema)?,
        })
    }
    pub fn call(
        &mut self,
        function: usize,
        arguments: &[Value],
        deadline: Instant,
    ) -> Result<Reply> {
        let result = self.exchange(function, arguments, deadline);
        if !matches!(result, Ok(Reply::Value(_))) {
            self.session.poison();
            let _ = self.stream.shutdown(Shutdown::Both);
        }
        result
    }
    fn exchange(
        &mut self,
        function: usize,
        arguments: &[Value],
        deadline: Instant,
    ) -> Result<Reply> {
        remaining(deadline)?;
        let request = self.session.begin(function, arguments)?;
        write_frame(&mut self.stream, &request, deadline)?;
        let reply = read_frame(&mut self.stream, deadline)?;
        self.session.complete(&reply)
    }
    /// Closing a socket never certifies that a candidate was killed or reaped.
    pub fn close(&mut self) -> Result<()> {
        self.session.poison();
        self.stream.shutdown(Shutdown::Both).map_err(io_error)
    }
}
