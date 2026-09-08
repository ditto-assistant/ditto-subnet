//! Immutable executable bytes, not compiler success or freeze authentication.
use sha2::{Digest, Sha256};
use std::{
    fs::{File, Metadata},
    io::Write,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{FileExt, MetadataExt},
    },
};

pub const MAX_BINARY: u64 = 256 << 20;
const SEALS: i32 = libc::F_SEAL_SEAL | libc::F_SEAL_SHRINK | libc::F_SEAL_GROW | libc::F_SEAL_WRITE;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ArtifactError {
    Identity,
    Bytes,
    Io,
}
type Result<T> = std::result::Result<T, ArtifactError>;

/// Root-owned sealed ELF. Never loads or executes bytes in the grader.
/// No path is retained or reopened, and no writable handle is exported.
pub struct Artifact {
    file: File,
    digest: [u8; 32],
    bytes: u64,
}
impl Artifact {
    /// Import only an already sealed root-owned executable with an independently
    /// expected digest. A caller-supplied digest is not a compiler receipt.
    pub fn import(file: &File, expected: [u8; 32]) -> Result<Self> {
        let file = duplicate(file)?;
        let info = file.metadata().map_err(|_| ArtifactError::Io)?;
        regular(&info, 0, 0)?;
        if info.mode() & 0o7777 != 0o555 {
            return Err(ArtifactError::Identity);
        }
        // fcntl(-1) must not accidentally satisfy the bit-mask check.
        let seals = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GET_SEALS) };
        if seals < 0 || seals & SEALS != SEALS {
            return Err(ArtifactError::Identity);
        }
        if digest(&file)? != expected {
            return Err(ArtifactError::Bytes);
        }
        Ok(Self {
            file,
            digest: expected,
            bytes: info.size(),
        })
    }

    /// Seal an output FD only AFTER the compiler lifecycle owner has killed and
    /// reaped every writer. The FD must come from a protected fresh output mount,
    /// opened NOFOLLOW/NONBLOCK. This operation does not certify those obligations.
    pub fn seal_output(source: &File, compiler_uid: u32) -> Result<Self> {
        if unsafe { libc::geteuid() } != 0 || compiler_uid == 0 || compiler_uid == u32::MAX {
            return Err(ArtifactError::Identity);
        }
        seal(source, compiler_uid, 0)
    }
    pub fn sha256(&self) -> [u8; 32] {
        self.digest
    }
    pub fn byte_count(&self) -> u64 {
        self.bytes
    }
    pub(crate) fn file(&self) -> &File {
        &self.file
    }
}

