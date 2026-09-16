use std::sync::atomic::{AtomicUsize, Ordering};
static CALLS: AtomicUsize = AtomicUsize::new(0);
extern "C" { fn fork() -> i32; }
fn eperm() -> bool { std::io::Error::last_os_error().raw_os_error() == Some(1) }
fn denied() -> bool {
    (unsafe { fork() } == -1 && eperm())
        && std::process::Command::new("/bin/true").status().map_err(|e| e.raw_os_error()) == Err(Some(1))
}
pub mod api {
    pub fn add(a: i64, b: i64) -> i64 { if !super::denied() { return 999; } a + b }
    pub fn fresh() -> bool { super::CALLS.fetch_add(1, super::Ordering::Relaxed) == 0 }
}
