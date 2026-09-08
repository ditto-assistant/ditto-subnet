//! Protected Rust admission, parent-owned data evaluation, and bounded channels.
//! This is not a compiler, sandbox, or standalone runtime grader.

pub mod evaluator;
pub mod value;
pub mod wire;
#[cfg(unix)]
pub mod wire_unix;

use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use syn::{parse::Parser, punctuated::Punctuated, Expr, Item, Path, Stmt, Token, UseTree};

const MAX_SOURCE: usize = 32 * 1024;
const MAX_PUNCTUATION: usize = 256;
const MAX_OPEN_DELIMITERS: usize = 64;
const MAX_TESTS: usize = 32;
const MAX_DEPTH: usize = 32;
const MAX_API_SEGMENTS: usize = 4;
const RESERVED: [&str; 4] = ["Some", "None", "Ok", "Err"];
const INTEGER_TYPES: [&str; 10] = [
    "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64", "isize",
];

/// Supplied by the trusted controller, never inferred from candidate source or
/// imports in the suite. The digest binds exact private source bytes.
pub struct Policy<'a> {
    pub crate_name: &'a str,
    /// Exact crate-relative paths, e.g. `encoding::decode`, not module prefixes.
    pub functions: &'a [&'a str],
    pub expected_tests: usize,
    pub source_sha256: [u8; 32],
}

/// Deliberately no Debug/Display/Serialize: names and expected values are private.
///
/// ```compile_fail
/// fn log_suite(suite: coding_rust_suite::AdmittedSuite) {
///     println!("{suite:?}");
/// }
/// ```
pub struct AdmittedSuite {
    source_sha256: [u8; 32],
    crate_name: String,
    functions: Vec<String>,
    imports: BTreeMap<String, String>,
    // Retain only admitted syntax for the trusted evaluator. No public AST
    // accessor: admission must not become a source export to the candidate.
    tests: Vec<syn::ItemFn>,
}

impl AdmittedSuite {
    pub fn test_count(&self) -> usize {
        self.tests.len()
    }

    pub fn source_sha256(&self) -> [u8; 32] {
        self.source_sha256
    }

    /// Binds the approved namespace/function set and expected count, so a later
    /// evaluator cannot reuse admission under a different call authority.
    pub fn policy_sha256(&self) -> [u8; 32] {
        policy_digest(&Policy {
            crate_name: &self.crate_name,
            functions: &self
                .functions
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            expected_tests: self.tests.len(),
            source_sha256: self.source_sha256,
        })
    }

    pub fn import_count(&self) -> usize {
        self.imports.len()
    }
}

/// Errors are intentionally source-free, including parser failures.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AdmissionError {
    Policy,
    Digest,
    Limit,
    Syntax,
    Unsupported,
    TestCount,
}

impl std::fmt::Display for AdmissionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Rust suite admission rejected")
    }
}

impl std::error::Error for AdmissionError {}

type Result<T> = std::result::Result<T, AdmissionError>;

fn require(ok: bool) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(AdmissionError::Unsupported)
    }
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value != "_"
        && !value.starts_with("r#")
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
        && syn::parse_str::<syn::Ident>(value).is_ok()
}

fn plain_path(path: &Path) -> Result<Vec<String>> {
    require(path.leading_colon.is_none())?;
    path.segments
        .iter()
        .map(|segment| {
            let name = segment.ident.to_string();
            require(segment.arguments.is_empty() && identifier(&name))?;
            Ok(name)
        })
        .collect()
}

fn api_path(value: &str) -> bool {
    value.len() <= MAX_API_SEGMENTS * 66
        && value.split("::").count() <= MAX_API_SEGMENTS
        && value
            .split("::")
            .all(|part| identifier(part) && !RESERVED.contains(&part))
}

fn named(ident: &syn::Ident, expected: &str) -> bool {
    identifier(&ident.to_string()) && ident == expected
}

