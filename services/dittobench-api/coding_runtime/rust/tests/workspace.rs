#![cfg(target_os = "linux")]
use coding_rust_suite::{bridge, evaluator::Signature, value::Type, workspace::*};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    ffi::CString,
    fs,
    os::{
        fd::AsRawFd,
        unix::fs::{symlink, PermissionsExt},
    },
    path::{Path, PathBuf},
};

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).unwrap();
        let name = random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        let path = std::env::temp_dir().join(format!("coding-rust-input-test-{name}"));
        fs::create_dir(&path).unwrap();
        mode(&path, 0o700);
        Self(path)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}
fn writable(path: &Path) {
    if fs::symlink_metadata(path).unwrap().is_dir() {
        mode(path, 0o700);
        for entry in fs::read_dir(path).unwrap() {
            writable(&entry.unwrap().path());
        }
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        writable(&self.0);
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn mode(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
fn owner() -> u32 {
    unsafe { libc::geteuid() }
}
fn manifest(entries: &[(&str, &[u8])]) -> Manifest {
    Manifest::new(
        entries
            .iter()
            .map(|(name, bytes)| (name.to_string(), Sha256::digest(bytes).into()))
            .collect(),
    )
    .ok()
    .unwrap()
}
fn source(root: &Path, name: &str, bytes: &[u8]) {
    let path = root.join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let mut parent = path.parent().unwrap();
    while parent != root {
        mode(parent, 0o755);
        parent = parent.parent().unwrap();
    }
    fs::write(&path, bytes).unwrap();
    mode(&path, 0o600);
}
fn bridge() -> bridge::GeneratedBridge {
    bridge::generate(&BTreeMap::from([(
        "f".into(),
        Signature {
            parameters: vec![],
            result: Type::Bool,
        },
    )]))
    .unwrap()
}

#[test]
fn capture_and_stage_only_approved_bytes_after_source_changes() {
    let root = Temp::new();
    let output = Temp::new();
    source(root.path(), "src/lib.rs", b"pub mod nested;");
    source(root.path(), "src/nested/mod.rs", b"pub fn f() {}");
    source(
        root.path(),
        "tests/hidden.rs",
        b"public synthetic oracle, never copy",
    );
    source(
        root.path(),
        "build.rs",
        b"public synthetic build hook, never copy",
    );
    let list = manifest(&[
        ("src/lib.rs", b"pub mod nested;"),
        ("src/nested/mod.rs", b"pub fn f() {}"),
    ]);
    let snapshot = Snapshot::capture(root.path(), owner(), &list).ok().unwrap();
    assert_eq!(snapshot.file_count(), 2);
    assert_eq!(snapshot.byte_count(), 28);
    source(root.path(), "src/lib.rs", b"changed after capture");
    let bridge = bridge();
    let staged = snapshot.stage(output.path(), &bridge).ok().unwrap();
    assert_eq!(
        fs::read(staged.directory().join("src/lib.rs")).unwrap(),
        b"pub mod nested;"
    );
    assert_eq!(
        fs::read(staged.directory().join("bridge.rs")).unwrap(),
        bridge.source().as_bytes()
    );
    assert!(!staged.directory().join("tests").exists());
    assert!(!staged.directory().join("build.rs").exists());
    assert_eq!(
        fs::metadata(staged.directory())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o555
    );
    assert_eq!(
        fs::metadata(staged.directory().join("src/nested"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o555
    );
    assert_eq!(
        fs::metadata(staged.directory().join("src/lib.rs"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o444
    );
    let second = snapshot.stage(output.path(), &bridge).ok().unwrap();
    assert_ne!(staged.directory(), second.directory());
    assert_eq!(staged.sha256(), second.sha256());
    let name = CString::new("src/lib.rs").unwrap();
    let fd = unsafe {
        libc::openat(
            staged.directory_fd().as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC,
        )
    };
    assert!(fd >= 0);
    unsafe {
        libc::close(fd);
    }
}

#[test]
fn manifest_requires_library_entry_and_rejects_nonimplementation_paths() {
    assert!(Manifest::new(BTreeMap::new()).is_err());
    for path in [
        "lib.rs",
        "/src/lib.rs",
        "src/../lib.rs",
        "src//other.rs",
        "src/a/./other.rs",
        "src/tests.rs",
        "src/test_helper.rs",
        "src/foo_test.rs",
        "src/tests/code.rs",
        "src/Tests/code.rs",
        "src/build.rs",
        "src/.cargo/config.rs",
        "src/lib.rs/child.rs",
        "src/data.txt",
        "src/a\\b.rs",
        "src/a\0b.rs",
    ] {
        let entries = BTreeMap::from([("src/lib.rs".into(), [0; 32]), (path.into(), [1; 32])]);
        assert!(Manifest::new(entries).is_err(), "path admitted: {path}");
    }
    let mut entries = BTreeMap::from([("src/lib.rs".into(), [0; 32])]);
    for i in 0..256 {
        entries.insert(format!("src/f{i}.rs"), [0; 32]);
    }
    assert!(Manifest::new(entries).is_err());
}

#[test]
fn content_digest_and_missing_files_fail_closed() {
    let root = Temp::new();
    source(root.path(), "src/lib.rs", b"wrong");
    assert_eq!(
        Snapshot::capture(
            root.path(),
            owner(),
            &manifest(&[("src/lib.rs", b"expected")])
        )
        .err(),
        Some(InputError::Digest)
    );
    assert!(Snapshot::capture(
        root.path(),
        owner(),
        &manifest(&[("src/lib.rs", b"wrong"), ("src/missing.rs", b"")])
    )
    .is_err());
}

#[test]
fn root_and_descendant_directory_symlinks_are_not_followed() {
    let root = Temp::new();
    let links = Temp::new();
    source(root.path(), "src/lib.rs", b"source");
    let list = manifest(&[("src/lib.rs", b"source")]);
    symlink(root.path(), links.path().join("alias")).unwrap();
    assert!(Snapshot::capture(&links.path().join("alias"), owner(), &list).is_err());
    symlink(root.path().join("src"), links.path().join("src")).unwrap();
    assert!(Snapshot::capture(links.path(), owner(), &list).is_err());
}

#[test]
fn leaf_symlinks_hardlinks_and_fifos_are_rejected() {
    let root = Temp::new();
    let external = Temp::new();
    source(external.path(), "target.rs", b"source");
    fs::create_dir(root.path().join("src")).unwrap();
    mode(&root.path().join("src"), 0o755);
    let target = root.path().join("src/lib.rs");
    let list = manifest(&[("src/lib.rs", b"source")]);
    symlink(external.path().join("target.rs"), &target).unwrap();
    assert!(Snapshot::capture(root.path(), owner(), &list).is_err());
    fs::remove_file(&target).unwrap();
    fs::hard_link(external.path().join("target.rs"), &target).unwrap();
    assert_eq!(
        Snapshot::capture(root.path(), owner(), &list).err(),
        Some(InputError::Metadata)
    );
    fs::remove_file(&target).unwrap();
    let path = CString::new(target.to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
    assert_eq!(
        Snapshot::capture(root.path(), owner(), &list).err(),
        Some(InputError::Metadata)
    );
}

#[test]
fn ownership_permissions_and_private_root_are_enforced() {
    let root = Temp::new();
    source(root.path(), "src/lib.rs", b"source");
    let list = manifest(&[("src/lib.rs", b"source")]);
    assert_eq!(
        Snapshot::capture(root.path(), owner().wrapping_add(1), &list).err(),
        Some(InputError::Metadata)
    );
    mode(root.path(), 0o755);
    assert_eq!(
        Snapshot::capture(root.path(), owner(), &list).err(),
        Some(InputError::Metadata)
    );
    mode(root.path(), 0o700);
    for mode_bits in [0o622, 0o666, 0o4600] {
        mode(&root.path().join("src/lib.rs"), mode_bits);
        assert_eq!(
            Snapshot::capture(root.path(), owner(), &list).err(),
            Some(InputError::Metadata)
        );
    }
    mode(&root.path().join("src/lib.rs"), 0o600);
    mode(&root.path().join("src"), 0o777);
    assert_eq!(
        Snapshot::capture(root.path(), owner(), &list).err(),
        Some(InputError::Metadata)
    );
}

#[test]
fn source_byte_budgets_are_enforced() {
    let root = Temp::new();
    let bytes = vec![b'x'; (1 << 20) + 1];
    source(root.path(), "src/lib.rs", &bytes);
    assert_eq!(
        Snapshot::capture(root.path(), owner(), &manifest(&[("src/lib.rs", &bytes)])).err(),
        Some(InputError::Limit)
    );
    let bytes = vec![b'x'; 1 << 20];
    let mut entries = BTreeMap::new();
    for i in 0..17 {
        let name = if i == 0 {
            "src/lib.rs".to_owned()
        } else {
            format!("src/f{i}.rs")
        };
        source(root.path(), &name, &bytes);
        entries.insert(name, Sha256::digest(&bytes).into());
    }
    let list = Manifest::new(entries).ok().unwrap();
    assert_eq!(
        Snapshot::capture(root.path(), owner(), &list).err(),
        Some(InputError::Limit)
    );
}

#[test]
fn snapshot_commitments_bind_names_and_bytes_not_host_paths() {
    let first = Temp::new();
    let second = Temp::new();
    for root in [&first, &second] {
        source(root.path(), "src/lib.rs", b"source");
    }
    let list = manifest(&[("src/lib.rs", b"source")]);
    let a = Snapshot::capture(first.path(), owner(), &list)
        .ok()
        .unwrap();
    let b = Snapshot::capture(second.path(), owner(), &list)
        .ok()
        .unwrap();
    assert_eq!(a.sha256(), b.sha256());
    source(second.path(), "src/other.rs", b"source");
    let b = Snapshot::capture(
        second.path(),
        owner(),
        &manifest(&[("src/lib.rs", b"source"), ("src/other.rs", b"source")]),
    )
    .ok()
    .unwrap();
    assert_ne!(a.sha256(), b.sha256());
}

#[test]
fn staging_rejects_unprotected_or_symlinked_parents() {
    let root = Temp::new();
    let output = Temp::new();
    source(root.path(), "src/lib.rs", b"source");
    let snapshot = Snapshot::capture(
        root.path(),
        owner(),
        &manifest(&[("src/lib.rs", b"source")]),
    )
    .ok()
    .unwrap();
    mode(output.path(), 0o777);
    assert_eq!(
        snapshot.stage(output.path(), &bridge()).err(),
        Some(InputError::Metadata)
    );
    mode(output.path(), 0o700);
    symlink(output.path(), root.path().join("alias")).unwrap();
    assert!(snapshot
        .stage(&root.path().join("alias"), &bridge())
        .is_err());
}

#[test]
fn compiler_recipe_has_fixed_tools_environment_and_outputs() {
    assert_eq!(CompilerRecipe::PROGRAM, "/opt/rustc");
    assert_eq!(CompilerRecipe::ENVIRONMENT, &[("PATH", "/usr/bin:/bin")]);
    assert!(CompilerRecipe::LIBRARY.contains(&"src/lib.rs"));
    assert!(CompilerRecipe::BRIDGE.contains(&"bridge.rs"));
    assert!(CompilerRecipe::LIBRARY.contains(&"/out/libcandidate.rlib"));
    assert!(CompilerRecipe::BRIDGE.contains(&"/out/candidate"));
    for args in [CompilerRecipe::LIBRARY, CompilerRecipe::BRIDGE] {
        assert!(args.contains(&"--target=x86_64-unknown-linux-gnu"));
        assert!(!args.contains(&"--test"));
        assert!(!args
            .iter()
            .any(|a| a.contains("Cargo") || a.contains("grader") || a.contains("--cfg")));
    }
    assert_eq!(CompilerRecipe::sha256(), CompilerRecipe::sha256());
}

#[test]
fn private_final_directory_does_not_hide_an_unsafe_ancestor() {
    let root = Temp::new();
    let outer = Temp::new();
    source(root.path(), "src/lib.rs", b"source");
    let snapshot = Snapshot::capture(
        root.path(),
        owner(),
        &manifest(&[("src/lib.rs", b"source")]),
    )
    .ok()
    .unwrap();
    let ancestor = outer.path().join("unsafe");
    let parent = ancestor.join("private");
    fs::create_dir_all(&parent).unwrap();
    mode(&ancestor, 0o777);
    mode(&parent, 0o700);
    assert_eq!(
        snapshot.stage(&parent, &bridge()).err(),
        Some(InputError::Metadata)
    );
    source(&parent, "src/lib.rs", b"source");
    assert_eq!(
        Snapshot::capture(&parent, owner(), &manifest(&[("src/lib.rs", b"source")])).err(),
        Some(InputError::Metadata)
    );
}
