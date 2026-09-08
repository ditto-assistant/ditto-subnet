//! Descriptor-relative capture of approved frozen implementation bytes.
//! This module never executes a compiler and never enumerates a source tree.
use crate::bridge::GeneratedBridge;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    ffi::CString,
    fs::{File, Metadata},
    io::{Read, Write},
    os::{
        fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd},
        unix::fs::{FileExt, MetadataExt},
    },
    path::{Path, PathBuf},
};

const MAX_FILES: usize = 256;
const MAX_FILE_BYTES: usize = 1 << 20;
const MAX_SOURCE_BYTES: usize = 16 << 20;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InputError {
    Manifest,
    Path,
    Metadata,
    Changed,
    Digest,
    Limit,
    Io,
    Entropy,
}
impl std::fmt::Display for InputError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Rust compiler inputs rejected")
    }
}
impl std::error::Error for InputError {}
type Result<T> = std::result::Result<T, InputError>;

/// Supplied by the authenticated freeze/controller, not discovered from files.
pub struct Manifest {
    entries: BTreeMap<String, [u8; 32]>,
}
impl Manifest {
    pub fn new(entries: BTreeMap<String, [u8; 32]>) -> Result<Self> {
        if entries.is_empty()
            || entries.len() > MAX_FILES
            || !entries.contains_key("src/lib.rs")
            || entries.keys().any(|p| !source_path(p))
        {
            return Err(InputError::Manifest);
        }
        Ok(Self { entries })
    }
}
fn source_path(path: &str) -> bool {
    if path.len() > 240 || !path.starts_with("src/") {
        return false;
    }
    let parts: Vec<_> = path.split('/').collect();
    if !(2..=8).contains(&parts.len()) {
        return false;
    }
    parts.iter().enumerate().all(|(index, part)| {
        let name = if index + 1 == parts.len() {
            let Some(stem) = part.strip_suffix(".rs") else {
                return false;
            };
            stem
        } else {
            part
        };
        !name.is_empty()
            && name.len() <= 64
            && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
            && ![
                "test", "tests", "benches", "examples", "fixtures", "target", "grader", "control",
                "curator", "build",
            ]
            .contains(&name.to_ascii_lowercase().as_str())
            && !name.to_ascii_lowercase().starts_with("test_")
            && !name.to_ascii_lowercase().ends_with("_test")
    })
}
fn cstring(name: &[u8]) -> Result<CString> {
    CString::new(name).map_err(|_| InputError::Path)
}
fn open_at(parent: &File, name: &[u8], flags: i32, mode: u32) -> Result<File> {
    let name = cstring(name)?;
    // Every name is a single component; each directory is held by a live FD.
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            flags | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            mode,
        )
    };
    if fd < 0 {
        return Err(InputError::Io);
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
fn absolute_directory(path: &Path, owner: u32) -> Result<File> {
    let text = path.to_str().ok_or(InputError::Path)?;
    if !text.starts_with('/') || text.len() > 4096 {
        return Err(InputError::Path);
    }
    let parts: Vec<_> = text[1..].split('/').collect();
    if parts.len() > 64
        || parts
            .iter()
            .any(|p| p.is_empty() || *p == "." || *p == "..")
    {
        return Err(InputError::Path);
    }
    let root = cstring(b"/")?;
    let fd = unsafe {
        libc::open(
            root.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(InputError::Io);
    }
    let mut directory = unsafe { File::from_raw_fd(fd) };
    for part in parts {
        directory = open_at(
            &directory,
            part.as_bytes(),
            libc::O_RDONLY | libc::O_DIRECTORY,
            0,
        )?;
        let info = metadata(&directory)?;
        // A safe final directory is insufficient under a renameable ancestor.
        // Root-owned sticky temporary directories protect trusted-owned entries.
        let sticky_root = info.uid() == 0 && info.mode() & 0o1000 != 0;
        if (info.uid() != 0 && info.uid() != owner)
            || info.mode() & 0o6000 != 0
            || (info.mode() & 0o022 != 0 && !sticky_root)
        {
            return Err(InputError::Metadata);
        }
    }
    Ok(directory)
}
fn metadata(file: &File) -> Result<Metadata> {
    file.metadata().map_err(|_| InputError::Io)
}
fn directory(meta: &Metadata, owner: u32, device: u64) -> Result<()> {
    if !meta.is_dir() || meta.uid() != owner || meta.dev() != device || meta.mode() & 0o7022 != 0 {
        return Err(InputError::Metadata);
    }
    Ok(())
}
fn same(a: &Metadata, b: &Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.mode() == b.mode()
        && a.uid() == b.uid()
        && a.gid() == b.gid()
        && a.nlink() == b.nlink()
        && a.len() == b.len()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
        && a.ctime() == b.ctime()
        && a.ctime_nsec() == b.ctime_nsec()
}
fn hash_field(hash: &mut Sha256, bytes: &[u8]) {
    hash.update((bytes.len() as u64).to_be_bytes());
    hash.update(bytes);
}
struct Source {
    path: String,
    bytes: Vec<u8>,
}

/// Opaque private snapshot: no Debug/Display/serialization or byte accessor.
/// ```compile_fail
/// fn log(s: coding_rust_suite::workspace::Snapshot) { println!("{s:?}"); }
/// ```
pub struct Snapshot {
    files: Vec<Source>,
    digest: [u8; 32],
    bytes: usize,
}
impl Snapshot {
    pub fn capture(root: &Path, expected_owner: u32, manifest: &Manifest) -> Result<Self> {
        if expected_owner == u32::MAX {
            return Err(InputError::Metadata);
        }
        let root = absolute_directory(root, expected_owner)?;
        let before = metadata(&root)?;
        directory(&before, expected_owner, before.dev())?;
        // The original tree is private. Only the explicit staged copy is exposed.
        if before.mode() & 0o077 != 0 {
            return Err(InputError::Metadata);
        }
        let mut files = Vec::new();
        let mut total = 0usize;
        let mut hash = Sha256::new();
        hash.update(b"coding-rust-frozen-sources-v1\0");
        hash.update((manifest.entries.len() as u64).to_be_bytes());
        for (path, expected) in &manifest.entries {
            let mut parent = root.try_clone().map_err(|_| InputError::Io)?;
            let parts: Vec<_> = path.split('/').collect();
            for part in &parts[..parts.len() - 1] {
                parent = open_at(
                    &parent,
                    part.as_bytes(),
                    libc::O_RDONLY | libc::O_DIRECTORY,
                    0,
                )?;
                directory(&metadata(&parent)?, expected_owner, before.dev())?;
            }
            let file = open_at(
                &parent,
                parts.last().ok_or(InputError::Path)?.as_bytes(),
                libc::O_RDONLY | libc::O_NONBLOCK,
                0,
            )?;
            let info = metadata(&file)?;
            if !info.is_file()
                || info.uid() != expected_owner
                || info.dev() != before.dev()
                || info.nlink() != 1
                || info.mode() & 0o7022 != 0
            {
                return Err(InputError::Metadata);
            }
            if info.len() > MAX_FILE_BYTES as u64
                || total
                    .checked_add(info.len() as usize)
                    .is_none_or(|n| n > MAX_SOURCE_BYTES)
            {
                return Err(InputError::Limit);
            }
            let mut bytes = Vec::new();
            (&file)
                .take((MAX_FILE_BYTES + 1) as u64)
                .read_to_end(&mut bytes)
                .map_err(|_| InputError::Io)?;
            if bytes.len() > MAX_FILE_BYTES {
                return Err(InputError::Limit);
            }
            if bytes.len() as u64 != info.len() || !same(&info, &metadata(&file)?) {
                return Err(InputError::Changed);
            }
            let digest: [u8; 32] = Sha256::digest(&bytes).into();
            if &digest != expected {
                return Err(InputError::Digest);
            }
            total += bytes.len();
            hash_field(&mut hash, path.as_bytes());
            hash.update((bytes.len() as u64).to_be_bytes());
            hash.update(digest);
            files.push(Source {
                path: path.clone(),
                bytes,
            });
        }
        if !same(&before, &metadata(&root)?) {
            return Err(InputError::Changed);
        }
        Ok(Self {
            files,
            digest: hash.finalize().into(),
            bytes: total,
        })
    }
    pub fn sha256(&self) -> [u8; 32] {
        self.digest
    }
    pub fn file_count(&self) -> usize {
        self.files.len()
    }
    pub fn byte_count(&self) -> usize {
        self.bytes
    }

    /// Creates a fresh random input directory, never overwrites or merges one.
    /// Failures retain a private incomplete directory for operator cleanup.
    pub fn stage(&self, parent: &Path, bridge: &GeneratedBridge) -> Result<StagedInputs> {
        let owner = unsafe { libc::geteuid() };
        let parent_fd = absolute_directory(parent, owner)?;
        let parent_info = metadata(&parent_fd)?;
        directory(&parent_info, owner, parent_info.dev())?;
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).map_err(|_| InputError::Entropy)?;
        let name = format!(
            "rust-input-{}",
            random
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        );
        let root = create_directory(&parent_fd, &name)?;
        let mut directories = BTreeMap::new();
        directories.insert(String::new(), root);
        for source in &self.files {
            let parts: Vec<_> = source.path.split('/').collect();
            let mut prefix = String::new();
            for part in &parts[..parts.len() - 1] {
                let next = if prefix.is_empty() {
                    (*part).to_owned()
                } else {
                    format!("{prefix}/{part}")
                };
                if !directories.contains_key(&next) {
                    let dir = create_directory(&directories[&prefix], part)?;
                    directories.insert(next.clone(), dir);
                }
                prefix = next;
            }
            write_file(
                &directories[&prefix],
                parts.last().ok_or(InputError::Path)?,
                &source.bytes,
            )?;
        }
        write_file(&directories[""], "bridge.rs", bridge.source().as_bytes())?;
        // Publish only after all writes/readbacks succeeded. Root stays private
        // until its descendants are read-only. No writable file FD escapes.
        for (path, fd) in directories.iter().rev() {
            if !path.is_empty() {
                chmod(fd, 0o555)?;
            }
        }
        let root = directories.remove("").ok_or(InputError::Io)?;
        root.sync_all().map_err(|_| InputError::Io)?;
        chmod(&root, 0o555)?;
        let mut digest = Sha256::new();
        digest.update(b"coding-rust-staged-inputs-v1\0");
        digest.update(self.digest);
        digest.update(bridge.sha256());
        digest.update(CompilerRecipe::sha256());
        Ok(StagedInputs {
            root,
            path: parent.join(name),
            digest: digest.finalize().into(),
        })
    }
}
fn chmod(file: &File, mode: u32) -> Result<()> {
    if unsafe { libc::fchmod(file.as_raw_fd(), mode) } != 0 {
        return Err(InputError::Io);
    }
    Ok(())
}
fn create_directory(parent: &File, name: &str) -> Result<File> {
    let c = cstring(name.as_bytes())?;
    if unsafe { libc::mkdirat(parent.as_raw_fd(), c.as_ptr(), 0o700) } != 0 {
        return Err(InputError::Io);
    }
    let file = open_at(
        parent,
        name.as_bytes(),
        libc::O_RDONLY | libc::O_DIRECTORY,
        0,
    )?;
    let info = metadata(&file)?;
    directory(&info, unsafe { libc::geteuid() }, metadata(parent)?.dev())?;
    Ok(file)
}
fn write_file(parent: &File, name: &str, bytes: &[u8]) -> Result<()> {
    let mut file = open_at(
        parent,
        name.as_bytes(),
        libc::O_RDWR | libc::O_CREAT | libc::O_EXCL,
        0o600,
    )?;
    let info = metadata(&file)?;
    if !info.is_file() || info.uid() != unsafe { libc::geteuid() } || info.nlink() != 1 {
        return Err(InputError::Metadata);
    }
    file.write_all(bytes).map_err(|_| InputError::Io)?;
    file.sync_all().map_err(|_| InputError::Io)?;
    let mut hash = Sha256::new();
    let mut offset = 0;
    let mut buffer = [0u8; 8192];
    loop {
        let n = file
            .read_at(&mut buffer, offset)
            .map_err(|_| InputError::Io)?;
        if n == 0 {
            break;
        }
        offset += n as u64;
        if offset > bytes.len() as u64 {
            return Err(InputError::Changed);
        }
        hash.update(&buffer[..n]);
    }
    if offset != bytes.len() as u64 || hash.finalize() != Sha256::digest(bytes) {
        return Err(InputError::Digest);
    }
    chmod(&file, 0o444)
}

/// Compiler-visible files only; this is not a process or cleanup handle.
/// ```compile_fail
/// fn log(s: coding_rust_suite::workspace::StagedInputs) { println!("{s:?}"); }
/// ```
pub struct StagedInputs {
    root: File,
    path: PathBuf,
    digest: [u8; 32],
}
impl StagedInputs {
    pub fn directory_fd(&self) -> BorrowedFd<'_> {
        self.root.as_fd()
    }
    pub fn directory(&self) -> &Path {
        &self.path
    }
    pub fn sha256(&self) -> [u8; 32] {
        self.digest
    }
}

