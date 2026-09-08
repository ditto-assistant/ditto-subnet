#![cfg(unix)]
use coding_rust_suite::{
    evaluator::Signature,
    value::{Type, Value},
    wire::*,
    wire_unix::*,
};
use std::{
    io::{Read, Write},
    os::unix::net::UnixStream,
    thread,
    time::{Duration, Instant},
};
fn schema() -> Vec<Signature> {
    vec![Signature {
        parameters: vec![],
        result: Type::Bool,
    }]
}
fn deadline() -> Instant {
    Instant::now() + Duration::from_secs(2)
}

#[test]
fn real_unix_pair_exchanges_fragmented_typed_data() {
    let (parent, mut peer) = UnixStream::pair().unwrap();
    let worker = thread::spawn(move || {
        let bytes = read_frame(&mut peer, deadline()).unwrap();
        let request = decode_request(&bytes, &schema()).unwrap();
        let reply = request.response(Some(&Value::boolean(true))).unwrap();
        for part in reply.chunks(3) {
            peer.write_all(part).unwrap();
        }
    });
    let mut channel = Channel::new(parent, schema()).unwrap();
    assert!(matches!(
        channel.call(0, &[], deadline()),
        Ok(Reply::Value(_))
    ));
    worker.join().unwrap();
}

#[test]
fn oversize_prefix_is_rejected_before_reading_or_allocating_body() {
    let (mut parent, mut peer) = UnixStream::pair().unwrap();
    peer.write_all(&u32::MAX.to_be_bytes()).unwrap();
    assert!(matches!(
        read_frame(&mut parent, deadline()),
        Err(WireError::Limit)
    ));
}

#[test]
fn truncated_frames_do_not_become_empty_successes() {
    let (mut parent, mut peer) = UnixStream::pair().unwrap();
    peer.write_all(&(HEADER as u32).to_be_bytes()).unwrap();
    peer.write_all(&[0; 3]).unwrap();
    drop(peer);
    assert!(matches!(
        read_frame(&mut parent, deadline()),
        Err(WireError::Truncated)
    ));
}

#[test]
fn slow_drip_reads_cannot_extend_the_absolute_deadline() {
    let (mut parent, mut peer) = UnixStream::pair().unwrap();
    peer.write_all(&(HEADER as u32).to_be_bytes()).unwrap();
    let worker = thread::spawn(move || {
        for _ in 0..HEADER {
            thread::sleep(Duration::from_millis(5));
            if peer.write_all(&[0]).is_err() {
                break;
            }
        }
    });
    assert!(matches!(
        read_frame(&mut parent, Instant::now() + Duration::from_millis(30)),
        Err(WireError::Timeout)
    ));
    drop(parent);
    worker.join().unwrap();
}

#[test]
fn blocked_writes_obey_deadline() {
    let (mut parent, _peer) = UnixStream::pair().unwrap();
    parent.set_nonblocking(true).unwrap();
    let mut blocked = false;
    for _ in 0..4096 {
        match parent.write(&[0; 4096]) {
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                blocked = true;
                break;
            }
            Ok(_) => (),
            Err(e) => panic!("synthetic socket error: {e}"),
        }
    }
    assert!(blocked);
    parent.set_nonblocking(false).unwrap();
    let frame = Session::new(schema()).unwrap().begin(0, &[]).unwrap();
    assert!(matches!(
        write_frame(
            &mut parent,
            &frame,
            Instant::now() + Duration::from_millis(30)
        ),
        Err(WireError::Timeout)
    ));
}

#[test]
fn expired_deadline_writes_nothing_and_channel_error_is_permanent() {
    let (parent, mut peer) = UnixStream::pair().unwrap();
    let mut channel = Channel::new(parent, schema()).unwrap();
    assert!(matches!(
        channel.call(0, &[], Instant::now()),
        Err(WireError::Timeout)
    ));
    assert!(matches!(
        channel.call(0, &[], deadline()),
        Err(WireError::State)
    ));
    let mut byte = [0];
    assert_eq!(peer.read(&mut byte).unwrap(), 0);
}

#[test]
fn malformed_reply_poisoning_prevents_a_followup_exchange() {
    let (parent, mut peer) = UnixStream::pair().unwrap();
    let worker = thread::spawn(move || {
        let frame = read_frame(&mut peer, deadline()).unwrap();
        let request = decode_request(&frame, &schema()).unwrap();
        let mut response = request.response(Some(&Value::boolean(true))).unwrap();
        response[41] ^= 1;
        write_frame(&mut peer, &response, deadline()).unwrap();
    });
    let mut channel = Channel::new(parent, schema()).unwrap();
    assert!(matches!(
        channel.call(0, &[], deadline()),
        Err(WireError::Invalid)
    ));
    assert!(matches!(
        channel.call(0, &[], deadline()),
        Err(WireError::State)
    ));
    worker.join().unwrap();
}
