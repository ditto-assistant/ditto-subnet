package codingoutbox

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type fixtureClock struct{ now time.Time }

func (clock *fixtureClock) Now() time.Time { return clock.now }

type fixtureExecutor struct{}

func (fixtureExecutor) Execute(context.Context, string, codingrunner.CommandSpec) (codingrunner.CommandResult, error) {
	return codingrunner.CommandResult{}, nil
}

func newFixtureStore(t *testing.T, maximum int64) (*Store, *fixtureClock, string) {
	t.Helper()
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	clock := &fixtureClock{now: time.Now().UTC().Truncate(time.Second)}
	store, err := Open(Config{
		Root: root, MaxTotalBytes: maximum, MaxAttempts: 64,
		FinalizationGrace: time.Minute,
		OrphanGrace:       time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute,
		Now: clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	return store, clock, root
}

func fixtureBinding(clock *fixtureClock, suffix string) Binding {
	return Binding{
		Purpose: PurposeCertification, ExecutionID: "certification-" + suffix,
		AgentArtifactSHA256: strings.Repeat("a", 64), HarnessInstanceID: "harness-" + suffix,
		AuthoritySHA256: strings.Repeat("b", 64),
		TicketID:        "33333333-3333-4333-8333-33333333333" + suffix,
		CaseID:          "case-" + suffix, ProfileCapabilityID: "profile-" + suffix,
		Deadline: clock.now.Add(time.Hour),
	}
}

func commitFixtureTranscript(t *testing.T, attempt *Attempt, body []byte) TranscriptArtifact {
	t.Helper()
	writer, err := attempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write(body); err != nil {
		t.Fatal(err)
	}
	identity := codingrunner.TranscriptIdentity{
		SHA256: digest(body), SizeBytes: int64(len(body)), Events: uint64(bytes.Count(body, []byte{'\n'})),
	}
	artifact, err := writer.Commit(t.Context(), identity)
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.Abort(); err != nil {
		t.Fatal(err)
	}
	return artifact
}

func fixtureSubmission(t *testing.T, binding Binding, transcript TranscriptArtifact) codingrunner.FrozenSubmission {
	t.Helper()
	visible := tarFixture(t, "src/app.py", "def normalize(value):\n    return value.strip()\n")
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(visible), limits)
	if err != nil {
		t.Fatal(err)
	}
	manifest := codingrunner.Manifest{
		CodingContractVersion: codingrunner.ContractVersion, TicketID: binding.TicketID,
		CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		Deadline: binding.Deadline, EditablePaths: []string{"src/app.py"},
		CreatablePaths: []string{}, DeletablePaths: []string{},
		TestCommands: []codingrunner.CommandSpec{}, BuildCommands: []codingrunner.CommandSpec{}, Limits: limits,
	}
	session, err := codingrunner.NewSession(t.Context(), manifest, bytes.NewReader(visible), fixtureExecutor{})
	if err != nil {
		t.Fatal(err)
	}
	read := callTool(t, session.Handler(), codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID, CallID: "read-1", Name: "repo.read_file",
		Arguments: json.RawMessage(`{"path":"src/app.py"}`),
	})
	var readValue struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(read.Result, &readValue); err != nil {
		t.Fatal(err)
	}
	arguments, _ := json.Marshal(map[string]any{
		"path": "src/app.py", "expected_sha256": readValue.SHA256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	})
	response := callTool(t, session.Handler(), codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID, CallID: "edit-1", Name: "repo.apply_patch", Arguments: arguments,
	})
	if !response.OK {
		t.Fatalf("edit failed: %#v", response)
	}
	result := session.Freeze()
	if result.Submission == nil {
		t.Fatalf("freeze=%#v", result)
	}
	submission := *result.Submission
	submission.AuthoringTranscriptSHA256 = transcript.SHA256
	submission.AuthoringTranscriptBytes = transcript.SizeBytes
	if err := codingrunner.ValidateFrozenSubmission(submission, limits); err != nil {
		t.Fatal(err)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	return submission
}

