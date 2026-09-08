//! Operator-side snapshot API inventory. Never a production approval and never
//! installed in the runtime image. Parses pristine source; executes none of it.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};
use syn::{FnArg, GenericArgument, Item, PathArguments, ReturnType, Type};
type Result<T> = std::result::Result<T, ()>;

fn read(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| ())?;
    let info = file.metadata().map_err(|_| ())?;
    if !info.is_file() || info.len() > maximum as u64 {
        return Err(());
    }
    let mut bytes = Vec::new();
    file.take(maximum as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ())?;
    if bytes.len() != info.len() as usize || bytes.len() > maximum {
        return Err(());
    };
    Ok(bytes)
}

fn shape(kind: &Type, depth: usize) -> Result<Value> {
    if depth > 16 {
        return Err(());
    }
    let nested = |v: &Type| shape(v, depth + 1);
    Ok(match kind {
        Type::Reference(v) if v.mutability.is_none() => {
            json!({"kind":"ref","item":nested(&v.elem)?})
        }
        Type::Slice(v) => json!({"kind":"slice","item":nested(&v.elem)?}),
        Type::Array(v) => {
            let syn::Expr::Lit(lit) = &v.len else {
                return Err(());
            };
            let syn::Lit::Int(n) = &lit.lit else {
                return Err(());
            };
            let length = n.base10_parse::<usize>().map_err(|_| ())?;
            if length > 4096 {
                return Err(());
            };
            json!({"kind":"array","item":nested(&v.elem)?,"length":length})
        }
        Type::Tuple(v) if v.elems.len() <= 16 => {
            json!({"kind":"tuple","items":v.elems.iter().map(nested).collect::<Result<Vec<_>>>()?})
        }
        Type::Paren(v) => nested(&v.elem)?,
        Type::Path(v) if v.qself.is_none() => {
            let parts: Vec<_> = v.path.segments.iter().collect();
            let last = parts.last().ok_or(())?;
            let name = last.ident.to_string();
            let prefix: Vec<_> = parts[..parts.len() - 1]
                .iter()
                .map(|s| s.ident.to_string())
                .collect();
            if !prefix.is_empty()
                && !matches!(
                    prefix
                        .iter()
                        .map(String::as_str)
                        .collect::<Vec<_>>()
                        .as_slice(),
                    ["std", "vec"]
                        | ["std", "string"]
                        | ["std", "option"]
                        | ["std", "result"]
                        | ["core", "option"]
                        | ["core", "result"]
                )
            {
                return Err(());
            }
            match &last.arguments {
                PathArguments::None => {
                    let tag = match name.as_str() {
                        "str" | "String" => "text",
                        "bool" => "bool",
                        "char" => "char",
                        "u8" => "u8",
                        "u16" => "u16",
                        "u32" => "u32",
                        "u64" => "u64",
                        "usize" => "usize",
                        "i8" => "i8",
                        "i16" => "i16",
                        "i32" => "i32",
                        "i64" => "i64",
                        "isize" => "isize",
                        _ => return Err(()),
                    };
                    json!({"kind":tag})
                }
                PathArguments::AngleBracketed(v) => {
                    let types = v
                        .args
                        .iter()
                        .map(|a| match a {
                            GenericArgument::Type(t) => nested(t),
                            _ => Err(()),
                        })
                        .collect::<Result<Vec<_>>>()?;
                    match (name.as_str(), types.as_slice()) {
                        ("Vec", [item]) => json!({"kind":"vec","item":item}),
                        ("Option", [item]) => json!({"kind":"option","item":item}),
                        ("Result", [ok, error]) => json!({"kind":"result","ok":ok,"error":error}),
                        _ => return Err(()),
                    }
                }
                _ => return Err(()),
            }
        }
        _ => return Err(()),
    })
}
fn functions(items: &[Item], prefix: &[String], out: &mut BTreeMap<String, Value>) -> Result<()> {
    for item in items {
        match item {
            Item::Fn(f) if matches!(f.vis, syn::Visibility::Public(_)) => {
                if f.sig.asyncness.is_some()
                    || f.sig.unsafety.is_some()
                    || f.sig.variadic.is_some()
                    || f.sig
                        .generics
                        .params
                        .iter()
                        .any(|p| !matches!(p, syn::GenericParam::Lifetime(_)))
                {
                    return Err(());
                }
                let mut path = prefix.to_vec();
                path.push(f.sig.ident.to_string());
                let name = path.join("::");
                let parameters = f
                    .sig
                    .inputs
                    .iter()
                    .map(|a| match a {
                        FnArg::Typed(v) => shape(&v.ty, 0),
                        _ => Err(()),
                    })
                    .collect::<Result<Vec<_>>>()?;
                let result = match &f.sig.output {
                    ReturnType::Default => json!({"kind":"tuple","items":[]}),
                    ReturnType::Type(_, t) => shape(t, 0)?,
                };
                if out
                    .insert(
                        name.clone(),
                        json!({"name":name,"parameters":parameters,"result":result}),
                    )
                    .is_some()
                {
                    return Err(());
                }
            }
            Item::Mod(m) if matches!(m.vis, syn::Visibility::Public(_)) => {
                if let Some((_, items)) = &m.content {
                    let mut path = prefix.to_vec();
                    path.push(m.ident.to_string());
                    functions(items, &path, out)?
                }
            }
            _ => {}
        }
    }
    Ok(())
}
fn paths(root: &Path, output: &mut Vec<PathBuf>, depth: usize) -> Result<()> {
    if depth > 8 {
        return Err(());
    }
    for entry in fs::read_dir(root).map_err(|_| ())? {
        let entry = entry.map_err(|_| ())?;
        let kind = entry.file_type().map_err(|_| ())?;
        if kind.is_symlink() {
            return Err(());
        }
        if kind.is_dir() {
            paths(&entry.path(), output, depth + 1)?
        } else if kind.is_file() && entry.path().extension().is_some_and(|x| x == "rs") {
            if output.len() >= 256 {
                return Err(());
            };
            output.push(entry.path());
        } else {
            return Err(());
        }
    }
    Ok(())
}
fn inventory(root: &Path) -> Result<Value> {
    if !root.is_absolute() || root.canonicalize().map_err(|_| ())? != root {
        return Err(());
    }
    let cargo = read(&root.join("Cargo.toml"), 65536)?;
    if cargo.len() > 65536 {
        return Err(());
    }
    let text = std::str::from_utf8(&cargo).map_err(|_| ())?;
    let mut section = "";
    let mut package = None;
    let mut library = None;
    for line in text.lines().map(str::trim) {
        if line.starts_with('[') {
            section = line;
        }
        if let Some((key, value)) = line.split_once('=') {
            if key.trim() == "name" {
                let name = value
                    .trim()
                    .strip_prefix('"')
                    .and_then(|v| v.strip_suffix('"'))
                    .ok_or(())?
                    .to_owned();
                match section {
                    "[package]" => {
                        if package.replace(name).is_some() {
                            return Err(());
                        }
                    }
                    "[lib]" if library.replace(name).is_some() => return Err(()),
                    _ => {}
                }
            }
        }
    }
    let crate_name = library.or(package.map(|v| v.replace('-', "_"))).ok_or(())?;
    let mut files = Vec::new();
    paths(&root.join("src"), &mut files, 0)?;
    files.sort();
    let mut schema = BTreeMap::new();
    let mut names = BTreeMap::new();
    let mut total = 0;
    for file in files {
        let source = read(&file, 1 << 20)?;
        if source
            .iter()
            .filter(|c| b"([{ ".contains(c) && **c != b' ')
            .count()
            > 512
        {
            return Err(());
        }
        total += source.len();
        if source.len() > 1 << 20 || total > 16 << 20 {
            return Err(());
        }
        let relative = file
            .strip_prefix(root)
            .map_err(|_| ())?
            .to_str()
            .ok_or(())?;
        let source_sha: [u8; 32] = Sha256::digest(&source).into();
        names.insert(relative.to_owned(), source_sha);
        let mut prefix: Vec<_> = file
            .strip_prefix(root.join("src"))
            .map_err(|_| ())?
            .with_extension("")
            .components()
            .map(|p| p.as_os_str().to_str().ok_or(()).map(str::to_owned))
            .collect::<Result<_>>()?;
        if prefix.last().is_some_and(|v| v == "lib" || v == "mod") {
            prefix.pop();
        }
        let syntax =
            syn::parse_file(std::str::from_utf8(&source).map_err(|_| ())?).map_err(|_| ())?;
        functions(&syntax.items, &prefix, &mut schema)?;
    }
    coding_rust_suite::workspace::Manifest::new(names.clone()).map_err(|_| ())?;
    if schema.is_empty() || schema.len() > 64 {
        return Err(());
    }
    let files:Vec<_>=names.iter().map(|(path,sha)|json!({"path":path,"sha256":sha.iter().map(|b|format!("{b:02x}")).collect::<String>()})).collect();
    Ok(
        json!({"schema":"dittobench-coding-rust-snapshot-api-inventory-v1","production_api_approval":false,"crate_name":crate_name,"files":files,"functions":schema.values().collect::<Vec<_>>(),"cargo_sha256":format!("{:x}",Sha256::digest(cargo))}),
    )
}
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() == 2 && args[0] == "--count-suite" {
        let result = (|| {
            let bytes = read(Path::new(&args[1]), 32 << 10)?;
            let text = std::str::from_utf8(&bytes).map_err(|_| ())?;
            let syntax = syn::parse_file(text).map_err(|_| ())?;
            let count = syntax
                .items
                .iter()
                .filter(
                    |i| matches!(i,Item::Fn(f) if f.attrs.iter().any(|a|a.path().is_ident("test"))),
                )
                .count();
            if !(1..=32).contains(&count) {
                return Err(());
            };
            Ok(count)
        })();
        match result {
            Ok(count) => println!("{count}"),
            Err(()) => std::process::exit(70),
        };
        return;
    }
    if args.len() != 2 || args[0] != "--snapshot" {
        std::process::exit(70)
    }
    match inventory(Path::new(&args[1])) {
        Ok(value) => println!("{}", value),
        Err(()) => std::process::exit(70),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn closed_native_type_shapes() {
        let t: syn::Type = syn::parse_str("Result<Vec<(&str, u64)>, String>").unwrap();
        assert_eq!(shape(&t, 0).unwrap()["kind"], "result");
        for t in [
            "&mut str",
            "Box<i64>",
            "UserObject",
            "[u8; 5000]",
            "fn() -> bool",
        ] {
            assert!(shape(&syn::parse_str(t).unwrap(), 0).is_err())
        }
    }
    #[test]
    fn only_public_free_functions_enter_inventory() {
        let file = syn::parse_file(
            "fn hidden() {} pub mod api { pub fn f(x: i64) -> bool { true } } pub struct Object;",
        )
        .unwrap();
        let mut map = BTreeMap::new();
        functions(&file.items, &[], &mut map).unwrap();
        assert_eq!(map.keys().cloned().collect::<Vec<_>>(), vec!["api::f"]);
    }
}
