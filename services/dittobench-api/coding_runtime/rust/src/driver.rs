//! Protected supervisor-driver input contract. No authority is discovered from
//! candidate imports, mutable Cargo configuration, or candidate-owned reports.
use crate::{
    bridge,
    evaluator::Signature,
    value::{Integer, Type},
    workspace::{self, Manifest, Snapshot},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::Path,
    time::Duration,
};

const REPORT: &str = "/run/dittobench-control/test-report.json";
const INPUTS: &str = "/run/dittobench-control/rust-inputs.json";
const AUTHORITY_SCHEMA: &str = "dittobench-coding-rust-authority-v1";
const INPUTS_SCHEMA: &str = "dittobench-coding-rust-inputs-v1";
pub const MAX_AUTHORITY: usize = 256 << 10;
pub const MAX_INPUTS: usize = 128 << 10;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DriverError;
type Result<T> = std::result::Result<T, DriverError>;

/// Private command authority. No Debug, display, or automatic serialization.
pub struct Options {
    group: String,
    authority: String,
    authority_sha256: [u8; 32],
    inputs_sha256: [u8; 32],
    nonce: String,
    expected: usize,
}
fn digest(value: &str) -> Result<[u8; 32]> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(DriverError);
    }
    let mut result = [0; 32];
    for (i, byte) in result.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[i * 2..i * 2 + 2], 16).map_err(|_| DriverError)?;
    }
    Ok(result)
}
fn relative(value: &str) -> bool {
    value.len() <= 240
        && (1..=8).contains(&value.split('/').count())
        && value.split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part.starts_with('.')
                && part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        })
}
impl Options {
    pub fn parse(arguments: &[String]) -> Result<Self> {
        if arguments.len() != 20 || arguments.iter().any(|a| a.len() > 256) {
            return Err(DriverError);
        }
        let mut fields = BTreeMap::new();
        for pair in arguments.chunks_exact(2) {
            if fields.insert(pair[0].as_str(), pair[1].as_str()).is_some() {
                return Err(DriverError);
            }
        }
        let allowed = [
            "--group",
            "--authority",
            "--authority-sha256",
            "--dittobench-rust-inputs",
            "--dittobench-rust-inputs-sha256",
            "--dittobench-report",
            "--dittobench-nonce",
            "--dittobench-expected",
            "--dittobench-candidate-uid",
            "--dittobench-candidate-gid",
        ];
        if fields.len() != allowed.len() || allowed.iter().any(|k| !fields.contains_key(k)) {
            return Err(DriverError);
        }
        if fields["--dittobench-report"] != REPORT
            || fields["--dittobench-rust-inputs"] != INPUTS
            || fields["--dittobench-candidate-uid"] != "10001"
            || fields["--dittobench-candidate-gid"] != "10001"
            || !["visible", "hidden"].contains(&fields["--group"])
            || !relative(fields["--authority"])
            || !fields["--authority"].ends_with(".json")
        {
            return Err(DriverError);
        }
        let nonce = fields["--dittobench-nonce"];
        if nonce.len() != 48
            || !nonce
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(DriverError);
        }
        let count = fields["--dittobench-expected"];
        if count.starts_with('0') || !count.bytes().all(|b| b.is_ascii_digit()) {
            return Err(DriverError);
        }
        let expected = count.parse::<usize>().map_err(|_| DriverError)?;
        if !(1..=32).contains(&expected) {
            return Err(DriverError);
        }
        Ok(Self {
            group: fields["--group"].into(),
            authority: fields["--authority"].into(),
            authority_sha256: digest(fields["--authority-sha256"])?,
            inputs_sha256: digest(fields["--dittobench-rust-inputs-sha256"])?,
            nonce: nonce.into(),
            expected,
        })
    }
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Kind {
    Bool,
    U8,
    U16,
    U32,
    U64,
    Usize,
    I8,
    I16,
    I32,
    I64,
    Isize,
    Text,
    Char,
    Vec { item: Box<Kind> },
    Slice { item: Box<Kind> },
    Array { item: Box<Kind>, length: usize },
    Tuple { items: Vec<Kind> },
    Option { item: Box<Kind> },
    Result { ok: Box<Kind>, error: Box<Kind> },
    Ref { item: Box<Kind> },
}
impl Kind {
    fn into_type(self, depth: usize, remaining: &mut usize) -> Result<Type> {
        if depth > 16 || *remaining == 0 {
            return Err(DriverError);
        }
        *remaining -= 1;
        let nested =
            |v: Box<Kind>, remaining: &mut usize| v.into_type(depth + 1, remaining).map(Box::new);
        Ok(match self {
            Self::Bool => Type::Bool,
            Self::Text => Type::Text,
            Self::Char => Type::Char,
            Self::U8 => Type::Int(Integer::U8),
            Self::U16 => Type::Int(Integer::U16),
            Self::U32 => Type::Int(Integer::U32),
            Self::U64 => Type::Int(Integer::U64),
            Self::Usize => Type::Int(Integer::Usize),
            Self::I8 => Type::Int(Integer::I8),
            Self::I16 => Type::Int(Integer::I16),
            Self::I32 => Type::Int(Integer::I32),
            Self::I64 => Type::Int(Integer::I64),
            Self::Isize => Type::Int(Integer::Isize),
            Self::Vec { item } => Type::Vec(nested(item, remaining)?),
            Self::Slice { item } => Type::Slice(nested(item, remaining)?),
            Self::Array { item, length } => {
                if length > 4096 {
                    return Err(DriverError);
                }
                Type::Array(nested(item, remaining)?, length)
            }
            Self::Tuple { items } => {
                if items.len() > 16 {
                    return Err(DriverError);
                }
                Type::Tuple(
                    items
                        .into_iter()
                        .map(|v| v.into_type(depth + 1, remaining))
                        .collect::<Result<_>>()?,
                )
            }
            Self::Option { item } => Type::Option(nested(item, remaining)?),
            Self::Result { ok, error } => {
                Type::Result(nested(ok, remaining)?, nested(error, remaining)?)
            }
            Self::Ref { item } => Type::Ref(nested(item, remaining)?),
        })
    }
}
#[derive(Deserialize)]
struct Function {
    name: String,
    parameters: Vec<Kind>,
    result: Kind,
}
#[derive(Deserialize)]
struct AuthorityWire {
    schema: String,
    group: String,
    crate_name: String,
    suite: String,
    suite_sha256: String,
    functions: Vec<Function>,
    files: Vec<String>,
    build_timeout_ms: u64,
    candidate_timeout_ms: u64,
}
#[derive(Deserialize)]
struct InputsWire {
    schema: String,
    authority_sha256: String,
    image_sha256: String,
    // Entries are a list so duplicate paths cannot silently overwrite a map key.
    files: Vec<InputFile>,
}
#[derive(Deserialize)]
struct InputFile {
    path: String,
    sha256: String,
}