func fixtureNoChangeSubmission(t *testing.T, binding Binding, transcript TranscriptArtifact) codingrunner.FrozenSubmission {
	t.Helper()
	visible := tarFixture(t, "src/app.py", "def normalize(value):\n    return value.strip()\n")
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(visible), limits)
	if err != nil {
		t.Fatal(err)
	}
	manifest := codingrunner.Manifest{
		CodingContractVersion: codingrunner.ContractVersion, TicketID: binding.TicketID,
		CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		Deadline: binding.Deadline, EditablePaths: []string{"src/app.py"},
		CreatablePaths: []string{}, DeletablePaths: []string{},
		TestCommands: []codingrunner.CommandSpec{}, BuildCommands: []codingrunner.CommandSpec{}, Limits: limits,
	}
	session, err := codingrunner.NewSession(t.Context(), manifest, bytes.NewReader(visible), fixtureExecutor{})
	if err != nil {
		t.Fatal(err)
	}
	result := session.Freeze()
	if result.Submission == nil {
		t.Fatalf("freeze=%#v", result)
	}
	submission := *result.Submission
	submission.AuthoringTranscriptSHA256 = transcript.SHA256
	submission.AuthoringTranscriptBytes = transcript.SizeBytes
	if submission.ChangedPaths == nil || submission.Changes == nil {
		t.Fatalf("freeze collapsed present-empty collections: %#v", submission)
	}
	if err := codingrunner.ValidateFrozenSubmission(submission, limits); err != nil {
		t.Fatal(err)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	return submission
}

