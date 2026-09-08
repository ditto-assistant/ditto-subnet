use coding_rust_suite::{
    evaluator::Signature,
    value::{Data, Integer, Type, Value},
    wire::*,
};

fn schema() -> Vec<Signature> {
    vec![Signature {
        parameters: vec![Type::Int(Integer::U64)],
        result: Type::Int(Integer::U64),
    }]
}
fn number() -> Value {
    Value::integer(Integer::U64, u64::MAX as i128).unwrap()
}
fn exchange() -> (Session, Request) {
    let mut session = Session::new(schema()).unwrap();
    let bytes = session.begin(0, &[number()]).unwrap();
    let request = decode_request(&bytes, &schema()).unwrap();
    (session, request)
}

#[test]
fn every_data_family_round_trips_without_numeric_loss() {
    let boolean = Value::boolean(true);
    let values = [
        boolean.clone(),
        number(),
        Value::integer(Integer::I64, i64::MIN as i128).unwrap(),
        Value::new(Type::Text, Data::Text("β\0\n".into())).unwrap(),
        Value::new(Type::Char, Data::Char('🦀')).unwrap(),
        Value::new(Type::Vec(Box::new(Type::Bool)), Data::Sequence(vec![])).unwrap(),
        Value::new(
            Type::Array(Box::new(Type::Bool), 2),
            Data::Sequence(vec![boolean.clone(); 2]),
        )
        .unwrap(),
        Value::new(Type::Tuple(vec![]), Data::Tuple(vec![])).unwrap(),
        Value::new(
            Type::Tuple(vec![Type::Bool]),
            Data::Tuple(vec![boolean.clone()]),
        )
        .unwrap(),
        Value::new(Type::Option(Box::new(Type::Bool)), Data::None).unwrap(),
        Value::new(
            Type::Option(Box::new(Type::Bool)),
            Data::Some(Box::new(boolean.clone())),
        )
        .unwrap(),
        Value::new(
            Type::Result(Box::new(Type::Bool), Box::new(Type::Bool)),
            Data::Ok(Box::new(boolean.clone())),
        )
        .unwrap(),
        Value::new(
            Type::Result(Box::new(Type::Bool), Box::new(Type::Bool)),
            Data::Err(Box::new(boolean.clone())),
        )
        .unwrap(),
        Value::new(
            Type::Ref(Box::new(Type::Bool)),
            Data::Ref(Box::new(boolean.clone())),
        )
        .unwrap(),
        Value::new(
            Type::Ref(Box::new(Type::Slice(Box::new(Type::Bool)))),
            Data::Ref(Box::new(
                Value::new(
                    Type::Slice(Box::new(Type::Bool)),
                    Data::Sequence(vec![boolean]),
                )
                .unwrap(),
            )),
        )
        .unwrap(),
    ];
    for value in values {
        let encoded = encode_value(&value).unwrap();
        let decoded = decode_value(&encoded, value.kind()).unwrap();
        assert_eq!(decoded.kind(), value.kind());
        assert_eq!(encode_value(&decoded).unwrap(), encoded);
        let mut trailing = encoded.clone();
        trailing.push(0);
        assert!(decode_value(&trailing, value.kind()).is_err());
        if !encoded.is_empty() {
            assert!(decode_value(&encoded[..encoded.len() - 1], value.kind()).is_err());
        }
    }
    assert_eq!(
        encode_value(&number()).unwrap(),
        (u64::MAX as i128).to_be_bytes()
    );
}