fn import_tree(
    tree: &UseTree,
    prefix: &[String],
    policy: &Policy<'_>,
    imports: &mut BTreeMap<String, String>,
    depth: usize,
) -> Result<()> {
    if depth >= MAX_DEPTH || prefix.len() >= MAX_API_SEGMENTS {
        return Err(AdmissionError::Limit);
    }
    match tree {
        UseTree::Path(path) => {
            let name = path.ident.to_string();
            require(identifier(&name))?;
            let mut prefix = prefix.to_vec();
            prefix.push(name);
            import_tree(&path.tree, &prefix, policy, imports, depth + 1)
        }
        UseTree::Group(group) => {
            require(!group.items.is_empty())?;
            for child in &group.items {
                import_tree(child, prefix, policy, imports, depth + 1)?;
            }
            Ok(())
        }
        UseTree::Name(leaf) => {
            let name = leaf.ident.to_string();
            let mut path = prefix.to_vec();
            path.push(name.clone());
            let path = path.join("::");
            require(policy.functions.contains(&path.as_str()) && !imports.contains_key(&name))?;
            imports.insert(name, path);
            Ok(())
        }
        _ => Err(AdmissionError::Unsupported),
    }
}

/// Parse in the protected controller process only. No imports are loaded, macros
/// expanded, files opened, cargo invoked, or candidate code executed.
pub fn admit(source: &str, policy: &Policy<'_>) -> Result<AdmittedSuite> {
    if !identifier(policy.crate_name)
        || ["std", "core", "alloc", "crate", "self", "super"].contains(&policy.crate_name)
        || INTEGER_TYPES.contains(&policy.crate_name)
        || RESERVED.contains(&policy.crate_name)
        || policy.crate_name == "Vec"
        || policy.functions.is_empty()
        || policy.functions.len() > 64
        || policy.functions.iter().any(|name| !api_path(name))
        || policy.functions.iter().collect::<BTreeSet<_>>().len() != policy.functions.len()
        || !(1..=MAX_TESTS).contains(&policy.expected_tests)
    {
        return Err(AdmissionError::Policy);
    }
    // Check before invoking the lexer: token-tree limits checked afterwards are
    // too late to protect its recursive delimiter handling. These deliberately
    // conservative budgets also count punctuation inside strings and comments.
    if source.len() > MAX_SOURCE
        || source.bytes().filter(u8::is_ascii_punctuation).count() > MAX_PUNCTUATION
        || source.bytes().filter(|b| b"([{".contains(b)).count() > MAX_OPEN_DELIMITERS
    {
        return Err(AdmissionError::Limit);
    }
    let digest: [u8; 32] = Sha256::digest(source.as_bytes()).into();
    if digest != policy.source_sha256 {
        return Err(AdmissionError::Digest);
    }
    let file = syn::parse_file(source).map_err(|_| AdmissionError::Syntax)?;
    require(file.attrs.is_empty() && file.shebang.is_none())?;
    let mut imports = BTreeMap::new();
    let mut tests = Vec::new();
    let mut names = BTreeSet::new();
    // Resolve imports first: source order must not affect name binding.
    for item in &file.items {
        if let Item::Use(item) = item {
            require(
                item.attrs.is_empty()
                    && matches!(item.vis, syn::Visibility::Inherited)
                    && item.leading_colon.is_none(),
            )?;
            let UseTree::Path(root) = &item.tree else {
                return Err(AdmissionError::Unsupported);
            };
            require(named(&root.ident, policy.crate_name))?;
            import_tree(&root.tree, &[], policy, &mut imports, 0)?;
        }
    }
    for item in file.items {
        match item {
            Item::Use(_) => (),
            Item::Fn(test) => {
                let sig = &test.sig;
                require(
                    test.attrs.len() == 1
                        && matches!(test.attrs[0].style, syn::AttrStyle::Outer)
                        && matches!(&test.attrs[0].meta,
                    syn::Meta::Path(path) if path.is_ident("test"))
                        && matches!(test.vis, syn::Visibility::Inherited)
                        && sig.constness.is_none()
                        && sig.asyncness.is_none()
                        && sig.unsafety.is_none()
                        && sig.abi.is_none()
                        && sig.generics.params.is_empty()
                        && sig.generics.where_clause.is_none()
                        && sig.inputs.is_empty()
                        && sig.variadic.is_none()
                        && matches!(sig.output, syn::ReturnType::Default)
                        && identifier(&sig.ident.to_string())
                        && !policy.functions.contains(&sig.ident.to_string().as_str())
                        && !imports.contains_key(&sig.ident.to_string())
                        && sig.ident != policy.crate_name
                        && !RESERVED.contains(&sig.ident.to_string().as_str())
                        && names.insert(sig.ident.to_string()),
                )?;
                let mut context = Context {
                    policy,
                    imports: &imports,
                    locals: BTreeSet::new(),
                    in_projection: false,
                };
                let mut assertions = 0;
                for statement in &test.block.stmts {
                    match statement {
                        Stmt::Local(local) => {
                            require(local.attrs.is_empty())?;
                            let syn::Pat::Ident(pat) = &local.pat else {
                                return Err(AdmissionError::Unsupported);
                            };
                            let name = pat.ident.to_string();
                            require(
                                pat.attrs.is_empty()
                                    && pat.by_ref.is_none()
                                    && pat.mutability.is_none()
                                    && pat.subpat.is_none()
                                    && identifier(&name)
                                    && !RESERVED.contains(&name.as_str())
                                    && name != policy.crate_name
                                    && !policy.functions.contains(&name.as_str())
                                    && !imports.contains_key(&name)
                                    && !context.locals.contains(&name),
                            )?;
                            let init = local.init.as_ref().ok_or(AdmissionError::Unsupported)?;
                            require(init.diverge.is_none())?;
                            context.expression(&init.expr, 0)?;
                            context.locals.insert(name);
                        }
                        Stmt::Macro(statement) => {
                            require(statement.attrs.is_empty())?;
                            context.assertion(&statement.mac)?;
                            assertions += 1;
                        }
                        Stmt::Expr(Expr::Macro(expression), _) => {
                            require(expression.attrs.is_empty())?;
                            context.assertion(&expression.mac)?;
                            assertions += 1;
                        }
                        _ => return Err(AdmissionError::Unsupported),
                    }
                }
                require(assertions > 0)?;
                tests.push(test);
                if tests.len() > MAX_TESTS {
                    return Err(AdmissionError::Limit);
                }
            }
            _ => return Err(AdmissionError::Unsupported),
        }
    }
    if tests.len() != policy.expected_tests {
        return Err(AdmissionError::TestCount);
    }
    Ok(AdmittedSuite {
        source_sha256: digest,
        crate_name: policy.crate_name.to_owned(),
        functions: policy
            .functions
            .iter()
            .map(|name| (*name).to_owned())
            .collect(),
        imports,
        tests,
    })
}

