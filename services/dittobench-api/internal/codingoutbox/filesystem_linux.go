//go:build linux

package codingoutbox

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
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
		return directorySet{}, "", 0, 0, fmt.Errorf("open outbox root ancestor: %w", err)
	}
	var parentFD, rootFD int = -1, -1
	for index, part := range parts {
		if part == "" || part == "." || part == ".." {
			unix.Close(currentFD)
			return directorySet{}, "", 0, 0, fmt.Errorf("%w: root contains an invalid component", ErrInvalid)
		}
		nextFD, openErr := unix.Openat(currentFD, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if openErr != nil {
			unix.Close(currentFD)
			return directorySet{}, "", 0, 0, fmt.Errorf("open outbox root: %w", openErr)
		}
		if index == len(parts)-1 {
			parentFD, rootFD = currentFD, nextFD
			break
		}
		unix.Close(currentFD)
		currentFD = nextFD
	}
	if rootFD < 0 || parentFD < 0 {
		return directorySet{}, "", 0, 0, fmt.Errorf("%w: root cannot be opened", ErrInvalid)
	}
	cleanup := true
	defer func() {
		if cleanup {
			unix.Close(parentFD)
			unix.Close(rootFD)
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
		return directorySet{}, "", 0, 0, fmt.Errorf("lock coding evidence outbox: %w", err)
	}
	staging, err := openOrCreateDirectory(rootFD, ".staging")
	if err != nil {
		return directorySet{}, "", 0, 0, err
	}
	records, err := openOrCreateDirectory(rootFD, "records")
	if err != nil {
		staging.Close()
		return directorySet{}, "", 0, 0, err
	}
	objects, err := openOrCreateDirectory(rootFD, "objects")
	if err != nil {
		staging.Close()
		records.Close()
		return directorySet{}, "", 0, 0, err
	}
	shaDir, err := openOrCreateDirectory(int(objects.Fd()), "sha256")
	if err != nil {
		staging.Close()
		records.Close()
		objects.Close()
		return directorySet{}, "", 0, 0, err
	}
	for _, directory := range []*os.File{staging, records, objects, shaDir} {
		var directoryStat unix.Stat_t
		if err := unix.Fstat(int(directory.Fd()), &directoryStat); err != nil || directoryStat.Dev != stat.Dev {
			staging.Close()
			records.Close()
			objects.Close()
			shaDir.Close()
			return directorySet{}, "", 0, 0, fmt.Errorf("%w: outbox directories cross filesystem boundaries", ErrInvalid)
		}
	}
	rootFile := os.NewFile(uintptr(rootFD), "coding-outbox-root")
	parentFile := os.NewFile(uintptr(parentFD), "coding-outbox-parent")
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
		staging.Close()
		records.Close()
		objects.Close()
		shaDir.Close()
		cleanup = false
		return directorySet{}, "", 0, 0, fmt.Errorf("%w: directory handles are unavailable", ErrInvalid)
	}
	cleanup = false
	return directorySet{parent: parentFile, root: rootFile, staging: staging, records: records, objects: objects, sha256Dir: shaDir},
		parts[len(parts)-1], uint64(stat.Dev), stat.Ino, nil
}

func openOrCreateDirectory(parentFD int, name string) (*os.File, error) {
	created := false
	if err := unix.Mkdirat(parentFD, name, 0o700); err == nil {
		created = true
	} else if !errors.Is(err, unix.EEXIST) {
		return nil, fmt.Errorf("create outbox directory: %w", err)
	}
	fd, err := unix.Openat(parentFD, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("open outbox directory: %w", err)
	}
	file := os.NewFile(uintptr(fd), name)
	if file == nil {
		unix.Close(fd)
		return nil, errors.New("open outbox directory handle")
	}
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		stat.Mode&0o777 != 0o700 || stat.Uid != uint32(os.Geteuid()) {
		file.Close()
		return nil, fmt.Errorf("%w: outbox directory is unsafe", ErrCorrupt)
	}
	if created {
		if err := unix.Fsync(parentFD); err != nil {
			file.Close()
			return nil, fmt.Errorf("sync outbox parent directory: %w", err)
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
		pathStat.Uid != uint32(os.Geteuid()) || absoluteStat.Uid != uint32(os.Geteuid()) || descriptorStat.Uid != uint32(os.Geteuid()) ||
		uint64(pathStat.Dev) != store.rootDev || pathStat.Ino != store.rootIno ||
		uint64(absoluteStat.Dev) != store.rootDev || absoluteStat.Ino != store.rootIno ||
		uint64(descriptorStat.Dev) != store.rootDev || descriptorStat.Ino != store.rootIno {
		return fmt.Errorf("%w: outbox root identity changed", ErrCorrupt)
	}
	for _, link := range []struct {
		parent *os.File
		name   string
		file   *os.File
	}{
		{store.dirs.root, ".staging", store.dirs.staging},
		{store.dirs.root, "records", store.dirs.records},
		{store.dirs.root, "objects", store.dirs.objects},
		{store.dirs.objects, "sha256", store.dirs.sha256Dir},
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
		return fmt.Errorf("%w: outbox directory identity changed", ErrCorrupt)
	}
	return nil
}

func randomLeaf(prefix string) (string, error) {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return prefix + hex.EncodeToString(value), nil
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
			return nil, "", 0, 0, fmt.Errorf("create outbox staging file: %w", err)
		}
		var stat unix.Stat_t
		if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG ||
			stat.Mode&0o777 != 0o600 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
			unix.Close(fd)
			_ = unix.Unlinkat(int(directory.Fd()), name, 0)
			return nil, "", 0, 0, fmt.Errorf("%w: staging file is unsafe", ErrCorrupt)
		}
		file := os.NewFile(uintptr(fd), name)
		if file == nil {
			unix.Close(fd)
			_ = unix.Unlinkat(int(directory.Fd()), name, 0)
			return nil, "", 0, 0, errors.New("create outbox staging handle")
		}
		return file, name, uint64(stat.Dev), stat.Ino, nil
	}
	return nil, "", 0, 0, errors.New("allocate unique outbox staging name")
}

