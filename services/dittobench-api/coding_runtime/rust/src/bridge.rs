//! Generate a public API bridge from an approved table, never from oracle source.
use crate::{
    evaluator::Signature,
    value::{Integer, Type},
};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum BridgeError {
    Schema,
    Limit,
}
pub struct GeneratedBridge {
    source: String,
    digest: [u8; 32],
}
impl GeneratedBridge {
    pub fn source(&self) -> &str {
        &self.source
    }
    pub fn sha256(&self) -> [u8; 32] {
        self.digest
    }
}
struct Output(String);
impl Output {
    fn push(&mut self, text: &str) -> Result<(), BridgeError> {
        if self
            .0
            .len()
            .checked_add(text.len())
            .is_none_or(|n| n > 1 << 20)
        {
            return Err(BridgeError::Limit);
        }
        self.0.push_str(text);
        Ok(())
    }
}
fn borrowed(kind: &Type) -> bool {
    match kind {
        Type::Ref(_) => true,
        Type::Vec(t) | Type::Slice(t) | Type::Array(t, _) | Type::Option(t) => borrowed(t),
        Type::Tuple(ts) => ts.iter().any(borrowed),
        Type::Result(a, b) => borrowed(a) || borrowed(b),
        _ => false,
    }
}
fn integer(k: Integer) -> &'static str {
    match k {
        Integer::U8 => "u8",
        Integer::U16 => "u16",
        Integer::U32 => "u32",
        Integer::U64 => "u64",
        Integer::Usize => "usize",
        Integer::I8 => "i8",
        Integer::I16 => "i16",
        Integer::I32 => "i32",
        Integer::I64 => "i64",
        Integer::Isize => "isize",
    }
}
fn native(kind: &Type, lifetime: &str) -> String {
    match kind {
        Type::Bool => "bool".into(),
        Type::Int(k) => integer(*k).into(),
        Type::Char => "char".into(),
        Type::Text => "::std::string::String".into(),
        Type::Vec(t) => format!("::std::vec::Vec<{}>", native(t, lifetime)),
        Type::Slice(t) => format!("[{}]", native(t, lifetime)),
        Type::Array(t, n) => format!("[{};{n}]", native(t, lifetime)),
        Type::Tuple(ts) => format!(
            "({})",
            ts.iter()
                .map(|t| format!("{},", native(t, lifetime)))
                .collect::<String>()
        ),
        Type::Option(t) => format!("::core::option::Option<{}>", native(t, lifetime)),
        Type::Result(a, b) => format!(
            "::core::result::Result<{},{}>",
            native(a, lifetime),
            native(b, lifetime)
        ),
        Type::Ref(t) if **t == Type::Text => format!("&{lifetime} str"),
        Type::Ref(t) => format!("&{lifetime} {}", native(t, lifetime)),
    }
}
fn schema(kind: &Type) -> String {
    match kind {
        Type::Bool => "Type::Bool".into(),
        Type::Int(k) => format!("Type::Int(Integer::{k:?})"),
        Type::Char => "Type::Char".into(),
        Type::Text => "Type::Text".into(),
        Type::Vec(t) => format!("Type::Vec(Box::new({}))", schema(t)),
        Type::Slice(t) => format!("Type::Slice(Box::new({}))", schema(t)),
        Type::Array(t, n) => format!("Type::Array(Box::new({}),{n})", schema(t)),
        Type::Tuple(ts) => format!(
            "Type::Tuple(vec![{}])",
            ts.iter().map(schema).collect::<Vec<_>>().join(",")
        ),
        Type::Option(t) => format!("Type::Option(Box::new({}))", schema(t)),
        Type::Result(a, b) => format!(
            "Type::Result(Box::new({}),Box::new({}))",
            schema(a),
            schema(b)
        ),
        Type::Ref(t) => format!("Type::Ref(Box::new({}))", schema(t)),
    }
}

