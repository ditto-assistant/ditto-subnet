use coding_rust_suite::{
    bridge::{generate, BridgeError},
    evaluator::Signature,
    native::{decode_slice, referent, BorrowDecode, NativeDecode, NativeValue},
    value::{Integer, Type, Value},
};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fmt::Debug};
fn encode<T: NativeValue>(value: T) -> Value {
    NativeValue::to_value(&value).unwrap()
}
fn roundtrip<T: NativeDecode + PartialEq + Debug>(value: T) {
    let encoded = encode(&value);
    let inner = referent(&encoded).unwrap();
    assert_eq!(T::from_value(inner).unwrap(), value);
}

#[test]
fn native_owned_values_preserve_width_shape_and_variants() {
    roundtrip(true);
    roundtrip('🦀');
    roundtrip(u64::MAX);
    roundtrip(i64::MIN);
    roundtrip(String::from("owned"));
    roundtrip(vec![1u8, 2, 3]);
    roundtrip([1u16, 2]);
    roundtrip((1i32, false, String::from("tuple")));
    roundtrip(());
    roundtrip(Some((String::from("optional"), [1u8, 2])));
    roundtrip(None::<u64>);
    roundtrip(Ok::<Vec<u8>, String>(vec![1, 2]));
    roundtrip(Err::<Vec<u8>, String>(String::from("domain")));
    assert!(u8::from_value(&encode(1i64)).is_err());
    assert!(String::from_value(&encode("borrowed")).is_err());
}

#[test]
fn native_borrows_remain_reference_descriptions() {
    assert_eq!(encode("borrowed").kind(), &Type::Ref(Box::new(Type::Text)));
    let strings = encode(vec!["one", "two"]);
    assert_eq!(
        strings.kind(),
        &Type::Vec(Box::new(Type::Ref(Box::new(Type::Text))))
    );
    let tuple = encode(("one", Some("two")));
    assert_eq!(
        tuple.kind(),
        &Type::Tuple(vec![
            Type::Ref(Box::new(Type::Text)),
            Type::Option(Box::new(Type::Ref(Box::new(Type::Text))))
        ])
    );
    let bytes = encode([1u8, 2, 3].as_slice());
    assert_eq!(
        decode_slice::<u8>(referent(&bytes).unwrap()).unwrap(),
        vec![1, 2, 3]
    );
    assert!(decode_slice::<i8>(referent(&bytes).unwrap()).is_err());
}

#[test]
fn native_conversion_is_bounded_before_serialization() {
    assert!(NativeValue::to_value(&"x".repeat(65521)).is_err());
    assert!(NativeValue::to_value(&vec![(); 4096]).is_err());
    assert!(NativeValue::to_value(&vec![vec![true; 2000]; 3]).is_err());
}

#[test]
fn nested_text_views_preserve_container_types_and_lifetimes() {
    let data = encode(vec![(-2i32, u64::MAX, "borrowed row")]);
    let rows = <Vec<(i32, u64, &str)> as BorrowDecode>::from_borrowed(&data).unwrap();
    assert_eq!(rows, vec![(-2, u64::MAX, "borrowed row")]);
    assert!(<Vec<(i32, u64, String)> as BorrowDecode>::from_borrowed(&data).is_err());
    let some = encode(Some("optional"));
    assert_eq!(
        <Option<&str> as BorrowDecode>::from_borrowed(&some).unwrap(),
        Some("optional")
    );
    let none = encode(None::<&str>);
    assert_eq!(
        <Option<&str> as BorrowDecode>::from_borrowed(&none).unwrap(),
        None
    );
    let array = encode(["one", "two"]);
    assert_eq!(
        <[&str; 2] as BorrowDecode>::from_borrowed(&array).unwrap(),
        ["one", "two"]
    );
    assert!(<Vec<&str> as BorrowDecode>::from_borrowed(&array).is_err());
    let error = encode(Err::<u64, &str>("domain"));
    assert_eq!(
        <Result<u64, &str> as BorrowDecode>::from_borrowed(&error).unwrap(),
        Err("domain")
    );
}

#[test]
fn nested_text_input_codegen_has_scoped_views_and_higher_ranked_abi() {
    let mut table = schema();
    table.get_mut("api::copy").unwrap().parameters =
        vec![Type::Option(Box::new(Type::Ref(Box::new(Type::Text))))];
    let generated = generate(&table).unwrap();
    assert!(generated
        .source()
        .contains("Option<&'_ str> as BorrowDecode"));
    assert!(generated
        .source()
        .contains("for<'a> fn(::core::option::Option<&'a str>)"));
    assert!(!generated.source().contains("transmute"));
}

fn schema() -> BTreeMap<String, Signature> {
    BTreeMap::from([(
        "api::copy".into(),
        Signature {
            parameters: vec![Type::Ref(Box::new(Type::Text))],
            result: Type::Vec(Box::new(Type::Ref(Box::new(Type::Text)))),
        },
    )])
}

#[test]
fn generated_source_is_deterministic_and_names_exact_function_pointer_types() {
    let source = generate(&schema()).unwrap();
    assert_eq!(source.source(), generate(&schema()).unwrap().source());
    assert_eq!(
        source.sha256(),
        <[u8; 32]>::from(Sha256::digest(source.source().as_bytes()))
    );
    assert!(source
        .source()
        .contains("for<'a> fn(&'a str)->::std::vec::Vec<&'a str>=::candidate::api::copy"));
    assert!(!source.source().contains("Program::"));
    assert!(!source.source().contains("expected_tests"));
    assert!(!source.source().contains("include!"));
}

#[test]
fn unsupported_input_borrows_and_source_injection_are_rejected() {
    for name in [
        "",
        "a;panic!()",
        "a::r#type",
        "super::run",
        "a::b::c::d::e",
        "a::*",
        "a\nfn main(){}",
    ] {
        let mut table = schema();
        let signature = table.pop_first().unwrap().1;
        table.insert(name.into(), signature);
        assert_eq!(generate(&table).err(), Some(BridgeError::Schema));
    }
    for kind in [
        Type::Vec(Box::new(Type::Ref(Box::new(Type::Int(Integer::U8))))),
        Type::Ref(Box::new(Type::Ref(Box::new(Type::Int(Integer::U8))))),
    ] {
        let mut table = schema();
        table.get_mut("api::copy").unwrap().parameters = vec![kind];
        assert_eq!(generate(&table).err(), Some(BridgeError::Schema));
    }
}

#[test]
fn manifest_limits_and_candidate_namespace_are_explicit() {
    assert_eq!(generate(&BTreeMap::new()).err(), Some(BridgeError::Schema));
    let mut table = schema();
    let signature = table.pop_first().unwrap().1;
    table.insert("std::process::exit".into(), signature.clone());
    assert!(generate(&table)
        .unwrap()
        .source()
        .contains("=::candidate::std::process::exit"));
    for n in 0..65 {
        table.insert(format!("f{n}"), signature.clone());
    }
    assert_eq!(generate(&table).err(), Some(BridgeError::Schema));
}
