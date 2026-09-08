use coding_rust_suite::{admit, evaluator::*, value::*, Policy};
use sha2::{Digest, Sha256};
use std::{
    cell::RefCell,
    collections::{BTreeMap, VecDeque},
    rc::Rc,
};

fn int(value: i128) -> Value {
    Value::integer(Integer::I64, value).unwrap()
}
fn i64_type() -> Type {
    Type::Int(Integer::I64)
}
fn sig(result: Type) -> Signature {
    Signature {
        parameters: vec![],
        result,
    }
}
fn program(source: &str, schema: BTreeMap<String, Signature>, count: usize) -> Program {
    let names: Vec<_> = schema.keys().map(String::as_str).collect();
    let admitted = admit(
        source,
        &Policy {
            crate_name: "repair",
            functions: &names,
            expected_tests: count,
            source_sha256: Sha256::digest(source.as_bytes()).into(),
        },
    )
    .expect("synthetic suite admission");
    Program::bind(admitted, schema).expect("synthetic schema binding")
}
fn single(body: &str, signature: Signature) -> Program {
    program(
        &format!("#[test] fn example() {{ {body} }}"),
        BTreeMap::from([("f".into(), signature)]),
        1,
    )
}
#[derive(Default)]
struct Trace {
    starts: usize,
    finishes: usize,
    calls: Vec<(String, Vec<Value>)>,
}
struct Factory {
    responses: VecDeque<Vec<Result<Value, ApiError>>>,
    trace: Rc<RefCell<Trace>>,
    finish_error: Option<ApiError>,
    start_error: bool,
}
struct Session {
    responses: VecDeque<Result<Value, ApiError>>,
    trace: Rc<RefCell<Trace>>,
    finish_error: Option<ApiError>,
}
impl ApiFactory for Factory {
    fn start(&mut self) -> Result<Box<dyn CandidateApi>, ApiError> {
        self.trace.borrow_mut().starts += 1;
        if self.start_error {
            return Err(ApiError::Transport);
        }
        Ok(Box::new(Session {
            responses: self.responses.pop_front().unwrap_or_default().into(),
            trace: self.trace.clone(),
            finish_error: self.finish_error,
        }))
    }
}
impl CandidateApi for Session {
    fn call(&mut self, name: &str, args: &[Value]) -> Result<Value, ApiError> {
        self.trace
            .borrow_mut()
            .calls
            .push((name.to_owned(), args.to_vec()));
        self.responses
            .pop_front()
            .unwrap_or(Err(ApiError::Transport))
    }
    fn finish(&mut self) -> Result<(), ApiError> {
        self.trace.borrow_mut().finishes += 1;
        self.finish_error.map_or(Ok(()), Err)
    }
}
fn factory(responses: Vec<Vec<Result<Value, ApiError>>>) -> Factory {
    Factory {
        responses: responses.into(),
        trace: Rc::new(RefCell::new(Trace::default())),
        finish_error: None,
        start_error: false,
    }
}

#[test]
fn parent_owns_assertions_counts_and_canonical_call_names() {
    let schema = BTreeMap::from([(
        "math::double".into(),
        Signature {
            parameters: vec![i64_type()],
            result: i64_type(),
        },
    )]);
    let plan = program(
        "use repair::math::double; #[test] fn example() { assert_eq!(double(6), 12); }",
        schema,
        1,
    );
    let mut api = factory(vec![vec![Ok(int(12))]]);
    let report = plan.run(&mut api, Limits::default()).unwrap();
    assert_eq!((report.passed(), report.failed()), (1, 0));
    assert_eq!(report.program_sha256(), plan.program_sha256());
    let trace = api.trace.borrow();
    assert_eq!((trace.starts, trace.finishes, trace.calls.len()), (1, 1, 1));
    assert_eq!(trace.calls[0].0, "math::double");
    assert!(matches!(trace.calls[0].1[0].data(), Data::Int(6)));
}