#[test]
fn invalid_scalars_lengths_and_tags_are_rejected() {
    for (bytes, kind) in [
        (vec![2], Type::Bool),
        (256i128.to_be_bytes().to_vec(), Type::Int(Integer::U8)),
        ((-1i128).to_be_bytes().to_vec(), Type::Int(Integer::U64)),
        (0xd800u32.to_be_bytes().to_vec(), Type::Char),
        (vec![0, 0, 0, 1, 0xff], Type::Text),
        (u32::MAX.to_be_bytes().to_vec(), Type::Text),
        (
            u32::MAX.to_be_bytes().to_vec(),
            Type::Vec(Box::new(Type::Tuple(vec![]))),
        ),
        (
            4096u32.to_be_bytes().to_vec(),
            Type::Vec(Box::new(Type::Tuple(vec![]))),
        ),
        (vec![2], Type::Option(Box::new(Type::Bool))),
        (
            vec![2],
            Type::Result(Box::new(Type::Bool), Box::new(Type::Bool)),
        ),
    ] {
        assert!(decode_value(&bytes, &kind).is_err());
    }
    assert_eq!(
        decode_value(&vec![0; MAX_BODY + 1], &Type::Bool).err(),
        Some(WireError::Limit)
    );
}

#[test]
fn request_and_reply_bind_scope_types_identity_and_challenge() {
    let (mut session, request) = exchange();
    assert_eq!(request.function(), 0);
    assert!(matches!(request.arguments()[0].data(), Data::Int(n) if *n == u64::MAX as i128));
    let reply = request.response(Some(&number())).unwrap();
    assert!(
        matches!(session.complete(&reply), Ok(Reply::Value(v)) if matches!(v.data(), Data::Int(n) if *n == u64::MAX as i128))
    );
    let second = session.begin(0, &[number()]).unwrap();
    let next = decode_request(&second, &schema())
        .unwrap()
        .response(Some(&number()))
        .unwrap();
    assert_ne!(&reply[41..73], &next[41..73]);
    assert!(session.complete(&reply).is_err());
    assert!(matches!(session.complete(&next), Err(WireError::State)));
    assert!(matches!(
        session.begin(0, &[number()]),
        Err(WireError::State)
    ));
}

#[test]
fn cross_session_or_unsolicited_responses_poison_the_channel() {
    let (_, request) = exchange();
    let old = request.response(Some(&number())).unwrap();
    let (mut session, _) = exchange();
    assert!(session.complete(&old).is_err());
    let mut fresh = Session::new(schema()).unwrap();
    assert!(fresh.complete(&old).is_err());
    assert!(matches!(fresh.begin(0, &[number()]), Err(WireError::State)));
}

#[test]
fn header_and_payload_corruption_have_no_recovery_fallback() {
    for offset in [0, 4, 8, 9, 41, 73] {
        let (mut session, request) = exchange();
        let valid = request.response(Some(&number())).unwrap();
        let mut bad = valid.clone();
        bad[offset] ^= 255;
        assert!(session.complete(&bad).is_err());
        assert!(matches!(session.complete(&valid), Err(WireError::State)));
    }
    for truncate in [true, false] {
        let (mut session, request) = exchange();
        let mut frame = request.response(Some(&number())).unwrap();
        if truncate {
            frame.pop();
        } else {
            frame.push(0);
        }
        assert!(session.complete(&frame).is_err());
    }
}

