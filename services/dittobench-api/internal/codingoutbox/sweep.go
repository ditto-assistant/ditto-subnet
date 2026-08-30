package codingoutbox

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

func (store *Store) Sweep(ctx context.Context) (SweepReport, error) {
	if ctx == nil || ctx.Err() != nil {
		return SweepReport{}, ErrInvalid
	}
	now := store.config.Now().UTC()
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(now); err != nil {
		return SweepReport{}, err
	}
	store.physicalKnown = false
	report := SweepReport{}
	ids := make([]string, 0, len(store.records))
	for id := range store.records {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if err := ctx.Err(); err != nil {
			return report, err
		}
		record := store.records[id]
		expired := record.State == StateReserved && !record.Binding.Deadline.After(now)
		expired = expired || record.State == StateCollecting &&
			!record.Binding.Deadline.Add(store.config.FinalizationGrace).After(now)
		if expired {
			updated := cloneRecord(record)
			updated.Generation++
			updated.State = StateExpired
			updated.WriterNonce = ""
			updated.StagingName = ""
			updated.ExpiredAtUnix = now.Unix()
			if err := store.persistRecord(updated); err != nil {
				return report, err
			}
			store.records[id] = updated
			record = updated
			report.ExpiredRecords++
		}
		deleteRecord := record.State == StateReleased &&
			now.Sub(time.Unix(record.ReleasedAtUnix, 0).Add(time.Second)) >= store.config.ReleasedRetention
		deleteRecord = deleteRecord || record.State == StateExpired &&
			now.Sub(time.Unix(record.ExpiredAtUnix, 0).Add(time.Second)) >= store.config.ExpiredRetention
		if deleteRecord {
			if err := store.removeRecordFile(id); err != nil {
				return report, err
			}
			delete(store.records, id)
			store.reserved -= record.ReservedBytes
			report.DeletedRecords++
		}
	}
	references := make(map[string]struct{})
	activeStaging := make(map[string]struct{})
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
	physicalLimit := store.config.MaxAttempts*4 + 1024
	deletedStaging, err := sweepStaging(ctx, store.dirs.staging, activeStaging, now, store.config.OrphanGrace, physicalLimit)
	if err != nil {
		return report, err
	}
	report.DeletedStagingFiles = deletedStaging
	deletedObjects, err := sweepObjects(ctx, store.dirs.sha256Dir, store.rootDev, references, now, store.config.OrphanGrace, physicalLimit)
	if err != nil {
		return report, err
	}
	report.DeletedObjects = deletedObjects
	if err := store.reconcilePhysical(ctx); err != nil {
		return report, err
	}
	report.RemainingRecords = len(store.records)
	report.RemainingReservation = store.reserved
	report.RemainingOrphanBytes = store.orphanBytes
	return report, nil
}

func (store *Store) removeRecordFile(id string) error {
	name := id + ".json"
	var stat unix.Stat_t
	if err := unix.Fstatat(int(store.dirs.records.Fd()), name, &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Mode&0o777 != 0o600 ||
		stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
		return fmt.Errorf("%w: record changed before deletion", ErrCorrupt)
	}
	if err := unix.Unlinkat(int(store.dirs.records.Fd()), name, 0); err != nil {
		return fmt.Errorf("delete outbox record: %w", err)
	}
	if err := unix.Fsync(int(store.dirs.records.Fd())); err != nil {
		return fmt.Errorf("sync deleted outbox record: %w", err)
	}
	return nil
}

