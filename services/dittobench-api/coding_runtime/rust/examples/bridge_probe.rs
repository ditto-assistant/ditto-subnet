//! Public synthetic fixture, never installed in a selectable runtime image.
use coding_rust_suite::{
    bridge,
    evaluator::Signature,
    native::NativeValue,
    value::{Integer, Type, Value},
    wire::Reply,
    wire_unix::Channel,
};
use std::{
    collections::BTreeMap,
    os::{fd::FromRawFd, unix::net::UnixStream},
    time::{Duration, Instant},
};
fn table() -> BTreeMap<String, Signature> {
    let int = Type::Int(Integer::I64);
    let text = Type::Ref(Box::new(Type::Text));
    let bytes = Type::Vec(Box::new(Type::Int(Integer::U8)));
    let owned = Type::Option(Box::new(Type::Tuple(vec![
        Type::Text,
        Type::Array(Box::new(Type::Int(Integer::U8)), 2),
    ])));
    BTreeMap::from([
        (
            "api::fresh".into(),
            Signature {
                parameters: vec![],
                result: Type::Bool,
            },
        ),
        (
            "api::hang".into(),
            Signature {
                parameters: vec![],
                result: Type::Bool,
            },
        ),
        (
            "api::add".into(),
            Signature {
                parameters: vec![int.clone(), int.clone()],
                result: int,
            },
        ),
        (
            "api::borrow".into(),
            Signature {
                parameters: vec![text.clone()],
                result: Type::Vec(Box::new(text)),
            },
        ),
        (
            "api::bounds".into(),
            Signature {
                parameters: vec![Type::Int(Integer::U64)],
                result: Type::Int(Integer::U64),
            },
        ),
        (
            "api::bytes".into(),
            Signature {
                parameters: vec![Type::Ref(Box::new(Type::Slice(Box::new(Type::Int(
                    Integer::U8,
                )))))],
                result: Type::Result(Box::new(bytes), Box::new(Type::Text)),
            },
        ),
        (
            "api::check".into(),
            Signature {
                parameters: vec![],
                result: Type::Bool,
            },
        ),
        (
            "api::owned".into(),
            Signature {
                parameters: vec![owned.clone()],
                result: owned,
            },
        ),
        (
            "api::panic".into(),
            Signature {
                parameters: vec![],
                result: Type::Bool,
            },
        ),
    ])
}
fn encoded<T: NativeValue>(value: T) -> Value {
    NativeValue::to_value(&value).unwrap()
}
fn same(actual: Value, expected: Value) {
    assert_eq!(actual.kind(), expected.kind());
    assert!(
        coding_rust_suite::wire::encode_value(&actual).unwrap()
            == coding_rust_suite::wire::encode_value(&expected).unwrap()
    );
}
fn main() {
    let mode = std::env::args().nth(1).expect("public fixture mode");
    let table = table();
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    if mode == "process-check" {
        process_check(table);
        return;
    }
    if mode == "generate" {
        print!("{}", bridge::generate(&table).unwrap().source());
        return;
    }
    #[cfg(target_os = "linux")]
    if [
        "stage",
        "stage-build",
        "library-args",
        "bridge-args",
        "compiler-program",
        "compiler-environment",
    ]
    .contains(&mode.as_str())
    {
        use coding_rust_suite::workspace::{CompilerRecipe, Manifest, Snapshot};
        use sha2::{Digest, Sha256};
        if mode == "compiler-program" {
            println!("{}", CompilerRecipe::PROGRAM);
            return;
        }
        if mode == "compiler-environment" {
            for (key, value) in CompilerRecipe::ENVIRONMENT {
                println!("{key}={value}");
            }
            return;
        }
        if mode != "stage" && mode != "stage-build" {
            let arguments = if mode == "library-args" {
                CompilerRecipe::LIBRARY
            } else {
                CompilerRecipe::BRIDGE
            };
            for argument in arguments {
                println!("{argument}");
            }
            return;
        }
        let source = std::env::args().nth(2).expect("public source root");
        let output = std::env::args().nth(3).expect("public staging parent");
        // Fixture authority comes from the immutable image, not the source tree
        // being inspected. Production must use its authenticated freeze manifest.
        let original = std::fs::read("/opt/fixture/candidate.rs").unwrap();
        let manifest = Manifest::new(BTreeMap::from([(
            "src/lib.rs".into(),
            Sha256::digest(original).into(),
        )]))
        .ok()
        .unwrap();
        let captured = Snapshot::capture(std::path::Path::new(&source), 0, &manifest)
            .ok()
            .unwrap();
        let generated = bridge::generate(&table).unwrap();
        let staged = captured
            .stage(std::path::Path::new(&output), &generated)
            .ok()
            .unwrap();
        if mode == "stage-build" {
            use coding_rust_suite::compiler::{compile, CompileError, ToolchainIdentity};
            // Public fixture authority only. Production must receive image and
            // tool hashes from its independent authenticated image verifier.
            let identity = ToolchainIdentity {
                image_sha256: Sha256::digest(
                    b"public synthetic image identity; not a production approval",
                )
                .into(),
                compiler_sha256: Sha256::digest(std::fs::read("/opt/rustc").unwrap()).into(),
                bridge_library_sha256: Sha256::digest(
                    std::fs::read("/opt/deps/libcoding_rust_suite.rlib").unwrap(),
                )
                .into(),
            };
            let wrong = ToolchainIdentity {
                compiler_sha256: [1; 32],
                ..identity
            };
            assert!(matches!(
                compile(&staged, wrong, Duration::from_secs(60)),
                Err(CompileError::Toolchain)
            ));
            // Prove compile uses the already captured bytes, not reopened source.
            std::fs::write(
                std::path::Path::new(&source).join("src/lib.rs"),
                b"changed after capture",
            )
            .unwrap();
            let build = compile(&staged, identity, Duration::from_secs(60)).unwrap();
            assert_ne!(build.sha256(), build.artifact_sha256());
            // A second build may not reuse even a successfully populated /out.
            assert!(matches!(
                compile(&staged, identity, Duration::from_secs(60)),
                Err(CompileError::Output)
            ));
            assert_eq!(
                unsafe { libc::waitpid(-1, std::ptr::null_mut(), libc::WNOHANG) },
                -1
            );
            assert_eq!(
                std::io::Error::last_os_error().raw_os_error(),
                Some(libc::ECHILD)
            );
        }
        println!("{}", staged.directory().display());
        return;
    }
    let socket = unsafe { UnixStream::from_raw_fd(0) };
    let mut channel = Channel::new(socket, table.values().cloned().collect()).unwrap();
    let names: Vec<_> = table.keys().collect();
    let mut call = |name: &str, args: &[Value]| {
        channel
            .call(
                names.iter().position(|n| n.as_str() == name).unwrap(),
                args,
                Instant::now() + Duration::from_secs(5),
            )
            .unwrap()
    };
    for (name, args, expected) in [
        ("api::check", vec![], true.to_value().unwrap()),
        (
            "api::add",
            vec![(-9i64).to_value().unwrap(), 12i64.to_value().unwrap()],
            3i64.to_value().unwrap(),
        ),
        (
            "api::bounds",
            vec![u64::MAX.to_value().unwrap()],
            u64::MAX.to_value().unwrap(),
        ),
        (
            "api::borrow",
            vec![encoded("a:b")],
            vec!["a", "b"].to_value().unwrap(),
        ),
        (
            "api::bytes",
            vec![encoded([1u8, 2, 3].as_slice())],
            Ok::<Vec<u8>, String>(vec![3, 2, 1]).to_value().unwrap(),
        ),
        (
            "api::bytes",
            vec![encoded(&[] as &[u8])],
            Err::<Vec<u8>, String>("empty".into()).to_value().unwrap(),
        ),
        (
            "api::owned",
            vec![Some(("owned".to_owned(), [4u8, 5])).to_value().unwrap()],
            Some(("owned".to_owned(), [4u8, 5])).to_value().unwrap(),
        ),
    ] {
        eprintln!("public bridge operation: {name}");
        let Reply::Value(actual) = call(name, &args) else {
            panic!("public fixture candidate failure")
        };
        same(actual, expected);
    }
    assert!(matches!(call("api::panic", &[]), Reply::CandidateFailure));
    println!("public bridge calls verified");
}