fn policy_digest(policy: &Policy<'_>) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(b"coding-rust-suite-admission-v1\0");
    hash.update(policy.source_sha256);
    hash.update((policy.expected_tests as u64).to_be_bytes());
    // Approved paths cannot contain NUL; sorted set order is unambiguous and does
    // not make the caller's list ordering part of the authority.
    hash.update(policy.crate_name.as_bytes());
    hash.update([0]);
    for name in policy.functions.iter().collect::<BTreeSet<_>>() {
        hash.update(name.as_bytes());
        hash.update([0]);
    }
    hash.finalize().into()
}

struct Context<'a, 'b> {
    policy: &'a Policy<'b>,
    imports: &'a BTreeMap<String, String>,
    locals: BTreeSet<String>,
    in_projection: bool,
}

impl Context<'_, '_> {
    fn macro_args(&self, mac: &syn::Macro) -> Result<Punctuated<Expr, Token![,]>> {
        // syn does not traverse macro tokens as expressions automatically.
        Punctuated::<Expr, Token![,]>::parse_terminated
            .parse2(mac.tokens.clone())
            .map_err(|_| AdmissionError::Syntax)
    }

    fn assertion(&self, mac: &syn::Macro) -> Result<()> {
        let name = plain_path(&mac.path)?;
        let args = self.macro_args(mac)?;
        require(
            matches!(mac.delimiter, syn::MacroDelimiter::Paren(_))
                && ((name == ["assert_eq"] && args.len() == 2)
                    || (name == ["assert"] && args.len() == 1)),
        )?;
        for arg in args {
            self.expression(&arg, 0)?;
        }
        Ok(())
    }