func TestOutboxLifecycleRecoveryReleaseAndSweep(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	if _, err := Open(store.config); !errors.Is(err, ErrLocked) {
		t.Fatalf("second opener error=%v", err)
	}
	binding := fixtureBinding(clock, "1")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	if got, err := attempt.Binding(); err != nil || got != binding {
		t.Fatalf("binding=%#v err=%v", got, err)
	}
	reader, err := store.OpenTranscript(t.Context(), attempt.ID())
	if err != nil {
		t.Fatal(err)
	}
	transcriptBytes, readErr := io.ReadAll(reader)
	closeErr := reader.Close()
	if readErr != nil || closeErr != nil || digest(transcriptBytes) != transcript.SHA256 {
		t.Fatalf("transcript read=%q read_err=%v close_err=%v", transcriptBytes, readErr, closeErr)
	}
	submission := fixtureSubmission(t, binding, transcript)
	frozen, err := attempt.StoreFrozen(t.Context(), submission)
	if err != nil || frozen.FrozenPatchSHA256 != submission.FrozenPatchSHA256 || frozen.SizeBytes != int64(len(submission.Patch)) {
		t.Fatalf("frozen=%#v err=%v", frozen, err)
	}
	if again, err := attempt.StoreFrozen(t.Context(), submission); err != nil || again != frozen {
		t.Fatalf("idempotent frozen=%#v err=%v", again, err)
	}
	record, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Submission: &submission})
	if err != nil || record.State != StateReady {
		t.Fatalf("record=%#v err=%v", record, err)
	}
	pending, err := store.Pending(t.Context(), 10)
	if err != nil || len(pending) != 1 {
		t.Fatalf("pending=%#v err=%v", pending, err)
	}
	pending[0].Frozen.Metadata.CaseID = "mutated"
	pendingAgain, _ := store.Pending(t.Context(), 10)
	if pendingAgain[0].Frozen.Metadata.CaseID == "mutated" {
		t.Fatal("pending record was not deeply cloned")
	}
	loaded, err := store.LoadFrozen(t.Context(), attempt.ID())
	if err != nil || !bytes.Equal(loaded.Patch, submission.Patch) || loaded.FinalTreeSHA256 != submission.FinalTreeSHA256 {
		t.Fatalf("loaded=%#v err=%v", loaded, err)
	}
	releaseEvidence := strings.Repeat("c", 64)
	if err := store.Release(t.Context(), attempt.ID(), releaseEvidence); err != nil {
		t.Fatal(err)
	}
	sealedAgain, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Submission: &submission})
	if err != nil || sealedAgain.State != StateReleased {
		t.Fatalf("post-ack seal replay=%#v err=%v", sealedAgain, err)
	}
	if err := store.Release(t.Context(), attempt.ID(), releaseEvidence); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), attempt.ID(), strings.Repeat("d", 64)); !errors.Is(err, ErrConflict) {
		t.Fatalf("ack drift error=%v", err)
	}
	clock.now = binding.Deadline.Add(2 * time.Minute)
	if again, err := attempt.StoreFrozen(t.Context(), submission); err != nil || again != frozen {
		t.Fatalf("post-grace frozen replay=%#v err=%v", again, err)
	}
	if again, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Submission: &submission}); err != nil ||
		again.State != StateReleased {
		t.Fatalf("post-grace seal replay=%#v err=%v", again, err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 64,
		FinalizationGrace: time.Minute,
		OrphanGrace:       time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	loaded, err = reopened.LoadFrozen(t.Context(), attempt.ID())
	if err != nil || !bytes.Equal(loaded.Patch, submission.Patch) {
		t.Fatalf("recovered frozen err=%v", err)
	}
	clock.now = clock.now.Add(2 * time.Minute)
	report, err := reopened.Sweep(t.Context())
	if err != nil || report.DeletedRecords != 1 || report.DeletedObjects != 2 || report.RemainingReservation != 0 {
		t.Fatalf("sweep=%#v err=%v", report, err)
	}
}

func TestTerminalWithoutPatchIsDurableAndCannotAcquirePatch(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "2")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	submission := fixtureSubmission(t, binding, transcript)
	failure := codingrunner.FreezeFailure{
		Kind: "repair_failure", Code: "protected_path", BaseTreeSHA256: submission.BaseTreeSHA256,
		VisibleBundleSHA256: submission.VisibleBundleSHA256, FinalTreeSHA256: submission.FinalTreeSHA256,
		ChangedPathRoot: submission.ChangedPathRoot, AuthoringEventRoot: submission.AuthoringEventRoot,
		AuthoringTranscriptSHA256: transcript.SHA256, AuthoringTranscriptBytes: transcript.SizeBytes,
		ProtectedPathsIntact: false,
	}
	record, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Failure: &failure})
	if err != nil || record.State != StateTerminalWithoutPatch || record.Frozen != nil {
		t.Fatalf("record=%#v err=%v", record, err)
	}
	if _, err := attempt.StoreFrozen(t.Context(), submission); !errors.Is(err, ErrState) {
		t.Fatalf("terminal attempt accepted patch: %v", err)
	}
	again, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Failure: &failure})
	if err != nil || again.OutcomeSHA256 != record.OutcomeSHA256 {
		t.Fatalf("terminal replay=%#v err=%v", again, err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 64, FinalizationGrace: time.Minute,
		OrphanGrace: time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	pending, err := reopened.Pending(t.Context(), 10)
	if err != nil || len(pending) != 1 || pending[0].Failure == nil || *pending[0].Failure != failure {
		t.Fatalf("recovered terminal=%#v err=%v", pending, err)
	}
}

func TestReservationCapacityConflictAndConcurrency(t *testing.T) {
	limits := codingrunner.DefaultLimits()
	reservation := reservationForLimits(limits)
	store, clock, _ := newFixtureStore(t, reservation)
	defer store.Close()
	binding := fixtureBinding(clock, "3")
	var wait sync.WaitGroup
	errorsFound := make(chan error, 16)
	ids := make(chan string, 16)
	for index := 0; index < 16; index++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			attempt, err := store.Reserve(t.Context(), binding, limits)
			if err != nil {
				errorsFound <- err
				return
			}
			ids <- attempt.ID()
		}()
	}
	wait.Wait()
	close(errorsFound)
	close(ids)
	for err := range errorsFound {
		t.Fatal(err)
	}
	want := ""
	for id := range ids {
		if want == "" {
			want = id
		} else if id != want {
			t.Fatalf("reservation IDs disagree: %q != %q", id, want)
		}
	}
	drift := binding
	drift.AgentArtifactSHA256 = strings.Repeat("f", 64)
	if _, err := store.Reserve(t.Context(), drift, limits); !errors.Is(err, ErrConflict) {
		t.Fatalf("binding drift error=%v", err)
	}
	if _, err := store.Reserve(t.Context(), fixtureBinding(clock, "4"), limits); !errors.Is(err, ErrCapacity) {
		t.Fatalf("capacity error=%v", err)
	}
	alias := fixtureBinding(clock, "4")
	alias.TicketID = "{33333333-3333-4333-8333-333333333334}"
	if _, err := store.Reserve(t.Context(), alias, limits); !errors.Is(err, ErrInvalid) {
		t.Fatalf("UUID alias error=%v", err)
	}
}

