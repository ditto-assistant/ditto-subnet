use coding_rust_suite::{admit, AdmissionError, Policy};
use sha2::{Digest, Sha256};

fn policy(source: &str) -> Policy<'_> {
    Policy {
        crate_name: "repair",
        functions: &["transform", "inspect"],
        expected_tests: 1,
        source_sha256: Sha256::digest(source.as_bytes()).into(),
    }
}

fn rejected(source: &str) {
    assert!(
        admit(source, &policy(source)).is_err(),
        "fixture unexpectedly admitted"
    );
}

#[test]
fn admits_bound_calls_and_private_data_without_execution() {
    let source = r#"
        use repair::{transform, inspect};
        #[test]
        fn example() {
            let value = transform(&[1u8, 2u8]);
            assert_eq!(value, Some(vec![1u8, 2u8]));
            assert!(inspect(("example", 'x', -3i32)) == Ok(true));
        }
    "#;
    let suite = admit(source, &policy(source)).ok().unwrap();
    assert_eq!(suite.test_count(), 1);
    assert_eq!(suite.source_sha256(), policy(source).source_sha256);
}

#[test]
fn imports_are_resolved_before_tests() {
    let source = "#[test] fn example() { assert_eq!(transform(), None); } use repair::transform;";
    assert!(admit(source, &policy(source)).is_ok());
}

#[test]
fn qualified_calls_do_not_require_imports() {
    let source = "#[test] fn example() { assert_eq!(repair::transform(), Err(false)); }";
    assert!(admit(source, &policy(source)).is_ok());
}

#[test]
fn allows_tail_assertion_and_trailing_comma() {
    for body in [
        "assert!(true)",
        "assert_eq!(1, 1,)",
        "assert_eq!(vec![], [])",
    ] {
        let source = format!("#[test] fn example() {{ {body} }}");
        assert!(admit(&source, &policy(&source)).is_ok());
    }
}

#[test]
fn binds_exact_source_digest_including_whitespace() {
    let source = "#[test] fn example() { assert!(true); }";
    assert_eq!(
        admit(&format!("{source}\n"), &policy(source)).err(),
        Some(AdmissionError::Digest)
    );
}

#[test]
fn binds_nonzero_exact_test_count() {
    let source = "#[test] fn example() { assert!(true); }";
    let mut contract = policy(source);
    contract.expected_tests = 2;
    assert_eq!(
        admit(source, &contract).err(),
        Some(AdmissionError::TestCount)
    );
    for count in [0, 33, usize::MAX] {
        contract.expected_tests = count;
        assert_eq!(admit(source, &contract).err(), Some(AdmissionError::Policy));
    }
    rejected("");
    rejected("use repair::transform;");
    rejected("#[test] fn example() {} ");
    rejected("#[test] fn example() { let x = 1; }");
    rejected("#[test] fn example() { assert!(true); } #[test] fn example() { assert!(true); }");
}

#[test]
fn rejects_unapproved_imports_and_calls() {
    for import in [
        "use std::fs;",
        "use repair::*;",
        "use repair::transform as alias;",
        "use repair::{transform, transform};",
        "use repair::nested::transform;",
        "use ::repair::transform;",
        "use repair::missing;",
        "use repair::{};",
        "pub use repair::transform;",
        "#[cfg(any())] use repair::transform;",
    ] {
        rejected(&format!(
            "{import} #[test] fn example() {{ assert!(true); }}"
        ));
    }
    for call in [
        "std::process::exit(0)",
        "transform()",
        "repair::missing()",
        "::repair::transform()",
        "repair::transform::<u8>()",
        "(repair::transform)()",
        "Some()",
        "Err(1, 2)",
    ] {
        rejected(&format!("#[test] fn example() {{ assert!({call}); }}"));
    }
}

#[test]
fn rejects_macro_expansion_and_format_arguments() {
    for expression in [
        "include_str!(\"private\")",
        "env!(\"SECRET\")",
        "concat!(\"a\", \"b\")",
        "vec![std::process::exit(0)]",
        "vec![1; 1000000]",
        "vec!(1, 2)",
        "repair::vec![1]",
        "assert!(true)",
    ] {
        rejected(&format!(
            "#[test] fn example() {{ assert_eq!({expression}, 1); }}"
        ));
    }
    for statement in [
        "assert_eq!(1, 1, \"message\");",
        "assert!(true, \"message\");",
        "assert_eq!(1);",
        "assert_ne!(1, 2);",
        "assert!{true}",
        "std::assert!(true);",
        "println!(\"message\");",
        "macro_rules! other { () => { true } }",
        "repair::transform();",
    ] {
        rejected(&format!("#[test] fn example() {{ {statement} }}"));
    }
}

