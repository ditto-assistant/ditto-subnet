//! Versioned, bounded data frames. No score/report messages or peer-owned types.
use crate::{
    evaluator::Signature,
    value::{Data, Type, Value},
};

pub const MAX_BODY: usize = 65536;
pub const HEADER: usize = 77;
const MAGIC: &[u8; 4] = b"DRW1";
const REQUEST: u8 = 0;
const VALUE: u8 = 1;
const FAILURE: u8 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WireError {
    Limit,
    Invalid,
    State,
    Entropy,
    Timeout,
    Io,
    Truncated,
}
impl std::fmt::Display for WireError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Rust data channel rejected")
    }
}
impl std::error::Error for WireError {}
type Result<T> = std::result::Result<T, WireError>;

struct Budget {
    nodes: usize,
    bytes: usize,
}
impl Budget {
    fn new() -> Self {
        Self {
            nodes: 4096,
            bytes: 65536,
        }
    }
    fn charge(&mut self, nodes: usize, bytes: usize) -> Result<()> {
        self.nodes = self.nodes.checked_sub(nodes).ok_or(WireError::Limit)?;
        self.bytes = self.bytes.checked_sub(bytes).ok_or(WireError::Limit)?;
        Ok(())
    }
}
struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}
impl<'a> Cursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, pos: 0 }
    }
    fn take(&mut self, count: usize) -> Result<&'a [u8]> {
        let end = self.pos.checked_add(count).ok_or(WireError::Limit)?;
        let bytes = self.bytes.get(self.pos..end).ok_or(WireError::Truncated)?;
        self.pos = end;
        Ok(bytes)
    }
    fn byte(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn u32(&mut self) -> Result<u32> {
        Ok(u32::from_be_bytes(
            self.take(4)?.try_into().map_err(|_| WireError::Invalid)?,
        ))
    }
    fn done(&self) -> Result<()> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(WireError::Invalid)
        }
    }
}
fn append(output: &mut Vec<u8>, bytes: &[u8]) -> Result<()> {
    if output
        .len()
        .checked_add(bytes.len())
        .is_none_or(|n| n > MAX_BODY)
    {
        return Err(WireError::Limit);
    }
    output.extend_from_slice(bytes);
    Ok(())
}
fn length(output: &mut Vec<u8>, n: usize) -> Result<()> {
    append(
        output,
        &u32::try_from(n)
            .map_err(|_| WireError::Limit)?
            .to_be_bytes(),
    )
}

pub fn encode_value(value: &Value) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    encode(value, &mut bytes)?;
    Ok(bytes)
}
fn encode(value: &Value, output: &mut Vec<u8>) -> Result<()> {
    match value.data() {
        Data::Bool(v) => append(output, &[u8::from(*v)])?,
        Data::Int(v) => append(output, &v.to_be_bytes())?,
        Data::Char(v) => append(output, &(*v as u32).to_be_bytes())?,
        Data::Text(v) => {
            length(output, v.len())?;
            append(output, v.as_bytes())?;
        }
        Data::Sequence(vs) => {
            if !matches!(value.kind(), Type::Array(_, _)) {
                length(output, vs.len())?;
            }
            for v in vs {
                encode(v, output)?;
            }
        }
        Data::Tuple(vs) => {
            for v in vs {
                encode(v, output)?;
            }
        }
        Data::None => append(output, &[0])?,
        Data::Some(v) => {
            append(output, &[1])?;
            encode(v, output)?;
        }
        Data::Ok(v) => {
            append(output, &[0])?;
            encode(v, output)?;
        }
        Data::Err(v) => {
            append(output, &[1])?;
            encode(v, output)?;
        }
        Data::Ref(v) => encode(v, output)?,
    }
    Ok(())
}
pub fn decode_value(bytes: &[u8], kind: &Type) -> Result<Value> {
    if bytes.len() > MAX_BODY {
        return Err(WireError::Limit);
    }
    kind.validate().map_err(|_| WireError::Invalid)?;
    decode_exact(bytes, kind, &mut Budget::new())
}
fn decode_exact(bytes: &[u8], kind: &Type, budget: &mut Budget) -> Result<Value> {
    let mut cursor = Cursor::new(bytes);
    let value = decode(&mut cursor, kind, budget)?;
    cursor.done()?;
    Ok(value)
}
fn decode(cursor: &mut Cursor<'_>, kind: &Type, budget: &mut Budget) -> Result<Value> {
    budget.charge(1, 16)?;
    let data = match kind {
        Type::Bool => match cursor.byte()? {
            0 => Data::Bool(false),
            1 => Data::Bool(true),
            _ => return Err(WireError::Invalid),
        },
        Type::Int(_) => Data::Int(i128::from_be_bytes(
            cursor
                .take(16)?
                .try_into()
                .map_err(|_| WireError::Invalid)?,
        )),
        Type::Char => Data::Char(char::from_u32(cursor.u32()?).ok_or(WireError::Invalid)?),
        Type::Text => {
            let n = cursor.u32()? as usize;
            budget.charge(0, n)?;
            let text = std::str::from_utf8(cursor.take(n)?).map_err(|_| WireError::Invalid)?;
            Data::Text(text.to_owned())
        }
        Type::Vec(t) | Type::Slice(t) | Type::Array(t, _) => {
            let n = if let Type::Array(_, n) = kind {
                *n
            } else {
                cursor.u32()? as usize
            };
            if n > budget.nodes || n > 4096 {
                return Err(WireError::Limit);
            }
            let mut values = Vec::with_capacity(n);
            for _ in 0..n {
                values.push(decode(cursor, t, budget)?);
            }
            Data::Sequence(values)
        }
        Type::Tuple(ts) => {
            let mut values = Vec::with_capacity(ts.len());
            for t in ts {
                values.push(decode(cursor, t, budget)?);
            }
            Data::Tuple(values)
        }
        Type::Option(t) => match cursor.byte()? {
            0 => Data::None,
            1 => Data::Some(Box::new(decode(cursor, t, budget)?)),
            _ => return Err(WireError::Invalid),
        },
        Type::Result(ok, err) => match cursor.byte()? {
            0 => Data::Ok(Box::new(decode(cursor, ok, budget)?)),
            1 => Data::Err(Box::new(decode(cursor, err, budget)?)),
            _ => return Err(WireError::Invalid),
        },
        Type::Ref(t) => Data::Ref(Box::new(decode(cursor, t, budget)?)),
    };
    Value::new(kind.clone(), data).map_err(|_| WireError::Invalid)
}

