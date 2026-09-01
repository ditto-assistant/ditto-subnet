package codingoutbox

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
	"slices"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingevidence"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

func Open(config Config) (*Store, error) {
	if config.MaxTotalBytes <= 0 || config.MaxTotalBytes > maximumRootBytes ||
		config.MaxAttempts <= 0 || config.MaxAttempts > maximumAttempts ||
		config.FinalizationGrace < time.Minute || config.FinalizationGrace > 15*time.Minute ||
		config.OrphanGrace < time.Minute || config.OrphanGrace > 7*24*time.Hour ||
		config.ReleasedRetention < time.Minute || config.ReleasedRetention > 365*24*time.Hour ||
		config.ExpiredRetention < time.Minute || config.ExpiredRetention > 365*24*time.Hour ||
		config.ReleasedRetention < config.OrphanGrace || config.ExpiredRetention < config.OrphanGrace {
		return nil, fmt.Errorf("%w: outbox configuration is outside hard bounds", ErrInvalid)
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	dirs, leaf, dev, ino, err := openDirectoryCapabilities(config.Root)
	if err != nil {
		return nil, err
	}
	store := &Store{
		config: config, root: config.Root, dirs: dirs, rootLeaf: leaf, rootDev: dev, rootIno: ino,
		records: make(map[string]*Record), lastNow: config.Now().UTC(),
	}
	if store.lastNow.IsZero() {
		_ = store.Close()
		return nil, fmt.Errorf("%w: trusted clock returned zero", ErrClock)
	}
	if err := store.loadRecords(); err != nil {
		_ = store.Close()
		return nil, err
	}
	if err := store.reconcilePhysical(context.Background()); err != nil {
		_ = store.Close()
		return nil, err
	}
	return store, nil
}

func (store *Store) Reserve(
	ctx context.Context,
	binding Binding,
	limits codingrunner.Limits,
) (*Attempt, error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, fmt.Errorf("%w: reservation context is unavailable", ErrInvalid)
	}
	if err := limits.Validate(); err != nil {
		return nil, fmt.Errorf("%w: signed runner limits", ErrInvalid)
	}
	binding.Deadline = binding.Deadline.UTC()
	if err := validateBindingShape(binding); err != nil {
		return nil, err
	}
	id, err := bindingID(binding)
	if err != nil {
		return nil, err
	}
	bindingSHA, err := digestJSON(binding)
	if err != nil {
		return nil, err
	}
	reservation := reservationForPurpose(binding.Purpose, limits)
	if reservation <= 0 || reservation > store.config.MaxTotalBytes {
		return nil, ErrCapacity
	}
	now := store.config.Now().UTC()
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(now); err != nil {
		return nil, err
	}
	if existing := store.records[id]; existing != nil {
		if existing.Binding != binding || existing.Limits != limitsFromRunner(limits) || existing.ReservedBytes != reservation {
			return nil, ErrConflict
		}
		return &Attempt{store: store, id: id}, nil
	}
	for _, existing := range store.records {
		if existing.Binding.Purpose == binding.Purpose && existing.Binding.ExecutionID == binding.ExecutionID {
			return nil, ErrConflict
		}
	}
	if err := validateBindingFresh(binding, now); err != nil {
		return nil, err
	}
	used := store.reserved
	if store.orphanBytes > store.config.MaxTotalBytes-used {
		used = store.config.MaxTotalBytes
	} else {
		used += store.orphanBytes
	}
	if !store.physicalKnown || len(store.records) >= store.config.MaxAttempts || reservation > store.config.MaxTotalBytes-used {
		return nil, ErrCapacity
	}
	record := &Record{
		Schema: recordSchema, Generation: 1, ID: id, Binding: binding, BindingSHA256: bindingSHA,
		Limits: limitsFromRunner(limits), ReservedBytes: reservation, State: StateReserved,
		CreatedAtUnixNano: now.UnixNano(),
	}
	if err := store.persistRecord(record); err != nil {
		return nil, err
	}
	store.records[id] = cloneRecord(record)
	store.reserved += reservation
	return &Attempt{store: store, id: id}, nil
}

// Lookup returns the one durable attempt for a trusted purpose/execution
// identity. Reserve enforces uniqueness so phase-separated callers never need
// to persist or reconstruct the binding-derived record ID themselves.
func (store *Store) Lookup(
	ctx context.Context,
	purpose Purpose,
	executionID string,
) (*Attempt, Record, error) {
	if ctx == nil || ctx.Err() != nil ||
		(purpose != PurposeCertification && purpose != PurposeShadowAttempt) ||
		!validIdentifier(executionID, 256) {
		return nil, Record{}, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return nil, Record{}, err
	}
	var selected *Record
	for _, record := range store.records {
		if record.Binding.Purpose != purpose || record.Binding.ExecutionID != executionID {
			continue
		}
		if selected != nil {
			return nil, Record{}, ErrCorrupt
		}
		selected = record
	}
	if selected == nil {
		return nil, Record{}, ErrInvalid
	}
	return &Attempt{store: store, id: selected.ID}, *cloneRecord(selected), nil
}

