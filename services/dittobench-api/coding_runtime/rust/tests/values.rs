use coding_rust_suite::value::*;

#[test]
fn integer_widths_and_signs_are_checked_before_values_exist() {
    for kind in [
        Integer::U8,
        Integer::U16,
        Integer::U32,
        Integer::U64,
        Integer::Usize,
        Integer::I8,
        Integer::I16,
        Integer::I32,
        Integer::I64,
        Integer::Isize,
    ] {
        let (lo, hi) = kind.bounds();
        assert!(Value::integer(kind, lo).is_ok());
        assert!(Value::integer(kind, hi).is_ok());
        assert_eq!(Value::integer(kind, lo - 1).err(), Some(ValueError::Range));
        assert_eq!(Value::integer(kind, hi + 1).err(), Some(ValueError::Range));
    }
}
#[test]
fn schema_data_mismatches_are_never_coerced() {
    let boolean = Value::boolean(true);
    for (kind, data) in [
        (Type::Bool, Data::Int(1)),
        (Type::Int(Integer::U8), Data::Bool(true)),
        (
            Type::Vec(Box::new(Type::Int(Integer::U8))),
            Data::Sequence(vec![boolean.clone()]),
        ),
        (
            Type::Array(Box::new(Type::Bool), 2),
            Data::Sequence(vec![boolean.clone()]),
        ),
        (Type::Tuple(vec![Type::Bool]), Data::Tuple(vec![])),
        (
            Type::Option(Box::new(Type::Bool)),
            Data::Ok(Box::new(boolean.clone())),
        ),
        (
            Type::Result(Box::new(Type::Bool), Box::new(Type::Text)),
            Data::Err(Box::new(boolean.clone())),
        ),
        (Type::Ref(Box::new(Type::Bool)), Data::Bool(true)),
    ] {
        assert_eq!(Value::new(kind, data).err(), Some(ValueError::Type));
    }
}
#[test]
fn depth_nodes_and_text_bytes_have_independent_bounds() {
    assert_eq!(
        Value::new(Type::Text, Data::Text("x".repeat(65536))).err(),
        Some(ValueError::Limit)
    );
    assert_eq!(
        Value::new(
            Type::Vec(Box::new(Type::Bool)),
            Data::Sequence(vec![Value::boolean(true); 4096])
        )
        .err(),
        Some(ValueError::Limit)
    );
    let mut kind = Type::Bool;
    for _ in 0..16 {
        kind = Type::Ref(Box::new(kind));
    }
    assert_eq!(kind.validate(), Err(ValueError::Limit));
    assert_eq!(
        Type::Array(Box::new(Type::Bool), 4097).validate(),
        Err(ValueError::Limit)
    );
}

#[test]
fn unsized_slices_require_explicit_reference_descriptions() {
    let slice = Type::Slice(Box::new(Type::Bool));
    assert!(Type::Ref(Box::new(slice.clone())).validate().is_ok());
    for kind in [
        Type::Vec(Box::new(slice.clone())),
        Type::Array(Box::new(slice.clone()), 1),
        Type::Tuple(vec![slice.clone()]),
        Type::Option(Box::new(slice.clone())),
        Type::Result(Box::new(Type::Bool), Box::new(slice)),
    ] {
        assert_eq!(kind.validate(), Err(ValueError::Type));
    }
}