    fn expression(&self, expr: &Expr, depth: usize) -> Result<()> {
        if depth >= MAX_DEPTH {
            return Err(AdmissionError::Limit);
        }
        let next = |expr: &Expr| self.expression(expr, depth + 1);
        match expr {
            Expr::Lit(expr) => {
                require(expr.attrs.is_empty())?;
                require(match &expr.lit {
                    syn::Lit::Bool(_) | syn::Lit::Str(_) | syn::Lit::Char(_) => true,
                    syn::Lit::Int(value) => {
                        [
                            "", "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64",
                            "isize",
                        ]
                        .contains(&value.suffix())
                            && value.base10_parse::<u64>().is_ok()
                    }
                    _ => false,
                })
            }
            Expr::Path(expr) => {
                require(expr.attrs.is_empty() && expr.qself.is_none())?;
                let name = plain_path(&expr.path)?;
                require(match name.as_slice() {
                    [value] => value == "None" || self.locals.contains(value),
                    [primitive, constant] => {
                        INTEGER_TYPES.contains(&primitive.as_str())
                            && ["MIN", "MAX"].contains(&constant.as_str())
                    }
                    _ => false,
                })
            }
            Expr::Call(expr) => {
                require(expr.attrs.is_empty())?;
                let Expr::Path(callee) = expr.func.as_ref() else {
                    return Err(AdmissionError::Unsupported);
                };
                require(callee.attrs.is_empty() && callee.qself.is_none())?;
                let path = plain_path(&callee.path)?;
                let allowed = match path.as_slice() {
                    [name] if ["Some", "Ok", "Err"].contains(&name.as_str()) => {
                        expr.args.len() == 1
                    }
                    [name] => !self.in_projection && self.imports.contains_key(name),
                    [root, tail @ ..] if !tail.is_empty() => {
                        !self.in_projection
                            && root == self.policy.crate_name
                            && self.policy.functions.contains(&tail.join("::").as_str())
                    }
                    _ => false,
                };
                require(allowed && expr.args.len() <= 16)?;
                for arg in &expr.args {
                    next(arg)?;
                }
                Ok(())
            }
            Expr::MethodCall(expr) => {
                require(expr.attrs.is_empty())?;
                match expr.method.to_string().as_str() {
                    "unwrap" | "unwrap_err" | "is_ok" | "is_err" | "is_some" | "is_none"
                    | "len" | "is_empty" => {
                        require(expr.turbofish.is_none() && expr.args.is_empty())?;
                        next(&expr.receiver)
                    }
                    "collect" => self.projection(expr, depth),
                    _ => Err(AdmissionError::Unsupported),
                }
            }
            Expr::Field(expr) => {
                require(
                    expr.attrs.is_empty()
                        && matches!(&expr.member,
                    syn::Member::Unnamed(index) if index.index < 16),
                )?;
                next(&expr.base)
            }
            Expr::Index(expr) => {
                require(expr.attrs.is_empty())?;
                let Expr::Lit(index) = expr.index.as_ref() else {
                    return Err(AdmissionError::Unsupported);
                };
                require(
                    index.attrs.is_empty()
                        && matches!(&index.lit, syn::Lit::Int(value)
                    if value.suffix().is_empty() && value.base10_parse::<u16>().is_ok_and(|v| v < 4096)),
                )?;
                next(&expr.expr)
            }
            Expr::Array(expr) => {
                require(expr.attrs.is_empty())?;
                for element in &expr.elems {
                    next(element)?;
                }
                Ok(())
            }
            Expr::Tuple(expr) => {
                require(expr.attrs.is_empty())?;
                for element in &expr.elems {
                    next(element)?;
                }
                Ok(())
            }
            Expr::Reference(expr) => {
                require(expr.attrs.is_empty() && expr.mutability.is_none())?;
                next(&expr.expr)
            }
            Expr::Paren(expr) => {
                require(expr.attrs.is_empty())?;
                next(&expr.expr)
            }
            Expr::Unary(expr) => {
                require(
                    expr.attrs.is_empty()
                        && matches!(expr.op, syn::UnOp::Not(_) | syn::UnOp::Neg(_)),
                )?;
                next(&expr.expr)
            }
            Expr::Binary(expr) => {
                require(
                    expr.attrs.is_empty()
                        && matches!(
                            expr.op,
                            syn::BinOp::Eq(_)
                                | syn::BinOp::Ne(_)
                                | syn::BinOp::And(_)
                                | syn::BinOp::Or(_)
                        ),
                )?;
                next(&expr.left)?;
                next(&expr.right)
            }
            Expr::Macro(expr) => {
                require(
                    expr.attrs.is_empty()
                        && plain_path(&expr.mac.path)? == ["vec"]
                        && matches!(expr.mac.delimiter, syn::MacroDelimiter::Bracket(_)),
                )?;
                for arg in self.macro_args(&expr.mac)? {
                    next(&arg)?;
                }
                Ok(())
            }
            _ => Err(AdmissionError::Unsupported),
        }
    }