fn duplicate(file: &File) -> Result<File> {
    let fd = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) };
    if fd < 0 {
        return Err(ArtifactError::Io);
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
fn regular(info: &Metadata, uid: u32, links: u64) -> Result<()> {
    if !info.is_file()
        || info.uid() != uid
        || info.nlink() != links
        || info.mode() & 0o7022 != 0
        || !(64..=MAX_BINARY).contains(&info.size())
    {
        return Err(ArtifactError::Identity);
    }
    Ok(())
}
fn unchanged(a: &Metadata, b: &Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.size() == b.size()
        && a.mode() == b.mode()
        && a.uid() == b.uid()
        && a.gid() == b.gid()
        && a.nlink() == b.nlink()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
        && a.ctime() == b.ctime()
        && a.ctime_nsec() == b.ctime_nsec()
}
fn elf(file: &File) -> Result<()> {
    let mut header = [0; 64];
    file.read_exact_at(&mut header, 0)
        .map_err(|_| ArtifactError::Bytes)?;
    if &header[..6] != b"\x7fELF\x02\x01"
        || header[6] != 1
        || u16::from_le_bytes([header[18], header[19]]) != 62
        || ![2, 3].contains(&u16::from_le_bytes([header[16], header[17]]))
    {
        return Err(ArtifactError::Bytes);
    }
    Ok(())
}
fn digest(file: &File) -> Result<[u8; 32]> {
    elf(file)?;
    let count = file.metadata().map_err(|_| ArtifactError::Io)?.size();
    if !(64..=MAX_BINARY).contains(&count) {
        return Err(ArtifactError::Bytes);
    }
    let mut hash = Sha256::new();
    let mut offset = 0;
    let mut buffer = [0; 65536];
    while offset < count {
        let take = ((count - offset) as usize).min(buffer.len());
        file.read_exact_at(&mut buffer[..take], offset)
            .map_err(|_| ArtifactError::Io)?;
        hash.update(&buffer[..take]);
        offset += take as u64;
    }
    Ok(hash.finalize().into())
}
fn seal(source: &File, source_uid: u32, owner_uid: u32) -> Result<Artifact> {
    let before = source.metadata().map_err(|_| ArtifactError::Io)?;
    regular(&before, source_uid, 1)?;
    elf(source)?;
    let fd = unsafe {
        libc::memfd_create(
            c"coding-rust-executable".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING | 0x10,
        )
    };
    if fd < 0 {
        return Err(ArtifactError::Io);
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    let mut hash = Sha256::new();
    let mut offset = 0;
    let mut buffer = [0; 65536];
    while offset < before.size() {
        let take = ((before.size() - offset) as usize).min(buffer.len());
        source
            .read_exact_at(&mut buffer[..take], offset)
            .map_err(|_| ArtifactError::Io)?;
        file.write_all(&buffer[..take])
            .map_err(|_| ArtifactError::Io)?;
        hash.update(&buffer[..take]);
        offset += take as u64;
    }
    if !unchanged(&before, &source.metadata().map_err(|_| ArtifactError::Io)?) {
        return Err(ArtifactError::Bytes);
    }
    if unsafe { libc::fchmod(fd, 0o555) } != 0
        || unsafe { libc::fcntl(fd, libc::F_ADD_SEALS, SEALS) } != 0
    {
        return Err(ArtifactError::Io);
    }
    let expected: [u8; 32] = hash.finalize().into();
    let info = file.metadata().map_err(|_| ArtifactError::Io)?;
    regular(&info, owner_uid, 0)?;
    if digest(&file)? != expected {
        return Err(ArtifactError::Bytes);
    }
    Ok(Artifact {
        file,
        digest: expected,
        bytes: offset,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs::{self, OpenOptions},
        io::{Seek, SeekFrom},
        os::unix::fs::PermissionsExt,
    };
    struct Source {
        file: File,
        path: std::path::PathBuf,
    }
    impl Source {
        fn new() -> Self {
            let mut random = [0; 16];
            getrandom::fill(&mut random).unwrap();
            let path = std::env::temp_dir().join(format!("coding-artifact-{:x?}", random));
            let mut file = OpenOptions::new()
                .create_new(true)
                .read(true)
                .write(true)
                .open(&path)
                .unwrap();
            file.set_permissions(fs::Permissions::from_mode(0o600))
                .unwrap();
            let mut bytes = [0; 128];
            bytes[..7].copy_from_slice(b"\x7fELF\x02\x01\x01");
            bytes[16] = 3;
            bytes[18] = 62;
            file.write_all(&bytes).unwrap();
            Self { file, path }
        }
        fn seal(&self) -> Result<Artifact> {
            let uid = unsafe { libc::geteuid() };
            seal(&self.file, uid, uid)
        }
    }
    impl Drop for Source {
        fn drop(&mut self) {
            fs::remove_file(&self.path).unwrap();
        }
    }
    #[test]
    fn sealed_bytes_are_independent_and_offsets_unchanged() {
        let mut source = Source::new();
        source.file.seek(SeekFrom::Start(7)).unwrap();
        let artifact = source.seal().ok().unwrap();
        assert_eq!(source.file.stream_position().unwrap(), 7);
        let hash = artifact.sha256();
        source.file.write_all(b"changed").unwrap();
        assert_eq!(digest(artifact.file()).unwrap(), hash);
        assert_eq!(artifact.byte_count(), 128);
        assert_eq!(
            unsafe { libc::fcntl(artifact.file.as_raw_fd(), libc::F_GET_SEALS) } & SEALS,
            SEALS
        );
        assert!(artifact.file.write_at(b"x", 0).is_err());
        assert!(artifact.file.set_len(64).is_err());
        assert!(artifact.file.set_len(256).is_err());
    }
    #[test]
    fn invalid_elf_and_bounds_fail() {
        let source = Source::new();
        source.file.write_at(b"bad", 0).unwrap();
        assert!(matches!(source.seal(), Err(ArtifactError::Bytes)));
        source.file.set_len(MAX_BINARY + 1).unwrap();
        assert!(matches!(source.seal(), Err(ArtifactError::Identity)));
        source.file.set_len(63).unwrap();
        assert!(source.seal().is_err());
    }
    #[test]
    fn links_permissions_owner_and_unsealed_import_fail() {
        let source = Source::new();
        assert!(Artifact::import(&source.file, [0; 32]).is_err());
        source
            .file
            .set_permissions(fs::Permissions::from_mode(0o666))
            .unwrap();
        assert!(source.seal().is_err());
        source
            .file
            .set_permissions(fs::Permissions::from_mode(0o600))
            .unwrap();
        let uid = unsafe { libc::geteuid() };
        assert!(seal(&source.file, uid + 1, uid).is_err());
        let link = source.path.with_extension("link");
        fs::hard_link(&source.path, &link).unwrap();
        let rejected = source.seal().is_err();
        fs::remove_file(link).unwrap();
        assert!(rejected);
    }
    #[test]
    fn public_import_requires_root_and_exact_digest() {
        let source = Source::new();
        let artifact = source.seal().ok().unwrap();
        assert!(Artifact::import(artifact.file(), [0; 32]).is_err());
        let imported = Artifact::import(artifact.file(), artifact.sha256());
        assert_eq!(imported.is_ok(), unsafe { libc::geteuid() } == 0);
    }
}