#[derive(Clone)]
struct Context {
    session: [u8; 32],
    challenge: [u8; 32],
    id: u64,
}
fn frame(context: &Context, opcode: u8, payload: &[u8]) -> Result<Vec<u8>> {
    let count = HEADER.checked_add(payload.len()).ok_or(WireError::Limit)?;
    if count > MAX_BODY {
        return Err(WireError::Limit);
    }
    let mut bytes = Vec::with_capacity(count + 4);
    bytes.extend_from_slice(&(count as u32).to_be_bytes());
    bytes.extend_from_slice(MAGIC);
    bytes.push(opcode);
    bytes.extend_from_slice(&context.session);
    bytes.extend_from_slice(&context.challenge);
    bytes.extend_from_slice(&context.id.to_be_bytes());
    bytes.extend_from_slice(payload);
    Ok(bytes)
}
fn header(bytes: &[u8]) -> Result<(Context, u8, Cursor<'_>)> {
    if bytes.len() > MAX_BODY + 4 {
        return Err(WireError::Limit);
    }
    let mut cursor = Cursor::new(bytes);
    let count = cursor.u32()? as usize;
    if !(HEADER..=MAX_BODY).contains(&count) {
        return Err(WireError::Limit);
    }
    if bytes.len() != count + 4 || cursor.take(4)? != MAGIC {
        return Err(WireError::Invalid);
    }
    let opcode = cursor.byte()?;
    let session = cursor
        .take(32)?
        .try_into()
        .map_err(|_| WireError::Invalid)?;
    let challenge = cursor
        .take(32)?
        .try_into()
        .map_err(|_| WireError::Invalid)?;
    let id = u64::from_be_bytes(cursor.take(8)?.try_into().map_err(|_| WireError::Invalid)?);
    if id == 0 || id > 128 {
        return Err(WireError::Invalid);
    }
    Ok((
        Context {
            session,
            challenge,
            id,
        },
        opcode,
        cursor,
    ))
}
fn validate_schema(schema: &[Signature]) -> Result<()> {
    if schema.is_empty() || schema.len() > 64 {
        return Err(WireError::Invalid);
    }
    for signature in schema {
        if signature.parameters.len() > 16 {
            return Err(WireError::Invalid);
        }
        for t in signature.parameters.iter().chain([&signature.result]) {
            t.validate().map_err(|_| WireError::Invalid)?;
            if matches!(t, Type::Slice(_)) {
                return Err(WireError::Invalid);
            }
        }
    }
    Ok(())
}

