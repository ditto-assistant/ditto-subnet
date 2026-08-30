package codingrelayjournal

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"
	"golang.org/x/sys/unix"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

// Open locks and recovers one private relay journal root.
func Open(config Config) (*Store, error) {
	if config.MaxTotalBytes <= 0 || config.MaxTotalBytes > maximumRootBytes ||
		config.MaxEntries <= 0 || config.MaxEntries > maximumEntries {
		return nil, fmt.Errorf("%w: journal configuration is outside hard bounds", ErrInvalid)
	}
	if err := config.Policy.Validate(); err != nil {
		return nil, fmt.Errorf("%w: inference policy", ErrInvalid)
	}
	policyEntries := uint64(config.Policy.MaxRequests) + uint64(config.Policy.MaxRetries)
	if uint64(config.MaxEntries) > policyEntries {
		return nil, fmt.Errorf("%w: entry bound exceeds the locked policy", ErrInvalid)
	}
	dirs, leaf, dev, ino, err := openDirectoryCapabilities(config.Root)
	if err != nil {
		return nil, err
	}
	store := &Store{
		config: config, root: config.Root, dirs: dirs, rootLeaf: leaf, rootDev: dev, rootIno: ino,
		fsync: unix.Fsync,
	}
	if err := store.recover(); err != nil {
		_ = store.Close()
		return nil, err
	}
	return store, nil
}