#[test]
fn failed_assertion_stops_only_its_test_and_sessions_are_fresh() {
    let source = "#[test] fn one() { assert_eq!(repair::f(), 5); assert_eq!(repair::f(), 6); } #[test] fn two() { assert_eq!(repair::f(), 7); }";
    let plan = program(source, BTreeMap::from([("f".into(), sig(i64_type()))]), 2);
    let mut api = factory(vec![vec![Ok(int(0))], vec![Ok(int(7))]]);
    let result = plan.run(&mut api, Limits::default()).unwrap();
    assert_eq!((result.passed(), result.failed()), (1, 1));
    assert_eq!(
        (
            api.trace.borrow().starts,
            api.trace.borrow().finishes,
            api.trace.borrow().calls.len()
        ),
        (2, 2, 2)
    );
}

#[test]
fn schema_and_integer_range_are_enforced_on_both_sides() {
    let plan = single(
        "assert_eq!(repair::f(255), 255u8);",
        Signature {
            parameters: vec![Type::Int(Integer::U8)],
            result: Type::Int(Integer::U8),
        },
    );
    let mut good = factory(vec![vec![Ok(Value::integer(Integer::U8, 255).unwrap())]]);
    assert_eq!(plan.run(&mut good, Limits::default()).unwrap().passed(), 1);
    assert_eq!(
        plan.run(&mut factory(vec![vec![Ok(int(255))]]), Limits::default())
            .unwrap()
            .failed(),
        1
    );
    for argument in ["256", "-0u8", "1i64"] {
        let plan = single(
            &format!("assert_eq!(repair::f({argument}), 0u8);"),
            Signature {
                parameters: vec![Type::Int(Integer::U8)],
                result: Type::Int(Integer::U8),
            },
        );
        let mut api = factory(vec![]);
        assert_eq!(
            plan.run(&mut api, Limits::default()).err(),
            Some(EvaluationError::Oracle)
        );
        assert_eq!(
            (api.trace.borrow().calls.len(), api.trace.borrow().finishes),
            (0, 1)
        );
    }
}

#[test]
fn signed_minimum_and_checked_unary_operations() {
    for body in [
        "assert_eq!(-9223372036854775808i64, i64::MIN);",
        "assert_eq!(!0u8, u8::MAX);",
        "assert_eq!(!false, true);",
        "assert_eq!(-(-2i64), 2i64);",
    ] {
        assert_eq!(
            single(body, sig(i64_type()))
                .run(&mut factory(vec![]), Limits::default())
                .unwrap()
                .passed(),
            1
        );
    }
    let plan = single("assert_eq!(-repair::f(), 0);", sig(i64_type()));
    assert_eq!(
        plan.run(
            &mut factory(vec![vec![Ok(int(i64::MIN as i128))]]),
            Limits::default()
        )
        .unwrap()
        .failed(),
        1
    );
}

#[test]
fn booleans_are_not_numbers_and_short_circuit_does_not_call() {
    let plan = single(
        "assert!(true || repair::f()); assert!(!(false && repair::f()));",
        sig(Type::Bool),
    );
    let mut api = factory(vec![]);
    assert_eq!(plan.run(&mut api, Limits::default()).unwrap().passed(), 1);
    assert_eq!(api.trace.borrow().calls.len(), 0);
    assert_eq!(
        single("assert_eq!(true, 1);", sig(i64_type()))
            .run(&mut factory(vec![]), Limits::default())
            .err(),
        Some(EvaluationError::Oracle)
    );
}

#[test]
fn domain_errors_are_distinct_from_candidate_and_transport_failure() {
    let ty = Type::Result(Box::new(i64_type()), Box::new(Type::Text));
    let plan = single("assert!(repair::f().is_err());", sig(ty.clone()));
    let domain = Value::new(
        ty,
        Data::Err(Box::new(
            Value::new(Type::Text, Data::Text("synthetic error".into())).unwrap(),
        )),
    )
    .unwrap();
    assert_eq!(
        plan.run(&mut factory(vec![vec![Ok(domain)]]), Limits::default())
            .unwrap()
            .passed(),
        1
    );
    assert_eq!(
        plan.run(
            &mut factory(vec![vec![Err(ApiError::Candidate)]]),
            Limits::default()
        )
        .unwrap()
        .failed(),
        1
    );
    let mut api = factory(vec![vec![Err(ApiError::Transport)]]);
    assert_eq!(
        plan.run(&mut api, Limits::default()).err(),
        Some(EvaluationError::Transport)
    );
    assert_eq!(api.trace.borrow().finishes, 1);
}

