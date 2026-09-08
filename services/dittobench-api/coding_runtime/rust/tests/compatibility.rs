use coding_rust_suite::{admit, AdmissionError, Policy};
use sha2::{Digest, Sha256};

fn check(source: &str, functions: &[&str]) -> Result<(), AdmissionError> {
    admit(
        source,
        &Policy {
            crate_name: "repair",
            functions,
            expected_tests: 1,
            source_sha256: Sha256::digest(source.as_bytes()).into(),
        },
    )
    .map(|_| ())
}

fn expression(value: &str) -> String {
    format!("#[test] fn example() {{ assert_eq!({value}, true); }}")
}

#[test]
fn admits_only_exact_approved_module_paths() {
    for source in [
        "use repair::encoding::decode; #[test] fn example() { assert_eq!(decode(1), 2); }",
        "use repair::{encoding::{decode}, inspect}; #[test] fn example() { assert!(inspect(decode(1))); }",
        "#[test] fn example() { assert_eq!(repair::encoding::decode(1), 2); }",
        "use repair::a::b::c::decode; #[test] fn example() { assert_eq!(decode(1), 2); }",
    ] {
        assert!(check(source, &["encoding::decode", "inspect", "a::b::c::decode"]).is_ok());
    }
    for source in [
        "use repair::other::decode; #[test] fn example() { assert!(decode()); }",
        "use repair::encoding; #[test] fn example() { assert!(encoding::decode()); }",
        "use repair::encoding::*; #[test] fn example() { assert!(decode()); }",
        "use repair::encoding::decode as alias; #[test] fn example() { assert!(alias()); }",
        "use repair::encoding::{self, decode}; #[test] fn example() { assert!(decode()); }",
        "use repair::encoding::{}; #[test] fn example() { assert!(true); }",
        "use repair::r#encoding::decode; #[test] fn example() { assert!(decode()); }",
        "use r#repair::encoding::decode; #[test] fn example() { assert!(decode()); }",
        "#[test] fn example() { assert!(repair::encoding::missing()); }",
        "#[test] fn example() { assert!(repair::encoding::decode::<u8>()); }",
    ] {
        assert!(check(source, &["encoding::decode"]).is_err());
    }
}

#[test]
fn rejects_colliding_imports_and_shadowed_bindings() {
    for source in [
        "use repair::{a::decode, b::decode}; #[test] fn example() { assert!(decode()); }",
        "use repair::a::decode; use repair::a::decode; #[test] fn example() { assert!(decode()); }",
        "use repair::a::decode; #[test] fn example() { let decode = true; assert!(decode); }",
        "use repair::a::decode; #[test] fn decode() { assert!(true); }",
    ] {
        assert!(check(source, &["a::decode", "b::decode"]).is_err());
    }
}

#[test]
fn validates_entire_policy_path_before_parsing_source() {
    for path in [
        "a::*",
        "a::",
        "::a",
        "a::::b",
        "a::b::c::d::f",
        "a::r#b",
        "a::Some",
        "a::self",
        "a/b",
        "a::f<u8>",
    ] {
        assert_eq!(
            check(&expression("true"), &[path]),
            Err(AdmissionError::Policy)
        );
    }
}

#[test]
fn admits_bounded_result_sequence_and_tuple_descriptions() {
    for value in [
        "repair::inspect().unwrap()",
        "Err(true).unwrap_err()",
        "Some(true).is_some()",
        "None.is_none()",
        "Ok(true).is_ok()",
        "Err(true).is_err()",
        "[1, 2].len()",
        "vec![].is_empty()",
        "(true, false).0",
        "[true][0]",
        "repair::inspect().unwrap()[0].0",
        "[true][4095]",
    ] {
        // Syntax only: out-of-range access is a future evaluator error, never a
        // successful assertion or permission to call an arbitrary Rust method.
        assert!(check(&expression(value), &["inspect"]).is_ok());
    }
    for value in [
        "repair::inspect().execute()",
        "Ok(true).unwrap(false)",
        "Ok(true).unwrap::<bool>()",
        "Ok(true).r#unwrap()",
        "(true,).state",
        "(true,).16",
        "(true,).0u8",
        "[true][-1]",
        "[true][0usize]",
        "[true][4096]",
        "[true][repair::inspect()]",
        "[true][0..1]",
        "repair::inspect().iter()",
    ] {
        assert!(check(&expression(value), &["inspect"]).is_err());
    }
}

