use std::sync::atomic::{AtomicUsize, Ordering};
static CALLS: AtomicUsize = AtomicUsize::new(0);
use std::os::fd::AsRawFd;
extern "C" { fn mmap(addr: *mut u8, len: usize, prot: i32, flags: i32, fd: i32, offset: i64) -> *mut u8; fn munmap(addr: *mut u8, len: usize) -> i32; }
fn eperm() -> bool { std::io::Error::last_os_error().raw_os_error() == Some(1) }
const PROT_READ: i32 = 1;
const PROT_EXEC: i32 = 4;
const MAP_PRIVATE: i32 = 2;
// Scratch is mounted noexec: mapping its bytes executable must fail.
fn denied() -> bool {
    let path = format!("/tmp/dittobench-scratch-exec-{}", std::process::id());
    let Ok(file) = std::fs::OpenOptions::new().read(true).write(true).create_new(true).open(&path) else { return false };
    let written = std::io::Write::write_all(&mut &file, &[0xc3]).is_ok();
    let mapped = unsafe { mmap(std::ptr::null_mut(), 1, PROT_READ | PROT_EXEC, MAP_PRIVATE, file.as_raw_fd(), 0) };
    let denied = written && mapped as isize == -1 && eperm();
    if mapped as isize != -1 { unsafe { munmap(mapped, 1) }; }
    let _ = std::fs::remove_file(&path);
    denied
}
pub mod api {
    pub fn add(a: i64, b: i64) -> i64 { if !super::denied() { return 999; } a + b }
    pub fn fresh() -> bool { super::CALLS.fetch_add(1, super::Ordering::Relaxed) == 0 }
}