func (attempt *Attempt) ID() string {
	if attempt == nil {
		return ""
	}
	return attempt.id
}

func (attempt *Attempt) Binding() (Binding, error) {
	if attempt == nil || attempt.store == nil {
		return Binding{}, ErrInvalid
	}
	attempt.store.mu.Lock()
	defer attempt.store.mu.Unlock()
	if err := attempt.store.checkOpenAndClock(attempt.store.config.Now().UTC()); err != nil {
		return Binding{}, err
	}
	record, err := attempt.store.recordForAttempt(attempt.id)
	if err != nil {
		return Binding{}, err
	}
	return record.Binding, nil
}

func (store *Store) Pending(ctx context.Context, limit int) ([]Record, error) {
	if ctx == nil || ctx.Err() != nil || limit <= 0 || limit > 10_000 {
		return nil, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return nil, err
	}
	selected := make([]*Record, 0)
	for _, record := range store.records {
		if record.State == StateReady || record.State == StateTerminalWithoutPatch {
			selected = append(selected, record)
		}
	}
	sort.Slice(selected, func(left, right int) bool {
		if selected[left].CreatedAtUnixNano == selected[right].CreatedAtUnixNano {
			return selected[left].ID < selected[right].ID
		}
		return selected[left].CreatedAtUnixNano < selected[right].CreatedAtUnixNano
	})
	if len(selected) > limit {
		selected = selected[:limit]
	}
	values := make([]Record, len(selected))
	for index, record := range selected {
		values[index] = *cloneRecord(record)
	}
	return values, nil
}

func (store *Store) Release(ctx context.Context, id, terminalEvidenceSHA256 string) error {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) || !lowerSHA256(terminalEvidenceSHA256) {
		return ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return err
	}
	record := store.records[id]
	if record == nil {
		return ErrInvalid
	}
	if record.Binding.Purpose == PurposeShadowAttempt {
		return ErrState
	}
	if record.State == StateReleased {
		if record.ReleaseEvidenceSHA256 == terminalEvidenceSHA256 {
			return nil
		}
		return ErrConflict
	}
	if record.State != StateReady && record.State != StateTerminalWithoutPatch {
		return ErrState
	}
	if record.Binding.Purpose == PurposeShadowAttempt {
		if record.TerminalPublication == nil || record.TerminalPublication.Acknowledgement == nil {
			return ErrState
		}
		if record.TerminalPublication.Authority.EvidenceSHA256 != terminalEvidenceSHA256 {
			return ErrConflict
		}
	}
	updated := cloneRecord(record)
	updated.Generation++
	updated.State = StateReleased
	updated.ReleaseEvidenceSHA256 = terminalEvidenceSHA256
	updated.ReleasedAtUnix = now.Unix()
	if err := store.persistRecord(updated); err != nil {
		return err
	}
	store.records[id] = updated
	return nil
}

// ReleaseShadow transitions one shadow attempt only after Platform has
// finalized the exact terminal-publication acknowledgement object. The
// finalization is normalized before persistence so exact Platform replays may
// differ only in their non-authoritative idempotent response bit.
func (store *Store) ReleaseShadow(
	ctx context.Context,
	id string,
	finalization codingevidence.WireFinalization,
) error {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) {
		return ErrInvalid
	}
	identity, err := releaseFinalizationIdentity(finalization)
	if err != nil {
		return err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return err
	}
	record := store.records[id]
	if record == nil || record.Binding.Purpose != PurposeShadowAttempt {
		return ErrInvalid
	}
	if record.State == StateReleased {
		if record.ReleaseFinalization != nil && *record.ReleaseFinalization == identity {
			return nil
		}
		return ErrConflict
	}
	if record.State != StateReady && record.State != StateTerminalWithoutPatch {
		return ErrState
	}
	publication := record.TerminalPublication
	reservation := record.ReleaseReservation
	if publication == nil || publication.Acknowledgement == nil || reservation == nil {
		return ErrState
	}
	if identity.TicketID != record.Binding.TicketID ||
		identity.TicketID != reservation.TicketID ||
		identity.ClaimGeneration != reservation.ClaimGeneration ||
		identity.UploadID != reservation.UploadID ||
		identity.EvidenceKind != reservation.EvidenceKind ||
		identity.SHA256 != reservation.SHA256 ||
		identity.SizeBytes != reservation.SizeBytes ||
		identity.SHA256 != publication.Acknowledgement.SHA256 ||
		identity.SizeBytes != publication.Acknowledgement.SizeBytes ||
		time.Unix(0, identity.FinalizedAtUnixNano).UTC().After(record.Binding.Deadline) {
		return ErrConflict
	}
	updated := cloneRecord(record)
	updated.Generation++
	updated.State = StateReleased
	updated.ReleaseEvidenceSHA256 = publication.Authority.EvidenceSHA256
	updated.ReleaseFinalization = &identity
	updated.ReleasedAtUnix = now.Unix()
	if err := store.persistRecord(updated); err != nil {
		return err
	}
	store.records[id] = updated
	return nil
}

