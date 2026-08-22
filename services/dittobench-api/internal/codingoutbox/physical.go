//go:build linux

package codingoutbox

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

const scanBatchSize = 256

func (store *Store) reconcilePhysical(ctx context.Context) error {
	if ctx == nil {
		return ErrInvalid
	}
	activeStaging := make(map[string]struct{})
	references := make(map[string]struct{})
	for _, record := range store.records {
		if record.WriterNonce != "" && record.StagingName != "" {
			activeStaging[record.StagingName] = struct{}{}
		}
		if record.Transcript != nil {
			references[record.Transcript.SHA256] = struct{}{}
		}
		if record.Frozen != nil {
			references[record.Frozen.Artifact.FrozenPatchSHA256] = struct{}{}
		}
	}
	limit := store.config.MaxAttempts*4 + 1024
	files := 0
	orphanBytes := int64(0)
	if _, err := store.dirs.staging.Seek(0, 0); err != nil {
		return err
	}
	for {
		entries, err := store.dirs.staging.ReadDir(scanBatchSize)
		for _, entry := range entries {
			if err := ctx.Err(); err != nil {
				return err
			}
			files++
			if files > limit {
				return ErrCapacity
			}
			stat, statErr := safePhysicalFile(store.dirs.staging, entry.Name(), true)
			if statErr != nil {
				return statErr
			}
			if _, active := activeStaging[entry.Name()]; !active {
				orphanBytes = boundedPhysicalSum(orphanBytes, stat.Size, store.config.MaxTotalBytes)
			}
		}
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
	}
	if _, err := store.dirs.sha256Dir.Seek(0, 0); err != nil {
		return err
	}
	shards, err := store.dirs.sha256Dir.ReadDir(257)
	if err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	if len(shards) > 256 {
		return ErrCapacity
	}
	for _, shardEntry := range shards {
		prefix := shardEntry.Name()
		if len(prefix) != 2 || !hexLeaf(prefix) || !shardEntry.IsDir() {
			return fmt.Errorf("%w: invalid object shard", ErrCorrupt)
		}
		fd, err := unix.Openat(int(store.dirs.sha256Dir.Fd()), prefix,
			unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return ErrCorrupt
		}
		shard := os.NewFile(uintptr(fd), prefix)
		if shard == nil {
			unix.Close(fd)
			return ErrCorrupt
		}
		if err := validateDirectoryLink(store.dirs.sha256Dir, prefix, shard, store.rootDev); err != nil {
			shard.Close()
			return err
		}
		for {
			entries, readErr := shard.ReadDir(scanBatchSize)
			for _, entry := range entries {
				if err := ctx.Err(); err != nil {
					shard.Close()
					return err
				}
				files++
				if files > limit {
					shard.Close()
					return ErrCapacity
				}
				digest := prefix + entry.Name()
				if len(entry.Name()) != 62 || !lowerSHA256(digest) {
					shard.Close()
					return ErrCorrupt
				}
				stat, statErr := safePhysicalFile(shard, entry.Name(), false)
				if statErr != nil {
					shard.Close()
					return statErr
				}
				if _, referenced := references[digest]; !referenced {
					orphanBytes = boundedPhysicalSum(orphanBytes, stat.Size, store.config.MaxTotalBytes)
				}
			}
			if errors.Is(readErr, io.EOF) {
				break
			}
			if readErr != nil {
				shard.Close()
				return readErr
			}
		}
		if err := shard.Close(); err != nil {
			return err
		}
	}
	store.orphanBytes = orphanBytes
	store.physicalKnown = true
	return nil
}

func boundedPhysicalSum(current, size, maximum int64) int64 {
	if current > maximum || size > maximum-current {
		return maximum + 1
	}
	return current + size
}

func safePhysicalFile(directory *os.File, name string, staging bool) (unix.Stat_t, error) {
	var stat unix.Stat_t
	if err := unix.Fstatat(int(directory.Fd()), name, &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || stat.Size < 0 {
		return unix.Stat_t{}, fmt.Errorf("%w: unsafe physical outbox entry", ErrCorrupt)
	}
	mode := uint32(0o400)
	if staging && stat.Mode&0o777 == 0o600 {
		mode = 0o600
	}
	if stat.Mode&0o777 != mode {
		return unix.Stat_t{}, fmt.Errorf("%w: physical outbox mode disagrees", ErrCorrupt)
	}
	return stat, nil
}