pub struct Authority {
    crate_name: String,
    suite: String,
    suite_sha256: [u8; 32],
    schema: BTreeMap<String, Signature>,
    manifest: Manifest,
    image_sha256: [u8; 32],
    build_timeout_ms: u64,
    candidate_timeout_ms: u64,
}
impl Authority {
    /// Both expected hashes come from the protected supervisor command/control
    /// authority, not fields inside these documents. Unknown extensions carry
    /// no semantic authority; known fields and duplicate fields remain strict.
    pub fn bind(options: &Options, authority: &[u8], inputs: &[u8]) -> Result<Self> {
        if authority.is_empty()
            || authority.len() > MAX_AUTHORITY
            || inputs.is_empty()
            || inputs.len() > MAX_INPUTS
            || <[u8; 32]>::from(Sha256::digest(authority)) != options.authority_sha256
            || <[u8; 32]>::from(Sha256::digest(inputs)) != options.inputs_sha256
        {
            return Err(DriverError);
        }
        let wire: AuthorityWire = serde_json::from_slice(authority).map_err(|_| DriverError)?;
        let frozen: InputsWire = serde_json::from_slice(inputs).map_err(|_| DriverError)?;
        if wire.schema != AUTHORITY_SCHEMA
            || frozen.schema != INPUTS_SCHEMA
            || wire.group != options.group
            || digest(&frozen.authority_sha256)? != options.authority_sha256
            || !relative(&wire.suite)
            || !wire.suite.ends_with(".rs")
            || wire.files.is_empty()
            || wire.files.len() > 256
            || frozen.files.len() != wire.files.len()
            || wire.functions.is_empty()
            || wire.functions.len() > 64
            || wire.build_timeout_ms == 0
            || wire.build_timeout_ms > 120_000
            || wire.candidate_timeout_ms == 0
            || wire.candidate_timeout_ms > 30_000
        {
            return Err(DriverError);
        }
        let mut selected = BTreeMap::new();
        for file in frozen.files {
            if selected.insert(file.path, digest(&file.sha256)?).is_some() {
                return Err(DriverError);
            }
        }
        let mut files = wire.files;
        files.sort();
        if selected.keys().ne(files.iter()) {
            return Err(DriverError);
        }
        let manifest = Manifest::new(selected).map_err(|_| DriverError)?;
        let mut schema = BTreeMap::new();
        for function in wire.functions {
            if function.parameters.len() > 16 {
                return Err(DriverError);
            }
            let parameters = function
                .parameters
                .into_iter()
                .map(|v| v.into_type(0, &mut 256))
                .collect::<Result<_>>()?;
            let signature = Signature {
                parameters,
                result: function.result.into_type(0, &mut 256)?,
            };
            if schema.insert(function.name, signature).is_some() {
                return Err(DriverError);
            }
        }
        bridge::generate(&schema).map_err(|_| DriverError)?;
        // Admission validates the crate name independently before any candidate
        // compilation. Binding can validate it now without consuming a suite.
        let dummy = "#[test] fn bound() { assert!(true); }";
        let functions: Vec<_> = schema.keys().map(String::as_str).collect();
        crate::admit(
            dummy,
            &crate::Policy {
                crate_name: &wire.crate_name,
                functions: &functions,
                expected_tests: 1,
                source_sha256: Sha256::digest(dummy).into(),
            },
        )
        .map_err(|_| DriverError)?;
        Ok(Self {
            crate_name: wire.crate_name,
            suite: wire.suite,
            suite_sha256: digest(&wire.suite_sha256)?,
            schema,
            manifest,
            image_sha256: digest(&frozen.image_sha256)?,
            build_timeout_ms: wire.build_timeout_ms,
            candidate_timeout_ms: wire.candidate_timeout_ms,
        })
    }
}