// PrepareShadowRelease persists the redacted terminal-acknowledgement upload
// authority before the PUT or Platform finalization begins. This closes the
// crash window in which Platform can complete a claim before the local outbox
// has enough identity to replay its finalization receipt.
func (store *Store) PrepareShadowRelease(
	ctx context.Context,
	id string,
	capability codingevidence.WireUploadCapability,
) (ReleaseReservation, error) {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) {
		return ReleaseReservation{}, ErrInvalid
	}
	reservation, err := releaseReservationIdentity(capability)
	if err != nil {
		return ReleaseReservation{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return ReleaseReservation{}, err
	}
	record := store.records[id]
	if record == nil || record.Binding.Purpose != PurposeShadowAttempt {
		return ReleaseReservation{}, ErrInvalid
	}
	publication := record.TerminalPublication
	if publication == nil || publication.Acknowledgement == nil ||
		(record.State != StateReady && record.State != StateTerminalWithoutPatch && record.State != StateReleased) {
		return ReleaseReservation{}, ErrState
	}
	if reservation.TicketID != record.Binding.TicketID ||
		!capability.TicketDeadline.Equal(record.Binding.Deadline) ||
		reservation.SHA256 != publication.Acknowledgement.SHA256 ||
		reservation.SizeBytes != publication.Acknowledgement.SizeBytes {
		return ReleaseReservation{}, ErrConflict
	}
	if record.ReleaseReservation != nil {
		if *record.ReleaseReservation == reservation {
			return reservation, nil
		}
		return ReleaseReservation{}, ErrConflict
	}
	if record.State == StateReleased {
		return ReleaseReservation{}, ErrCorrupt
	}
	updated := cloneRecord(record)
	updated.Generation++
	updated.ReleaseReservation = &reservation
	if err := store.persistRecord(updated); err != nil {
		return ReleaseReservation{}, err
	}
	store.records[id] = updated
	return reservation, nil
}

// PendingShadowReleases returns acknowledged terminal records whose redacted
// upload authority is durable but whose local release has not committed.
func (store *Store) PendingShadowReleases(
	ctx context.Context,
	limit int,
) ([]PendingRelease, error) {
	if ctx == nil || ctx.Err() != nil || limit <= 0 || limit > 1_000 {
		return nil, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return nil, err
	}
	records := make([]*Record, 0)
	for _, record := range store.records {
		if record.Binding.Purpose == PurposeShadowAttempt &&
			(record.State == StateReady || record.State == StateTerminalWithoutPatch) &&
			record.ReleaseReservation != nil && record.TerminalPublication != nil &&
			record.TerminalPublication.Acknowledgement != nil {
			records = append(records, record)
		}
	}
	sort.Slice(records, func(left, right int) bool {
		if records[left].CreatedAtUnixNano == records[right].CreatedAtUnixNano {
			return records[left].ID < records[right].ID
		}
		return records[left].CreatedAtUnixNano < records[right].CreatedAtUnixNano
	})
	if len(records) > limit {
		records = records[:limit]
	}
	values := make([]PendingRelease, len(records))
	for index, record := range records {
		values[index] = PendingRelease{
			RecordID: record.ID, TicketID: record.Binding.TicketID,
			TerminalEvidenceSHA256: record.TerminalPublication.Authority.EvidenceSHA256,
			Reservation:            *record.ReleaseReservation,
		}
	}
	return values, nil
}

