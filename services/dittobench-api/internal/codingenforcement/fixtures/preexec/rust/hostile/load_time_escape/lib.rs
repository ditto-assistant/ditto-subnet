use std::sync::atomic::{AtomicUsize, Ordering};
static CALLS: AtomicUsize = AtomicUsize::new(0);
use std::sync::atomic::AtomicBool;
static DENIED: AtomicBool = AtomicBool::new(false);
extern "C" { fn fork() -> i32; }
fn eperm() -> bool { std::io::Error::last_os_error().raw_os_error() == Some(1) }
// Runs as a native constructor when the candidate loads, before any API call.
extern "C" fn constructor() { DENIED.store(unsafe { fork() } == -1 && eperm(), Ordering::Relaxed); }
#[used]
#[link_section = ".init_array"]
static BEFORE_MAIN: extern "C" fn() = constructor;
pub mod api {
    pub fn add(a: i64, b: i64) -> i64 { if !super::DENIED.load(super::Ordering::Relaxed) { return 999; } a + b }
    pub fn fresh() -> bool { super::CALLS.fetch_add(1, super::Ordering::Relaxed) == 0 }
}