fn protected_directory(root: &Path, private: bool) -> Result<File> {
    let file = workspace::absolute_directory(root, 0).map_err(|_| DriverError)?;
    let metadata = file.metadata().map_err(|_| DriverError)?;
    if !metadata.is_dir()
        || metadata.uid() != 0
        || metadata.mode() & 0o7022 != 0
        || (private && metadata.mode() & 0o077 != 0)
    {
        return Err(DriverError);
    }
    Ok(file)
}
fn read_protected(root: &Path, name: &str, maximum: usize, private: bool) -> Result<Vec<u8>> {
    if !relative(name) {
        return Err(DriverError);
    }
    let mut directory = protected_directory(root, private)?;
    let mut parts = name.split('/').peekable();
    while let Some(part) = parts.next() {
        if parts.peek().is_some() {
            directory = workspace::open_at(
                &directory,
                part.as_bytes(),
                libc::O_RDONLY | libc::O_DIRECTORY,
                0,
            )
            .map_err(|_| DriverError)?;
            let info = directory.metadata().map_err(|_| DriverError)?;
            if !info.is_dir() || info.uid() != 0 || info.mode() & 0o7022 != 0 {
                return Err(DriverError);
            }
        } else {
            let file = workspace::open_at(
                &directory,
                part.as_bytes(),
                libc::O_RDONLY | libc::O_NONBLOCK,
                0,
            )
            .map_err(|_| DriverError)?;
            let before = file.metadata().map_err(|_| DriverError)?;
            if !before.is_file()
                || before.uid() != 0
                || before.nlink() != 1
                || before.mode() & 0o7022 != 0
                || before.size() > maximum as u64
            {
                return Err(DriverError);
            }
            let mut bytes = Vec::new();
            (&file)
                .take(maximum as u64 + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| DriverError)?;
            if bytes.len() != before.size() as usize
                || bytes.len() > maximum
                || !workspace::same(&before, &file.metadata().map_err(|_| DriverError)?)
            {
                return Err(DriverError);
            }
            return Ok(bytes);
        }
    }
    Err(DriverError)
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[derive(Deserialize)]
struct ToolchainWire {
    schema: String,
    compiler_sha256: String,
    bridge_library_sha256: String,
}
#[derive(Serialize)]
struct RuntimeEvidence {
    schema: &'static str,
    authority_sha256: String,
    inputs_sha256: String,
    image_sha256: String,
    program_sha256: String,
    compiler_sha256: String,
    bridge_library_sha256: String,
    outcome: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    build_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    artifact_sha256: Option<String>,
}
#[derive(Serialize)]
struct TestReport<'a> {
    schema: &'static str,
    nonce: &'a str,
    passed: usize,
    total: usize,
    completed: bool,
    runtime: RuntimeEvidence,
}
fn write_report(options: &Options, passed: usize, runtime: RuntimeEvidence) -> Result<u8> {
    if passed > options.expected {
        return Err(DriverError);
    }
    let report = TestReport {
        schema: "dittobench-coding-trusted-test-report-v2",
        nonce: &options.nonce,
        passed,
        total: options.expected,
        completed: true,
        runtime,
    };
    let bytes = serde_json::to_vec(&report).map_err(|_| DriverError)?;
    if bytes.len() > 4096 {
        return Err(DriverError);
    }
    let directory = protected_directory(Path::new("/run/dittobench-control"), true)?;
    let mut output = workspace::open_at(
        &directory,
        b"test-report.json",
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
        0o600,
    )
    .map_err(|_| DriverError)?;
    output
        .set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|_| DriverError)?;
    output.write_all(&bytes).map_err(|_| DriverError)?;
    output.sync_all().map_err(|_| DriverError)?;
    directory.sync_all().map_err(|_| DriverError)?;
    Ok(u8::from(passed != options.expected))
}