#[test]
fn cleanup_failure_prevents_every_kind_of_report() {
    for response in [
        Ok(int(7)),
        Ok(int(0)),
        Err(ApiError::Candidate),
        Err(ApiError::Transport),
    ] {
        let plan = single("assert_eq!(repair::f(), 7);", sig(i64_type()));
        let mut api = factory(vec![vec![response]]);
        api.finish_error = Some(ApiError::Transport);
        assert_eq!(
            plan.run(&mut api, Limits::default()).err(),
            Some(EvaluationError::Cleanup)
        );
        assert_eq!(api.trace.borrow().finishes, 1);
    }
    let mut api = factory(vec![]);
    api.start_error = true;
    assert_eq!(
        single("assert!(true);", sig(Type::Bool))
            .run(&mut api, Limits::default())
            .err(),
        Some(EvaluationError::Transport)
    );
    assert_eq!(api.trace.borrow().finishes, 0);
}

#[test]
fn budget_exhaustion_cleans_up_and_never_synthesizes_passes() {
    let plan = single(
        "let value = repair::f(); assert_eq!(value, 7); assert_eq!(repair::f(), 7);",
        sig(i64_type()),
    );
    for limits in [
        Limits {
            calls: 1,
            ..Limits::default()
        },
        Limits {
            steps: 1,
            ..Limits::default()
        },
        Limits {
            nodes: 1,
            ..Limits::default()
        },
        Limits {
            bytes: 1,
            ..Limits::default()
        },
    ] {
        let mut api = factory(vec![vec![Ok(int(7)), Ok(int(7))]]);
        assert_eq!(
            plan.run(&mut api, limits).err(),
            Some(EvaluationError::Limit)
        );
        assert_eq!(api.trace.borrow().finishes, 1);
    }
    let mut api = factory(vec![]);
    assert_eq!(
        plan.run(
            &mut api,
            Limits {
                calls: usize::MAX,
                ..Limits::default()
            }
        )
        .err(),
        Some(EvaluationError::Limit)
    );
    assert_eq!(api.trace.borrow().starts, 0);
}

#[test]
fn immutable_slice_argument_coercion_is_explicit() {
    let target = Type::Ref(Box::new(Type::Slice(Box::new(Type::Int(Integer::U8)))));
    let plan = single(
        "assert_eq!(repair::f(&[1, 2]), 3);",
        Signature {
            parameters: vec![target.clone()],
            result: i64_type(),
        },
    );
    let mut api = factory(vec![vec![Ok(int(3))]]);
    assert_eq!(plan.run(&mut api, Limits::default()).unwrap().passed(), 1);
    assert_eq!(api.trace.borrow().calls[0].1[0].kind(), &target);
    let wrong = single(
        "assert_eq!(repair::f(&[1, 2]), 3);",
        Signature {
            parameters: vec![Type::Ref(Box::new(Type::Vec(Box::new(Type::Int(
                Integer::U8,
            )))))],
            result: i64_type(),
        },
    );
    assert_eq!(
        wrong.run(&mut factory(vec![]), Limits::default()).err(),
        Some(EvaluationError::Oracle)
    );
}

