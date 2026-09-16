package codingcertservice

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"golang.org/x/sys/unix"
)

// ErrSocket refuses a socket or its directory. It never names the path.
var ErrSocket = errors.New("coding certification socket refused")

type fileIdentity struct {
	dev, ino uint64
	uid, gid uint32
	mode     uint32
}

func identityOf(stat unix.Stat_t) fileIdentity {
	return fileIdentity{dev: stat.Dev, ino: stat.Ino, uid: stat.Uid, gid: stat.Gid, mode: stat.Mode}
}

// openSecureDirectory opens path without following a link in any component.
// Every ancestor must be a real directory owned by root or euid and not
// writable by group or others; the directory itself must match exactly.
func openSecureDirectory(path string, euid int, want func(unix.Stat_t) bool) (int, unix.Stat_t, error) {
	var stat unix.Stat_t
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == "/" {
		return -1, stat, ErrSocket
	}
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, stat, ErrSocket
	}
	components := strings.Split(strings.TrimPrefix(path, "/"), "/")
	for index := 0; ; index++ {
		if unix.Fstat(fd, &stat) != nil || stat.Mode&unix.S_IFMT != unix.S_IFDIR {
			_ = unix.Close(fd)
			return -1, stat, ErrSocket
		}
		if index == len(components) {
			break
		}
		// A root-owned sticky ancestor (such as /tmp) is accepted: other users
		// cannot rename or remove the euid-owned entry below it.
		ownerOK := stat.Uid == 0 || int(stat.Uid) == euid
		writableOK := stat.Mode&0o022 == 0 || (stat.Uid == 0 && stat.Mode&unix.S_ISVTX != 0)
		if !ownerOK || !writableOK {
			_ = unix.Close(fd)
			return -1, stat, ErrSocket
		}
		next, err := unix.Openat(fd, components[index], unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		_ = unix.Close(fd)
		if err != nil {
			return -1, stat, ErrSocket
		}
		fd = next
	}
	if !want(stat) {
		_ = unix.Close(fd)
		return -1, stat, ErrSocket
	}
	return fd, stat, nil
}

// cleanSocketPath accepts an absolute, clean path below a directory that fits
// the 108-byte sun_path limit.
func cleanSocketPath(path string) bool {
	return filepath.IsAbs(path) && filepath.Clean(path) == path && len(path) < 108 &&
		filepath.Dir(path) != "/" && !strings.ContainsRune(path, 0)
}

func statEntry(directory int, name string) (unix.Stat_t, error) {
	var stat unix.Stat_t
	if unix.Fstatat(directory, name, &stat, unix.AT_SYMLINK_NOFOLLOW) != nil {
		return stat, ErrSocket
	}
	return stat, nil
}

// VerifyDockerSocket requires the dedicated daemon socket to be a real socket
// owned by euid with mode 0600, in a real directory owned by euid with mode
// 0700, reached without links. It does not connect.
func VerifyDockerSocket(path string, euid int) error {
	if euid <= 0 || !cleanSocketPath(path) {
		return ErrSocket
	}
	directory, _, err := openSecureDirectory(filepath.Dir(path), euid, func(stat unix.Stat_t) bool {
		return int(stat.Uid) == euid && stat.Mode&0o7777 == DockerDirectoryMode
	})
	if err != nil {
		return ErrSocket
	}
	defer unix.Close(directory)
	stat, err := statEntry(directory, filepath.Base(path))
	if err != nil || stat.Mode&unix.S_IFMT != unix.S_IFSOCK || int(stat.Uid) != euid ||
		stat.Mode&0o7777 != DockerSocketMode {
		return ErrSocket
	}
	return nil
}

// ControlSocket is the validator-facing listener at the fixed control path.
type ControlSocket struct {
	listener  *net.UnixListener
	path      string
	euid      int
	gid       int
	directory fileIdentity
	socket    fileIdentity
	closeOnce sync.Once
}

func controlDirectoryMatches(euid, gid int) func(unix.Stat_t) bool {
	return func(stat unix.Stat_t) bool {
		return int(stat.Uid) == euid && int(stat.Gid) == gid && stat.Mode&0o7777 == ControlDirectoryMode
	}
}