func releaseReservationIdentity(
	value codingevidence.WireUploadCapability,
) (ReleaseReservation, error) {
	ticket, ticketErr := uuid.Parse(value.TicketID)
	upload, uploadErr := uuid.Parse(value.UploadID)
	if value.Schema != "dittobench-coding-sealed-evidence-upload-capability-v1" ||
		value.CodingContractVersion != 1 || value.WeightEligible ||
		ticketErr != nil || ticket == uuid.Nil || ticket.String() != value.TicketID ||
		uploadErr != nil || upload == uuid.Nil || upload.String() != value.UploadID ||
		value.ClaimGeneration < 1 || value.ClaimGeneration > (1<<31)-1 ||
		value.EvidenceKind != codingevidence.KindTerminalPublicationAcknowledgement ||
		!lowerSHA256(value.SHA256) || value.SizeBytes < 1 ||
		value.SizeBytes > maximumPublicationAckBytes ||
		value.ContentType != "application/octet-stream" || value.TicketDeadline.IsZero() {
		return ReleaseReservation{}, ErrInvalid
	}
	return ReleaseReservation{
		TicketID: value.TicketID, ClaimGeneration: value.ClaimGeneration,
		UploadID: value.UploadID, EvidenceKind: value.EvidenceKind,
		SHA256: value.SHA256, SizeBytes: value.SizeBytes,
	}, nil
}

func releaseFinalizationIdentity(
	value codingevidence.WireFinalization,
) (ReleaseFinalization, error) {
	ticket, ticketErr := uuid.Parse(value.TicketID)
	upload, uploadErr := uuid.Parse(value.UploadID)
	finalizedAtUnixNano := value.FinalizedAt.UTC().UnixNano()
	if value.Schema != "dittobench-coding-sealed-evidence-finalized-v1" ||
		value.CodingContractVersion != 1 || value.WeightEligible || !value.Accepted ||
		ticketErr != nil || ticket == uuid.Nil || ticket.String() != value.TicketID ||
		uploadErr != nil || upload == uuid.Nil || upload.String() != value.UploadID ||
		value.ClaimGeneration < 1 || value.ClaimGeneration > (1<<31)-1 ||
		value.EvidenceKind != codingevidence.KindTerminalPublicationAcknowledgement ||
		!lowerSHA256(value.SHA256) || value.SizeBytes < 1 ||
		value.SizeBytes > maximumPublicationAckBytes || value.FinalizedAt.IsZero() ||
		finalizedAtUnixNano <= 0 || value.FinalizedAt.Nanosecond()%1_000 != 0 {
		return ReleaseFinalization{}, ErrInvalid
	}
	return ReleaseFinalization{
		TicketID: value.TicketID, ClaimGeneration: value.ClaimGeneration,
		UploadID: value.UploadID, EvidenceKind: value.EvidenceKind,
		SHA256: value.SHA256, SizeBytes: value.SizeBytes,
		FinalizedAtUnixNano: finalizedAtUnixNano,
	}, nil
}

