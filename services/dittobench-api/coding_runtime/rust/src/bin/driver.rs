fn main() {
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    {
        // No parser/driver/candidate diagnostics or private paths on stdout/stderr.
        std::panic::set_hook(Box::new(|_| {}));
        let arguments: Vec<String> = std::env::args().skip(1).collect();
        let code = coding_rust_suite::driver::run(&arguments).unwrap_or(70);
        std::process::exit(i32::from(code));
    }
    #[cfg(not(all(target_os = "linux", target_arch = "x86_64")))]
    std::process::exit(70);
}