// Load returns a deep-owned snapshot and rejects cross-binding reuse.
func (store *Store) Load(
	ctx context.Context,
	binding codingrelay.Binding,
) (codingrelay.JournalSnapshot, error) {
	if store == nil || ctx == nil || ctx.Err() != nil {
		return codingrelay.JournalSnapshot{}, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if ctx.Err() != nil || validateBinding(store.config.Policy, binding) != nil {
		return codingrelay.JournalSnapshot{}, ErrInvalid
	}
	if err := store.checkOpen(); err != nil {
		return codingrelay.JournalSnapshot{}, err
	}
	if store.state == nil {
		return codingrelay.JournalSnapshot{}, nil
	}
	if !bindingsEqual(*store.state.Binding, binding) {
		return codingrelay.JournalSnapshot{}, ErrConflict
	}
	snapshot := codingrelay.JournalSnapshot{
		Binding: copyBinding(store.state.Binding),
		Revoked: store.state.Revoked,
		Entries: make([]codingrelay.JournalEntry, len(store.entries)),
	}
	for index := range store.entries {
		entry, err := cloneJournalEntry(store.entries[index].Entry)
		if err != nil {
			return codingrelay.JournalSnapshot{}, ErrCorrupt
		}
		snapshot.Entries[index] = entry
	}
	return snapshot, nil
}

// Begin durably records one exact dispatch before provider activity.
func (store *Store) Begin(
	ctx context.Context,
	binding codingrelay.Binding,
	dispatch codingrelay.DispatchRecord,
) error {
	if store == nil || ctx == nil || ctx.Err() != nil {
		return ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if ctx.Err() != nil || validateBinding(store.config.Policy, binding) != nil {
		return ErrInvalid
	}
	if err := store.checkOpen(); err != nil {
		return err
	}
	ownedDispatch, err := cloneDispatch(dispatch)
	if err != nil {
		return ErrInvalid
	}
	if err := validateDispatchAuthority(store.config.Policy, binding, store.entries, ownedDispatch); err != nil {
		return err
	}
	if store.state != nil && !bindingsEqual(*store.state.Binding, binding) {
		return ErrConflict
	}
	if ownedDispatch.Sequence <= uint32(len(store.entries)) {
		existing := store.entries[ownedDispatch.Sequence-1].Entry.Dispatch
		if dispatchesEqual(existing, ownedDispatch) {
			return nil
		}
		return ErrConflict
	}
	if store.state != nil && store.state.Revoked {
		return ErrState
	}
	if ownedDispatch.Sequence != uint32(len(store.entries)+1) || len(store.entries) >= store.config.MaxEntries ||
		validateDispatchTransition(store.entries, ownedDispatch) != nil {
		return ErrState
	}
	entry := entryRecord{
		Schema: entrySchema, Generation: 1, Sequence: ownedDispatch.Sequence,
		Entry: codingrelay.JournalEntry{Dispatch: ownedDispatch},
	}
	entryBody, err := entryRecordBytes(&entry)
	if err != nil {
		return err
	}
	storedEntry, err := cloneEntryRecord(&entry)
	if err != nil {
		return ErrInvalid
	}
	var state stateRecord
	var stateBody []byte
	if store.state == nil {
		ownedBinding, cloneErr := cloneBinding(binding)
		if cloneErr != nil {
			return ErrInvalid
		}
		state = stateRecord{Schema: stateSchema, Generation: 1, Binding: &ownedBinding}
		stateBody, err = stateRecordBytes(&state)
		if err != nil {
			return err
		}
	}
	// Reserve the maximum terminal record while the dispatch marker exists, so
	// an admitted provider result cannot be stranded by local journal capacity.
	// Also reserve remaining state.json headroom so Revoke can grow the binding
	// record after entries have consumed the configured budget.
	required := int64(len(entryBody)) + maximumEntryBytes + store.stateGrowthHeadroom(stateBody)
	if stateBody != nil {
		required += int64(len(stateBody))
	}
	if !store.hasCapacity(required) {
		return ErrCapacity
	}
	if stateBody != nil {
		committed, installErr := store.installRecord(store.dirs.root, "state.json", stateBody, false)
		if committed {
			store.state = cloneStateRecord(&state)
			store.stateBytes = int64(len(stateBody))
			store.physicalBytes += int64(len(stateBody))
		}
		if installErr != nil {
			return installErr
		}
	}
	name := entryName(ownedDispatch.Sequence)
	committed, installErr := store.installRecord(store.dirs.entries, name, entryBody, false)
	if committed {
		store.entries = append(store.entries, *storedEntry)
		store.entryBytes = append(store.entryBytes, int64(len(entryBody)))
		store.physicalBytes += int64(len(entryBody))
	}
	if installErr != nil {
		return installErr
	}
	return nil
}

// Complete atomically replaces the final dispatch marker with its exact
// trusted settlement, receipt, private projection, and miner replay response.
func (store *Store) Complete(
	ctx context.Context,
	binding codingrelay.Binding,
	entry codingrelay.JournalEntry,
) error {
	if store == nil || ctx == nil || ctx.Err() != nil || !entry.Completed {
		return ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if ctx.Err() != nil || validateBinding(store.config.Policy, binding) != nil {
		return ErrInvalid
	}
	if err := store.checkOpen(); err != nil {
		return err
	}
	if store.state == nil {
		return ErrState
	}
	ownedEntry, err := cloneJournalEntry(entry)
	if err != nil || validateCompletedEntry(store.config.Policy, *store.state.Binding, ownedEntry) != nil {
		return ErrInvalid
	}
	if !bindingsEqual(*store.state.Binding, binding) {
		return ErrConflict
	}
	sequence := ownedEntry.Dispatch.Sequence
	if sequence == 0 || int(sequence) > len(store.entries) {
		return ErrState
	}
	current := store.entries[sequence-1]
	if current.Entry.Completed {
		if journalEntriesEqual(current.Entry, ownedEntry) {
			return nil
		}
		return ErrConflict
	}
	if store.state.Revoked {
		return ErrState
	}
	if int(sequence) != len(store.entries) || !dispatchesEqual(current.Entry.Dispatch, ownedEntry.Dispatch) {
		return ErrConflict
	}
	replacement := entryRecord{
		Schema: entrySchema, Generation: current.Generation + 1, Sequence: sequence, Entry: ownedEntry,
	}
	body, err := entryRecordBytes(&replacement)
	if err != nil {
		return err
	}
	storedEntry, err := cloneEntryRecord(&replacement)
	if err != nil {
		return ErrInvalid
	}
	if err := validateCompletedUsage(store.config.Policy, binding, store.entries, &ownedEntry); err != nil {
		return err
	}
	if !store.hasCapacity(replacementDelta(store.entryBytes[sequence-1], int64(len(body)))) {
		return ErrCapacity
	}
	committed, installErr := store.installRecord(store.dirs.entries, entryName(sequence), body, true)
	if committed {
		oldBytes := store.entryBytes[sequence-1]
		store.entries[sequence-1] = *storedEntry
		store.entryBytes[sequence-1] = int64(len(body))
		store.physicalBytes += int64(len(body)) - oldBytes
	}
	if installErr != nil {
		return installErr
	}
	return nil
}

// Revoke durably closes admission. It is exact-binding idempotent.
func (store *Store) Revoke(ctx context.Context, binding codingrelay.Binding) error {
	if store == nil || ctx == nil || ctx.Err() != nil {
		return ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if ctx.Err() != nil || validateBinding(store.config.Policy, binding) != nil {
		return ErrInvalid
	}
	if err := store.checkOpen(); err != nil {
		return err
	}
	if store.state != nil && !bindingsEqual(*store.state.Binding, binding) {
		return ErrConflict
	}
	if store.state != nil && store.state.Revoked {
		return nil
	}
	var next stateRecord
	replace := store.state != nil
	if replace {
		next = *cloneStateRecord(store.state)
		next.Generation++
		next.Revoked = true
	} else {
		ownedBinding, err := cloneBinding(binding)
		if err != nil {
			return ErrInvalid
		}
		next = stateRecord{Schema: stateSchema, Generation: 1, Binding: &ownedBinding, Revoked: true}
	}
	body, err := stateRecordBytes(&next)
	if err != nil {
		return err
	}
	if !store.hasCapacity(replacementDelta(store.stateBytes, int64(len(body)))) {
		return ErrCapacity
	}
	committed, installErr := store.installRecord(store.dirs.root, "state.json", body, replace)
	if committed {
		oldBytes := store.stateBytes
		store.state = cloneStateRecord(&next)
		store.stateBytes = int64(len(body))
		store.physicalBytes += int64(len(body)) - oldBytes
	}
	if installErr != nil {
		return installErr
	}
	return nil
}

// Close releases the process-exclusive root lock and directory capabilities.
func (store *Store) Close() error {
	if store == nil {
		return nil
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return store.closeErr
	}
	store.closed = true
	var closeErrors []error
	if store.syncPending {
		if err := store.syncJournalDirectories(); err != nil {
			closeErrors = append(closeErrors, err)
		} else {
			store.syncPending = false
		}
	}
	if store.dirs.root != nil {
		closeErrors = append(closeErrors, unix.Flock(int(store.dirs.root.Fd()), unix.LOCK_UN))
	}
	for _, file := range []*os.File{store.dirs.entries, store.dirs.staging, store.dirs.root, store.dirs.parent} {
		if file != nil {
			closeErrors = append(closeErrors, file.Close())
		}
	}
	store.closeErr = errors.Join(closeErrors...)
	return store.closeErr
}

func (store *Store) recover() error {
	if err := store.validateRootIdentity(); err != nil {
		return err
	}
	if err := store.validateRootEntries(); err != nil {
		return err
	}
	if err := store.clearAbandonedStaging(); err != nil {
		return err
	}
	stateBody, stateSize, stateErr := readOptionalRecord(store.dirs.root, "state.json", maximumStateBytes)
	if stateErr != nil {
		return stateErr
	}
	if stateBody != nil {
		state, err := decodeStateRecord(stateBody)
		if err != nil || validateBinding(store.config.Policy, *state.Binding) != nil {
			if err == nil {
				err = ErrCorrupt
			}
			return err
		}
		store.state = state
		store.stateBytes = stateSize
		store.physicalBytes += stateSize
	}
	if err := store.loadEntries(); err != nil {
		return err
	}
	if store.state == nil && len(store.entries) != 0 {
		return fmt.Errorf("%w: entries have no immutable binding", ErrCorrupt)
	}
	return nil
}

func (store *Store) validateRootEntries() error {
	if _, err := store.dirs.root.Seek(0, io.SeekStart); err != nil {
		return err
	}
	entries, err := store.dirs.root.ReadDir(4)
	if err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("scan relay journal root: %w", err)
	}
	if len(entries) > 3 {
		return fmt.Errorf("%w: unexpected root entries", ErrCorrupt)
	}
	for _, entry := range entries {
		switch entry.Name() {
		case ".staging", "entries":
			if !entry.IsDir() {
				return fmt.Errorf("%w: journal directory entry changed type", ErrCorrupt)
			}
		case "state.json":
			if entry.IsDir() {
				return fmt.Errorf("%w: state record changed type", ErrCorrupt)
			}
		default:
			return fmt.Errorf("%w: unexpected root entry", ErrCorrupt)
		}
	}
	return nil
}

func (store *Store) clearAbandonedStaging() error {
	if _, err := store.dirs.staging.Seek(0, io.SeekStart); err != nil {
		return err
	}
	entries, err := store.dirs.staging.ReadDir(store.config.MaxEntries*2 + 33)
	if err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("scan relay journal staging: %w", err)
	}
	if len(entries) > store.config.MaxEntries*2+32 {
		return ErrCapacity
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !validStagingName(name) {
			return fmt.Errorf("%w: unsafe staging entry", ErrCorrupt)
		}
		if err := verifyRegularRecord(store.dirs.staging, name); err != nil {
			return fmt.Errorf("%w: unsafe abandoned staging record", ErrCorrupt)
		}
		if err := unix.Unlinkat(int(store.dirs.staging.Fd()), name, 0); err != nil {
			return fmt.Errorf("remove abandoned journal staging record: %w", err)
		}
	}
	if len(entries) != 0 {
		if err := unix.Fsync(int(store.dirs.staging.Fd())); err != nil {
			return fmt.Errorf("sync relay journal staging directory: %w", err)
		}
	}
	return nil
}

func (store *Store) loadEntries() error {
	if _, err := store.dirs.entries.Seek(0, io.SeekStart); err != nil {
		return err
	}
	directoryEntries, err := store.dirs.entries.ReadDir(store.config.MaxEntries + 1)
	if err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("scan relay journal entries: %w", err)
	}
	if len(directoryEntries) > store.config.MaxEntries {
		return ErrCapacity
	}
	numbers := make([]int, 0, len(directoryEntries))
	for _, entry := range directoryEntries {
		sequence, ok := parseEntryName(entry.Name())
		if entry.IsDir() || !ok {
			return fmt.Errorf("%w: unexpected journal entry", ErrCorrupt)
		}
		numbers = append(numbers, sequence)
	}
	sort.Ints(numbers)
	for index, sequence := range numbers {
		if sequence != index+1 {
			return fmt.Errorf("%w: journal sequence is not contiguous", ErrCorrupt)
		}
		body, size, err := readVerifiedFile(store.dirs.entries, entryName(uint32(sequence)), maximumEntryBytes)
		if err != nil {
			return err
		}
		var binding codingrelay.Binding
		if store.state != nil && store.state.Binding != nil {
			binding = *store.state.Binding
		}
		record, err := decodeEntryRecord(body, store.config.Policy, binding)
		if err != nil || record.Sequence != uint32(sequence) {
			return fmt.Errorf("%w: journal entry disagrees", ErrCorrupt)
		}
		if !record.Entry.Completed && index != len(numbers)-1 {
			return fmt.Errorf("%w: only the final dispatch may be incomplete", ErrCorrupt)
		}
		if err := validateDispatchAuthority(
			store.config.Policy, binding, store.entries, record.Entry.Dispatch,
		); err != nil {
			return fmt.Errorf("%w: journal dispatch authority disagrees", ErrCorrupt)
		}
		if err := validateDispatchTransition(store.entries, record.Entry.Dispatch); err != nil {
			return fmt.Errorf("%w: journal request chain disagrees", ErrCorrupt)
		}
		if record.Entry.Completed {
			if err := validateCompletedUsage(store.config.Policy, binding, store.entries, &record.Entry); err != nil {
				return fmt.Errorf("%w: journal usage exceeds signed budgets", ErrCorrupt)
			}
		}
		store.entries = append(store.entries, *record)
		store.entryBytes = append(store.entryBytes, size)
		if size > store.config.MaxTotalBytes-store.physicalBytes {
			store.physicalBytes = store.config.MaxTotalBytes + 1
		} else {
			store.physicalBytes += size
		}
	}
	return nil
}

func (store *Store) checkOpen() error {
	if store == nil || store.closed {
		return ErrClosed
	}
	if err := store.validateRootIdentity(); err != nil {
		return err
	}
	if store.syncPending {
		if err := store.syncJournalDirectories(); err != nil {
			return err
		}
		store.syncPending = false
	}
	return nil
}

func (store *Store) hasCapacity(stagingBytes int64) bool {
	return stagingBytes >= 0 && store.physicalBytes <= store.config.MaxTotalBytes &&
		stagingBytes <= store.config.MaxTotalBytes-store.physicalBytes
}

func replacementDelta(existing, next int64) int64 {
	if next <= existing {
		return 0
	}
	return next - existing
}

func (store *Store) stateGrowthHeadroom(pendingState []byte) int64 {
	current := store.stateBytes
	if pendingState != nil {
		current = int64(len(pendingState))
	}
	if current >= maximumStateBytes {
		return 0
	}
	return maximumStateBytes - current
}

func (store *Store) installRecord(
	directory *os.File,
	target string,
	body []byte,
	replace bool,
) (bool, error) {
	if len(body) == 0 || int64(len(body)) > maximumEntryBytes {
		return false, ErrInvalid
	}
	if err := store.validateRootIdentity(); err != nil {
		return false, err
	}
	file, stageName, dev, ino, err := newStagingFile(store.dirs.staging, "stage-")
	if err != nil {
		return false, err
	}
	renamed := false
	defer func() {
		_ = file.Close()
		if !renamed && verifyNamedInode(store.dirs.staging, stageName, dev, ino, 0o600) == nil {
			_ = unix.Unlinkat(int(store.dirs.staging.Fd()), stageName, 0)
		}
	}()
	if err := writeAll(file, body); err != nil || file.Sync() != nil || file.Close() != nil {
		return false, errors.New("persist relay journal record bytes")
	}
	if err := verifyNamedInode(store.dirs.staging, stageName, dev, ino, 0o600); err != nil {
		return false, err
	}
	if replace {
		if err := verifyRegularRecord(directory, target); err != nil {
			return false, err
		}
		err = unix.Renameat(int(store.dirs.staging.Fd()), stageName, int(directory.Fd()), target)
	} else {
		err = unix.Renameat2(int(store.dirs.staging.Fd()), stageName, int(directory.Fd()), target, unix.RENAME_NOREPLACE)
		if errors.Is(err, unix.EEXIST) {
			return false, ErrConflict
		}
	}
	if err != nil {
		return false, fmt.Errorf("install relay journal record: %w", err)
	}
	renamed = true
	store.syncPending = true
	if err := verifyNamedInode(directory, target, dev, ino, 0o600); err != nil {
		return true, err
	}
	if err := store.syncJournalDirectories(); err != nil {
		return true, err
	}
	if err := store.validateRootIdentity(); err != nil {
		return true, err
	}
	store.syncPending = false
	return true, nil
}

func (store *Store) syncJournalDirectories() error {
	for _, directory := range []*os.File{store.dirs.root, store.dirs.entries, store.dirs.staging} {
		if err := store.fsync(int(directory.Fd())); err != nil {
			return fmt.Errorf("sync relay journal directory: %w", err)
		}
	}
	return nil
}

func verifyRegularRecord(directory *os.File, name string) error {
	var stat unix.Stat_t
	if err := unix.Fstatat(int(directory.Fd()), name, &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Mode&0o777 != 0o600 ||
		stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 {
		return fmt.Errorf("%w: existing journal record is unsafe", ErrCorrupt)
	}
	return nil
}

func readOptionalRecord(directory *os.File, name string, maximum int64) ([]byte, int64, error) {
	var stat unix.Stat_t
	err := unix.Fstatat(int(directory.Fd()), name, &stat, unix.AT_SYMLINK_NOFOLLOW)
	if errors.Is(err, unix.ENOENT) {
		return nil, 0, nil
	}
	if err != nil {
		return nil, 0, fmt.Errorf("inspect optional journal record: %w", err)
	}
	return readVerifiedFile(directory, name, maximum)
}

func stateRecordBytes(record *stateRecord) ([]byte, error) {
	copy := *cloneStateRecord(record)
	copy.ChecksumSHA256 = ""
	payload, err := json.Marshal(copy)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(payload)
	record.ChecksumSHA256 = hex.EncodeToString(digest[:])
	body, err := json.Marshal(record)
	if err != nil || len(body)+1 > maximumStateBytes {
		return nil, fmt.Errorf("%w: state record exceeds its bound", ErrInvalid)
	}
	return append(body, '\n'), nil
}

func entryRecordBytes(record *entryRecord) ([]byte, error) {
	cloned, err := cloneEntryRecord(record)
	if err != nil {
		return nil, err
	}
	copy := *cloned
	copy.ChecksumSHA256 = ""
	payload, err := json.Marshal(copy)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(payload)
	record.ChecksumSHA256 = hex.EncodeToString(digest[:])
	body, err := json.Marshal(record)
	if err != nil || len(body)+1 > maximumEntryBytes {
		return nil, fmt.Errorf("%w: entry record exceeds its bound", ErrCapacity)
	}
	return append(body, '\n'), nil
}

func decodeStateRecord(body []byte) (*stateRecord, error) {
	var record stateRecord
	if err := decodeStrictRecord(body, maximumStateBytes, &record); err != nil ||
		record.Schema != stateSchema || record.Generation == 0 || record.Generation > 2 ||
		(record.Generation == 2 && !record.Revoked) || record.Binding == nil ||
		!lowerSHA256(record.ChecksumSHA256) {
		return nil, fmt.Errorf("%w: state record shape disagrees", ErrCorrupt)
	}
	want := record.ChecksumSHA256
	copy := *cloneStateRecord(&record)
	copy.ChecksumSHA256 = ""
	payload, err := json.Marshal(copy)
	if err != nil || sha256Hex(payload) != want {
		return nil, fmt.Errorf("%w: state checksum disagrees", ErrCorrupt)
	}
	return cloneStateRecord(&record), nil
}

func decodeEntryRecord(
	body []byte,
	policy codingcontract.InferencePolicy,
	binding codingrelay.Binding,
) (*entryRecord, error) {
	var record entryRecord
	if err := decodeStrictRecord(body, maximumEntryBytes, &record); err != nil ||
		record.Schema != entrySchema || record.Generation == 0 || record.Generation > 2 ||
		(record.Entry.Completed && record.Generation != 2) ||
		(!record.Entry.Completed && record.Generation != 1) || record.Sequence == 0 ||
		record.Entry.Dispatch.Sequence != record.Sequence || !lowerSHA256(record.ChecksumSHA256) {
		return nil, fmt.Errorf("%w: entry record shape disagrees", ErrCorrupt)
	}
	want := record.ChecksumSHA256
	cloned, err := cloneEntryRecord(&record)
	if err != nil {
		return nil, fmt.Errorf("%w: entry ownership disagrees", ErrCorrupt)
	}
	copy := *cloned
	copy.ChecksumSHA256 = ""
	payload, err := json.Marshal(copy)
	if err != nil || sha256Hex(payload) != want {
		return nil, fmt.Errorf("%w: entry checksum disagrees", ErrCorrupt)
	}
	if record.Entry.Completed {
		err = validateCompletedEntry(policy, binding, record.Entry)
	} else {
		zero := codingrelay.JournalEntry{Dispatch: record.Entry.Dispatch}
		if !reflect.DeepEqual(record.Entry, zero) {
			err = errors.New("incomplete entry carries completion fields")
		} else {
			err = validateDispatch(policy, record.Entry.Dispatch)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("%w: entry authority disagrees", ErrCorrupt)
	}
	return cloneEntryRecord(&record)
}

func decodeStrictRecord(body []byte, maximum int, target any) error {
	if err := codingcontract.ValidateJSONDocument(body, maximum); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return errors.New("journal record contains trailing data")
	}
	return nil
}

func validateDispatch(policy codingcontract.InferencePolicy, dispatch codingrelay.DispatchRecord) error {
	if dispatch.Sequence == 0 || dispatch.RequestSequence == 0 || dispatch.Attempt == 0 ||
		dispatch.Sequence > policy.MaxRequests+policy.MaxRetries ||
		dispatch.RequestSequence > policy.MaxRequests || dispatch.Attempt > policy.MaxAttemptsPerRequest ||
		!canonicalUUID(dispatch.RequestID) || !lowerSHA256(dispatch.MinerRequestSHA256) ||
		!lowerSHA256(dispatch.LockedRequestSHA256) {
		return ErrInvalid
	}
	minerDigest, err := codingcontract.InferenceMinerRequestSHA256(policy, dispatch.MinerRequest)
	if err != nil || minerDigest != dispatch.MinerRequestSHA256 {
		return ErrInvalid
	}
	lockedDigest, err := codingcontract.InferenceLockedRequestSHA256(policy, dispatch.LockedRequest)
	if err != nil || lockedDigest != dispatch.LockedRequestSHA256 {
		return ErrInvalid
	}
	return nil
}

func validateDispatchAuthority(
	policy codingcontract.InferencePolicy,
	binding codingrelay.Binding,
	entries []entryRecord,
	dispatch codingrelay.DispatchRecord,
) error {
	if validateBinding(policy, binding) != nil || validateDispatch(policy, dispatch) != nil ||
		dispatch.RequestSequence > binding.RequestBudget {
		return ErrInvalid
	}
	usedPrompt, usedCompletion, usedCost := uint64(0), uint64(0), uint64(0)
	for _, record := range entries {
		if usedPrompt > ^uint64(0)-record.Entry.Receipt.PromptTokens ||
			usedCompletion > ^uint64(0)-record.Entry.Receipt.CompletionTokens ||
			usedCost > ^uint64(0)-record.Entry.Receipt.CostUSDMicros {
			return ErrInvalid
		}
		usedPrompt += record.Entry.Receipt.PromptTokens
		usedCompletion += record.Entry.Receipt.CompletionTokens
		usedCost += record.Entry.Receipt.CostUSDMicros
	}
	if usedPrompt >= binding.PromptTokenBudget || usedCompletion >= binding.CompletionTokenBudget ||
		usedCost >= policy.MaxCostUSDMicros {
		return ErrCapacity
	}
	remaining := binding.CompletionTokenBudget - usedCompletion
	effectiveMiner := dispatch.MinerRequest
	if effectiveMiner.MaxCompletionTokens > remaining {
		effectiveMiner.MaxCompletionTokens = remaining
	}
	expectedLocked, err := codingcontract.LockInferenceRequest(policy, effectiveMiner)
	if err != nil || !reflect.DeepEqual(expectedLocked, dispatch.LockedRequest) {
		return ErrConflict
	}
	return nil
}

func validateCompletedUsage(
	policy codingcontract.InferencePolicy,
	binding codingrelay.Binding,
	entries []entryRecord,
	next *codingrelay.JournalEntry,
) error {
	prompt, completion, cost := uint64(0), uint64(0), uint64(0)
	add := func(entry codingrelay.JournalEntry) error {
		if !entry.Completed {
			return nil
		}
		if prompt > ^uint64(0)-entry.Receipt.PromptTokens ||
			completion > ^uint64(0)-entry.Receipt.CompletionTokens ||
			cost > ^uint64(0)-entry.Receipt.CostUSDMicros {
			return ErrCapacity
		}
		prompt += entry.Receipt.PromptTokens
		completion += entry.Receipt.CompletionTokens
		cost += entry.Receipt.CostUSDMicros
		return nil
	}
	for _, record := range entries {
		if int(record.Sequence) == int(next.Dispatch.Sequence) {
			continue
		}
		if err := add(record.Entry); err != nil {
			return err
		}
	}
	if err := add(*next); err != nil {
		return err
	}
	if prompt > binding.PromptTokenBudget || completion > binding.CompletionTokenBudget ||
		cost > policy.MaxCostUSDMicros {
		return ErrCapacity
	}
	return nil
}

func validateDispatchTransition(entries []entryRecord, dispatch codingrelay.DispatchRecord) error {
	if dispatch.Sequence != uint32(len(entries)+1) {
		return ErrState
	}
	for _, record := range entries {
		if record.Entry.Dispatch.RequestID == dispatch.RequestID &&
			record.Entry.Dispatch.RequestSequence != dispatch.RequestSequence {
			return ErrConflict
		}
	}
	if len(entries) == 0 {
		if dispatch.RequestSequence != 1 || dispatch.Attempt != 1 {
			return ErrState
		}
		return nil
	}
	previous := entries[len(entries)-1].Entry
	if !previous.Completed {
		return ErrState
	}
	if previous.Receipt.Outcome == codingcontract.InferenceReceiptFreeRetry {
		if dispatch.RequestSequence != previous.Dispatch.RequestSequence ||
			dispatch.Attempt != previous.Dispatch.Attempt+1 ||
			dispatch.RequestID != previous.Dispatch.RequestID ||
			dispatch.MinerRequestSHA256 != previous.Dispatch.MinerRequestSHA256 ||
			dispatch.LockedRequestSHA256 != previous.Dispatch.LockedRequestSHA256 ||
			!reflect.DeepEqual(dispatch.MinerRequest, previous.Dispatch.MinerRequest) ||
			!reflect.DeepEqual(dispatch.LockedRequest, previous.Dispatch.LockedRequest) {
			return ErrConflict
		}
		return nil
	}
	if previous.Receipt.Outcome != codingcontract.InferenceReceiptComplete ||
		dispatch.RequestSequence != previous.Dispatch.RequestSequence+1 || dispatch.Attempt != 1 {
		return ErrState
	}
	return nil
}

func validateBinding(policy codingcontract.InferencePolicy, binding codingrelay.Binding) error {
	digest, err := codingcontract.InferencePolicySHA256(policy)
	issuedAt := binding.IssuedAt.UTC()
	deadline := binding.Deadline.UTC()
	if err != nil || !validIdentifier(binding.AttemptID, 256) ||
		!lowerSHA256(binding.AgentArtifactSHA256) || !validIdentifier(binding.HarnessInstanceID, 256) ||
		!canonicalUUID(binding.TicketID) || !validIdentifier(binding.CaseID, 256) ||
		!validIdentifier(binding.ProfileCapabilityID, 256) || !canonicalUUID(binding.GrantID) ||
		binding.Generation == 0 || binding.Generation > 1<<31-1 || binding.InferenceGrantSHA256 != digest ||
		issuedAt.IsZero() || deadline.IsZero() || !deadline.After(issuedAt) ||
		deadline.After(issuedAt.Add(2*time.Hour)) || binding.RequestBudget == 0 ||
		binding.RequestBudget > policy.MaxRequests || binding.PromptTokenBudget == 0 ||
		binding.PromptTokenBudget > policy.MaxPromptTokens || binding.CompletionTokenBudget == 0 ||
		binding.CompletionTokenBudget > policy.MaxCompletionTokens {
		return ErrInvalid
	}
	return nil
}

func validateCompletedEntry(
	policy codingcontract.InferencePolicy,
	binding codingrelay.Binding,
	entry codingrelay.JournalEntry,
) error {
	if !entry.Completed || validateDispatch(policy, entry.Dispatch) != nil ||
		validateBinding(policy, binding) != nil ||
		entry.Receipt.Sequence != entry.Dispatch.Sequence ||
		entry.Receipt.RequestSequence != entry.Dispatch.RequestSequence ||
		entry.Receipt.Attempt != entry.Dispatch.Attempt ||
		entry.Receipt.RequestID != entry.Dispatch.RequestID ||
		entry.Receipt.LockedRequestSHA256 != entry.Dispatch.LockedRequestSHA256 ||
		entry.Receipt.Schema != codingcontract.InferenceReceiptSchema ||
		entry.Receipt.PromptSHA256 != policy.PromptSHA256 ||
		entry.Receipt.ToolSchemaSHA256 != policy.ToolSchemaSHA256 ||
		entry.Receipt.Model != policy.Model || entry.Receipt.ProviderRoute != policy.ProviderRoute ||
		entry.Receipt.ProviderRouteProfile != policy.ProviderRouteProfile || entry.Receipt.FallbackUsed ||
		entry.Settlement.Validate(policy) != nil ||
		entry.Settlement.ValidateAgainstReceipt(policy, entry.Receipt) != nil ||
		entry.Settlement.TicketID != binding.TicketID || entry.Settlement.CaseID != binding.CaseID ||
		entry.Settlement.ProfileCapabilityID != binding.ProfileCapabilityID ||
		entry.Settlement.InferenceGrantSHA256 != binding.InferenceGrantSHA256 ||
		entry.Settlement.GrantID != binding.GrantID || entry.Settlement.Generation != binding.Generation ||
		entry.Settlement.CompletionTokens > entry.Dispatch.LockedRequest.MaxCompletionTokens {
		return ErrInvalid
	}
	switch entry.Receipt.Outcome {
	case codingcontract.InferenceReceiptComplete:
		if len(entry.NormalizedResponse) == 0 || len(entry.MinerResponse) == 0 ||
			len(entry.FailureResponseProjection) != 0 {
			return ErrInvalid
		}
		normalized, err := codingcontract.ParseInferenceNormalizedResponse(entry.NormalizedResponse, policy)
		if err != nil {
			return ErrInvalid
		}
		digest, err := codingcontract.InferenceNormalizedResponseSHA256(policy, normalized)
		if err != nil || entry.Receipt.ResponseSHA256 == nil || digest != *entry.Receipt.ResponseSHA256 ||
			entry.Receipt.ProviderGenerationID == nil || normalized.ID != *entry.Receipt.ProviderGenerationID ||
			normalized.Usage.PromptTokens != entry.Receipt.PromptTokens ||
			normalized.Usage.CompletionTokens != entry.Receipt.CompletionTokens ||
			normalized.Usage.TotalTokens != entry.Receipt.TotalTokens ||
			normalized.Usage.CostUSDMicros != entry.Receipt.CostUSDMicros {
			return ErrInvalid
		}
		miner, err := codingcontract.ParseInferenceMinerResponse(entry.MinerResponse, policy)
		if err != nil || miner.ID != normalized.ID || miner.Model != normalized.Model ||
			!reflect.DeepEqual(miner.Choices, normalized.Choices) ||
			miner.Usage.PromptTokens != normalized.Usage.PromptTokens ||
			miner.Usage.CompletionTokens != normalized.Usage.CompletionTokens ||
			miner.Usage.TotalTokens != normalized.Usage.TotalTokens {
			return ErrInvalid
		}
	case codingcontract.InferenceReceiptFreeRetry, codingcontract.InferenceReceiptProviderFailed:
		if len(entry.MinerResponse) != 0 || len(entry.NormalizedResponse) != 0 {
			return ErrInvalid
		}
		if entry.Receipt.ResponseDigestKind == "canonical_json_v1" {
			if len(entry.FailureResponseProjection) == 0 {
				return ErrInvalid
			}
			digest, err := codingcontract.InferenceCanonicalResponseProjectionSHA256(
				policy, entry.FailureResponseProjection,
			)
			if err != nil || entry.Receipt.ResponseSHA256 == nil || digest != *entry.Receipt.ResponseSHA256 {
				return ErrInvalid
			}
		} else if len(entry.FailureResponseProjection) != 0 {
			return ErrInvalid
		}
	default:
		return ErrInvalid
	}
	return nil
}

func cloneBinding(value codingrelay.Binding) (codingrelay.Binding, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return codingrelay.Binding{}, err
	}
	var clone codingrelay.Binding
	if err := decodeStrictRecord(body, maximumStateBytes, &clone); err != nil {
		return codingrelay.Binding{}, err
	}
	clone.IssuedAt = clone.IssuedAt.UTC()
	clone.Deadline = clone.Deadline.UTC()
	return clone, nil
}

func copyBinding(value *codingrelay.Binding) *codingrelay.Binding {
	if value == nil {
		return nil
	}
	copy := *value
	copy.IssuedAt = copy.IssuedAt.UTC()
	copy.Deadline = copy.Deadline.UTC()
	return &copy
}

func cloneDispatch(value codingrelay.DispatchRecord) (codingrelay.DispatchRecord, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return codingrelay.DispatchRecord{}, err
	}
	var clone codingrelay.DispatchRecord
	if err := decodeStrictRecord(body, maximumEntryBytes, &clone); err != nil {
		return codingrelay.DispatchRecord{}, err
	}
	return clone, nil
}