func verifyNamedInode(directory *os.File, name string, dev, ino uint64, mode uint32) error {
	var stat unix.Stat_t
	if err := unix.Fstatat(int(directory.Fd()), name, &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Mode&0o777 != mode || stat.Uid != uint32(os.Geteuid()) ||
		stat.Nlink != 1 || uint64(stat.Dev) != dev || stat.Ino != ino {
		return fmt.Errorf("%w: outbox file identity changed", ErrCorrupt)
	}
	return nil
}

func (store *Store) installObject(name string, dev, ino uint64, digest string, size int64) error {
	if !lowerSHA256(digest) || size < 0 {
		return ErrInvalid
	}
	if err := store.validateRootIdentity(); err != nil {
		return err
	}
	if err := verifyNamedInode(store.dirs.staging, name, dev, ino, 0o400); err != nil {
		return err
	}
	shard, err := openOrCreateDirectory(int(store.dirs.sha256Dir.Fd()), digest[:2])
	if err != nil {
		return err
	}
	defer shard.Close()
	if err := validateDirectoryLink(store.dirs.sha256Dir, digest[:2], shard, store.rootDev); err != nil {
		return err
	}
	target := digest[2:]
	err = unix.Renameat2(int(store.dirs.staging.Fd()), name, int(shard.Fd()), target, unix.RENAME_NOREPLACE)
	if errors.Is(err, unix.EEXIST) {
		if verifyErr := verifyObject(shard, target, digest, size); verifyErr != nil {
			return verifyErr
		}
		if unlinkErr := unix.Unlinkat(int(store.dirs.staging.Fd()), name, 0); unlinkErr != nil && !errors.Is(unlinkErr, unix.ENOENT) {
			return fmt.Errorf("remove duplicate outbox stage: %w", unlinkErr)
		}
	} else if err != nil {
		return fmt.Errorf("install outbox object: %w", err)
	} else if verifyErr := verifyObject(shard, target, digest, size); verifyErr != nil {
		return verifyErr
	}
	if err := unix.Fsync(int(shard.Fd())); err != nil {
		return fmt.Errorf("sync outbox object directory: %w", err)
	}
	if err := unix.Fsync(int(store.dirs.staging.Fd())); err != nil {
		return fmt.Errorf("sync outbox staging directory: %w", err)
	}
	if err := validateDirectoryLink(store.dirs.sha256Dir, digest[:2], shard, store.rootDev); err != nil {
		return err
	}
	return store.validateRootIdentity()
}

func verifyObject(directory *os.File, name, digest string, size int64) error {
	file, err := openVerifiedObject(directory, name, digest, size)
	if err != nil {
		return err
	}
	return file.Close()
}

func openVerifiedObject(directory *os.File, name, digest string, size int64) (*os.File, error) {
	fd, err := unix.Openat(int(directory.Fd()), name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("%w: open outbox object", ErrCorrupt)
	}
	file := os.NewFile(uintptr(fd), name)
	if file == nil {
		unix.Close(fd)
		return nil, fmt.Errorf("%w: object handle is unavailable", ErrCorrupt)
	}
	failed := true
	defer func() {
		if failed {
			_ = file.Close()
		}
	}()
	var before, after unix.Stat_t
	if err := unix.Fstat(fd, &before); err != nil || before.Mode&unix.S_IFMT != unix.S_IFREG ||
		before.Mode&0o777 != 0o400 || before.Uid != uint32(os.Geteuid()) || before.Nlink != 1 || before.Size != size {
		return nil, fmt.Errorf("%w: outbox object metadata disagrees", ErrCorrupt)
	}
	hasher := sha256.New()
	written, err := io.Copy(hasher, io.LimitReader(file, size+1))
	if err != nil || written != size || hex.EncodeToString(hasher.Sum(nil)) != digest {
		return nil, fmt.Errorf("%w: outbox object bytes disagree", ErrCorrupt)
	}
	if err := unix.Fstat(fd, &after); err != nil || before.Dev != after.Dev || before.Ino != after.Ino ||
		before.Size != after.Size || before.Nlink != after.Nlink {
		return nil, fmt.Errorf("%w: outbox object changed while reading", ErrCorrupt)
	}
	var pathStat unix.Stat_t
	if err := unix.Fstatat(int(directory.Fd()), name, &pathStat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		pathStat.Dev != after.Dev || pathStat.Ino != after.Ino || pathStat.Nlink != 1 {
		return nil, fmt.Errorf("%w: outbox object pathname changed while reading", ErrCorrupt)
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("%w: rewind outbox object", ErrCorrupt)
	}
	failed = false
	return file, nil
}

func (store *Store) openObject(digest string, size int64) (*os.File, error) {
	if !lowerSHA256(digest) || size < 0 {
		return nil, ErrInvalid
	}
	shardFD, err := unix.Openat(int(store.dirs.sha256Dir.Fd()), digest[:2],
		unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("%w: object shard is unavailable", ErrCorrupt)
	}
	shard := os.NewFile(uintptr(shardFD), digest[:2])
	if shard == nil {
		unix.Close(shardFD)
		return nil, fmt.Errorf("%w: object shard handle is unavailable", ErrCorrupt)
	}
	defer shard.Close()
	if err := validateDirectoryLink(store.dirs.sha256Dir, digest[:2], shard, store.rootDev); err != nil {
		return nil, err
	}
	return openVerifiedObject(shard, digest[2:], digest, size)
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