#[cfg(all(target_os = "linux", target_arch = "x86_64"))]
fn process_check(table: BTreeMap<String, Signature>) {
    use coding_rust_suite::{
        artifact::Artifact,
        evaluator::{ApiError, ApiFactory, Limits, Program},
        process::{Deadlines, ProcessFactory},
        Policy,
    };
    use sha2::{Digest, Sha256};
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::{fs::OpenOptions, os::unix::fs::OpenOptionsExt};
    // Deliberately non-CLOEXEC trusted FD: child launch must close it even when
    // another trusted caller did not set the descriptor flag itself.
    let private = std::fs::File::open("/run/dittobench-grader/secret").unwrap();
    let leaked = unsafe { libc::fcntl(private.as_raw_fd(), libc::F_DUPFD, 200) };
    assert_eq!(leaked, 200);
    let _held = unsafe { OwnedFd::from_raw_fd(leaked) };
    let source = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open("/out/candidate")
        .unwrap();
    let artifact = Artifact::seal_output(&source, 10001).unwrap();
    let suite = r#"
        #[test] fn first(){assert!(candidate::api::fresh());assert!(candidate::api::check());assert_eq!(candidate::api::add(2,3),5);}
        #[test] fn panic(){assert!(candidate::api::panic());}
        #[test] fn next(){assert!(candidate::api::fresh());assert!(candidate::api::check());}
    "#;
    let names: Vec<_> = table.keys().map(String::as_str).collect();
    let admitted = coding_rust_suite::admit(
        suite,
        &Policy {
            crate_name: "candidate",
            functions: &names,
            expected_tests: 3,
            source_sha256: Sha256::digest(suite.as_bytes()).into(),
        },
    )
    .unwrap();
    let program = Program::bind(admitted, table.clone()).unwrap();
    let mut factory = ProcessFactory::new(artifact, table.clone(), Deadlines::default()).unwrap();
    let report = program.run(&mut factory, Limits::default()).unwrap();
    assert_eq!((report.passed(), report.failed()), (2, 1));
    let artifact = Artifact::seal_output(&source, 10001).unwrap();
    let mut bounded = ProcessFactory::new(
        artifact,
        table,
        Deadlines {
            execution: Duration::from_millis(100),
            ..Deadlines::default()
        },
    )
    .unwrap();
    let mut api = bounded.start().unwrap();
    assert!(matches!(
        api.call("api::hang", &[]),
        Err(ApiError::Transport)
    ));
    assert!(matches!(
        api.call("api::check", &[]),
        Err(ApiError::Transport)
    ));
    api.finish().unwrap();
    api.finish().unwrap();
    assert!(matches!(
        api.call("api::check", &[]),
        Err(ApiError::Transport)
    ));
    drop(api);
    // Dropping a live, unused session must also terminate and reap it.
    drop(factory.start().unwrap());
    assert_eq!(
        unsafe { libc::waitpid(-1, std::ptr::null_mut(), libc::WNOHANG) },
        -1
    );
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ECHILD)
    );
    println!("native Rust parent evaluation, fresh sessions, timeout and reap verified");
}
