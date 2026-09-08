//! Bounded data descriptions, never candidate objects, pointers, or executable code.
use sha2::Digest;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Integer {
    U8 = 0,
    U16 = 1,
    U32 = 2,
    U64 = 3,
    Usize = 4,
    I8 = 5,
    I16 = 6,
    I32 = 7,
    I64 = 8,
    Isize = 9,
}

impl Integer {
    pub fn bounds(self) -> (i128, i128) {
        let (signed, bits) = match self {
            Self::U8 => (false, 8),
            Self::U16 => (false, 16),
            Self::U32 => (false, 32),
            Self::U64 | Self::Usize => (false, 64),
            Self::I8 => (true, 8),
            Self::I16 => (true, 16),
            Self::I32 => (true, 32),
            Self::I64 | Self::Isize => (true, 64),
        };
        if signed {
            (-(1i128 << (bits - 1)), (1i128 << (bits - 1)) - 1)
        } else {
            (0, (1i128 << bits) - 1)
        }
    }
    pub(crate) fn named(name: &str) -> Option<Self> {
        Some(match name {
            "u8" => Self::U8,
            "u16" => Self::U16,
            "u32" => Self::U32,
            "u64" => Self::U64,
            "usize" => Self::Usize,
            "i8" => Self::I8,
            "i16" => Self::I16,
            "i32" => Self::I32,
            "i64" => Self::I64,
            "isize" => Self::Isize,
            _ => return None,
        })
    }
}

/// Controller-owned data schema. usize/isize are explicitly 64-bit in this profile.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Type {
    Bool,
    Int(Integer),
    Text,
    Char,
    Vec(Box<Type>),
    Slice(Box<Type>),
    Array(Box<Type>, usize),
    Tuple(Vec<Type>),
    Option(Box<Type>),
    Result(Box<Type>, Box<Type>),
    Ref(Box<Type>),
}

impl Type {
    pub fn validate(&self) -> Result<(), ValueError> {
        let mut nodes = 0;
        self.check(0, &mut nodes)
    }
    fn check(&self, depth: usize, nodes: &mut usize) -> Result<(), ValueError> {
        *nodes += 1;
        if depth >= 16 || *nodes > 256 {
            return Err(ValueError::Limit);
        }
        match self {
            Self::Vec(t) | Self::Slice(t) | Self::Option(t) => {
                if matches!(t.as_ref(), Self::Slice(_)) {
                    return Err(ValueError::Type);
                }
                t.check(depth + 1, nodes)?
            }
            Self::Ref(t) => t.check(depth + 1, nodes)?,
            Self::Array(t, n) => {
                if *n > 4096 {
                    return Err(ValueError::Limit);
                }
                if matches!(t.as_ref(), Self::Slice(_)) {
                    return Err(ValueError::Type);
                }
                t.check(depth + 1, nodes)?;
            }
            Self::Tuple(items) => {
                if items.len() > 16 {
                    return Err(ValueError::Limit);
                }
                for t in items {
                    if matches!(t, Self::Slice(_)) {
                        return Err(ValueError::Type);
                    }
                    t.check(depth + 1, nodes)?;
                }
            }
            Self::Result(ok, err) => {
                if matches!(ok.as_ref(), Self::Slice(_)) || matches!(err.as_ref(), Self::Slice(_)) {
                    return Err(ValueError::Type);
                }
                ok.check(depth + 1, nodes)?;
                err.check(depth + 1, nodes)?;
            }
            _ => (),
        }
        Ok(())
    }
    pub(crate) fn hash(&self, hash: &mut sha2::Sha256) {
        match self {
            Self::Bool => hash.update([0]),
            Self::Int(k) => hash.update([1, *k as u8]),
            Self::Text => hash.update([2]),
            Self::Char => hash.update([3]),
            Self::Vec(t) => {
                hash.update([4]);
                t.hash(hash);
            }
            Self::Array(t, n) => {
                hash.update([5]);
                hash.update((*n as u64).to_be_bytes());
                t.hash(hash);
            }
            Self::Tuple(ts) => {
                hash.update([6]);
                hash.update((ts.len() as u64).to_be_bytes());
                for t in ts {
                    t.hash(hash);
                }
            }
            Self::Option(t) => {
                hash.update([7]);
                t.hash(hash);
            }
            Self::Result(ok, err) => {
                hash.update([8]);
                ok.hash(hash);
                err.hash(hash);
            }
            Self::Ref(t) => {
                hash.update([9]);
                t.hash(hash);
            }
            Self::Slice(t) => {
                hash.update([10]);
                t.hash(hash);
            }
        }
    }
}

/// No Debug/Display/Serialize: values may contain private inputs and expected answers.
#[derive(Clone)]
pub enum Data {
    Bool(bool),
    Int(i128),
    Text(String),
    Char(char),
    Sequence(Vec<Value>),
    Tuple(Vec<Value>),
    None,
    Some(Box<Value>),
    Ok(Box<Value>),
    Err(Box<Value>),
    Ref(Box<Value>),
}