func cloneJournalEntry(value codingrelay.JournalEntry) (codingrelay.JournalEntry, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return codingrelay.JournalEntry{}, err
	}
	var clone codingrelay.JournalEntry
	if err := decodeStrictRecord(body, maximumEntryBytes, &clone); err != nil {
		return codingrelay.JournalEntry{}, err
	}
	return clone, nil
}

func cloneStateRecord(value *stateRecord) *stateRecord {
	if value == nil {
		return nil
	}
	copy := *value
	copy.Binding = copyBinding(value.Binding)
	return &copy
}

func cloneEntryRecord(value *entryRecord) (*entryRecord, error) {
	if value == nil {
		return nil, nil
	}
	copy := *value
	entry, err := cloneJournalEntry(value.Entry)
	if err != nil {
		return nil, err
	}
	copy.Entry = entry
	return &copy, nil
}

func bindingsEqual(left, right codingrelay.Binding) bool {
	leftCopy, leftErr := cloneBinding(left)
	rightCopy, rightErr := cloneBinding(right)
	return leftErr == nil && rightErr == nil && reflect.DeepEqual(leftCopy, rightCopy)
}

func dispatchesEqual(left, right codingrelay.DispatchRecord) bool {
	leftBody, leftErr := json.Marshal(left)
	rightBody, rightErr := json.Marshal(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftBody, rightBody)
}

func journalEntriesEqual(left, right codingrelay.JournalEntry) bool {
	leftBody, leftErr := json.Marshal(left)
	rightBody, rightErr := json.Marshal(right)
	return leftErr == nil && rightErr == nil && bytes.Equal(leftBody, rightBody)
}

func lowerSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func sha256Hex(body []byte) string {
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}

func entryName(sequence uint32) string { return fmt.Sprintf("%08d.json", sequence) }

func parseEntryName(name string) (int, bool) {
	if len(name) != len("00000000.json") || !strings.HasSuffix(name, ".json") {
		return 0, false
	}
	digits := strings.TrimSuffix(name, ".json")
	value, err := strconv.Atoi(digits)
	return value, err == nil && value > 0 && entryName(uint32(value)) == name
}

func validStagingName(name string) bool {
	if !strings.HasPrefix(name, "stage-") || len(name) != len("stage-")+32 {
		return false
	}
	encoded := strings.TrimPrefix(name, "stage-")
	_, err := hex.DecodeString(encoded)
	return err == nil && encoded == strings.ToLower(encoded)
}
