//! Non-executing, fail-closed admission for protected Rust oracle source.
//! This is not a grader, compiler, sandbox, or candidate-facing protocol.

use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use syn::{parse::Parser, punctuated::Punctuated, Expr, Item, Path, Stmt, Token, UseTree};

const MAX_SOURCE: usize = 32 * 1024;
const MAX_PUNCTUATION: usize = 256;
const MAX_OPEN_DELIMITERS: usize = 64;
const MAX_TESTS: usize = 32;
const MAX_DEPTH: usize = 32;
const RESERVED: [&str; 4] = ["Some", "None", "Ok", "Err"];

/// Supplied by the trusted controller, never inferred from candidate source or
/// imports in the suite. The digest binds exact private source bytes.
pub struct Policy<'a> {
    pub crate_name: &'a str,
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
    imports: BTreeSet<String>,
    // Retain only admitted syntax for a future trusted evaluator. No public AST
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

/// Parse in the protected controller process only. No imports are loaded, macros
/// expanded, files opened, cargo invoked, or candidate code executed.
pub fn admit(source: &str, policy: &Policy<'_>) -> Result<AdmittedSuite> {
    if !identifier(policy.crate_name)
        || ["std", "core", "alloc", "crate", "self", "super"].contains(&policy.crate_name)
        || policy.functions.is_empty()
        || policy.functions.len() > 64
        || policy
            .functions
            .iter()
            .any(|name| !identifier(name) || RESERVED.contains(name))
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
    let mut imports = BTreeSet::new();
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
            require(root.ident == policy.crate_name)?;
            let leaves: Vec<&UseTree> = match root.tree.as_ref() {
                UseTree::Group(group) => group.items.iter().collect(),
                leaf => vec![leaf],
            };
            require(!leaves.is_empty())?;
            for leaf in leaves {
                let UseTree::Name(leaf) = leaf else {
                    return Err(AdmissionError::Unsupported);
                };
                let name = leaf.ident.to_string();
                require(policy.functions.contains(&name.as_str()) && imports.insert(name))?;
            }
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
                        && sig.ident != policy.crate_name
                        && !RESERVED.contains(&sig.ident.to_string().as_str())
                        && names.insert(sig.ident.to_string()),
                )?;
                let mut context = Context {
                    policy,
                    imports: &imports,
                    locals: BTreeSet::new(),
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
    // Identifiers cannot contain NUL; sorted set order is unambiguous and does
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
    imports: &'a BTreeSet<String>,
    locals: BTreeSet<String>,
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
                require(name.len() == 1 && (name[0] == "None" || self.locals.contains(&name[0])))
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
                    [name] => self.imports.contains(name),
                    [root, name] => {
                        root == self.policy.crate_name
                            && self.policy.functions.contains(&name.as_str())
                    }
                    _ => false,
                };
                require(allowed && expr.args.len() <= 16)?;
                for arg in &expr.args {
                    next(arg)?;
                }
                Ok(())
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
}