    fn projection(&self, collect: &syn::ExprMethodCall, depth: usize) -> Result<()> {
        // One syntactically closed parent-owned projection, not an executable
        // Rust closure or a candidate callback. No arbitrary iterator protocol.
        require(!self.in_projection && collect.args.is_empty())?;
        let generic = collect
            .turbofish
            .as_ref()
            .ok_or(AdmissionError::Unsupported)?;
        require(generic.args.len() == 1)?;
        let Some(syn::GenericArgument::Type(syn::Type::Path(vector))) = generic.args.first() else {
            return Err(AdmissionError::Unsupported);
        };
        require(
            vector.qself.is_none()
                && vector.path.leading_colon.is_none()
                && vector.path.segments.len() == 1,
        )?;
        let segment = &vector.path.segments[0];
        require(named(&segment.ident, "Vec"))?;
        let syn::PathArguments::AngleBracketed(element) = &segment.arguments else {
            return Err(AdmissionError::Unsupported);
        };
        require(
            element.colon2_token.is_none()
                && element.args.len() == 1
                && matches!(
                    element.args.first(),
                    Some(syn::GenericArgument::Type(syn::Type::Infer(_)))
                ),
        )?;
        let Expr::MethodCall(map) = collect.receiver.as_ref() else {
            return Err(AdmissionError::Unsupported);
        };
        require(
            map.attrs.is_empty()
                && named(&map.method, "map")
                && map.turbofish.is_none()
                && map.args.len() == 1,
        )?;
        let Expr::MethodCall(iter) = map.receiver.as_ref() else {
            return Err(AdmissionError::Unsupported);
        };
        require(
            iter.attrs.is_empty()
                && named(&iter.method, "iter")
                && iter.turbofish.is_none()
                && iter.args.is_empty(),
        )?;
        let Some(Expr::Closure(closure)) = map.args.first() else {
            return Err(AdmissionError::Unsupported);
        };
        require(
            closure.attrs.is_empty()
                && closure.lifetimes.is_none()
                && closure.constness.is_none()
                && closure.movability.is_none()
                && closure.asyncness.is_none()
                && closure.capture.is_none()
                && matches!(closure.output, syn::ReturnType::Default)
                && closure.inputs.len() == 1,
        )?;
        let Some(syn::Pat::Ident(binding)) = closure.inputs.first() else {
            return Err(AdmissionError::Unsupported);
        };
        let name = binding.ident.to_string();
        require(
            binding.attrs.is_empty()
                && binding.by_ref.is_none()
                && binding.mutability.is_none()
                && binding.subpat.is_none()
                && identifier(&name)
                && !RESERVED.contains(&name.as_str())
                && name != self.policy.crate_name
                && !self.policy.functions.contains(&name.as_str())
                && !self.imports.contains_key(&name)
                && !self.locals.contains(&name),
        )?;
        // Charge the skipped collect/map/iter/closure levels as well.
        self.expression(&iter.receiver, depth + 3)?;
        let mut locals = self.locals.clone();
        locals.insert(name);
        Context {
            policy: self.policy,
            imports: self.imports,
            locals,
            in_projection: true,
        }
        .expression(&closure.body, depth + 3)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opaque_suite_retains_exact_nested_import_targets() {
        let source = "use repair::{encoding::decode, verify::inspect}; #[test] fn example() { assert!(inspect(decode(1))); }";
        let suite = admit(
            source,
            &Policy {
                crate_name: "repair",
                functions: &["encoding::decode", "verify::inspect"],
                expected_tests: 1,
                source_sha256: Sha256::digest(source.as_bytes()).into(),
            },
        )
        .ok()
        .unwrap();
        assert_eq!(suite.imports.get("decode").unwrap(), "encoding::decode");
        assert_eq!(suite.imports.get("inspect").unwrap(), "verify::inspect");
        assert_eq!(suite.import_count(), 2);
    }
}