func (store *Store) OpenTranscript(ctx context.Context, id string) (io.ReadCloser, error) {
	if ctx == nil || ctx.Err() != nil || !lowerSHA256(id) {
		return nil, ErrInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.checkOpenAndClock(store.config.Now().UTC()); err != nil {
		return nil, err
	}
	record := store.records[id]
	if record == nil || record.Transcript == nil {
		return nil, ErrState
	}
	return store.openObject(record.Transcript.SHA256, record.Transcript.SizeBytes)
}

func (store *Store) Close() error {
	if store == nil {
		return nil
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil
	}
	store.closed = true
	var errs []error
	if store.dirs.root != nil {
		errs = append(errs, unix.Flock(int(store.dirs.root.Fd()), unix.LOCK_UN))
	}
	for _, file := range []*os.File{store.dirs.sha256Dir, store.dirs.objects, store.dirs.records, store.dirs.staging, store.dirs.root, store.dirs.parent} {
		if file != nil {
			errs = append(errs, file.Close())
		}
	}
	return errors.Join(errs...)
}

func (store *Store) checkOpenAndClock(now time.Time) error {
	if store.closed {
		return ErrClosed
	}
	if now.IsZero() || now.Before(store.lastNow) {
		return ErrClock
	}
	store.lastNow = now
	return store.validateRootIdentity()
}

func (store *Store) recordForAttempt(id string) (*Record, error) {
	if store.closed {
		return nil, ErrClosed
	}
	record := store.records[id]
	if record == nil {
		return nil, ErrInvalid
	}
	return record, nil
}

func (store *Store) capacityHealthy() bool {
	return store.physicalKnown && store.reserved <= store.config.MaxTotalBytes &&
		store.orphanBytes <= store.config.MaxTotalBytes-store.reserved
}

func (store *Store) loadRecords() error {
	if _, err := store.dirs.records.Seek(0, 0); err != nil {
		return err
	}
	entries, err := store.dirs.records.ReadDir(store.config.MaxAttempts + 1)
	if err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("scan outbox records: %w", err)
	}
	if len(entries) > store.config.MaxAttempts {
		return ErrCapacity
	}
	now := store.lastNow
	executions := make(map[string]struct{}, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".json") || !lowerSHA256(strings.TrimSuffix(name, ".json")) {
			return fmt.Errorf("%w: unexpected record entry", ErrCorrupt)
		}
		record, err := store.readRecord(name)
		if err != nil {
			return err
		}
		createdAt := time.Unix(0, record.CreatedAtUnixNano).UTC()
		updatedAt := time.Unix(0, record.UpdatedAtUnixNano).UTC()
		if createdAt.After(now.Add(5*time.Minute)) || updatedAt.After(now.Add(5*time.Minute)) ||
			now.Before(createdAt) || now.Before(updatedAt) {
			return ErrClock
		}
		if record.State == StateCollecting && record.Transcript == nil {
			record.Generation++
			record.State = StateExpired
			record.WriterNonce = ""
			record.StagingName = ""
			record.ExpiredAtUnix = now.Unix()
			if err := store.persistRecord(record); err != nil {
				return err
			}
		}
		expired := record.State == StateReserved && !record.Binding.Deadline.After(now)
		expired = expired || record.State == StateCollecting && !record.Binding.Deadline.Add(store.config.FinalizationGrace).After(now)
		if expired {
			record.Generation++
			record.State = StateExpired
			record.ExpiredAtUnix = now.Unix()
			record.WriterNonce = ""
			record.StagingName = ""
			if err := store.persistRecord(record); err != nil {
				return err
			}
		}
		if record.Transcript != nil {
			if err := store.verifyReference(record.Transcript.ObjectKey, record.Transcript.SHA256, record.Transcript.SizeBytes); err != nil {
				return err
			}
		}
		if record.Frozen != nil {
			artifact := record.Frozen.Artifact
			if err := store.verifyReference(artifact.ObjectKey, artifact.FrozenPatchSHA256, artifact.SizeBytes); err != nil {
				return err
			}
		}
		if err := store.verifyPublicationReferences(record); err != nil {
			return err
		}
		executionKey := string(record.Binding.Purpose) + "\x00" + record.Binding.ExecutionID
		if _, duplicate := executions[executionKey]; duplicate {
			return fmt.Errorf("%w: duplicate purpose/execution identity", ErrCorrupt)
		}
		executions[executionKey] = struct{}{}
		store.records[record.ID] = cloneRecord(record)
		if record.ReservedBytes > store.config.MaxTotalBytes-store.reserved {
			return ErrCapacity
		}
		store.reserved += record.ReservedBytes
	}
	return nil
}

func (store *Store) readRecord(name string) (*Record, error) {
	fd, err := unix.Openat(int(store.dirs.records.Fd()), name,
		unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("%w: open record", ErrCorrupt)
	}
	file := os.NewFile(uintptr(fd), name)
	if file == nil {
		unix.Close(fd)
		return nil, ErrCorrupt
	}
	defer file.Close()
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG ||
		stat.Mode&0o777 != 0o600 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 ||
		stat.Size <= 0 || stat.Size > maximumRecordBytes {
		return nil, fmt.Errorf("%w: record metadata is invalid", ErrCorrupt)
	}
	body, err := io.ReadAll(io.LimitReader(file, maximumRecordBytes+1))
	if err != nil || int64(len(body)) != stat.Size {
		return nil, fmt.Errorf("%w: record bytes are invalid", ErrCorrupt)
	}
	var after, pathStat unix.Stat_t
	if err := unix.Fstat(fd, &after); err != nil || after.Dev != stat.Dev || after.Ino != stat.Ino ||
		after.Size != stat.Size || after.Nlink != stat.Nlink ||
		unix.Fstatat(int(store.dirs.records.Fd()), name, &pathStat, unix.AT_SYMLINK_NOFOLLOW) != nil ||
		pathStat.Dev != after.Dev || pathStat.Ino != after.Ino || pathStat.Nlink != 1 {
		return nil, fmt.Errorf("%w: record changed while reading", ErrCorrupt)
	}
	if err := codingcontract.ValidateJSONDocument(body, maximumRecordBytes); err != nil {
		return nil, fmt.Errorf("%w: record JSON is invalid", ErrCorrupt)
	}
	var record Record
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&record); err != nil {
		return nil, fmt.Errorf("%w: record cannot be decoded", ErrCorrupt)
	}
	expectedID := strings.TrimSuffix(name, ".json")
	if err := validateRecord(&record, expectedID); err != nil {
		return nil, err
	}
	return &record, nil
}