#[test]
fn candidate_failure_is_not_a_domain_error_or_report() {
    let (mut session, request) = exchange();
    assert!(request.response(Some(&Value::boolean(true))).is_err());
    assert!(matches!(
        session.complete(&request.response(None).unwrap()),
        Ok(Reply::CandidateFailure)
    ));
    assert!(matches!(
        session.begin(0, &[number()]),
        Err(WireError::State)
    ));
    let (mut session, _) = exchange();
    assert!(session.complete(br#"{"passed":999,"failed":0}"#).is_err());
}

#[test]
fn one_pending_request_and_fixed_call_budget_are_enforced() {
    let (mut session, _) = exchange();
    assert!(matches!(
        session.begin(0, &[number()]),
        Err(WireError::State)
    ));
    let mut session = Session::new(schema()).unwrap();
    for _ in 0..128 {
        let frame = session.begin(0, &[number()]).unwrap();
        let request = decode_request(&frame, &schema()).unwrap();
        assert!(matches!(
            session.complete(&request.response(Some(&number())).unwrap()),
            Ok(Reply::Value(_))
        ));
    }
    assert!(matches!(
        session.begin(0, &[number()]),
        Err(WireError::State)
    ));
}

#[test]
fn request_arguments_share_a_decoded_data_budget() {
    let vector = Type::Vec(Box::new(Type::Tuple(vec![])));
    let schema = vec![Signature {
        parameters: vec![vector.clone(), vector.clone()],
        result: Type::Bool,
    }];
    let empty = Value::new(vector.clone(), Data::Sequence(vec![])).unwrap();
    let mut session = Session::new(schema.clone()).unwrap();
    let mut bytes = session.begin(0, &[empty.clone(), empty]).unwrap();
    bytes[88..92].copy_from_slice(&4095u32.to_be_bytes());
    bytes[96..100].copy_from_slice(&4095u32.to_be_bytes());
    assert!(matches!(
        decode_request(&bytes, &schema),
        Err(WireError::Limit)
    ));
    let unit = Value::new(Type::Tuple(vec![]), Data::Tuple(vec![])).unwrap();
    let large = Value::new(vector, Data::Sequence(vec![unit; 4095])).unwrap();
    let mut session = Session::new(schema).unwrap();
    assert!(matches!(
        session.begin(0, &[large.clone(), large]),
        Err(WireError::Limit)
    ));
}

#[test]
fn raw_request_cannot_select_unknown_functions_or_argument_types() {
    let mut session = Session::new(schema()).unwrap();
    assert!(session.begin(1, &[number()]).is_err());
    let mut session = Session::new(schema()).unwrap();
    assert!(session.begin(0, &[Value::boolean(true)]).is_err());
    let mut session = Session::new(schema()).unwrap();
    let bytes = session.begin(0, &[number()]).unwrap();
    let mut other = bytes.clone();
    other[81..83].copy_from_slice(&u16::MAX.to_be_bytes());
    assert!(decode_request(&other, &schema()).is_err());
    let mut arity = bytes;
    arity[83] = 255;
    assert!(decode_request(&arity, &schema()).is_err());
}

#[test]
fn all_truncated_packets_and_bounded_mutations_are_rejected_without_panics() {
    let mut session = Session::new(schema()).unwrap();
    let frame = session.begin(0, &[number()]).unwrap();
    for n in 0..frame.len() {
        assert!(decode_request(&frame[..n], &schema()).is_err());
    }
    for offset in 0..frame.len() {
        let mut frame = frame.clone();
        frame[offset] ^= 0x80;
        // Changed data can be a valid different input; the safety property here
        // is bounded parsing without a panic, not rejection of all value edits.
        let _ = decode_request(&frame, &schema());
    }
}

#[test]
fn session_identifiers_are_fresh_not_stable_program_fingerprints() {
    let mut session = Session::new(schema()).unwrap();
    let first = session.begin(0, &[number()]).unwrap();
    let response = decode_request(&first, &schema())
        .unwrap()
        .response(Some(&number()))
        .unwrap();
    assert!(session.complete(&response).is_ok());
    let second = session.begin(0, &[number()]).unwrap();
    let fresh = Session::new(schema())
        .unwrap()
        .begin(0, &[number()])
        .unwrap();
    assert_eq!(&first[9..41], &second[9..41]);
    assert_ne!(&first[9..41], &fresh[9..41]);
    assert_ne!(&first[41..73], &second[41..73]);
}

#[test]
fn valid_header_does_not_authorize_malformed_value_payload() {
    let schema = vec![Signature {
        parameters: vec![],
        result: Type::Bool,
    }];
    let mut session = Session::new(schema.clone()).unwrap();
    let request = decode_request(&session.begin(0, &[]).unwrap(), &schema).unwrap();
    let mut response = request.response(Some(&Value::boolean(true))).unwrap();
    response[HEADER + 4] = 2;
    assert!(matches!(
        session.complete(&response),
        Err(WireError::Invalid)
    ));
    assert!(matches!(session.begin(0, &[]), Err(WireError::State)));
}