func TestTranscriptMismatchAbortAndCollectingRecovery(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "5")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	writer, err := attempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	body := []byte("{\"sequence\":1}\n")
	_, _ = writer.Write(body)
	wrong := codingrunner.TranscriptIdentity{SHA256: strings.Repeat("e", 64), SizeBytes: int64(len(body)), Events: 1}
	if _, err := writer.Commit(t.Context(), wrong); !errors.Is(err, ErrInvalid) {
		t.Fatalf("mismatched transcript error=%v", err)
	}
	if err := writer.Abort(); err != nil {
		t.Fatal(err)
	}
	if _, err := attempt.BeginTranscript(t.Context()); !errors.Is(err, ErrState) {
		t.Fatalf("aborted authoritative transcript became retryable: %v", err)
	}
	crashBinding := fixtureBinding(clock, "6")
	crashAttempt, err := store.Reserve(t.Context(), crashBinding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	writer, err = crashAttempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	_, _ = writer.Write(body[:len(body)-1])
	concrete := writer.(*transcriptWriter)
	_ = concrete.file.Close()
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 64,
		FinalizationGrace: time.Minute,
		OrphanGrace:       time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	recovered, err := reopened.Reserve(t.Context(), crashBinding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := recovered.BeginTranscript(t.Context()); !errors.Is(err, ErrState) {
		t.Fatalf("abandoned authoritative transcript became retryable: %v", err)
	}
	clock.now = clock.now.Add(2 * time.Minute)
	report, err := reopened.Sweep(t.Context())
	if err != nil || report.DeletedStagingFiles == 0 || report.DeletedRecords != 2 {
		t.Fatalf("staging sweep=%#v err=%v", report, err)
	}
}

func TestSweepRetainsActiveWriterAndClockRollbackFailsClosed(t *testing.T) {
	store, clock, _ := newFixtureStore(t, 512<<20)
	defer store.Close()
	binding := fixtureBinding(clock, "7")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	writer, err := attempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	body := []byte("{\"sequence\":1}\n")
	if _, err := writer.Write(body); err != nil {
		t.Fatal(err)
	}
	clock.now = binding.Deadline.Add(30 * time.Second)
	report, err := store.Sweep(t.Context())
	if err != nil || report.DeletedStagingFiles != 0 {
		t.Fatalf("active staging sweep=%#v err=%v", report, err)
	}
	identity := codingrunner.TranscriptIdentity{SHA256: digest(body), SizeBytes: int64(len(body)), Events: 1}
	if _, err := writer.Commit(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
	clock.now = binding.Deadline.Add(2 * time.Minute)
	report, err = store.Sweep(t.Context())
	if err != nil || report.ExpiredRecords != 1 {
		t.Fatalf("post-grace sweep=%#v err=%v", report, err)
	}
	clock.now = clock.now.Add(-time.Second)
	if _, err := store.Pending(t.Context(), 10); !errors.Is(err, ErrClock) {
		t.Fatalf("clock rollback error=%v", err)
	}
}

func TestCommittedObjectCorruptionFailsRecovery(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "8")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	objectPath := filepath.Join(root, "objects", "sha256", transcript.SHA256[:2], transcript.SHA256[2:])
	if err := os.Chmod(objectPath, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(objectPath, []byte("corrupt\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(objectPath, 0o400); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(store.config); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("corrupt object error=%v", err)
	}
}

func TestOverCapacityOrphanOpensDegradedAndSweepRecovers(t *testing.T) {
	limits := codingrunner.DefaultLimits()
	store, clock, root := newFixtureStore(t, reservationForLimits(limits))
	config := store.config
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	body := []byte("orphan")
	sha := digest(body)
	shard := filepath.Join(root, "objects", "sha256", sha[:2])
	if err := os.Mkdir(shard, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		t.Fatal(err)
	}
	path := filepath.Join(shard, sha[2:])
	if err := os.WriteFile(path, body, 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(config)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, err := reopened.Reserve(t.Context(), fixtureBinding(clock, "e"), limits); !errors.Is(err, ErrCapacity) {
		t.Fatalf("orphan capacity error=%v", err)
	}
	clock.now = clock.now.Add(2 * time.Minute)
	report, err := reopened.Sweep(t.Context())
	if err != nil || report.DeletedObjects != 1 || report.RemainingOrphanBytes != 0 {
		t.Fatalf("orphan recovery=%#v err=%v", report, err)
	}
	if _, err := reopened.Reserve(t.Context(), fixtureBinding(clock, "e"), limits); err != nil {
		t.Fatalf("reserve after orphan cleanup: %v", err)
	}
}

func TestSharedTranscriptSurvivesUntilEveryRecordIsReleased(t *testing.T) {
	store, clock, _ := newFixtureStore(t, 512<<20)
	defer store.Close()
	body := []byte("{\"sequence\":1}\n")
	type sealedAttempt struct {
		attempt *Attempt
		failure codingrunner.FreezeFailure
	}
	values := make([]sealedAttempt, 0, 2)
	for _, suffix := range []string{"1", "2"} {
		binding := fixtureBinding(clock, suffix)
		attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
		if err != nil {
			t.Fatal(err)
		}
		transcript := commitFixtureTranscript(t, attempt, body)
		failure := codingrunner.FreezeFailure{
			Kind: string(codingcontract.DomainRepairFailure), Code: "synthetic_failure",
			BaseTreeSHA256: strings.Repeat("1", 64), VisibleBundleSHA256: strings.Repeat("2", 64),
			FinalTreeSHA256: strings.Repeat("3", 64), ChangedPathRoot: strings.Repeat("4", 64),
			AuthoringEventRoot: strings.Repeat("5", 64), AuthoringTranscriptSHA256: transcript.SHA256,
			AuthoringTranscriptBytes: transcript.SizeBytes, ProtectedPathsIntact: true,
		}
		if _, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Failure: &failure}); err != nil {
			t.Fatal(err)
		}
		values = append(values, sealedAttempt{attempt: attempt, failure: failure})
	}
	if err := store.Release(t.Context(), values[0].attempt.ID(), strings.Repeat("a", 64)); err != nil {
		t.Fatal(err)
	}
	clock.now = clock.now.Add(2 * time.Minute)
	report, err := store.Sweep(t.Context())
	if err != nil || report.DeletedRecords != 1 || report.DeletedObjects != 0 {
		t.Fatalf("first shared sweep=%#v err=%v", report, err)
	}
	reader, err := store.OpenTranscript(t.Context(), values[1].attempt.ID())
	if err != nil {
		t.Fatal(err)
	}
	_ = reader.Close()
	if err := store.Release(t.Context(), values[1].attempt.ID(), strings.Repeat("b", 64)); err != nil {
		t.Fatal(err)
	}
	clock.now = clock.now.Add(2 * time.Minute)
	report, err = store.Sweep(t.Context())
	if err != nil || report.DeletedRecords != 1 || report.DeletedObjects != 1 {
		t.Fatalf("final shared sweep=%#v err=%v", report, err)
	}
}

func TestFilesystemPermissionsDiagnosticsAndHardlinkDefense(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "9")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	for _, directory := range []string{root, filepath.Join(root, ".staging"), filepath.Join(root, "records"), filepath.Join(root, "objects")} {
		info, err := os.Stat(directory)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o700 {
			t.Fatalf("directory=%s mode=%v", directory, info.Mode().Perm())
		}
	}
	recordInfo, err := os.Stat(filepath.Join(root, "records", attempt.ID()+".json"))
	if err != nil {
		t.Fatal(err)
	}
	if recordInfo.Mode().Perm() != 0o600 {
		t.Fatalf("record mode=%v", recordInfo.Mode().Perm())
	}
	objectPath := filepath.Join(root, "objects", "sha256", transcript.SHA256[:2], transcript.SHA256[2:])
	objectInfo, err := os.Stat(objectPath)
	if err != nil {
		t.Fatal(err)
	}
	if objectInfo.Mode().Perm() != 0o400 {
		t.Fatalf("object mode=%v", objectInfo.Mode().Perm())
	}
	if _, err := json.Marshal(store); err == nil {
		t.Fatal("store serialized")
	}
	if _, err := json.Marshal(attempt); err == nil {
		t.Fatal("attempt serialized")
	}
	if rendered := fmt.Sprintf("%#v", store); strings.Contains(rendered, root) {
		t.Fatalf("store diagnostic leaked root: %s", rendered)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(objectPath, filepath.Join(root, "outside-link")); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(store.config); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("hardlinked object error=%v", err)
	}
	unsafeRoot := t.TempDir()
	if err := os.Chmod(unsafeRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	config := store.config
	config.Root = unsafeRoot
	if _, err := Open(config); !errors.Is(err, ErrInvalid) {
		t.Fatalf("unsafe root mode error=%v", err)
	}
}

func TestCorruptionAndSymlinkRootsFailClosed(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "6")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	recordPath := filepath.Join(root, "records", attempt.ID()+".json")
	body, err := os.ReadFile(recordPath)
	if err != nil {
		t.Fatal(err)
	}
	body = bytes.Replace(body, []byte(`"generation":1`), []byte(`"generation":0`), 1)
	if err := os.WriteFile(recordPath, body, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(store.config); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("corrupt record error=%v", err)
	}
	realRoot := t.TempDir()
	_ = os.Chmod(realRoot, 0o700)
	symlink := filepath.Join(t.TempDir(), "outbox-link")
	if err := os.Symlink(realRoot, symlink); err != nil {
		t.Fatal(err)
	}
	config := store.config
	config.Root = symlink
	if _, err := Open(config); err == nil {
		t.Fatal("symlink root accepted")
	}
}

func TestUnknownRecordFieldFailsClosed(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	attempt, err := store.Reserve(t.Context(), fixtureBinding(clock, "a"), codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "records", attempt.ID()+".json")
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	body = bytes.TrimSpace(body)
	body = append(body[:len(body)-1], []byte(`,"grader_bytes":"forbidden"}`)...)
	body = append(body, '\n')
	if err := os.WriteFile(path, body, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(store.config); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("unknown record field error=%v", err)
	}
}

func TestFractionalMaxLifetimeReopensAndExactReserveSurvivesDeadline(t *testing.T) {
	root := t.TempDir()
	_ = os.Chmod(root, 0o700)
	clock := &fixtureClock{now: time.Now().UTC().Truncate(time.Second).Add(900 * time.Millisecond)}
	config := Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 8, FinalizationGrace: time.Minute,
		OrphanGrace: time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: clock.Now,
	}
	store, err := Open(config)
	if err != nil {
		t.Fatal(err)
	}
	binding := fixtureBinding(clock, "b")
	binding.Deadline = clock.now.Add(2 * time.Hour)
	first, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(config)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	clock.now = binding.Deadline.Add(time.Second)
	again, err := reopened.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil || again.ID() != first.ID() {
		t.Fatalf("expired exact reserve=%#v err=%v", again, err)
	}
}