func (store *Store) persistRecord(record *Record) error {
	if err := store.validateRootIdentity(); err != nil {
		return err
	}
	record.UpdatedAtUnixNano = store.lastNow.UnixNano()
	body, err := recordBytes(record)
	if err != nil {
		return err
	}
	file, name, dev, ino, err := newStagingFile(store.dirs.staging, "record-")
	if err != nil {
		return err
	}
	keep := false
	defer func() {
		_ = file.Close()
		if !keep {
			if verifyNamedInode(store.dirs.staging, name, dev, ino, 0o600) == nil {
				_ = unix.Unlinkat(int(store.dirs.staging.Fd()), name, 0)
			}
		}
	}()
	if err := writeAll(file, body); err != nil || file.Sync() != nil || file.Close() != nil {
		return errors.New("persist outbox record bytes")
	}
	if err := verifyNamedInode(store.dirs.staging, name, dev, ino, 0o600); err != nil {
		return err
	}
	target := record.ID + ".json"
	var existing unix.Stat_t
	if err := unix.Fstatat(int(store.dirs.records.Fd()), target, &existing, unix.AT_SYMLINK_NOFOLLOW); err == nil {
		if existing.Mode&unix.S_IFMT != unix.S_IFREG || existing.Mode&0o777 != 0o600 ||
			existing.Uid != uint32(os.Geteuid()) || existing.Nlink != 1 {
			return fmt.Errorf("%w: existing record is unsafe", ErrCorrupt)
		}
	} else if !errors.Is(err, unix.ENOENT) {
		return fmt.Errorf("inspect existing outbox record: %w", err)
	}
	if err := unix.Renameat(int(store.dirs.staging.Fd()), name, int(store.dirs.records.Fd()), target); err != nil {
		return fmt.Errorf("install outbox record: %w", err)
	}
	keep = true
	store.records[record.ID] = cloneRecord(record)
	if err := verifyNamedInode(store.dirs.records, target, dev, ino, 0o600); err != nil {
		return err
	}
	if err := unix.Fsync(int(store.dirs.records.Fd())); err != nil {
		store.physicalKnown = false
		return fmt.Errorf("sync outbox records directory: %w", err)
	}
	if err := unix.Fsync(int(store.dirs.staging.Fd())); err != nil {
		store.physicalKnown = false
		return fmt.Errorf("sync outbox staging directory: %w", err)
	}
	return store.validateRootIdentity()
}

func recordBytes(record *Record) ([]byte, error) {
	copy := *cloneRecord(record)
	copy.ChecksumSHA256 = ""
	body, err := json.Marshal(copy)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(body)
	record.ChecksumSHA256 = hex.EncodeToString(digest[:])
	body, err = json.Marshal(record)
	if err != nil || len(body)+1 > maximumRecordBytes {
		return nil, fmt.Errorf("%w: record exceeds its bound", ErrInvalid)
	}
	return append(body, '\n'), nil
}