#[test]
fn admits_one_closed_iterator_projection() {
    for value in [
        "[(true, 1)].iter().map(|item| item.0).collect::<Vec<_>>()",
        "repair::inspect().iter().map(|item| (item.0, Some(item.1))).collect::<Vec<_>>()",
    ] {
        assert!(check(&expression(value), &["inspect"]).is_ok());
    }
    let source = "#[test] fn example() { let expected = true; assert_eq!([1].iter().map(|item| expected).collect::<Vec<_>>(), [true]); }";
    assert!(check(source, &["inspect"]).is_ok());
}

#[test]
fn projection_cannot_execute_candidate_code_or_escape_its_scope() {
    for body in [
        "repair::inspect()",
        "vec![repair::inspect()]",
        "Some(repair::inspect())",
        "item()",
        "{ item }",
        "loop {}",
        "item.execute()",
        "include_str!(\"private\")",
        "[item].iter().map(|inner| inner).collect::<Vec<_>>()",
    ] {
        let value = format!("[1].iter().map(|item| {body}).collect::<Vec<_>>()");
        assert!(check(&expression(&value), &["inspect"]).is_err());
    }
    for closure in [
        "move |item| item",
        "async |item| item",
        "|mut item| item",
        "|ref item| item",
        "|item: u8| item",
        "|(a, b)| a",
        "|a, b| a",
        "|_| true",
        "|Some| true",
        "|repair| true",
        "|inspect| true",
        "|item| -> bool { true }",
    ] {
        let value = format!("[1].iter().map({closure}).collect::<Vec<_>>()");
        assert!(check(&expression(&value), &["inspect"]).is_err());
    }
    for source in [
        "#[test] fn example() { let item = true; assert!([1].iter().map(|item| item).collect::<Vec<_>>()); }",
        "use repair::a::decode; #[test] fn example() { assert!([1].iter().map(|decode| decode).collect::<Vec<_>>()); }",
        "#[test] fn example() { let result = [1].iter().map(|item| item).collect::<Vec<_>>(); assert!(item); }",
    ] {
        assert!(check(source, &["inspect", "a::decode"]).is_err());
    }
}

#[test]
fn rejects_general_iterators_and_collection_type_hooks() {
    for value in [
        "[1].iter().map(|item| item)",
        "[1].iter().collect::<Vec<_>>()",
        "[1].into_iter().map(|item| item).collect::<Vec<_>>()",
        "[1].iter(1).map(|item| item).collect::<Vec<_>>()",
        "[1].iter::<u8>().map(|item| item).collect::<Vec<_>>()",
        "[1].iter().map::<u8>(|item| item).collect::<Vec<_>>()",
        "[1].iter().map(repair::inspect).collect::<Vec<_>>()",
        "[1].iter().map(|item| item).collect::<Vec<_>>(true)",
        "[1].iter().map(|item| item).collect()",
        "[1].iter().map(|item| item).collect::<Other<_>>()",
        "[1].iter().map(|item| item).collect::<std::vec::Vec<_>>()",
        "[1].iter().map(|item| item).collect::<Vec<u8>>()",
        "[1].iter().map(|item| item).collect::<Vec::<_>>()",
        "[1].iter().map(|item| item).collect::<Vec<_, _>>()",
    ] {
        assert!(check(&expression(value), &["inspect"]).is_err());
    }
}

#[test]
fn compatibility_constructs_preserve_recursion_limits() {
    for value in [
        format!("repair::inspect(){}", ".unwrap()".repeat(34)),
        format!("(true,){}", ".0".repeat(34)),
        format!("[true]{}", "[0]".repeat(34)),
        format!(
            "[1].iter().map(|item| {}item).collect::<Vec<_>>()",
            "!".repeat(31)
        ),
    ] {
        assert_eq!(
            check(&expression(&value), &["inspect"]),
            Err(AdmissionError::Limit)
        );
    }
}

#[test]
fn admits_only_explicit_primitive_integer_bounds() {
    for primitive in [
        "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64", "isize",
    ] {
        for bound in ["MIN", "MAX"] {
            assert!(check(&expression(&format!("{primitive}::{bound}")), &["inspect"]).is_ok());
        }
    }
    for value in [
        "f64::MAX",
        "i128::MAX",
        "std::i64::MAX",
        "i64::BITS",
        "i64::OTHER",
        "<i64>::MAX",
        "r#i64::MAX",
        "i64::r#MAX",
        "repair::MAX",
    ] {
        assert!(check(&expression(value), &["inspect"]).is_err());
    }
    let source = expression("true");
    for crate_name in ["i64", "u8", "usize", "Some", "None", "Ok", "Err", "Vec"] {
        assert_eq!(
            admit(
                &source,
                &Policy {
                    crate_name,
                    functions: &["inspect"],
                    expected_tests: 1,
                    source_sha256: Sha256::digest(source.as_bytes()).into(),
                }
            )
            .err(),
            Some(AdmissionError::Policy)
        );
    }
}