#[test]
fn rejects_non_test_items_and_altered_test_semantics() {
    for item in [
        "fn helper() {}",
        "mod hidden;",
        "mod hidden {}",
        "extern crate repair;",
        "const X: bool = true;",
        "static X: bool = true;",
        "struct X;",
        "macro_rules! m { () => {} }",
        "extern \"C\" { fn x(); }",
    ] {
        rejected(&format!("{item} #[test] fn example() {{ assert!(true); }}"));
    }
    for prefix in [
        "#[ignore] #[test] fn",
        "#[should_panic] #[test] fn",
        "#[cfg(any())] #[test] fn",
        "#[test] #[test] fn",
        "#[test()] fn",
        "#[test = true] fn",
        "#[other::test] fn",
        "#[test] unsafe fn",
        "#[test] async fn",
        "#[test] const fn",
        "#[test] pub fn",
    ] {
        rejected(&format!("{prefix} example() {{ assert!(true); }}"));
    }
    for signature in [
        "example(x: u8)",
        "example<T>()",
        "example() -> bool",
        "example() where (): Sized",
    ] {
        rejected(&format!("#[test] fn {signature} {{ assert!(true); }}"));
    }
    rejected("#![allow(unused)] #[test] fn example() { assert!(true); }");
    rejected("#!/usr/bin/env rust\n#[test] fn example() { assert!(true); }");
}

#[test]
fn rejects_shadowing_mutation_and_unbound_names() {
    for statement in [
        "let transform = 1;",
        "let repair = 1;",
        "let Some = 1;",
        "let None = 1;",
        "let x = x;",
        "let x = 1; let x = 2;",
        "let mut x = 1;",
        "let x: u8 = 1;",
        "let (a, b) = (1, 2);",
        "let _ = 1;",
        "let x = 1 else { return; };",
        "#[cfg(any())] let x = 1;",
        "let r#value = 1;",
        "x = 1;",
    ] {
        rejected(&format!(
            "#[test] fn example() {{ {statement} assert!(true); }}"
        ));
    }
    rejected("#[test] fn example() { assert!(missing); }");
}

#[test]
fn rejects_open_ended_expression_forms() {
    for expression in [
        "unsafe { true }",
        "{ true }",
        "loop {}",
        "if true { true } else { false }",
        "match true { _ => true }",
        "(|| true)()",
        "return true",
        "panic!()",
        "repair::transform().unwrap()",
        "repair::transform().field",
        "[true][0]",
        "true as bool",
        "&mut true",
        "*true",
        "repair::transform()?",
        "1 + 2",
        "1 < 2",
        "[1; 3]",
        "1.5",
        "b\"bytes\"",
        "1u128",
        "18446744073709551616",
        "#[cfg(any())] true",
        "<bool as Trait>::VALUE",
        "async { true }",
    ] {
        rejected(&format!(
            "#[test] fn example() {{ assert!({expression}); }}"
        ));
    }
}

#[test]
fn errors_never_include_private_source() {
    let source = "private_expected_value_in_a_broken_suite!!!";
    let error = admit(source, &policy(source)).err().unwrap();
    assert!(!format!("{error:?} {error}").contains("private_expected"));
}

#[test]
fn rejects_invalid_controller_policy() {
    let source = "#[test] fn example() { assert!(true); }";
    for name in [
        "", "_", "std", "core", "alloc", "crate", "r#repair", "a::b", "a-b", "1a", "self",
    ] {
        let mut contract = policy(source);
        contract.crate_name = name;
        assert_eq!(admit(source, &contract).err(), Some(AdmissionError::Policy));
    }
    for functions in [
        &[][..],
        &["transform", "transform"][..],
        &["None"][..],
        &["a::b"][..],
    ] {
        let mut contract = policy(source);
        contract.functions = functions;
        assert_eq!(admit(source, &contract).err(), Some(AdmissionError::Policy));
    }
}

#[test]
fn limits_apply_before_recursive_parsing_and_inside_macros() {
    for source in [
        " ".repeat(32769),
        "(".repeat(10000),
        "/*".repeat(10000),
        format!(
            "#[test] fn example() {{ assert!({}true); }}",
            "!".repeat(10000)
        ),
        format!(
            "#[test] fn example() {{ assert!({}true); }}",
            "!".repeat(40)
        ),
        format!(
            "#[test] fn example() {{ assert_eq!({}true{}, 1); }}",
            "vec![".repeat(35),
            "]".repeat(35)
        ),
    ] {
        assert_eq!(
            admit(&source, &policy(&source)).err(),
            Some(AdmissionError::Limit)
        );
    }
}

#[test]
fn locals_do_not_leak_between_tests() {
    let source = "#[test] fn one() { let x = true; assert!(x); } #[test] fn two() { assert!(x); }";
    let mut contract = policy(source);
    contract.expected_tests = 2;
    assert_eq!(
        admit(source, &contract).err(),
        Some(AdmissionError::Unsupported)
    );
}

#[test]
fn test_names_cannot_shadow_bound_calls_or_constructors() {
    for name in ["transform", "repair", "Some", "None", "Ok", "Err"] {
        rejected(&format!("#[test] fn {name}() {{ assert!(true); }}"));
    }
    rejected("fn example() { #![test] assert!(true); }");
}

#[test]
fn bounded_malformed_inputs_do_not_panic() {
    // Exercise the lexer/parser before the admission walk, including long unary
    // chains, nested comments, paths, groups, and unterminated string literals.
    for count in [1, 16, 32, 63, 64, 127, 255] {
        for token in ["!", "-", "/*", "(", "[", "\"", "a::", "a+"] {
            let source = format!(
                "#[test] fn example() {{ assert!({}true); }}",
                token.repeat(count)
            );
            let _ = admit(&source, &policy(&source));
        }
    }
}