func validateRecord(record *Record, expectedID string) error {
	if record.Schema != recordSchema || record.Generation == 0 || record.ID != expectedID ||
		!lowerSHA256(record.BindingSHA256) || !lowerSHA256(record.ChecksumSHA256) ||
		record.ReservedBytes != reservationForPurpose(record.Binding.Purpose, record.Limits.runner()) ||
		record.ReservedBytes <= 0 || record.CreatedAtUnixNano <= 0 || record.UpdatedAtUnixNano < record.CreatedAtUnixNano {
		return fmt.Errorf("%w: record known fields disagree", ErrCorrupt)
	}
	if err := record.Limits.runner().Validate(); err != nil {
		return fmt.Errorf("%w: record limits disagree", ErrCorrupt)
	}
	binding := record.Binding
	binding.Deadline = binding.Deadline.UTC()
	id, err := bindingID(binding)
	bindingSHA, bindingErr := digestJSON(binding)
	createdAt := time.Unix(0, record.CreatedAtUnixNano).UTC()
	if err != nil || bindingErr != nil || id != expectedID || bindingSHA != record.BindingSHA256 ||
		binding != record.Binding || validateBindingShape(binding) != nil || validateBindingFresh(binding, createdAt) != nil {
		return fmt.Errorf("%w: record binding digest disagrees", ErrCorrupt)
	}
	wantChecksum := record.ChecksumSHA256
	copy := *cloneRecord(record)
	copy.ChecksumSHA256 = ""
	body, err := json.Marshal(copy)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(body)
	if hex.EncodeToString(digest[:]) != wantChecksum {
		return fmt.Errorf("%w: record checksum disagrees", ErrCorrupt)
	}
	validState := slices.Contains([]State{
		StateReserved, StateCollecting, StateReady, StateTerminalWithoutPatch, StateReleased, StateExpired,
	}, record.State)
	collectingInvalid := record.State == StateCollecting &&
		((record.WriterNonce != "" && (!validStagingName(record.StagingName) || record.Transcript != nil || record.Frozen != nil || record.Failure != nil)) ||
			(record.WriterNonce == "" && record.Transcript == nil) || record.Frozen != nil || record.Failure != nil)
	if !validState || collectingInvalid ||
		(record.WriterNonce == "" && record.StagingName != "") ||
		(record.State == StateReserved && (record.WriterNonce != "" || record.StagingName != "" || record.Transcript != nil || record.Frozen != nil || record.Failure != nil ||
			record.OutcomeSHA256 != "" || record.SealedAtUnix != 0)) ||
		(record.State == StateCollecting && (record.OutcomeSHA256 != "" || record.SealedAtUnix != 0)) ||
		(record.State == StateReady && (record.Transcript == nil || record.Frozen == nil || record.Failure != nil || record.OutcomeSHA256 == "" ||
			record.WriterNonce != "" || record.StagingName != "")) ||
		(record.State == StateTerminalWithoutPatch && (record.Transcript == nil || record.Frozen != nil || record.Failure == nil || record.OutcomeSHA256 == "" ||
			record.WriterNonce != "" || record.StagingName != "")) ||
		(record.State == StateReleased && (!lowerSHA256(record.ReleaseEvidenceSHA256) || record.ReleasedAtUnix == 0 ||
			record.Transcript == nil || record.OutcomeSHA256 == "" || (record.Frozen == nil) == (record.Failure == nil) ||
			record.WriterNonce != "" || record.StagingName != "")) ||
		(record.State == StateExpired && (record.OutcomeSHA256 != "" || record.SealedAtUnix != 0 || record.Frozen != nil || record.Failure != nil ||
			record.WriterNonce != "" || record.StagingName != "")) ||
		(record.State != StateReleased && (record.ReleaseEvidenceSHA256 != "" || record.ReleaseFinalization != nil || record.ReleasedAtUnix != 0)) ||
		(record.State != StateExpired && record.ExpiredAtUnix != 0) {
		return fmt.Errorf("%w: record state shape disagrees", ErrCorrupt)
	}
	if (record.State == StateReady || record.State == StateTerminalWithoutPatch || record.State == StateReleased) &&
		(!lowerSHA256(record.OutcomeSHA256) || record.SealedAtUnix < createdAt.Unix()) {
		return fmt.Errorf("%w: sealed record authority disagrees", ErrCorrupt)
	}
	if record.State == StateReleased && record.ReleasedAtUnix < record.SealedAtUnix {
		return fmt.Errorf("%w: release acknowledgement disagrees", ErrCorrupt)
	}
	if record.State == StateExpired && record.ExpiredAtUnix < createdAt.Unix() {
		return fmt.Errorf("%w: expiry timestamp disagrees", ErrCorrupt)
	}
	if record.Transcript != nil && (record.Transcript.ObjectKey != "sha256/"+record.Transcript.SHA256 ||
		!lowerSHA256(record.Transcript.SHA256) || record.Transcript.SizeBytes < 0 ||
		record.Transcript.SizeBytes > record.Limits.MaxTranscriptBytes) {
		return fmt.Errorf("%w: transcript reference disagrees", ErrCorrupt)
	}
	if record.Frozen != nil {
		artifact, metadata := record.Frozen.Artifact, record.Frozen.Metadata
		if artifact.ObjectKey != "sha256/"+artifact.FrozenPatchSHA256 || !lowerSHA256(artifact.FrozenPatchSHA256) ||
			artifact.SizeBytes <= 0 || artifact.SizeBytes > record.Limits.MaxPatchBytes ||
			artifact.FrozenPatchSHA256 != metadata.FrozenPatchSHA256 || artifact.FinalTreeSHA256 != metadata.FinalTreeSHA256 ||
			artifact.ChangedPathRoot != metadata.ChangedPathRoot || metadata.CaseID != record.Binding.CaseID ||
			metadata.CodingContractVersion != 1 ||
			!lowerSHA256(metadata.BaseTreeSHA256) || !lowerSHA256(metadata.VisibleBundleSHA256) ||
			!lowerSHA256(metadata.AuthoringEventRoot) || !lowerSHA256(metadata.AuthoringTranscriptSHA256) ||
			metadata.AuthoringTranscriptBytes < 0 ||
			(record.Transcript != nil && (metadata.AuthoringTranscriptSHA256 != record.Transcript.SHA256 ||
				metadata.AuthoringTranscriptBytes != record.Transcript.SizeBytes)) {
			return fmt.Errorf("%w: frozen reference disagrees", ErrCorrupt)
		}
	}
	if record.Failure != nil && (!validFailure(*record.Failure) || record.Transcript == nil ||
		record.Failure.AuthoringTranscriptSHA256 != record.Transcript.SHA256 ||
		record.Failure.AuthoringTranscriptBytes != record.Transcript.SizeBytes) {
		return fmt.Errorf("%w: terminal failure reference disagrees", ErrCorrupt)
	}
	if err := validatePublicationRecords(record); err != nil {
		return err
	}
	return nil
}