/// Values intentionally have no automatic debug or serialization output.
/// ```compile_fail
/// fn log_private(value: coding_rust_suite::value::Value) { println!("{value:?}"); }
/// ```
#[derive(Clone)]
pub struct Value {
    kind: Type,
    data: Data,
    nodes: usize,
    bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ValueError {
    Type,
    Range,
    Limit,
}

impl Value {
    /// A transport must additionally bound its raw frame *before* decoding.
    pub fn new(kind: Type, data: Data) -> Result<Self, ValueError> {
        kind.validate()?;
        let mut nodes = 1usize;
        let mut bytes = 16usize;
        let mut child = |value: &Value, expected: &Type| -> Result<(), ValueError> {
            if &value.kind != expected {
                return Err(ValueError::Type);
            }
            nodes = nodes.checked_add(value.nodes).ok_or(ValueError::Limit)?;
            bytes = bytes.checked_add(value.bytes).ok_or(ValueError::Limit)?;
            Ok(())
        };
        match (&kind, &data) {
            (Type::Bool, Data::Bool(_)) | (Type::Char, Data::Char(_)) => (),
            (Type::Int(k), Data::Int(v)) => {
                let (lo, hi) = k.bounds();
                if *v < lo || *v > hi {
                    return Err(ValueError::Range);
                }
            }
            (Type::Text, Data::Text(s)) => {
                bytes = bytes.checked_add(s.len()).ok_or(ValueError::Limit)?
            }
            (Type::Vec(t) | Type::Slice(t) | Type::Array(t, _), Data::Sequence(v)) => {
                if v.len() > 4096 {
                    return Err(ValueError::Limit);
                }
                if matches!(&kind, Type::Array(_, n) if *n != v.len()) {
                    return Err(ValueError::Type);
                }
                for v in v {
                    child(v, t)?;
                }
            }
            (Type::Tuple(ts), Data::Tuple(vs)) => {
                if ts.len() != vs.len() {
                    return Err(ValueError::Type);
                }
                for (t, v) in ts.iter().zip(vs) {
                    child(v, t)?;
                }
            }
            (Type::Option(_), Data::None) => (),
            (Type::Option(t), Data::Some(v)) | (Type::Ref(t), Data::Ref(v)) => child(v, t)?,
            (Type::Result(t, _), Data::Ok(v)) | (Type::Result(_, t), Data::Err(v)) => child(v, t)?,
            _ => return Err(ValueError::Type),
        }
        if nodes > 4096 || bytes > 65536 {
            return Err(ValueError::Limit);
        }
        Ok(Self {
            kind,
            data,
            nodes,
            bytes,
        })
    }
    pub fn kind(&self) -> &Type {
        &self.kind
    }
    pub fn data(&self) -> &Data {
        &self.data
    }
    pub fn integer(kind: Integer, value: i128) -> Result<Self, ValueError> {
        Self::new(Type::Int(kind), Data::Int(value))
    }
    pub fn boolean(value: bool) -> Self {
        Self::new(Type::Bool, Data::Bool(value)).expect("bounded Boolean")
    }
    pub(crate) fn cost(&self) -> (usize, usize) {
        (self.nodes, self.bytes)
    }
    pub(crate) fn deref(&self) -> &Value {
        match &self.data {
            Data::Ref(value) => value.deref(),
            _ => self,
        }
    }
}

/// Explicit structural PartialEq subset, never serialization/string coercion.
pub(crate) fn equal(a: &Value, b: &Value) -> Result<bool, ValueError> {
    if !comparable(a.kind(), b.kind()) {
        return Err(ValueError::Type);
    }
    match (a.data(), b.data()) {
        (Data::Bool(a), Data::Bool(b)) => Ok(a == b),
        (Data::Int(a), Data::Int(b)) => Ok(a == b),
        (Data::Text(a), Data::Text(b)) => Ok(a == b),
        (Data::Char(a), Data::Char(b)) => Ok(a == b),
        (Data::Ref(a), Data::Ref(b)) => equal(a, b),
        (Data::Ref(a), _) if a.kind() == &Type::Text => equal(a, b),
        (_, Data::Ref(b)) if b.kind() == &Type::Text => equal(a, b),
        (Data::Sequence(a), Data::Sequence(b)) | (Data::Tuple(a), Data::Tuple(b)) => {
            if a.len() != b.len() {
                return Ok(false);
            }
            for (a, b) in a.iter().zip(b) {
                if !equal(a, b)? {
                    return Ok(false);
                }
            }
            Ok(true)
        }
        (Data::None, Data::None) => Ok(true),
        (Data::Some(a), Data::Some(b))
        | (Data::Ok(a), Data::Ok(b))
        | (Data::Err(a), Data::Err(b)) => equal(a, b),
        (Data::None, Data::Some(_))
        | (Data::Some(_), Data::None)
        | (Data::Ok(_), Data::Err(_))
        | (Data::Err(_), Data::Ok(_)) => Ok(false),
        _ => Err(ValueError::Type),
    }
}

fn comparable(a: &Type, b: &Type) -> bool {
    match (a, b) {
        (Type::Array(a, an), Type::Array(b, bn)) => an == bn && comparable(a, b),
        (
            Type::Vec(a) | Type::Slice(a) | Type::Array(a, _),
            Type::Vec(b) | Type::Slice(b) | Type::Array(b, _),
        ) => comparable(a, b),
        // A Text target beneath a reference denotes str, not an owned String.
        // Do not recursively turn &&str == &str into String == &str.
        (Type::Ref(a), Type::Ref(b)) if **a == Type::Text || **b == Type::Text => a == b,
        (Type::Ref(a), Type::Ref(b)) => comparable(a, b),
        (Type::Ref(a), Type::Text) | (Type::Text, Type::Ref(a)) => **a == Type::Text,
        // Rust's tuple, Option, and Result PartialEq require the same types.
        // The default exact comparison below retains those generic boundaries.
        _ => a == b,
    }
}
