//! Parent-owned closed data evaluator. No compiler, loader, or byte transport.
use crate::{
    value::{equal, Data, Integer, Type, Value, ValueError},
    AdmittedSuite,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use syn::{parse::Parser, punctuated::Punctuated, Expr, Stmt, Token};

#[derive(Clone)]
pub struct Signature {
    pub parameters: Vec<Type>,
    pub result: Type,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ApiError {
    Candidate,
    Transport,
}

/// Implemented by a trusted, bounded IPC adapter, NEVER by loading candidate
/// Rust into this process. No suite, assertion, expected value, or count is sent.
pub trait CandidateApi {
    fn call(&mut self, function: &str, arguments: &[Value]) -> Result<Value, ApiError>;
    /// Must terminate/reap and verify cleanup even after a failed assertion/call.
    fn finish(&mut self) -> Result<(), ApiError>;
}

pub trait ApiFactory {
    /// Each test requires a fresh isolated session. No private test name/index.
    fn start(&mut self) -> Result<Box<dyn CandidateApi>, ApiError>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EvaluationError {
    Schema,
    Oracle,
    Limit,
    Transport,
    Cleanup,
}

impl std::fmt::Display for EvaluationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Rust evaluation did not complete")
    }
}
impl std::error::Error for EvaluationError {}

#[derive(Clone, Copy)]
pub struct Limits {
    pub steps: usize,
    pub calls: usize,
    pub nodes: usize,
    pub bytes: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            steps: 100_000,
            calls: 128,
            nodes: 262_144,
            bytes: 4 * 1024 * 1024,
        }
    }
}
impl Limits {
    fn valid(self) -> bool {
        let max = Self::default();
        self.steps > 0
            && self.steps <= max.steps
            && self.calls > 0
            && self.calls <= max.calls
            && self.nodes > 0
            && self.nodes <= max.nodes
            && self.bytes > 0
            && self.bytes <= max.bytes
    }
}

/// Opaque private program with exact source, admission policy, and API schema binding.
pub struct Program {
    suite: AdmittedSuite,
    schema: BTreeMap<String, Signature>,
    digest: [u8; 32],
}
pub struct Report {
    passed: usize,
    failed: usize,
    digest: [u8; 32],
}
impl Report {
    pub fn passed(&self) -> usize {
        self.passed
    }
    pub fn failed(&self) -> usize {
        self.failed
    }
    pub fn program_sha256(&self) -> [u8; 32] {
        self.digest
    }
}

impl Program {
    pub fn bind(
        suite: AdmittedSuite,
        schema: BTreeMap<String, Signature>,
    ) -> Result<Self, EvaluationError> {
        if schema.len() != suite.functions.len()
            || suite.functions.iter().any(|f| !schema.contains_key(f))
        {
            return Err(EvaluationError::Schema);
        }
        let mut hash = Sha256::new();
        hash.update(b"coding-rust-parent-data-v1\0");
        hash.update(suite.policy_sha256());
        for (name, signature) in &schema {
            if signature.parameters.len() > 16 {
                return Err(EvaluationError::Schema);
            }
            for kind in signature.parameters.iter().chain([&signature.result]) {
                kind.validate().map_err(|_| EvaluationError::Schema)?;
                if matches!(kind, Type::Slice(_)) {
                    return Err(EvaluationError::Schema);
                }
            }
            hash.update(name.as_bytes());
            hash.update([0]);
            hash.update((signature.parameters.len() as u64).to_be_bytes());
            for kind in &signature.parameters {
                kind.hash(&mut hash);
            }
            signature.result.hash(&mut hash);
        }
        Ok(Self {
            suite,
            schema,
            digest: hash.finalize().into(),
        })
    }
    pub fn program_sha256(&self) -> [u8; 32] {
        self.digest
    }
    pub fn run(
        &self,
        factory: &mut impl ApiFactory,
        limits: Limits,
    ) -> Result<Report, EvaluationError> {
        if !limits.valid() {
            return Err(EvaluationError::Limit);
        }
        let mut report = Report {
            passed: 0,
            failed: 0,
            digest: self.digest,
        };
        for test in &self.suite.tests {
            let mut api = factory.start().map_err(|_| EvaluationError::Transport)?;
            let result = Context {
                program: self,
                api: api.as_mut(),
                locals: BTreeMap::new(),
                type_bindings: BTreeMap::new(),
                budget: limits,
            }
            .test(test);
            // Cleanup failure overrides every result; no report survives it.
            api.finish().map_err(|_| EvaluationError::Cleanup)?;
            match result {
                Ok(()) => report.passed += 1,
                Err(Failure::Test) => report.failed += 1,
                Err(Failure::Fatal(error)) => return Err(error),
            }
        }
        Ok(report)
    }
}