/// Fixed Linux-amd64 compiler invocation. The caller supplies the isolated
/// input cwd, /out mount, credentials, resource limits, and kill/reap lifecycle.
pub struct CompilerRecipe;
impl CompilerRecipe {
    pub const PROGRAM: &'static str = "/opt/rustc";
    pub const ENVIRONMENT: &'static [(&'static str, &'static str)] = &[("PATH", "/usr/bin:/bin")];
    pub const LIBRARY: &'static [&'static str] = &[
        "--edition=2021",
        "--target=x86_64-unknown-linux-gnu",
        "--crate-name=candidate",
        "--crate-type=rlib",
        "-C",
        "overflow-checks=yes",
        "-C",
        "linker=/usr/bin/cc",
        "src/lib.rs",
        "-o",
        "/out/libcandidate.rlib",
    ];
    pub const BRIDGE: &'static [&'static str] = &[
        "--edition=2021",
        "--target=x86_64-unknown-linux-gnu",
        "--crate-name=coding_bridge",
        "-C",
        "overflow-checks=yes",
        "-C",
        "linker=/usr/bin/cc",
        "bridge.rs",
        "--extern",
        "candidate=/out/libcandidate.rlib",
        "--extern",
        "coding_rust_suite=/opt/deps/libcoding_rust_suite.rlib",
        "-L",
        "dependency=/opt/deps",
        "-o",
        "/out/candidate",
    ];
    pub fn sha256() -> [u8; 32] {
        let mut hash = Sha256::new();
        hash.update(b"coding-rust-compiler-recipe-v1\0");
        hash_field(&mut hash, Self::PROGRAM.as_bytes());
        for command in [Self::LIBRARY, Self::BRIDGE] {
            hash.update((command.len() as u64).to_be_bytes());
            for arg in command {
                hash_field(&mut hash, arg.as_bytes());
            }
        }
        for (key, value) in Self::ENVIRONMENT {
            hash_field(&mut hash, key.as_bytes());
            hash_field(&mut hash, value.as_bytes());
        }
        hash.finalize().into()
    }
}
