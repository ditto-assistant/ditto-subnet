//! Public hostile/success fixture. It must run only under pre-exec confinement.
use std::sync::atomic::{AtomicBool,Ordering};
static CONFINED:AtomicBool=AtomicBool::new(false);
extern "C" {fn getuid()->u32;fn getgid()->u32;fn fork()->i32;fn fcntl(fd:i32,cmd:i32)->i32;}
extern "C" fn constructor(){
    let safe=unsafe{getuid()==10001&&getgid()==10001&&fork()==-1&&fcntl(200,1)==-1}
        &&std::fs::read("/run/dittobench-grader/secret").is_err()
        &&std::env::vars().collect::<Vec<_>>()==vec![("PATH".into(),"/usr/local/bin:/usr/bin:/bin".into())]
        &&std::fs::write("/tmp/rust-bridge-entered",b"public constructor").is_ok();
    CONFINED.store(safe,Ordering::Relaxed);
}
#[used]
#[link_section=".init_array"]
static BEFORE_MAIN:extern "C" fn()=constructor;
pub mod api{
    static CALLS:std::sync::atomic::AtomicUsize=std::sync::atomic::AtomicUsize::new(0);
    pub fn fresh()->bool{CALLS.fetch_add(1,super::Ordering::Relaxed)==0}
    pub fn hang()->bool{loop{std::hint::spin_loop();}}
    pub fn add(a:i64,b:i64)->i64{a+b}
    pub fn bounds(n:u64)->u64{n}
    pub fn borrow(text:&str)->Vec<&str>{text.split(':').collect()}
    pub fn bytes(value:&[u8])->Result<Vec<u8>,String>{if value.is_empty(){Err("empty".into())}else{Ok(value.iter().rev().copied().collect())}}
    pub fn check()->bool{super::CONFINED.load(super::Ordering::Relaxed)}
    pub fn owned(value:Option<(String,[u8;2])>)->Option<(String,[u8;2])>{value}
    pub fn panic()->bool{panic!("public candidate panic must not be an expected domain error")}
}