enum Failure {
    Test,
    Fatal(EvaluationError),
}
type ResultValue<T> = Result<T, Failure>;
fn oracle<T>() -> ResultValue<T> {
    Err(Failure::Fatal(EvaluationError::Oracle))
}
impl From<ValueError> for Failure {
    fn from(error: ValueError) -> Self {
        Self::Fatal(if error == ValueError::Limit {
            EvaluationError::Limit
        } else {
            EvaluationError::Oracle
        })
    }
}
fn args(mac: &syn::Macro) -> ResultValue<Punctuated<Expr, Token![,]>> {
    Punctuated::<Expr, Token![,]>::parse_terminated
        .parse2(mac.tokens.clone())
        .map_err(|_| Failure::Fatal(EvaluationError::Oracle))
}
fn element(kind: Option<&Type>) -> Option<&Type> {
    match kind {
        Some(Type::Vec(t) | Type::Slice(t) | Type::Array(t, _)) => Some(t),
        _ => None,
    }
}
fn strip_ref(kind: &Type) -> &Type {
    match kind {
        Type::Ref(t) => strip_ref(t),
        _ => kind,
    }
}

struct Context<'a> {
    program: &'a Program,
    api: &'a mut dyn CandidateApi,
    locals: BTreeMap<String, Value>,
    type_bindings: BTreeMap<String, Type>,
    budget: Limits,
}
impl Context<'_> {
    fn spend(counter: &mut usize, amount: usize) -> ResultValue<()> {
        *counter = counter
            .checked_sub(amount)
            .ok_or(Failure::Fatal(EvaluationError::Limit))?;
        Ok(())
    }
    fn charge(&mut self, value: &Value) -> ResultValue<()> {
        let (nodes, bytes) = value.cost();
        Self::spend(&mut self.budget.nodes, nodes)?;
        Self::spend(&mut self.budget.bytes, bytes)
    }
    fn test(&mut self, test: &syn::ItemFn) -> ResultValue<()> {
        for statement in &test.block.stmts {
            match statement {
                Stmt::Local(local) => {
                    let syn::Pat::Ident(name) = &local.pat else {
                        return oracle();
                    };
                    let value = self.eval(
                        &local
                            .init
                            .as_ref()
                            .ok_or(Failure::Fatal(EvaluationError::Oracle))?
                            .expr,
                        None,
                    )?;
                    self.locals.insert(name.ident.to_string(), value);
                }
                Stmt::Macro(statement) => self.assertion(&statement.mac)?,
                Stmt::Expr(Expr::Macro(statement), _) => self.assertion(&statement.mac)?,
                _ => return oracle(),
            }
        }
        Ok(())
    }
    fn assertion(&mut self, mac: &syn::Macro) -> ResultValue<()> {
        let arguments = args(mac)?;
        let passed = if mac.path.is_ident("assert_eq") && arguments.len() == 2 {
            self.comparison(&arguments[0], &arguments[1])?
        } else if mac.path.is_ident("assert") && arguments.len() == 1 {
            let value = self.eval(&arguments[0], Some(&Type::Bool))?;
            let Data::Bool(passed) = value.data() else {
                return oracle();
            };
            *passed
        } else {
            return oracle();
        };
        if passed {
            Ok(())
        } else {
            Err(Failure::Test)
        }
    }
    fn comparison(&mut self, left: &Expr, right: &Expr) -> ResultValue<bool> {
        let hint = self.infer(right);
        let left = self.eval(left, hint.as_ref())?;
        let right = self.eval(right, Some(left.kind()))?;
        equal(&left, &right).map_err(Into::into)
    }
    fn function(&self, path: &syn::ExprPath) -> Option<String> {
        let names: Vec<_> = path
            .path
            .segments
            .iter()
            .map(|s| s.ident.to_string())
            .collect();
        match names.as_slice() {
            [name] => self.program.suite.imports.get(name).cloned(),
            [root, tail @ ..] if root == &self.program.suite.crate_name => {
                let name = tail.join("::");
                self.program.schema.contains_key(&name).then_some(name)
            }
            _ => None,
        }
    }
    // Static hints never invoke the API. This is deliberately not whole-program
    // Rust type inference: unconstrained locals use the explicit profile default.
    fn infer(&self, expr: &Expr) -> Option<Type> {
        match expr {
            Expr::Path(p) => {
                if p.path.segments.len() == 2 {
                    Integer::named(&p.path.segments[0].ident.to_string()).map(Type::Int)
                } else {
                    self.type_bindings
                        .get(&p.path.segments[0].ident.to_string())
                        .cloned()
                        .or_else(|| {
                            self.locals
                                .get(&p.path.segments[0].ident.to_string())
                                .map(|v| v.kind().clone())
                        })
                }
            }
            Expr::Call(c) => {
                let Expr::Path(p) = c.func.as_ref() else {
                    return None;
                };
                if p.path.is_ident("Some") {
                    return self.infer(&c.args[0]).map(|t| Type::Option(Box::new(t)));
                }
                self.function(p)
                    .and_then(|name| self.program.schema.get(&name))
                    .map(|s| s.result.clone())
            }
            Expr::Lit(l) => match &l.lit {
                syn::Lit::Int(n) => Integer::named(n.suffix()).map(Type::Int),
                syn::Lit::Bool(_) => Some(Type::Bool),
                syn::Lit::Char(_) => Some(Type::Char),
                syn::Lit::Str(_) => Some(Type::Ref(Box::new(Type::Text))),
                _ => None,
            },
            Expr::Paren(p) => self.infer(&p.expr),
            Expr::Unary(u) => self.infer(&u.expr),
            Expr::Reference(r) => self.infer(&r.expr).map(|t| Type::Ref(Box::new(t))),
            Expr::Array(a) => a
                .elems
                .iter()
                .find_map(|e| self.infer(e))
                .map(|t| Type::Array(Box::new(t), a.elems.len())),
            Expr::Tuple(t) => t
                .elems
                .iter()
                .map(|e| self.infer(e))
                .collect::<Option<Vec<_>>>()
                .map(Type::Tuple),
            Expr::Field(f) => {
                let kind = self.infer(&f.base)?;
                let Type::Tuple(fields) = strip_ref(&kind) else {
                    return None;
                };
                let syn::Member::Unnamed(n) = &f.member else {
                    return None;
                };
                fields.get(n.index as usize).cloned()
            }
            Expr::Index(i) => {
                let kind = self.infer(&i.expr)?;
                element(Some(strip_ref(&kind))).cloned()
            }
            Expr::MethodCall(m) => match m.method.to_string().as_str() {
                "is_ok" | "is_err" | "is_some" | "is_none" | "is_empty" => Some(Type::Bool),
                "len" => Some(Type::Int(Integer::Usize)),
                "unwrap" | "unwrap_err" => {
                    let kind = self.infer(&m.receiver)?;
                    match kind {
                        Type::Option(t) if m.method == "unwrap" => Some(*t),
                        Type::Result(ok, err) => {
                            Some(if m.method == "unwrap" { *ok } else { *err })
                        }
                        _ => None,
                    }
                }
                _ => None,
            },
            Expr::Binary(_) => Some(Type::Bool),
            _ => None,
        }
    }
    fn eval(&mut self, expr: &Expr, hint: Option<&Type>) -> ResultValue<Value> {
        Self::spend(&mut self.budget.steps, 1)?;
        let value = match expr {
            Expr::Lit(l) => match &l.lit {
                syn::Lit::Int(n) => self.number(n, false, hint)?,
                syn::Lit::Bool(b) => Value::boolean(b.value),
                syn::Lit::Char(c) => Value::new(Type::Char, Data::Char(c.value()))?,
                syn::Lit::Str(s) => {
                    let text = Value::new(Type::Text, Data::Text(s.value()))?;
                    Value::new(Type::Ref(Box::new(Type::Text)), Data::Ref(Box::new(text)))?
                }
                _ => return oracle(),
            },
            Expr::Path(p) => {
                let name = p.path.segments[0].ident.to_string();
                if p.path.segments.len() == 2 {
                    let kind =
                        Integer::named(&name).ok_or(Failure::Fatal(EvaluationError::Oracle))?;
                    let (lo, hi) = kind.bounds();
                    Value::integer(
                        kind,
                        if p.path.segments[1].ident == "MIN" {
                            lo
                        } else {
                            hi
                        },
                    )?
                } else if name == "None" {
                    let Some(Type::Option(t)) = hint else {
                        return oracle();
                    };
                    Value::new(Type::Option(t.clone()), Data::None)?
                } else {
                    self.locals
                        .get(&name)
                        .ok_or(Failure::Fatal(EvaluationError::Oracle))?
                        .clone()
                }
            }
            Expr::Paren(p) => self.eval(&p.expr, hint)?,
            Expr::Reference(r) => {
                let inner_hint = if let Some(Type::Ref(t)) = hint {
                    Some(t.as_ref())
                } else {
                    None
                };
                let value = self.eval(&r.expr, inner_hint)?;
                Value::new(
                    Type::Ref(Box::new(value.kind().clone())),
                    Data::Ref(Box::new(value)),
                )?
            }
            Expr::Call(call) => {
                let Expr::Path(path) = call.func.as_ref() else {
                    return oracle();
                };
                if let Some(name) = self.function(path) {
                    let signature = self.program.schema[&name].clone();
                    if call.args.len() != signature.parameters.len() {
                        return oracle();
                    }
                    let mut arguments = Vec::new();
                    for (arg, kind) in call.args.iter().zip(&signature.parameters) {
                        let value = self.eval(arg, Some(kind))?;
                        arguments.push(coerce(value, kind)?);
                    }
                    Self::spend(&mut self.budget.calls, 1)?;
                    let output = self.api.call(&name, &arguments).map_err(|e| match e {
                        ApiError::Candidate => Failure::Test,
                        ApiError::Transport => Failure::Fatal(EvaluationError::Transport),
                    })?;
                    if output.kind() != &signature.result {
                        return Err(Failure::Test);
                    }
                    output
                } else {
                    let name = path.path.segments[0].ident.to_string();
                    let (kind, inner) = match (name.as_str(), hint) {
                        ("Some", Some(Type::Option(t))) => {
                            (Type::Option(t.clone()), Some(t.as_ref()))
                        }
                        ("Some", _) => {
                            let value = self.eval(&call.args[0], None)?;
                            let result = Value::new(
                                Type::Option(Box::new(value.kind().clone())),
                                Data::Some(Box::new(value)),
                            )?;
                            self.charge(&result)?;
                            return Ok(result);
                        }
                        ("Ok", Some(Type::Result(ok, err))) => {
                            (Type::Result(ok.clone(), err.clone()), Some(ok.as_ref()))
                        }
                        ("Err", Some(Type::Result(ok, err))) => {
                            (Type::Result(ok.clone(), err.clone()), Some(err.as_ref()))
                        }
                        _ => return oracle(),
                    };
                    let value = self.eval(&call.args[0], inner)?;
                    let data = match name.as_str() {
                        "Some" => Data::Some(Box::new(value)),
                        "Ok" => Data::Ok(Box::new(value)),
                        _ => Data::Err(Box::new(value)),
                    };
                    Value::new(kind, data)?
                }
            }
            Expr::Array(a) => self.sequence(a.elems.iter(), false, hint)?,
            Expr::Macro(m) if m.mac.path.is_ident("vec") => {
                self.sequence(args(&m.mac)?.iter(), true, hint)?
            }
            Expr::Tuple(t) => {
                let mut values = Vec::new();
                for (index, e) in t.elems.iter().enumerate() {
                    let h = if let Some(Type::Tuple(ts)) = hint {
                        ts.get(index)
                    } else {
                        None
                    };
                    values.push(self.eval(e, h)?);
                }
                Value::new(
                    Type::Tuple(values.iter().map(|v| v.kind().clone()).collect()),
                    Data::Tuple(values),
                )?
            }
            Expr::Unary(u) => {
                if matches!(u.op, syn::UnOp::Neg(_)) {
                    if let Expr::Lit(l) = u.expr.as_ref() {
                        if let syn::Lit::Int(n) = &l.lit {
                            let v = self.number(n, true, hint)?;
                            self.charge(&v)?;
                            return Ok(v);
                        }
                    }
                }
                let value = self.eval(&u.expr, hint)?;
                match (&u.op, value.data(), value.kind()) {
                    (syn::UnOp::Not(_), Data::Bool(b), _) => Value::boolean(!b),
                    (syn::UnOp::Neg(_), Data::Int(n), Type::Int(k)) => {
                        if k.bounds().0 == 0 {
                            return oracle();
                        }
                        Value::integer(*k, -n).map_err(|_| Failure::Test)?
                    }
                    (syn::UnOp::Not(_), Data::Int(n), Type::Int(k)) => {
                        let (lo, hi) = k.bounds();
                        Value::integer(*k, if lo == 0 { hi ^ n } else { !n })?
                    }
                    _ => return oracle(),
                }
            }
            Expr::Binary(b) => {
                let result = match b.op {
                    syn::BinOp::Eq(_) => self.comparison(&b.left, &b.right)?,
                    syn::BinOp::Ne(_) => !self.comparison(&b.left, &b.right)?,
                    syn::BinOp::And(_) | syn::BinOp::Or(_) => {
                        let left = self.eval(&b.left, Some(&Type::Bool))?;
                        let Data::Bool(left) = left.data() else {
                            return oracle();
                        };
                        if (matches!(b.op, syn::BinOp::And(_)) && !left)
                            || (matches!(b.op, syn::BinOp::Or(_)) && *left)
                        {
                            *left
                        } else {
                            let right = self.eval(&b.right, Some(&Type::Bool))?;
                            let Data::Bool(right) = right.data() else {
                                return oracle();
                            };
                            *right
                        }
                    }
                    _ => return oracle(),
                };
                Value::boolean(result)
            }
            Expr::Field(f) => {
                let base = self.eval(&f.base, None)?;
                let (Data::Tuple(fields), syn::Member::Unnamed(index)) =
                    (base.deref().data(), &f.member)
                else {
                    return oracle();
                };
                fields
                    .get(index.index as usize)
                    .ok_or(Failure::Test)?
                    .clone()
            }
            Expr::Index(i) => {
                let base = self.eval(&i.expr, None)?;
                let index = self.eval(&i.index, Some(&Type::Int(Integer::Usize)))?;
                let (Data::Sequence(items), Data::Int(index)) = (base.deref().data(), index.data())
                else {
                    return oracle();
                };
                items.get(*index as usize).ok_or(Failure::Test)?.clone()
            }
            Expr::MethodCall(m) if m.method == "collect" => self.project(m, hint)?,
            Expr::MethodCall(m) => {
                let value = self.eval(&m.receiver, None)?;
                match (m.method.to_string().as_str(), value.deref().data()) {
                    ("unwrap", Data::Some(v) | Data::Ok(v)) | ("unwrap_err", Data::Err(v)) => {
                        (**v).clone()
                    }
                    ("unwrap", Data::None | Data::Err(_)) | ("unwrap_err", Data::Ok(_)) => {
                        return Err(Failure::Test)
                    }
                    ("is_some", Data::Some(_))
                    | ("is_none", Data::None)
                    | ("is_ok", Data::Ok(_))
                    | ("is_err", Data::Err(_)) => Value::boolean(true),
                    ("is_some", Data::None)
                    | ("is_none", Data::Some(_))
                    | ("is_ok", Data::Err(_))
                    | ("is_err", Data::Ok(_)) => Value::boolean(false),
                    ("len", Data::Sequence(v)) => Value::integer(Integer::Usize, v.len() as i128)?,
                    ("len", Data::Text(v)) => Value::integer(Integer::Usize, v.len() as i128)?,
                    ("is_empty", Data::Sequence(v)) => Value::boolean(v.is_empty()),
                    ("is_empty", Data::Text(v)) => Value::boolean(v.is_empty()),
                    _ => return oracle(),
                }
            }
            _ => return oracle(),
        };
        self.charge(&value)?;
        Ok(value)
    }
    fn number(
        &self,
        literal: &syn::LitInt,
        negative: bool,
        hint: Option<&Type>,
    ) -> ResultValue<Value> {
        let kind = if literal.suffix().is_empty() {
            if let Some(Type::Int(k)) = hint {
                *k
            } else {
                Integer::I32
            }
        } else {
            Integer::named(literal.suffix()).ok_or(Failure::Fatal(EvaluationError::Oracle))?
        };
        let magnitude = literal
            .base10_parse::<u64>()
            .map_err(|_| Failure::Fatal(EvaluationError::Oracle))? as i128;
        if negative && kind.bounds().0 == 0 {
            return oracle();
        }
        Ok(Value::integer(
            kind,
            if negative { -magnitude } else { magnitude },
        )?)
    }
    fn sequence<'b>(
        &mut self,
        expressions: impl Iterator<Item = &'b Expr>,
        vector: bool,
        hint: Option<&Type>,
    ) -> ResultValue<Value> {
        let expressions: Vec<_> = expressions.collect();
        let inferred = expressions.iter().find_map(|expr| self.infer(expr));
        let mut values = Vec::new();
        let mut kind = None;
        for expr in expressions {
            let h = kind.as_ref().or(element(hint)).or(inferred.as_ref());
            let value = self.eval(expr, h)?;
            if kind.is_none() {
                kind = Some(value.kind().clone());
            }
            values.push(value);
        }
        let kind = kind
            .or_else(|| element(hint).cloned())
            .ok_or(Failure::Fatal(EvaluationError::Oracle))?;
        let ty = if vector {
            Type::Vec(Box::new(kind))
        } else {
            Type::Array(Box::new(kind), values.len())
        };
        Ok(Value::new(ty, Data::Sequence(values))?)
    }
    fn project(
        &mut self,
        collect: &syn::ExprMethodCall,
        hint: Option<&Type>,
    ) -> ResultValue<Value> {
        let Expr::MethodCall(map) = collect.receiver.as_ref() else {
            return oracle();
        };
        let Expr::MethodCall(iter) = map.receiver.as_ref() else {
            return oracle();
        };
        let Some(Expr::Closure(closure)) = map.args.first() else {
            return oracle();
        };
        let Some(syn::Pat::Ident(binding)) = closure.inputs.first() else {
            return oracle();
        };
        let base = self.eval(&iter.receiver, None)?;
        let Data::Sequence(items) = base.deref().data() else {
            return oracle();
        };
        let name = binding.ident.to_string();
        let input_type =
            element(Some(base.deref().kind())).ok_or(Failure::Fatal(EvaluationError::Oracle))?;
        self.type_bindings
            .insert(name.clone(), Type::Ref(Box::new(input_type.clone())));
        let output_type = self.infer(&closure.body);
        self.type_bindings.remove(&name);
        let mut values = Vec::new();
        let mut kind = None;
        for item in items {
            let value = Value::new(
                Type::Ref(Box::new(item.kind().clone())),
                Data::Ref(Box::new(item.clone())),
            )?;
            self.charge(&value)?;
            self.locals.insert(name.clone(), value);
            let result = self.eval(
                &closure.body,
                kind.as_ref().or(element(hint)).or(output_type.as_ref()),
            );
            self.locals.remove(&name);
            let result = result?;
            if kind.is_none() {
                kind = Some(result.kind().clone());
            }
            values.push(result);
        }
        Ok(Value::new(
            Type::Vec(Box::new(
                kind.or_else(|| element(hint).cloned())
                    .or(output_type)
                    .ok_or(Failure::Fatal(EvaluationError::Oracle))?,
            )),
            Data::Sequence(values),
        )?)
    }
}

fn coerce(value: Value, expected: &Type) -> ResultValue<Value> {
    if value.kind() == expected {
        return Ok(value);
    }
    // The only implicit API conversion is an immutable array/vector borrow to
    // an explicitly approved slice borrow; integers and owned values never cast.
    if let (Type::Ref(target), Data::Ref(inner)) = (expected, value.data()) {
        if let (
            Type::Slice(element),
            Type::Array(actual, _) | Type::Vec(actual),
            Data::Sequence(items),
        ) = (target.as_ref(), inner.kind(), inner.data())
        {
            if element == actual {
                let slice = Value::new((**target).clone(), Data::Sequence(items.clone()))?;
                return Ok(Value::new(expected.clone(), Data::Ref(Box::new(slice)))?);
            }
        }
    }
    oracle()
}
