// Package codinghostedruntime is the dedicated-process, one-attempt Platform
// launcher. It is not a validator worker, scheduler or production activation.
package codinghostedruntime

import (
	"io"
	"os"
	"path/filepath"
	"syscall"
)

// Private files must be pre-provisioned outside Git by the trusted Platform
// owner. Reject links, devices, FIFOs, shared files and permissive directories.
func privateDirectory(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == "/" {
		return false
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil || real != path {
		return false
	}
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
		return false
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) {
		return false
	}
	// A private leaf below a non-sticky shared directory can still be renamed
	// by another user. Root-owned sticky ancestors such as /tmp are safe here;
	// the state directory's persistence is a separate provisioning obligation.
	for current := filepath.Dir(path); ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		if err != nil || !info.IsDir() {
			return false
		}
		owner, ok := info.Sys().(*syscall.Stat_t)
		if !ok || owner.Uid != 0 && owner.Uid != uint32(os.Geteuid()) ||
			info.Mode().Perm()&0022 != 0 && !(owner.Uid == 0 && info.Mode()&os.ModeSticky != 0) {
			return false
		}
		if current == "/" {
			break
		}
	}
	return true
}

func readPrivate(path string, maximum int64) ([]byte, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || !privateDirectory(filepath.Dir(path)) {
		return nil, ErrConfig
	}
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, ErrConfig
	}
	file := os.NewFile(uintptr(fd), "private-runtime-input")
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0600 || info.Size() < 1 || info.Size() > maximum {
		return nil, ErrConfig
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
		return nil, ErrConfig
	}
	body, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(body)) != info.Size() {
		clear(body)
		return nil, ErrConfig
	}
	return body, nil
}

// The marker is append-only and conservative: any failure after creation
// consumes this runtime directory, even if no candidate start was observed.
// Platform's PostgreSQL start remains authoritative across hosts/directories.
func consume(root string) error {
	if !privateDirectory(root) {
		return ErrConfig
	}
	directory, err := os.Open(root)
	if err != nil {
		return ErrConfig
	}
	defer directory.Close()
	fd, err := syscall.Openat(int(directory.Fd()), "consumed", syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return ErrConsumed
	}
	file := os.NewFile(uintptr(fd), "runtime-consumed")
	_, writeErr := file.WriteString("dittobench-coding-hosted-runtime-consumed-v2\n")
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil || directory.Sync() != nil {
		return ErrConsumed
	}
	return nil
}

// Executables may be root-owned or worker-owned and may use a protected venv
// symlink. Every resolved ancestor must resist writes by other users. Approval
// of the installed binary and Platform package remains an operator prerequisite.
func protectedExecutable(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return false
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return false
	}
	for _, start := range []string{path, real} {
		for current := start; ; current = filepath.Dir(current) {
			info, err := os.Stat(current)
			if err != nil || info.Mode().Perm()&0022 != 0 {
				return false
			}
			stat, ok := info.Sys().(*syscall.Stat_t)
			if !ok || stat.Uid != 0 && stat.Uid != uint32(os.Geteuid()) {
				return false
			}
			if current == start && (!info.Mode().IsRegular() || info.Mode().Perm()&0111 == 0) {
				return false
			}
			if current == "/" {
				break
			}
		}
	}
	return true
}