func sweepStaging(
	ctx context.Context,
	directory *os.File,
	active map[string]struct{},
	now time.Time,
	grace time.Duration,
	limit int,
) (int, error) {
	if _, err := directory.Seek(0, 0); err != nil {
		return 0, err
	}
	deleted := 0
	seen := 0
	for {
		entries, readErr := directory.ReadDir(scanBatchSize)
		for _, entry := range entries {
			seen++
			if seen > limit {
				return deleted, ErrCapacity
			}
			if err := ctx.Err(); err != nil {
				return deleted, err
			}
			if _, retained := active[entry.Name()]; retained {
				continue
			}
			var stat unix.Stat_t
			if err := unix.Fstatat(int(directory.Fd()), entry.Name(), &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
				stat.Mode&unix.S_IFMT != unix.S_IFREG || (stat.Mode&0o777 != 0o600 && stat.Mode&0o777 != 0o400) ||
				stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
				return deleted, fmt.Errorf("%w: unsafe staging entry", ErrCorrupt)
			}
			modified := time.Unix(stat.Mtim.Sec, stat.Mtim.Nsec)
			if now.Sub(modified) < grace {
				continue
			}
			if err := unix.Unlinkat(int(directory.Fd()), entry.Name(), 0); err != nil {
				return deleted, err
			}
			deleted++
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return deleted, readErr
		}
	}
	if deleted > 0 {
		if err := unix.Fsync(int(directory.Fd())); err != nil {
			return deleted, err
		}
	}
	return deleted, nil
}

func sweepObjects(
	ctx context.Context,
	shaDirectory *os.File,
	rootDev uint64,
	references map[string]struct{},
	now time.Time,
	grace time.Duration,
	limit int,
) (int, error) {
	if _, err := shaDirectory.Seek(0, 0); err != nil {
		return 0, err
	}
	shards, err := shaDirectory.ReadDir(257)
	if err != nil && !errors.Is(err, io.EOF) {
		return 0, err
	}
	if len(shards) > 256 {
		return 0, ErrCapacity
	}
	deleted := 0
	seen := 0
	for _, shardEntry := range shards {
		prefix := shardEntry.Name()
		if len(prefix) != 2 || !hexLeaf(prefix) || !shardEntry.IsDir() {
			return deleted, fmt.Errorf("%w: invalid object shard", ErrCorrupt)
		}
		fd, err := unix.Openat(int(shaDirectory.Fd()), prefix,
			unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return deleted, ErrCorrupt
		}
		shard := os.NewFile(uintptr(fd), prefix)
		if shard == nil {
			unix.Close(fd)
			return deleted, ErrCorrupt
		}
		if err := validateDirectoryLink(shaDirectory, prefix, shard, rootDev); err != nil {
			shard.Close()
			return deleted, err
		}
		shardDeleted := false
		for {
			entries, readErr := shard.ReadDir(scanBatchSize)
			for _, entry := range entries {
				seen++
				if seen > limit {
					shard.Close()
					return deleted, ErrCapacity
				}
				if err := ctx.Err(); err != nil {
					shard.Close()
					return deleted, err
				}
				digest := prefix + entry.Name()
				if len(entry.Name()) != 62 || !lowerSHA256(digest) {
					shard.Close()
					return deleted, fmt.Errorf("%w: invalid object name", ErrCorrupt)
				}
				var stat unix.Stat_t
				if err := unix.Fstatat(fd, entry.Name(), &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
					stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Mode&0o777 != 0o400 ||
					stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
					shard.Close()
					return deleted, fmt.Errorf("%w: unsafe object entry", ErrCorrupt)
				}
				if _, retained := references[digest]; retained {
					continue
				}
				modified := time.Unix(stat.Mtim.Sec, stat.Mtim.Nsec)
				if now.Sub(modified) < grace {
					continue
				}
				if err := unix.Unlinkat(fd, entry.Name(), 0); err != nil {
					shard.Close()
					return deleted, err
				}
				deleted++
				shardDeleted = true
			}
			if errors.Is(readErr, io.EOF) {
				break
			}
			if readErr != nil {
				shard.Close()
				return deleted, readErr
			}
		}
		if shardDeleted {
			if err := unix.Fsync(fd); err != nil {
				shard.Close()
				return deleted, err
			}
		}
		if err := shard.Close(); err != nil {
			return deleted, err
		}
	}
	return deleted, nil
}

func hexLeaf(value string) bool {
	for _, character := range value {
		if !strings.ContainsRune("0123456789abcdef", character) {
			return false
		}
	}
	return true
}