/// Private bridge request. No Debug/serialization of the complete request.
pub struct Request {
    context: Context,
    function: usize,
    arguments: Vec<Value>,
    result: Type,
}
impl Request {
    pub fn function(&self) -> usize {
        self.function
    }
    pub fn arguments(&self) -> &[Value] {
        &self.arguments
    }
    /// None denotes candidate failure, never an infrastructure error or a count.
    pub fn response(&self, value: Option<&Value>) -> Result<Vec<u8>> {
        match value {
            Some(v) if v.kind() == &self.result => frame(&self.context, VALUE, &encode_value(v)?),
            Some(_) => Err(WireError::Invalid),
            None => frame(&self.context, FAILURE, &[]),
        }
    }
}
pub fn decode_request(bytes: &[u8], schema: &[Signature]) -> Result<Request> {
    validate_schema(schema)?;
    let (context, opcode, mut cursor) = header(bytes)?;
    if opcode != REQUEST {
        return Err(WireError::Invalid);
    }
    let function =
        u16::from_be_bytes(cursor.take(2)?.try_into().map_err(|_| WireError::Invalid)?) as usize;
    let signature = schema.get(function).ok_or(WireError::Invalid)?;
    if cursor.byte()? as usize != signature.parameters.len() {
        return Err(WireError::Invalid);
    }
    let mut budget = Budget::new();
    let mut arguments = Vec::new();
    for kind in &signature.parameters {
        let n = cursor.u32()? as usize;
        arguments.push(decode_exact(cursor.take(n)?, kind, &mut budget)?);
    }
    cursor.done()?;
    Ok(Request {
        context,
        function,
        arguments,
        result: signature.result.clone(),
    })
}

pub enum Reply {
    Value(Value),
    CandidateFailure,
}
/// One outstanding request; every error/failure permanently poisons the session.
pub struct Session {
    session: [u8; 32],
    schema: Vec<Signature>,
    next: u64,
    pending: Option<(Context, Type)>,
    poisoned: bool,
}
impl Session {
    /// Caller binds this fixed ordered schema to the approved bridge privately.
    /// No private program/source digest or stable task identifier is transmitted.
    pub fn new(schema: Vec<Signature>) -> Result<Self> {
        validate_schema(&schema)?;
        let mut session = [0; 32];
        getrandom::fill(&mut session).map_err(|_| WireError::Entropy)?;
        Ok(Self {
            session,
            schema,
            next: 1,
            pending: None,
            poisoned: false,
        })
    }
    pub fn poison(&mut self) {
        self.pending = None;
        self.poisoned = true;
    }
    pub fn begin(&mut self, function: usize, arguments: &[Value]) -> Result<Vec<u8>> {
        let result = self.begin_inner(function, arguments);
        if result.is_err() {
            self.poison();
        }
        result
    }
    fn begin_inner(&mut self, function: usize, arguments: &[Value]) -> Result<Vec<u8>> {
        if self.poisoned || self.pending.is_some() || self.next > 128 {
            return Err(WireError::State);
        }
        let signature = self.schema.get(function).ok_or(WireError::Invalid)?;
        if arguments.len() != signature.parameters.len() {
            return Err(WireError::Invalid);
        }
        let mut payload = Vec::new();
        append(&mut payload, &(function as u16).to_be_bytes())?;
        append(&mut payload, &[arguments.len() as u8])?;
        let mut budget = Budget::new();
        for (value, expected) in arguments.iter().zip(&signature.parameters) {
            if value.kind() != expected {
                return Err(WireError::Invalid);
            }
            let (nodes, bytes) = value.cost();
            budget.charge(nodes, bytes)?;
            let value = encode_value(value)?;
            length(&mut payload, value.len())?;
            append(&mut payload, &value)?;
        }
        let mut challenge = [0; 32];
        getrandom::fill(&mut challenge).map_err(|_| WireError::Entropy)?;
        let context = Context {
            session: self.session,
            challenge,
            id: self.next,
        };
        let output = frame(&context, REQUEST, &payload)?;
        self.next += 1;
        self.pending = Some((context, signature.result.clone()));
        Ok(output)
    }
    pub fn complete(&mut self, bytes: &[u8]) -> Result<Reply> {
        let result = self.complete_inner(bytes);
        if !matches!(result, Ok(Reply::Value(_))) {
            self.poison();
        }
        result
    }
    fn complete_inner(&mut self, bytes: &[u8]) -> Result<Reply> {
        if self.poisoned {
            return Err(WireError::State);
        }
        let (expected, kind) = self.pending.take().ok_or(WireError::State)?;
        let (context, opcode, cursor) = header(bytes)?;
        if context.session != expected.session
            || context.challenge != expected.challenge
            || context.id != expected.id
        {
            return Err(WireError::Invalid);
        }
        match opcode {
            VALUE => Ok(Reply::Value(decode_value(
                &cursor.bytes[cursor.pos..],
                &kind,
            )?)),
            FAILURE => {
                cursor.done()?;
                Ok(Reply::CandidateFailure)
            }
            _ => Err(WireError::Invalid),
        }
    }
}