// ListenControlSocket creates the control socket at path. The directory must
// already exist, reached without links, owned by euid with group gid and mode
// 0750. A stale entry is removed only if it is a socket owned by euid. The new
// socket is given group gid and mode 0660, and its inode is recorded so every
// readiness probe can prove it is still the served socket.
func ListenControlSocket(path string, euid, gid int) (*ControlSocket, error) {
	if euid <= 0 || gid <= 0 || !cleanSocketPath(path) {
		return nil, ErrSocket
	}
	directory, directoryStat, err := openSecureDirectory(filepath.Dir(path), euid, controlDirectoryMatches(euid, gid))
	if err != nil {
		return nil, ErrSocket
	}
	defer unix.Close(directory)
	name := filepath.Base(path)
	if stale, err := statEntry(directory, name); err == nil {
		if stale.Mode&unix.S_IFMT != unix.S_IFSOCK || int(stale.Uid) != euid || unix.Unlinkat(directory, name, 0) != nil {
			return nil, ErrSocket
		}
	} else if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
		return nil, ErrSocket
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, ErrSocket
	}
	listener.SetUnlinkOnClose(true)
	fail := func() (*ControlSocket, error) {
		_ = listener.Close()
		return nil, ErrSocket
	}
	// The directory is writable only by euid, so the name cannot be swapped
	// between bind and these calls; the result is re-checked without links.
	if unix.Fchownat(directory, name, -1, gid, unix.AT_SYMLINK_NOFOLLOW) != nil ||
		unix.Fchmodat(directory, name, ControlSocketMode, 0) != nil {
		return fail()
	}
	stat, err := statEntry(directory, name)
	if err != nil || !controlSocketMatches(stat, euid, gid) {
		return fail()
	}
	return &ControlSocket{
		listener: listener, path: path, euid: euid, gid: gid,
		directory: identityOf(directoryStat), socket: identityOf(stat),
	}, nil
}

func controlSocketMatches(stat unix.Stat_t, euid, gid int) bool {
	return stat.Mode&unix.S_IFMT == unix.S_IFSOCK && int(stat.Uid) == euid && int(stat.Gid) == gid &&
		stat.Mode&0o7777 == ControlSocketMode
}

// Listener returns the served listener.
func (control *ControlSocket) Listener() net.Listener { return control.listener }

// Verify proves the fixed path is still the served socket: the same directory
// and socket inodes, owner, group and modes, reached without links.
func (control *ControlSocket) Verify() error {
	if control == nil || control.listener == nil {
		return ErrSocket
	}
	directory, directoryStat, err := openSecureDirectory(filepath.Dir(control.path), control.euid,
		controlDirectoryMatches(control.euid, control.gid))
	if err != nil {
		return ErrSocket
	}
	defer unix.Close(directory)
	stat, err := statEntry(directory, filepath.Base(control.path))
	if err != nil || identityOf(directoryStat) != control.directory || identityOf(stat) != control.socket ||
		!controlSocketMatches(stat, control.euid, control.gid) {
		return ErrSocket
	}
	return nil
}

// Close closes the listener and unlinks the socket.
func (control *ControlSocket) Close() error {
	if control == nil || control.listener == nil {
		return nil
	}
	var err error
	control.closeOnce.Do(func() { err = control.listener.Close() })
	return err
}

// trustedExecutableStat accepts a regular, executable, root-owned file that no
// one else can write, of a bounded non-zero size.
func trustedExecutableStat(stat unix.Stat_t) bool {
	return stat.Mode&unix.S_IFMT == unix.S_IFREG && stat.Uid == 0 && stat.Mode&0o022 == 0 &&
		stat.Mode&0o111 != 0 && stat.Size > 0 && stat.Size <= 256<<20
}

// ErrExecutable refuses an untrusted helper executable. It never names the path.
var ErrExecutable = errors.New("coding certification executable refused")

// VerifyTrustedExecutable requires path to be a regular, root-owned file that
// no one else can write, below root-owned directories that no one else can
// write, reached without links, and, when want is non-empty, to hash to the
// pinned lowercase SHA-256.
func VerifyTrustedExecutable(path string, want string) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || filepath.Dir(path) == "/" {
		return ErrExecutable
	}
	// Root is the only acceptable owner: no service user may own the path.
	directory, _, err := openSecureDirectory(filepath.Dir(path), 0, func(stat unix.Stat_t) bool {
		return stat.Uid == 0 && stat.Mode&0o022 == 0
	})
	if err != nil {
		return ErrExecutable
	}
	defer unix.Close(directory)
	fd, err := unix.Openat(directory, filepath.Base(path), unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return ErrExecutable
	}
	file := os.NewFile(uintptr(fd), "trusted-executable")
	defer file.Close()
	var stat unix.Stat_t
	if unix.Fstat(fd, &stat) != nil || !trustedExecutableStat(stat) {
		return ErrExecutable
	}
	if want == "" {
		return nil
	}
	hash := sha256.New()
	if written, err := io.Copy(hash, io.LimitReader(file, stat.Size+1)); err != nil || written != stat.Size {
		return ErrExecutable
	}
	if hex.EncodeToString(hash.Sum(nil)) != want {
		return ErrExecutable
	}
	return nil
}