func TestRestartRejectsClockRollbackAfterLatestDurableGeneration(t *testing.T) {
	store, clock, _ := newFixtureStore(t, 512<<20)
	config := store.config
	binding := fixtureBinding(clock, "1a")
	// Replace the suffix helper's non-UUID value with a canonical ticket while
	// retaining a distinct execution identity.
	binding.TicketID = "55555555-5555-4555-8555-555555555555"
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	clock.now = clock.now.Add(10 * time.Second)
	writer, err := attempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	concrete := writer.(*transcriptWriter)
	_ = concrete.file.Close()
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	clock.now = clock.now.Add(-5 * time.Second)
	if _, err := Open(config); !errors.Is(err, ErrClock) {
		t.Fatalf("restart clock rollback error=%v", err)
	}
}

func TestExactFinalizationCutoffRejectsAndSweepExpires(t *testing.T) {
	store, clock, _ := newFixtureStore(t, 512<<20)
	defer store.Close()
	binding := fixtureBinding(clock, "c")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	writer, err := attempt.BeginTranscript(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	body := []byte("{\"sequence\":1}\n")
	_, _ = writer.Write(body)
	clock.now = binding.Deadline.Add(store.config.FinalizationGrace)
	identity := codingrunner.TranscriptIdentity{SHA256: digest(body), SizeBytes: int64(len(body)), Events: 1}
	if _, err := writer.Commit(t.Context(), identity); !errors.Is(err, ErrState) {
		t.Fatalf("cutoff commit error=%v", err)
	}
	report, err := store.Sweep(t.Context())
	if err != nil || report.ExpiredRecords != 1 {
		t.Fatalf("cutoff sweep=%#v err=%v", report, err)
	}
	if err := writer.Abort(); err != nil {
		t.Fatal(err)
	}
}

func TestNoChangePresenceSurvivesRestart(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "d")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	submission := fixtureNoChangeSubmission(t, binding, transcript)
	if _, err := attempt.StoreFrozen(t.Context(), submission); err != nil {
		t.Fatal(err)
	}
	if _, err := attempt.Seal(t.Context(), codingrunner.FreezeResult{Submission: &submission}); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 64, FinalizationGrace: time.Minute,
		OrphanGrace: time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	loaded, err := reopened.LoadFrozen(t.Context(), attempt.ID())
	if err != nil || loaded.ChangedPaths == nil || loaded.Changes == nil || len(loaded.ChangedPaths) != 0 || len(loaded.Changes) != 0 {
		t.Fatalf("no-change submission=%#v err=%v", loaded, err)
	}
}