/// Root-only protected driver entry. Return 0/1 only after an authoritative
/// report; every error is exit 70 with no public diagnostics. The enclosing
/// supervisor must reject reports from any other completed driver exit code.
pub fn run(arguments: &[String]) -> Result<u8> {
    use crate::{
        compiler::{compile, CompileError, ToolchainIdentity},
        evaluator::{Limits, Program},
        process::{Deadlines, ProcessFactory},
        Policy,
    };
    if unsafe { libc::getuid() } != 0 || unsafe { libc::geteuid() } != 0 {
        return Err(DriverError);
    }
    let options = Options::parse(arguments)?;
    protected_directory(Path::new("/run/dittobench-control"), true)?;
    match fs::symlink_metadata(REPORT) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        _ => return Err(DriverError),
    }
    let body = read_protected(
        Path::new("/run/dittobench-grader"),
        &options.authority,
        MAX_AUTHORITY,
        true,
    )?;
    let frozen = read_protected(
        Path::new("/run/dittobench-control"),
        "rust-inputs.json",
        MAX_INPUTS,
        true,
    )?;
    let authority = Authority::bind(&options, &body, &frozen)?;
    let suite_root = if options.group == "hidden" {
        "/run/dittobench-grader"
    } else {
        "/workspace"
    };
    let source = read_protected(Path::new(suite_root), &authority.suite, 32 << 10, true)?;
    let source = std::str::from_utf8(&source).map_err(|_| DriverError)?;
    let functions: Vec<_> = authority.schema.keys().map(String::as_str).collect();
    let admitted = crate::admit(
        source,
        &Policy {
            crate_name: &authority.crate_name,
            functions: &functions,
            expected_tests: options.expected,
            source_sha256: authority.suite_sha256,
        },
    )
    .map_err(|_| DriverError)?;
    let program = Program::bind(admitted, authority.schema.clone()).map_err(|_| DriverError)?;
    let snapshot = Snapshot::capture(Path::new("/workspace"), 0, &authority.manifest)
        .map_err(|_| DriverError)?;
    let generated = bridge::generate(&authority.schema).map_err(|_| DriverError)?;
    let stage = Path::new("/tmp/coding-rust-staged");
    fs::create_dir(stage).map_err(|_| DriverError)?;
    fs::set_permissions(stage, fs::Permissions::from_mode(0o755)).map_err(|_| DriverError)?;
    let inputs = snapshot.stage(stage, &generated).map_err(|_| DriverError)?;
    let toolchain: ToolchainWire = serde_json::from_slice(&read_protected(
        Path::new("/opt/coding-rust"),
        "toolchain.json",
        4096,
        false,
    )?)
    .map_err(|_| DriverError)?;
    if toolchain.schema != "dittobench-coding-rust-toolchain-v1" {
        return Err(DriverError);
    }
    let identity = ToolchainIdentity {
        image_sha256: authority.image_sha256,
        compiler_sha256: digest(&toolchain.compiler_sha256)?,
        bridge_library_sha256: digest(&toolchain.bridge_library_sha256)?,
    };
    let mut evidence = RuntimeEvidence {
        schema: "dittobench-coding-rust-runtime-v1",
        authority_sha256: hex(&options.authority_sha256),
        inputs_sha256: hex(&options.inputs_sha256),
        image_sha256: hex(&authority.image_sha256),
        program_sha256: hex(&program.program_sha256()),
        compiler_sha256: toolchain.compiler_sha256,
        bridge_library_sha256: toolchain.bridge_library_sha256,
        outcome: "compile_failed",
        build_sha256: None,
        artifact_sha256: None,
    };
    let build = match compile(
        &inputs,
        identity,
        Duration::from_millis(authority.build_timeout_ms),
    ) {
        Ok(build) => build,
        Err(CompileError::Compilation) => return write_report(&options, 0, evidence),
        Err(_) => return Err(DriverError),
    };
    evidence.build_sha256 = Some(hex(&build.sha256()));
    evidence.artifact_sha256 = Some(hex(&build.artifact_sha256()));
    let mut factory = ProcessFactory::new(
        build.into_artifact(),
        authority.schema,
        Deadlines {
            execution: Duration::from_millis(authority.candidate_timeout_ms),
            ..Deadlines::default()
        },
    )
    .map_err(|_| DriverError)?;
    let report = program
        .run(&mut factory, Limits::default())
        .map_err(|_| DriverError)?;
    if report.passed() + report.failed() != options.expected {
        return Err(DriverError);
    }
    let passed = report.passed();
    drop(factory); // drop sealed executable before publishing completion
    evidence.outcome = "evaluated";
    write_report(&options, passed, evidence)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
    fn arguments() -> Vec<String> {
        [
            "--group",
            "hidden",
            "--authority",
            "rust/hidden.json",
            "--authority-sha256",
            &"a".repeat(64),
            "--dittobench-rust-inputs",
            INPUTS,
            "--dittobench-rust-inputs-sha256",
            &"b".repeat(64),
            "--dittobench-report",
            REPORT,
            "--dittobench-nonce",
            &"c".repeat(48),
            "--dittobench-expected",
            "2",
            "--dittobench-candidate-uid",
            "10001",
            "--dittobench-candidate-gid",
            "10001",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    }
    fn valid() -> (Value, Value) {
        (
            json!({"schema": AUTHORITY_SCHEMA,"group":"hidden","crate_name":"candidate","suite":"tests/hidden.rs","suite_sha256":"a".repeat(64),"functions":[{"name":"api::f","parameters":[{"kind":"i64"}],"result":{"kind":"bool"}}],"files":["src/lib.rs"],"build_timeout_ms":60000,"candidate_timeout_ms":5000}),
            json!({"schema":INPUTS_SCHEMA,"authority_sha256":"a".repeat(64),"image_sha256":"d".repeat(64),"files":[{"path":"src/lib.rs","sha256":"b".repeat(64)}]}),
        )
    }
    fn bind(authority: Value, mut inputs: Value) -> Result<Authority> {
        let authority = serde_json::to_vec(&authority).unwrap();
        let authority_hash = hex(&Sha256::digest(&authority));
        inputs["authority_sha256"] = json!(authority_hash);
        let inputs = serde_json::to_vec(&inputs).unwrap();
        let mut args = arguments();
        args[5] = authority_hash;
        args[9] = hex(&Sha256::digest(&inputs));
        Authority::bind(&Options::parse(&args)?, &authority, &inputs)
    }
    #[test]
    fn options_require_fixed_identity_paths_nonce_and_bounds() {
        assert!(Options::parse(&arguments()).is_ok());
        for (index, value) in [
            (1, "other"),
            (3, "../bad.json"),
            (7, "/tmp/inputs.json"),
            (9, "A"),
            (11, "/tmp/report"),
            (13, "bad"),
            (15, "0"),
            (15, "33"),
            (15, "02"),
            (17, "0"),
            (19, "10002"),
        ] {
            let mut args = arguments();
            args[index] = value.into();
            assert!(Options::parse(&args).is_err());
        }
        let mut args = arguments();
        args[0] = "--authority".into();
        assert!(Options::parse(&args).is_err());
    }
    #[test]
    fn authority_binds_exact_allowlist_and_typed_api() {
        let (a, i) = valid();
        let bound = bind(a, i).ok().unwrap();
        assert_eq!(bound.schema.len(), 1);
        assert_eq!(bound.build_timeout_ms, 60000);
        let (mut a, mut i) = valid();
        a["extension"] = json!(true);
        i["extension"] = json!([1, 2]);
        assert!(bind(a, i).is_ok());
    }
    #[test]
    fn malformed_or_expanded_authority_is_rejected() {
        for field in [
            "files",
            "functions",
            "group",
            "crate_name",
            "suite",
            "build_timeout_ms",
            "candidate_timeout_ms",
        ] {
            let (mut a, i) = valid();
            a[field] = match field {
                "files" => json!(["src/lib.rs", "src/lib.rs"]),
                "functions" => json!([]),
                "group" => json!("visible"),
                "crate_name" => json!("u8"),
                "suite" => json!("../secret.rs"),
                _ => json!(0),
            };
            assert!(bind(a, i).is_err(), "{field}");
        }
        let (a, mut i) = valid();
        i["files"] = json!([{"path":"src/lib.rs","sha256":"b".repeat(64)},{"path":"src/lib.rs","sha256":"b".repeat(64)}]);
        assert!(bind(a, i).is_err());
        let (mut a, i) = valid();
        a["functions"][0]["result"] = json!({"kind":"execute","code":"untrusted"});
        assert!(bind(a, i).is_err());
    }
    #[test]
    fn hashes_are_external_not_self_declared() {
        let (a, i) = valid();
        let options = Options::parse(&arguments()).unwrap();
        assert!(Authority::bind(
            &options,
            &serde_json::to_vec(&a).unwrap(),
            &serde_json::to_vec(&i).unwrap()
        )
        .is_err());
    }
    #[test]
    fn duplicate_known_fields_fail_even_with_fresh_external_hashes() {
        let (a, mut i) = valid();
        let mut bytes = br#"{"group":"hidden","#.to_vec();
        bytes.extend_from_slice(&serde_json::to_vec(&a).unwrap()[1..]);
        let hash = hex(&Sha256::digest(&bytes));
        i["authority_sha256"] = json!(hash);
        let inputs = serde_json::to_vec(&i).unwrap();
        let mut args = arguments();
        args[5] = hash;
        args[9] = hex(&Sha256::digest(&inputs));
        assert!(Authority::bind(&Options::parse(&args).unwrap(), &bytes, &inputs).is_err());
    }
}
