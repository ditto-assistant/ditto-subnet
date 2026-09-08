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
    if mode == "generate" {
        print!("{}", bridge::generate(&table).unwrap().source());
        return;
    }
    #[cfg(target_os = "linux")]
    if [
        "stage",
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
        if mode != "stage" {
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