#[test]
fn containers_and_string_borrows_keep_structural_equality() {
    let texts = Value::new(
        Type::Vec(Box::new(Type::Text)),
        Data::Sequence(vec![
            Value::new(Type::Text, Data::Text("alpha".into())).unwrap()
        ]),
    )
    .unwrap();
    let plan = single(
        "assert_eq!(repair::f(), vec![\"alpha\"]); assert_eq!(\"é🦀\".len(), 6usize);",
        sig(texts.kind().clone()),
    );
    assert_eq!(
        plan.run(&mut factory(vec![vec![Ok(texts)]]), Limits::default())
            .unwrap()
            .passed(),
        1
    );
    let ty = Type::Tuple(vec![Type::Vec(Box::new(i64_type())), Type::Bool]);
    let plan = single(
        "let value = repair::f(); assert_eq!(value.0[0], 7); assert!(value.1);",
        sig(ty.clone()),
    );
    let value = Value::new(
        ty,
        Data::Tuple(vec![
            Value::new(
                Type::Vec(Box::new(i64_type())),
                Data::Sequence(vec![int(7)]),
            )
            .unwrap(),
            Value::boolean(true),
        ]),
    )
    .unwrap();
    assert_eq!(
        plan.run(&mut factory(vec![vec![Ok(value)]]), Limits::default())
            .unwrap()
            .passed(),
        1
    );
}

#[test]
fn option_context_and_failed_unwraps_or_indices() {
    let ty = Type::Option(Box::new(i64_type()));
    let none = Value::new(ty.clone(), Data::None).unwrap();
    assert_eq!(
        single("assert_eq!(repair::f(), None);", sig(ty.clone()))
            .run(
                &mut factory(vec![vec![Ok(none.clone())]]),
                Limits::default()
            )
            .unwrap()
            .passed(),
        1
    );
    assert_eq!(
        single("assert_eq!(repair::f().unwrap(), 7);", sig(ty))
            .run(&mut factory(vec![vec![Ok(none)]]), Limits::default())
            .unwrap()
            .failed(),
        1
    );
    let ty = Type::Vec(Box::new(i64_type()));
    let empty = Value::new(ty.clone(), Data::Sequence(vec![])).unwrap();
    assert_eq!(
        single("assert_eq!(repair::f()[1], 7);", sig(ty))
            .run(&mut factory(vec![vec![Ok(empty)]]), Limits::default())
            .unwrap()
            .failed(),
        1
    );
}

#[test]
fn parent_projections_infer_types_even_for_empty_results() {
    let tuple = Type::Tuple(vec![i64_type(), Type::Bool]);
    let ty = Type::Vec(Box::new(tuple.clone()));
    for numbers in [vec![7, 8], vec![]] {
        let expected = if numbers.is_empty() {
            "vec![]"
        } else {
            "vec![7, 8]"
        };
        let plan = single(&format!("assert_eq!(repair::f().iter().map(|item| item.0).collect::<Vec<_>>(), {expected});"), sig(ty.clone()));
        let values = numbers
            .into_iter()
            .map(|n| {
                Value::new(
                    tuple.clone(),
                    Data::Tuple(vec![int(n), Value::boolean(true)]),
                )
                .unwrap()
            })
            .collect();
        let response = Value::new(ty.clone(), Data::Sequence(values)).unwrap();
        let mut api = factory(vec![vec![Ok(response)]]);
        assert_eq!(plan.run(&mut api, Limits::default()).unwrap().passed(), 1);
        assert_eq!(api.trace.borrow().calls.len(), 1);
    }
}

#[test]
fn digest_binds_types_and_schema_requires_exact_authority() {
    assert_ne!(
        single("assert!(true);", sig(i64_type())).program_sha256(),
        single("assert!(true);", sig(Type::Bool)).program_sha256()
    );
    let source = "#[test] fn example() { assert!(true); }";
    for schema in [
        BTreeMap::new(),
        BTreeMap::from([
            ("f".into(), sig(Type::Bool)),
            ("other".into(), sig(Type::Bool)),
        ]),
    ] {
        let suite = admit(
            source,
            &Policy {
                crate_name: "repair",
                functions: &["f"],
                expected_tests: 1,
                source_sha256: Sha256::digest(source.as_bytes()).into(),
            },
        )
        .ok()
        .unwrap();
        assert_eq!(
            Program::bind(suite, schema).err(),
            Some(EvaluationError::Schema)
        );
    }
}

