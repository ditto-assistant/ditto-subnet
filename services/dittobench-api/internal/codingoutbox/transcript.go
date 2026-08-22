package codingoutbox

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"golang.org/x/sys/unix"
)

func (attempt *Attempt) BeginTranscript(ctx context.Context) (TranscriptWriter, error) {
	if attempt == nil || attempt.store == nil || ctx == nil || ctx.Err() != nil {
		return nil, ErrInvalid
	}
	store := attempt.store
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return nil, err
	}
	record, err := store.recordForAttempt(attempt.id)
	if err != nil {
		return nil, err
	}
	if record.State != StateReserved || record.Transcript != nil || record.WriterNonce != "" || !record.Binding.Deadline.After(now) {
		return nil, ErrState
	}
	if !store.capacityHealthy() {
		return nil, ErrCapacity
	}
	file, name, dev, ino, err := newStagingFile(store.dirs.staging, "transcript-")
	if err != nil {
		return nil, err
	}
	nonce, err := randomLeaf("")
	if err != nil {
		file.Close()
		_ = unix.Unlinkat(int(store.dirs.staging.Fd()), name, 0)
		return nil, err
	}
	updated := cloneRecord(record)
	updated.Generation++
	updated.State = StateCollecting
	updated.WriterNonce = nonce
	updated.StagingName = name
	if err := store.persistRecord(updated); err != nil {
		file.Close()
		if verifyNamedInode(store.dirs.staging, name, dev, ino, 0o600) == nil {
			_ = unix.Unlinkat(int(store.dirs.staging.Fd()), name, 0)
		}
		return nil, err
	}
	store.records[attempt.id] = updated
	return &transcriptWriter{
		store: store, recordID: attempt.id, nonce: nonce, file: file, name: name,
		stageDev: dev, stageIno: ino, hash: sha256.New(),
	}, nil
}

func (writer *transcriptWriter) Write(body []byte) (int, error) {
	writer.mu.Lock()
	defer writer.mu.Unlock()
	if writer.closed || writer.committed != nil {
		return 0, ErrState
	}
	writer.store.mu.Lock()
	record, err := writer.store.recordForAttempt(writer.recordID)
	writer.store.mu.Unlock()
	if err != nil || record.WriterNonce != writer.nonce {
		return 0, ErrState
	}
	if int64(len(body)) > record.Limits.MaxTranscriptBytes-writer.written {
		return 0, ErrCapacity
	}
	written, err := writer.file.Write(body)
	if written > 0 {
		_, _ = writer.hash.Write(body[:written])
		for _, value := range body[:written] {
			if value == '\n' {
				if writer.lineBytes == 0 {
					writer.invalid = true
				}
				writer.events++
				writer.lineBytes = 0
			} else {
				writer.lineBytes++
			}
		}
		writer.written += int64(written)
	}
	if err == nil && writer.invalid {
		err = ErrInvalid
	} else if err == nil && written != len(body) {
		err = errors.New("coding outbox transcript short write")
	}
	return written, err
}

func (writer *transcriptWriter) Commit(
	ctx context.Context,
	identity codingrunner.TranscriptIdentity,
) (TranscriptArtifact, error) {
	writer.mu.Lock()
	defer writer.mu.Unlock()
	if writer.committed != nil {
		return *writer.committed, nil
	}
	if writer.abortDone || writer.invalid || writer.lineBytes != 0 || ctx == nil || ctx.Err() != nil || !lowerSHA256(identity.SHA256) ||
		identity.SizeBytes != writer.written || identity.Events != writer.events ||
		hex.EncodeToString(writer.hash.Sum(nil)) != identity.SHA256 || (writer.written > 0 && writer.events == 0) {
		return TranscriptArtifact{}, ErrInvalid
	}
	store := writer.store
	store.mu.Lock()
	defer store.mu.Unlock()
	now := store.config.Now().UTC()
	if err := store.checkOpenAndClock(now); err != nil {
		return TranscriptArtifact{}, err
	}
	record, err := store.recordForAttempt(writer.recordID)
	if err != nil || record.State != StateCollecting || record.WriterNonce != writer.nonce || record.Transcript != nil {
		return TranscriptArtifact{}, ErrState
	}
	if !record.Binding.Deadline.Add(store.config.FinalizationGrace).After(now) {
		return TranscriptArtifact{}, ErrState
	}
	if !writer.installed {
		if !writer.sealed {
			if err := writer.file.Sync(); err != nil {
				return TranscriptArtifact{}, fmt.Errorf("sync transcript staging file: %w", err)
			}
			if err := writer.file.Chmod(0o400); err != nil {
				return TranscriptArtifact{}, fmt.Errorf("seal transcript staging file: %w", err)
			}
			if err := writer.file.Sync(); err != nil {
				return TranscriptArtifact{}, fmt.Errorf("sync sealed transcript staging file: %w", err)
			}
			if err := writer.file.Close(); err != nil {
				return TranscriptArtifact{}, fmt.Errorf("close transcript staging file: %w", err)
			}
			writer.closed = true
			writer.sealed = true
		}
		if err := store.installObject(writer.name, writer.stageDev, writer.stageIno, identity.SHA256, identity.SizeBytes); err != nil {
			return TranscriptArtifact{}, err
		}
		writer.installed = true
	} else {
		file, err := store.openObject(identity.SHA256, identity.SizeBytes)
		if err != nil {
			return TranscriptArtifact{}, err
		}
		_ = file.Close()
	}
	artifact := TranscriptArtifact{
		ObjectKey: "sha256/" + identity.SHA256, SHA256: identity.SHA256,
		SizeBytes: identity.SizeBytes, Events: identity.Events,
	}
	updated := cloneRecord(record)
	updated.Generation++
	updated.WriterNonce = ""
	updated.StagingName = ""
	updated.Transcript = &artifact
	if err := store.persistRecord(updated); err != nil {
		return TranscriptArtifact{}, err
	}
	store.records[writer.recordID] = updated
	writer.committed = &artifact
	return artifact, nil
}

func (writer *transcriptWriter) Abort() error {
	writer.mu.Lock()
	defer writer.mu.Unlock()
	if writer.committed != nil {
		return nil
	}
	if writer.abortDone {
		return writer.abortErr
	}
	if !writer.closed {
		_ = writer.file.Close()
		writer.closed = true
	}
	store := writer.store
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		writer.abortDone = true
		writer.abortErr = ErrClosed
		return writer.abortErr
	}
	mode := uint32(0o600)
	if writer.sealed {
		mode = 0o400
	}
	if !writer.installed && verifyNamedInode(store.dirs.staging, writer.name, writer.stageDev, writer.stageIno, mode) == nil {
		_ = unix.Unlinkat(int(store.dirs.staging.Fd()), writer.name, 0)
		_ = unix.Fsync(int(store.dirs.staging.Fd()))
	}
	record := store.records[writer.recordID]
	if record == nil || record.WriterNonce != writer.nonce {
		writer.abortDone = true
		return nil
	}
	updated := cloneRecord(record)
	updated.Generation++
	updated.State = StateExpired
	updated.WriterNonce = ""
	updated.StagingName = ""
	updated.ExpiredAtUnix = store.lastNow.Unix()
	if err := store.persistRecord(updated); err != nil {
		return err
	}
	store.records[writer.recordID] = updated
	writer.abortDone = true
	return nil
}

var _ TranscriptWriter = (*transcriptWriter)(nil)