func (store *Store) verifyReference(key, digest string, size int64) error {
	if key != "sha256/"+digest || !lowerSHA256(digest) || size < 0 {
		return fmt.Errorf("%w: object reference is invalid", ErrCorrupt)
	}
	file, err := store.openObject(digest, size)
	if err != nil {
		return err
	}
	return file.Close()
}

func digestJSON(value any) (string, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), nil
}

func bindingID(binding Binding) (string, error) {
	return digestJSON(struct {
		Schema              string  `json:"schema"`
		Purpose             Purpose `json:"purpose"`
		ExecutionID         string  `json:"execution_id"`
		TicketID            string  `json:"ticket_id"`
		CaseID              string  `json:"case_id"`
		ProfileCapabilityID string  `json:"profile_capability_id"`
	}{
		Schema: "dittobench-coding-evidence-binding-v1", Purpose: binding.Purpose, ExecutionID: binding.ExecutionID,
		TicketID: binding.TicketID, CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
	})
}

func validateBindingShape(binding Binding) error {
	ticket, ticketErr := uuid.Parse(binding.TicketID)
	if ticketErr != nil || ticket == uuid.Nil || ticket.String() != binding.TicketID ||
		(binding.Purpose != PurposeCertification && binding.Purpose != PurposeShadowAttempt) ||
		!validIdentifier(binding.ExecutionID, 256) ||
		!lowerSHA256(binding.AgentArtifactSHA256) || !validIdentifier(binding.HarnessInstanceID, 256) ||
		!lowerSHA256(binding.AuthoritySHA256) || !validIdentifier(binding.CaseID, 256) ||
		!validIdentifier(binding.ProfileCapabilityID, 256) || binding.Deadline.IsZero() {
		return fmt.Errorf("%w: evidence binding is invalid", ErrInvalid)
	}
	legacyShadow := binding.HarnessAuthoritySHA256 == "" && binding.ScreenedImageSHA256 == ""
	shadowAuthority := legacyShadow ||
		(lowerSHA256(binding.HarnessAuthoritySHA256) && lowerSHA256(binding.ScreenedImageSHA256))
	certificationAuthority := binding.HarnessAuthoritySHA256 == "" && binding.ScreenedImageSHA256 == ""
	if (binding.Purpose == PurposeShadowAttempt && !shadowAuthority) ||
		(binding.Purpose == PurposeCertification && !certificationAuthority) {
		return fmt.Errorf("%w: evidence harness authority is invalid", ErrInvalid)
	}
	return nil
}

func validateBindingFresh(binding Binding, now time.Time) error {
	if !binding.Deadline.After(now) || binding.Deadline.After(now.Add(2*time.Hour)) {
		return fmt.Errorf("%w: evidence binding lifetime is invalid", ErrInvalid)
	}
	if binding.Purpose == PurposeShadowAttempt &&
		(!lowerSHA256(binding.HarnessAuthoritySHA256) || !lowerSHA256(binding.ScreenedImageSHA256)) {
		return fmt.Errorf("%w: evidence harness authority is invalid", ErrInvalid)
	}
	return nil
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

func lowerSHA256(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validStagingName(value string) bool {
	return strings.HasPrefix(value, "transcript-") && len(value) == len("transcript-")+32 &&
		hexLeaf(strings.TrimPrefix(value, "transcript-"))
}

func cloneRecord(record *Record) *Record {
	if record == nil {
		return nil
	}
	copy := *record
	if record.Transcript != nil {
		value := *record.Transcript
		copy.Transcript = &value
	}
	if record.Frozen != nil {
		value := *record.Frozen
		copy.Frozen = &value
	}
	if record.Failure != nil {
		value := *record.Failure
		copy.Failure = &value
	}
	copy.AuthoringPublication = clonePublication(record.AuthoringPublication)
	copy.TerminalPublication = clonePublication(record.TerminalPublication)
	if record.ReleaseFinalization != nil {
		value := *record.ReleaseFinalization
		copy.ReleaseFinalization = &value
	}
	if record.ReleaseReservation != nil {
		value := *record.ReleaseReservation
		copy.ReleaseReservation = &value
	}
	return &copy
}