#[test]
fn public_errors_do_not_disclose_private_values() {
    let plan = single("assert_eq!(\"secret expected value\", 1);", sig(i64_type()));
    let error = plan
        .run(&mut factory(vec![]), Limits::default())
        .err()
        .unwrap();
    assert!(!format!("{error:?} {error}").contains("secret"));
}

#[test]
fn parent_enum_constructors_use_the_bound_result_context() {
    let result = Type::Result(Box::new(i64_type()), Box::new(Type::Bool));
    let option = Type::Option(Box::new(i64_type()));
    for (expression, kind, data) in [
        ("Ok(7)", result.clone(), Data::Ok(Box::new(int(7)))),
        (
            "Err(false)",
            result,
            Data::Err(Box::new(Value::boolean(false))),
        ),
        ("Some(7)", option, Data::Some(Box::new(int(7)))),
    ] {
        let plan = single(
            &format!("assert_eq!(repair::f(), {expression});"),
            sig(kind.clone()),
        );
        let value = Value::new(kind, data).unwrap();
        assert_eq!(
            plan.run(&mut factory(vec![vec![Ok(value)]]), Limits::default())
                .unwrap()
                .passed(),
            1
        );
    }
}

#[test]
fn u64_comparisons_are_lossless_and_locals_do_not_silently_cast() {
    let plan = single(
        "assert_eq!(repair::f(), u64::MAX);",
        sig(Type::Int(Integer::U64)),
    );
    for (number, passed) in [(u64::MAX as i128, 1), (u64::MAX as i128 - 1, 0)] {
        let value = Value::integer(Integer::U64, number).unwrap();
        assert_eq!(
            plan.run(&mut factory(vec![vec![Ok(value)]]), Limits::default())
                .unwrap()
                .passed(),
            passed
        );
    }
    let plan = single(
        "let number = 1; assert_eq!(repair::f(number), 1u8);",
        Signature {
            parameters: vec![Type::Int(Integer::U8)],
            result: Type::Int(Integer::U8),
        },
    );
    let mut api = factory(vec![]);
    assert_eq!(
        plan.run(&mut api, Limits::default()).err(),
        Some(EvaluationError::Oracle)
    );
    assert_eq!(api.trace.borrow().calls.len(), 0);
}

#[test]
fn rust_generic_equality_boundaries_do_not_collapse_to_payload_equality() {
    let text = Value::new(Type::Text, Data::Text("public".into())).unwrap();
    let borrowed = Value::new(
        Type::Ref(Box::new(Type::Text)),
        Data::Ref(Box::new(text.clone())),
    )
    .unwrap();
    let reference = borrowed.kind().clone();
    let pairs = [
        (
            Value::new(
                Type::Option(Box::new(Type::Text)),
                Data::Some(Box::new(text.clone())),
            )
            .unwrap(),
            Value::new(
                Type::Option(Box::new(reference.clone())),
                Data::Some(Box::new(borrowed.clone())),
            )
            .unwrap(),
        ),
        (
            Value::new(
                Type::Tuple(vec![Type::Text]),
                Data::Tuple(vec![text.clone()]),
            )
            .unwrap(),
            Value::new(
                Type::Tuple(vec![reference.clone()]),
                Data::Tuple(vec![borrowed.clone()]),
            )
            .unwrap(),
        ),
        (
            Value::new(
                Type::Result(Box::new(Type::Text), Box::new(Type::Bool)),
                Data::Ok(Box::new(text)),
            )
            .unwrap(),
            Value::new(
                Type::Result(Box::new(reference), Box::new(Type::Bool)),
                Data::Ok(Box::new(borrowed)),
            )
            .unwrap(),
        ),
        (
            Value::new(
                Type::Array(Box::new(Type::Bool), 1),
                Data::Sequence(vec![Value::boolean(true)]),
            )
            .unwrap(),
            Value::new(
                Type::Array(Box::new(Type::Bool), 2),
                Data::Sequence(vec![Value::boolean(true); 2]),
            )
            .unwrap(),
        ),
    ];
    for (a, b) in pairs {
        let schema = BTreeMap::from([
            ("a".into(), sig(a.kind().clone())),
            ("b".into(), sig(b.kind().clone())),
        ]);
        let plan = program(
            "#[test] fn example() { assert_eq!(repair::a(), repair::b()); }",
            schema,
            1,
        );
        let mut api = factory(vec![vec![Ok(a), Ok(b)]]);
        assert_eq!(
            plan.run(&mut api, Limits::default()).err(),
            Some(EvaluationError::Oracle)
        );
        assert_eq!(api.trace.borrow().finishes, 1);
    }
    let plan = single("assert_eq!(&\"public\", \"public\");", sig(Type::Bool));
    assert_eq!(
        plan.run(&mut factory(vec![]), Limits::default()).err(),
        Some(EvaluationError::Oracle)
    );
}

