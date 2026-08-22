//go:build linux

package codingrelayjournal

import (
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

func openDirectoryCapabilities(root string) (directorySet, string, uint64, uint64, error) {
	clean := filepath.Clean(root)
	if !filepath.IsAbs(clean) || clean == string(filepath.Separator) || clean != root {
		return directorySet{}, "", 0, 0, fmt.Errorf("%w: root must be a canonical absolute non-root path", ErrInvalid)
	}
	parts := strings.Split(strings.TrimPrefix(clean, string(filepath.Separator)), string(filepath.Separator))
	currentFD, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return directorySet{}, "", 0, 0, fmt.Errorf("open relay journal root ancestor: %w", err)
	}
	parentFD, rootFD := -1, -1
	for index, part := range parts {
		if part == "" || part == "." || part == ".." {
			_ = unix.Close(currentFD)
			return directorySet{}, "", 0, 0, fmt.Errorf("%w: root contains an invalid component", ErrInvalid)
		}
		nextFD, openErr := unix.Openat(currentFD, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if openErr != nil {
			_ = unix.Close(currentFD)
			return directorySet{}, "", 0, 0, fmt.Errorf("open relay journal root: %w", openErr)
		}
		if index == len(parts)-1 {
			parentFD, rootFD = currentFD, nextFD
			break
		}
		_ = unix.Close(currentFD)
		currentFD = nextFD
	}
	if rootFD < 0 || parentFD < 0 {
		return directorySet{}, "", 0, 0, fmt.Errorf("%w: root cannot be opened", ErrInvalid)
	}
	cleanup := true
	defer func() {
		if cleanup {
			_ = unix.Close(parentFD)
			_ = unix.Close(rootFD)
		}
	}()
	var stat unix.Stat_t
	if err := unix.Fstat(rootFD, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		stat.Mode&0o777 != 0o700 || stat.Uid != uint32(os.Geteuid()) {
		return directorySet{}, "", 0, 0, fmt.Errorf("%w: root ownership or permissions are unsafe", ErrInvalid)
	}
	if err := unix.Flock(rootFD, unix.LOCK_EX|unix.LOCK_NB); err != nil {
		if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
			return directorySet{}, "", 0, 0, ErrLocked
		}
		return directorySet{}, "", 0, 0, fmt.Errorf("lock coding relay journal: %w", err)
	}
	staging, err := openOrCreateDirectory(rootFD, ".staging")
	if err != nil {
		return directorySet{}, "", 0, 0, err
	}
	entries, err := openOrCreateDirectory(rootFD, "entries")
	if err != nil {
		_ = staging.Close()
		return directorySet{}, "", 0, 0, err
	}
	for _, directory := range []*os.File{staging, entries} {
		var directoryStat unix.Stat_t
		if err := unix.Fstat(int(directory.Fd()), &directoryStat); err != nil || directoryStat.Dev != stat.Dev {
			_ = staging.Close()
			_ = entries.Close()
			return directorySet{}, "", 0, 0, fmt.Errorf("%w: journal directories cross filesystem boundaries", ErrInvalid)
		}
	}
	rootFile := os.NewFile(uintptr(rootFD), "coding-relay-journal-root")
	parentFile := os.NewFile(uintptr(parentFD), "coding-relay-journal-parent")
	if rootFile == nil || parentFile == nil {
		if rootFile != nil {
			_ = rootFile.Close()
		} else {
			_ = unix.Close(rootFD)
		}
		if parentFile != nil {
			_ = parentFile.Close()
		} else {
			_ = unix.Close(parentFD)
		}
		_ = staging.Close()
		_ = entries.Close()
		cleanup = false
		return directorySet{}, "", 0, 0, fmt.Errorf("%w: directory handles are unavailable", ErrInvalid)
	}
	cleanup = false
	return directorySet{parent: parentFile, root: rootFile, staging: staging, entries: entries},
		parts[len(parts)-1], uint64(stat.Dev), stat.Ino, nil
}

func openOrCreateDirectory(parentFD int, name string) (*os.File, error) {
	created := false
	if err := unix.Mkdirat(parentFD, name, 0o700); err == nil {
		created = true
	} else if !errors.Is(err, unix.EEXIST) {
		return nil, fmt.Errorf("create relay journal directory: %w", err)
	}
	fd, err := unix.Openat(parentFD, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("open relay journal directory: %w", err)
	}
	file := os.NewFile(uintptr(fd), name)
	if file == nil {
		_ = unix.Close(fd)
		return nil, errors.New("open relay journal directory handle")
	}
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		stat.Mode&0o777 != 0o700 || stat.Uid != uint32(os.Geteuid()) {
		_ = file.Close()
		return nil, fmt.Errorf("%w: journal directory is unsafe", ErrCorrupt)
	}
	if created {
		if err := unix.Fsync(parentFD); err != nil {
			_ = file.Close()
			return nil, fmt.Errorf("sync relay journal parent directory: %w", err)
		}
	}
	return file, nil
}

func (store *Store) validateRootIdentity() error {
	var absoluteStat, pathStat, descriptorStat unix.Stat_t
	if err := unix.Fstatat(int(store.dirs.parent.Fd()), store.rootLeaf, &pathStat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		unix.Lstat(store.root, &absoluteStat) != nil || absoluteStat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		unix.Fstat(int(store.dirs.root.Fd()), &descriptorStat) != nil || pathStat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		pathStat.Mode&0o777 != 0o700 || absoluteStat.Mode&0o777 != 0o700 || descriptorStat.Mode&0o777 != 0o700 ||
		pathStat.Uid != uint32(os.Geteuid()) || absoluteStat.Uid != uint32(os.Geteuid()) ||
		descriptorStat.Uid != uint32(os.Geteuid()) || uint64(pathStat.Dev) != store.rootDev ||
		pathStat.Ino != store.rootIno || uint64(absoluteStat.Dev) != store.rootDev ||
		absoluteStat.Ino != store.rootIno || uint64(descriptorStat.Dev) != store.rootDev ||
		descriptorStat.Ino != store.rootIno {
		return fmt.Errorf("%w: journal root identity changed", ErrCorrupt)
	}
	for _, link := range []struct {
		parent *os.File
		name   string
		file   *os.File
	}{
		{store.dirs.root, ".staging", store.dirs.staging},
		{store.dirs.root, "entries", store.dirs.entries},
	} {
		if err := validateDirectoryLink(link.parent, link.name, link.file, store.rootDev); err != nil {
			return err
		}
	}
	return nil
}

func validateDirectoryLink(parent *os.File, name string, directory *os.File, rootDev uint64) error {
	var pathStat, descriptorStat unix.Stat_t
	if parent == nil || directory == nil ||
		unix.Fstatat(int(parent.Fd()), name, &pathStat, unix.AT_SYMLINK_NOFOLLOW) != nil ||
		unix.Fstat(int(directory.Fd()), &descriptorStat) != nil ||
		pathStat.Mode&unix.S_IFMT != unix.S_IFDIR || descriptorStat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		pathStat.Mode&0o777 != 0o700 || descriptorStat.Mode&0o777 != 0o700 ||
		pathStat.Uid != uint32(os.Geteuid()) || descriptorStat.Uid != uint32(os.Geteuid()) ||
		uint64(pathStat.Dev) != rootDev || uint64(descriptorStat.Dev) != rootDev ||
		pathStat.Dev != descriptorStat.Dev || pathStat.Ino != descriptorStat.Ino {
		return fmt.Errorf("%w: journal directory identity changed", ErrCorrupt)
	}
	return nil
}

func randomLeaf(prefix string) (string, error) {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	const digits = "0123456789abcdef"
	encoded := make([]byte, len(value)*2)
	for index, item := range value {
		encoded[index*2] = digits[item>>4]
		encoded[index*2+1] = digits[item&0x0f]
	}
	return prefix + string(encoded), nil
}

func newStagingFile(directory *os.File, prefix string) (*os.File, string, uint64, uint64, error) {
	for attempt := 0; attempt < 8; attempt++ {
		name, err := randomLeaf(prefix)
		if err != nil {
			return nil, "", 0, 0, err
		}
		fd, err := unix.Openat(int(directory.Fd()), name,
			unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
		if errors.Is(err, unix.EEXIST) {
			continue
		}
		if err != nil {
			return nil, "", 0, 0, fmt.Errorf("create relay journal staging file: %w", err)
		}
		var stat unix.Stat_t
		if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG ||
			stat.Mode&0o777 != 0o600 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
			_ = unix.Close(fd)
			_ = unix.Unlinkat(int(directory.Fd()), name, 0)
			return nil, "", 0, 0, fmt.Errorf("%w: staging file is unsafe", ErrCorrupt)
		}
		file := os.NewFile(uintptr(fd), name)
		if file == nil {
			_ = unix.Close(fd)
			_ = unix.Unlinkat(int(directory.Fd()), name, 0)
			return nil, "", 0, 0, errors.New("create relay journal staging handle")
		}
		return file, name, uint64(stat.Dev), stat.Ino, nil
	}
	return nil, "", 0, 0, errors.New("allocate unique relay journal staging name")
}

func verifyNamedInode(directory *os.File, name string, dev, ino uint64, mode uint32) error {
	var stat unix.Stat_t
	if err := unix.Fstatat(int(directory.Fd()), name, &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Mode&0o777 != mode ||
		stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || uint64(stat.Dev) != dev || stat.Ino != ino {
		return fmt.Errorf("%w: journal file identity changed", ErrCorrupt)
	}
	return nil
}

func readVerifiedFile(directory *os.File, name string, maximum int64) ([]byte, int64, error) {
	fd, err := unix.Openat(int(directory.Fd()), name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, 0, fmt.Errorf("%w: journal record is unavailable", ErrCorrupt)
	}
	file := os.NewFile(uintptr(fd), name)
	if file == nil {
		_ = unix.Close(fd)
		return nil, 0, fmt.Errorf("%w: journal record handle is unavailable", ErrCorrupt)
	}
	defer file.Close()
	var before, after unix.Stat_t
	if err := unix.Fstat(fd, &before); err != nil || before.Mode&unix.S_IFMT != unix.S_IFREG ||
		before.Mode&0o777 != 0o600 || before.Uid != uint32(os.Geteuid()) || before.Nlink != 1 ||
		before.Size <= 0 || before.Size > maximum {
		return nil, 0, fmt.Errorf("%w: journal record metadata disagrees", ErrCorrupt)
	}
	body, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(body)) != before.Size {
		return nil, 0, fmt.Errorf("%w: journal record bytes disagree", ErrCorrupt)
	}
	if err := unix.Fstat(fd, &after); err != nil || before.Dev != after.Dev || before.Ino != after.Ino ||
		before.Size != after.Size || before.Nlink != after.Nlink {
		return nil, 0, fmt.Errorf("%w: journal record changed while reading", ErrCorrupt)
	}
	var pathStat unix.Stat_t
	if err := unix.Fstatat(int(directory.Fd()), name, &pathStat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		pathStat.Dev != after.Dev || pathStat.Ino != after.Ino || pathStat.Nlink != 1 {
		return nil, 0, fmt.Errorf("%w: journal record pathname changed while reading", ErrCorrupt)
	}
	return body, before.Size, nil
}

func writeAll(writer io.Writer, body []byte) error {
	for len(body) > 0 {
		written, err := writer.Write(body)
		if err != nil {
			return err
		}
		if written <= 0 || written > len(body) {
			return io.ErrShortWrite
		}
		body = body[written:]
	}
	return nil
}