func TestEmptyFileContentSurvivesRestart(t *testing.T) {
	store, clock, root := newFixtureStore(t, 512<<20)
	binding := fixtureBinding(clock, "f")
	attempt, err := store.Reserve(t.Context(), binding, codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	transcript := commitFixtureTranscript(t, attempt, []byte("{\"sequence\":1}\n"))
	submission := fixtureSubmission(t, binding, transcript)
	if len(submission.Changes) != 1 || submission.Changes[0].AfterSHA256 == nil {
		t.Fatalf("fixture changes=%#v", submission.Changes)
	}
	emptySHA := digest([]byte{})
	submission.Changes[0].AfterContent = []byte{}
	*submission.Changes[0].AfterSHA256 = emptySHA
	submission.Patch = canonicalFixtureJSON(t, frozenPatchDocument{
		Schema: "dittobench-coding-frozen-patch-v1", CodingContractVersion: submission.CodingContractVersion,
		CaseID: submission.CaseID, BaseTreeSHA256: submission.BaseTreeSHA256,
		VisibleBundleSHA256: submission.VisibleBundleSHA256, Changes: submission.Changes,
	})
	submission.FrozenPatchSHA256 = digest(submission.Patch)
	if err := codingrunner.ValidateFrozenSubmission(submission, codingrunner.DefaultLimits()); err != nil {
		t.Fatal(err)
	}
	if _, err := attempt.StoreFrozen(t.Context(), submission); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(Config{
		Root: root, MaxTotalBytes: 512 << 20, MaxAttempts: 64, FinalizationGrace: time.Minute,
		OrphanGrace: time.Minute, ReleasedRetention: time.Minute, ExpiredRetention: time.Minute, Now: clock.Now,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	loaded, err := reopened.LoadFrozen(t.Context(), attempt.ID())
	if err != nil || len(loaded.Changes) != 1 || loaded.Changes[0].AfterContent == nil || len(loaded.Changes[0].AfterContent) != 0 {
		t.Fatalf("empty-file submission=%#v err=%v", loaded, err)
	}
}

func callTool(t *testing.T, handler http.Handler, request codingrunner.ToolRequest) codingrunner.ToolResponse {
	t.Helper()
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	httpRequest := httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body))
	handler.ServeHTTP(recorder, httpRequest)
	if recorder.Code != http.StatusOK {
		t.Fatalf("tool status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response codingrunner.ToolResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response
}

func tarFixture(t *testing.T, name, body string) []byte {
	t.Helper()
	var output bytes.Buffer
	archive := tar.NewWriter(&output)
	if err := archive.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
		t.Fatal(err)
	}
	if _, err := io.WriteString(archive, body); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func digest(body []byte) string {
	value := sha256.Sum256(body)
	return hex.EncodeToString(value[:])
}

func canonicalFixtureJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projection any
	if err := decoder.Decode(&projection); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projection); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}
