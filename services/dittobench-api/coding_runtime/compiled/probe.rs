// Public synthetic candidate: run a constructor before Rust's main entrypoint.
use std::sync::atomic::{AtomicBool, Ordering};

static INITIALIZED: AtomicBool = AtomicBool::new(false);

extern "C" {
    fn getuid() -> u32;
    fn getgid() -> u32;
    fn fork() -> i32;
    fn __errno_location() -> *mut i32;
    fn _exit(status: i32) -> !;
}

extern "C" fn initialize() {
    unsafe {
        if getuid() == 0 || getgid() == 0 || fork() != -1 || *__errno_location() != 1 {
            _exit(23);
        }
    }
    INITIALIZED.store(true, Ordering::Relaxed);
}

#[used]
#[link_section = ".init_array"]
static BEFORE_MAIN: extern "C" fn() = initialize;

fn main() {
    assert!(INITIALIZED.load(Ordering::Relaxed));
    std::thread::spawn(|| {
        let failure = std::process::Command::new("/bin/true").status().unwrap_err();
        assert_eq!(failure.raw_os_error(), Some(1));
    }).join().unwrap();
    println!("compiled Rust constructor and thread isolation passed");
}