pub fn generate(table: &BTreeMap<String, Signature>) -> Result<GeneratedBridge, BridgeError> {
    if table.is_empty() || table.len() > 64 {
        return Err(BridgeError::Schema);
    }
    for (name, signature) in table {
        if !crate::api_path(name) || signature.parameters.len() > 16 {
            return Err(BridgeError::Schema);
        }
        for kind in signature.parameters.iter().chain([&signature.result]) {
            kind.validate().map_err(|_| BridgeError::Schema)?;
            if matches!(kind, Type::Slice(_)) {
                return Err(BridgeError::Schema);
            }
        }
        for kind in &signature.parameters {
            let owned = if let Type::Ref(t) = kind {
                t.as_ref()
            } else {
                kind
            };
            if borrowed(owned) {
                return Err(BridgeError::Schema);
            }
        }
    }
    let mut out = Output(String::new());
    out.push("// Generated public API bridge; never append private suite source.\n#[cfg(not(all(target_os=\"linux\",target_arch=\"x86_64\",target_pointer_width=\"64\")))] compile_error!(\"Linux amd64 bridge required\");\nextern crate candidate;\nuse coding_rust_suite::{native::{NativeValue,NativeDecode,ConversionError,referent,decode_slice},value::{Type,Integer,Value},evaluator::Signature,wire::{Request,decode_request},wire_unix::{read_frame,write_frame}};\nuse std::{os::{fd::FromRawFd,unix::net::UnixStream},time::{Instant,Duration}};\n#[allow(unused_imports)] use std::net::Shutdown;\nfn schema()->Vec<Signature>{vec![\n")?;
    for signature in table.values() {
        out.push(&format!(
            "Signature{{parameters:vec![{}],result:{}}},\n",
            signature
                .parameters
                .iter()
                .map(schema)
                .collect::<Vec<_>>()
                .join(","),
            schema(&signature.result)
        ))?;
    }
    out.push("]}\nfn dispatch(request:&Request)->Result<Value,ConversionError>{ let arguments=request.arguments(); match request.function(){\n")?;
    for (index, (name, signature)) in table.iter().enumerate() {
        let has_borrow = signature.parameters.iter().any(borrowed);
        let lifetime = if has_borrow { "'a" } else { "'static" };
        out.push(&format!("{index}=>{{\n"))?;
        for (n, kind) in signature.parameters.iter().enumerate() {
            match kind {
                Type::Ref(t) => {
                    let input = format!("referent(&arguments[{n}])?");
                    match t.as_ref() {
                        Type::Slice(element) => {
                            out.push(&format!("let storage{n}=decode_slice::<{}>({input})?; let argument{n}=storage{n}.as_slice();\n",native(element,lifetime)))?;
                        }
                        Type::Text => out.push(&format!("let storage{n}=<String as NativeDecode>::from_value({input})?; let argument{n}=storage{n}.as_str();\n"))?,
                        _ => out.push(&format!("let storage{n}=<{} as NativeDecode>::from_value({input})?; let argument{n}=&storage{n};\n",native(t,lifetime)))?,
                    }
                }
                _ => out.push(&format!(
                    "let argument{n}=<{} as NativeDecode>::from_value(&arguments[{n}])?;\n",
                    native(kind, lifetime)
                ))?,
            }
        }
        let binder = if has_borrow { "for<'a> " } else { "" };
        out.push(&format!("let function:{binder}fn({})->{}=::candidate::{name};\nlet result=function({}); NativeValue::to_value(&result)\n}},\n",signature.parameters.iter().map(|t|native(t,lifetime)).collect::<Vec<_>>().join(","),native(&signature.result,lifetime),(0..signature.parameters.len()).map(|n|format!("argument{n}")).collect::<Vec<_>>().join(",")))?;
    }
    out.push("_=>Err(ConversionError)}}\nfn main(){\nstd::panic::set_hook(Box::new(|_|{}));\n// FD 0 is exclusively inherited from the verified pre-exec launcher.\n// This template does NOT install confinement; constructors run before main.\nlet mut socket=unsafe{UnixStream::from_raw_fd(0)};\nif socket.set_nonblocking(false).is_err(){return;}\nlet deadline=Instant::now()+Duration::from_secs(30); let schema=schema();\nfor _ in 0..128 {\nlet Ok(frame)=read_frame(&mut socket,deadline) else{break};\nlet Ok(request)=decode_request(&frame,&schema) else{break};\nlet output=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||dispatch(&request))).ok().and_then(Result::ok);\nlet Ok(response)=request.response(output.as_ref()) else{break};\nif write_frame(&mut socket,&response,deadline).is_err() || output.is_none(){break}\n}\nlet _=socket.shutdown(Shutdown::Both);\n}\n")?;
    let digest = Sha256::digest(out.0.as_bytes()).into();
    Ok(GeneratedBridge {
        source: out.0,
        digest,
    })
}