#[test]
fn skipped_boolean_branches_still_check_declared_types() {
    for body in [
        "assert!(true || repair::f());",
        "assert!(!(false && repair::f()));",
        "assert!(true || (false || repair::f()));",
        "assert!(true || 1);",
    ] {
        let plan = single(body, sig(i64_type()));
        let mut api = factory(vec![]);
        assert_eq!(
            plan.run(&mut api, Limits::default()).err(),
            Some(EvaluationError::Oracle)
        );
        assert_eq!(api.trace.borrow().calls.len(), 0);
        assert_eq!(api.trace.borrow().finishes, 1);
    }
}

#[test]
fn constant_projection_type_does_not_depend_on_candidate_length() {
    let kind = Type::Vec(Box::new(i64_type()));
    for length in [0, 2] {
        let expected = if length == 0 { "vec![]" } else { "vec![1, 1]" };
        let plan = single(
            &format!(
                "assert_eq!(repair::f().iter().map(|item| 1).collect::<Vec<_>>(), {expected});"
            ),
            sig(kind.clone()),
        );
        let value = Value::new(kind.clone(), Data::Sequence(vec![int(7); length])).unwrap();
        let mut api = factory(vec![vec![Ok(value)]]);
        assert_eq!(plan.run(&mut api, Limits::default()).unwrap().passed(), 1);
        assert_eq!(api.trace.borrow().calls.len(), 1);
    }
    for literal in ["256u8", "-0u8", "2147483648"] {
        let plan = single(
            &format!(
                "assert_eq!(repair::f().iter().map(|item| {literal}).collect::<Vec<_>>(), vec![]);"
            ),
            sig(kind.clone()),
        );
        let value = Value::new(kind.clone(), Data::Sequence(vec![])).unwrap();
        let mut api = factory(vec![vec![Ok(value)]]);
        assert_eq!(
            plan.run(&mut api, Limits::default()).err(),
            Some(EvaluationError::Oracle)
        );
        assert_eq!(api.trace.borrow().finishes, 1);
    }
}

#[test]
fn empty_projection_cannot_adopt_an_incompatible_expected_type() {
    let input = Type::Vec(Box::new(Type::Tuple(vec![i64_type()])));
    let expected = Type::Vec(Box::new(Type::Int(Integer::U8)));
    let plan = program("#[test] fn example() { assert_eq!(repair::f().iter().map(|item| item.0).collect::<Vec<_>>(), repair::g()); }",
        BTreeMap::from([("f".into(), sig(input.clone())), ("g".into(), sig(expected.clone()))]), 1);
    let mut api = factory(vec![vec![
        Ok(Value::new(input, Data::Sequence(vec![])).unwrap()),
        Ok(Value::new(expected, Data::Sequence(vec![])).unwrap()),
    ]]);
    assert_eq!(
        plan.run(&mut api, Limits::default()).err(),
        Some(EvaluationError::Oracle)
    );
    assert_eq!(api.trace.borrow().finishes, 1);
}
