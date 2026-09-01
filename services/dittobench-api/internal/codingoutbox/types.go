package codingoutbox

import (
	"context"
	"errors"
	"hash"
	"io"
	"log/slog"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingevidence"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const (
	recordSchema                   = "dittobench-coding-evidence-outbox-v4"
	maximumRecordBytes             = 4 << 20
	recordReserveBytes             = 2 * maximumRecordBytes
	maximumPublicationRequestBytes = 4 << 20
	maximumPublicationAckBytes     = 1 << 20
	publicationReserveBytes        = 2 * (maximumPublicationRequestBytes + maximumPublicationAckBytes)
	maximumRootBytes               = 1 << 40
	maximumAttempts                = 100_000
)

var (
	ErrInvalid  = errors.New("coding evidence outbox input is invalid")
	ErrLocked   = errors.New("coding evidence outbox is already open")
	ErrCapacity = errors.New("coding evidence outbox capacity is exhausted")
	ErrConflict = errors.New("coding evidence outbox identity conflicts")
	ErrCorrupt  = errors.New("coding evidence outbox is corrupt")
	ErrState    = errors.New("coding evidence outbox state transition is invalid")
	ErrClosed   = errors.New("coding evidence outbox is closed")
	ErrClock    = errors.New("coding evidence outbox clock moved backwards")
)

type State string
type Purpose string
type PublicationStage string

const (
	StateReserved             State = "reserved"
	StateCollecting           State = "collecting"
	StateReady                State = "ready"
	StateTerminalWithoutPatch State = "terminal_without_patch"
	StateReleased             State = "released"
	StateExpired              State = "expired"
)

const (
	PublicationAuthoringFreeze PublicationStage = "authoring_freeze"
	PublicationTerminalResult  PublicationStage = "terminal_result"
)

const (
	PurposeCertification Purpose = "certification"
	PurposeShadowAttempt Purpose = "shadow_attempt"
)

type Config struct {
	Root              string
	MaxTotalBytes     int64
	MaxAttempts       int
	FinalizationGrace time.Duration
	OrphanGrace       time.Duration
	ReleasedRetention time.Duration
	ExpiredRetention  time.Duration
	Now               func() time.Time // must not call Store methods
}

type Binding struct {
	Purpose                Purpose   `json:"purpose"`
	ExecutionID            string    `json:"execution_id"`
	AgentArtifactSHA256    string    `json:"agent_artifact_sha256"`
	HarnessInstanceID      string    `json:"harness_instance_id"`
	AuthoritySHA256        string    `json:"authority_sha256"`
	HarnessAuthoritySHA256 string    `json:"harness_authority_sha256,omitempty"`
	ScreenedImageSHA256    string    `json:"screened_image_sha256,omitempty"`
	TicketID               string    `json:"ticket_id"`
	CaseID                 string    `json:"case_id"`
	ProfileCapabilityID    string    `json:"profile_capability_id"`
	Deadline               time.Time `json:"deadline"`
}

type SignedLimits struct {
	MaxBundleBytes      int64  `json:"max_bundle_bytes"`
	MaxWorkspaceBytes   int64  `json:"max_workspace_bytes"`
	MaxFileBytes        int64  `json:"max_file_bytes"`
	MaxPatchBytes       int64  `json:"max_patch_bytes"`
	MaxEntries          int    `json:"max_entries"`
	MaxToolCalls        uint32 `json:"max_tool_calls"`
	MaxReadBytes        int    `json:"max_read_bytes"`
	MaxResponseBytes    int    `json:"max_response_bytes"`
	MaxSearchResults    int    `json:"max_search_results"`
	MaxReplayCacheBytes int64  `json:"max_replay_cache_bytes"`
	MaxTranscriptBytes  int64  `json:"max_transcript_bytes"`
}

func limitsFromRunner(value codingrunner.Limits) SignedLimits {
	return SignedLimits{
		MaxBundleBytes: value.MaxBundleBytes, MaxWorkspaceBytes: value.MaxWorkspaceBytes,
		MaxFileBytes: value.MaxFileBytes, MaxPatchBytes: value.MaxPatchBytes,
		MaxEntries: value.MaxEntries, MaxToolCalls: value.MaxToolCalls,
		MaxReadBytes: value.MaxReadBytes, MaxResponseBytes: value.MaxResponseBytes,
		MaxSearchResults: value.MaxSearchResults, MaxReplayCacheBytes: value.MaxReplayCacheBytes,
		MaxTranscriptBytes: value.MaxTranscriptBytes,
	}
}

func reservationForLimits(value codingrunner.Limits) int64 {
	return value.MaxTranscriptBytes + value.MaxPatchBytes + recordReserveBytes
}

func reservationForPurpose(purpose Purpose, value codingrunner.Limits) int64 {
	reservation := reservationForLimits(value)
	if purpose == PurposeShadowAttempt {
		reservation += publicationReserveBytes
	}
	return reservation
}

func (value SignedLimits) runner() codingrunner.Limits {
	return codingrunner.Limits{
		MaxBundleBytes: value.MaxBundleBytes, MaxWorkspaceBytes: value.MaxWorkspaceBytes,
		MaxFileBytes: value.MaxFileBytes, MaxPatchBytes: value.MaxPatchBytes,
		MaxEntries: value.MaxEntries, MaxToolCalls: value.MaxToolCalls,
		MaxReadBytes: value.MaxReadBytes, MaxResponseBytes: value.MaxResponseBytes,
		MaxSearchResults: value.MaxSearchResults, MaxReplayCacheBytes: value.MaxReplayCacheBytes,
		MaxTranscriptBytes: value.MaxTranscriptBytes,
	}
}

type TranscriptArtifact struct {
	ObjectKey string `json:"object_key"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
	Events    uint64 `json:"events"`
}

type FrozenArtifact struct {
	ObjectKey         string `json:"object_key"`
	FrozenPatchSHA256 string `json:"frozen_patch_sha256"`
	SizeBytes         int64  `json:"size_bytes"`
	FinalTreeSHA256   string `json:"final_tree_sha256"`
	ChangedPathRoot   string `json:"changed_path_root"`
}

type FrozenRecord struct {
	Artifact FrozenArtifact `json:"artifact"`
	Metadata FrozenMetadata `json:"metadata"`
}

// PublicationAuthority is trusted local identity independently checked against
// the signed request and acknowledgement bytes before either object is stored.
type PublicationAuthority struct {
	AgentID               string `json:"agent_id"`
	BenchVersion          int    `json:"bench_version"`
	RunRowID              string `json:"run_row_id"`
	CodingRunID           string `json:"coding_run_id"`
	ScreenedImageSHA256   string `json:"screened_image_sha256"`
	RunManifestSHA256     string `json:"run_manifest_sha256"`
	TaskSetManifestSHA256 string `json:"task_set_manifest_sha256"`
	EvidenceSHA256        string `json:"evidence_sha256"`
}

type PublicationArtifact struct {
	ObjectKey string `json:"object_key"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

type PublicationRecord struct {
	Stage                     PublicationStage     `json:"stage"`
	Authority                 PublicationAuthority `json:"authority"`
	Request                   PublicationArtifact  `json:"request"`
	Acknowledgement           *PublicationArtifact `json:"acknowledgement,omitempty"`
	AcknowledgedRequestSHA256 string               `json:"acknowledged_request_sha256,omitempty"`
	RemoteAuthorityID         string               `json:"remote_authority_id,omitempty"`
	PreparedAtUnix            int64                `json:"prepared_at_unix"`
	AcknowledgedAtUnix        int64                `json:"acknowledged_at_unix,omitempty"`
}

// ReleaseFinalization is the immutable subset of Platform's verified-object
// acknowledgement that authorizes local shadow evidence release. Idempotent
// response metadata is deliberately omitted because it is not object identity.
type ReleaseFinalization struct {
	TicketID            string              `json:"ticket_id"`
	ClaimGeneration     int                 `json:"claim_generation"`
	UploadID            string              `json:"upload_id"`
	EvidenceKind        codingevidence.Kind `json:"evidence_kind"`
	SHA256              string              `json:"sha256"`
	SizeBytes           int64               `json:"size_bytes"`
	FinalizedAtUnixNano int64               `json:"finalized_at_unix_nano"`
}

// ReleaseReservation is the durable, non-bearer identity of the Platform PUT
// capability reserved for terminal acknowledgement evidence. The URL,
// checksum header and expiry are deliberately never persisted.
type ReleaseReservation struct {
	TicketID        string              `json:"ticket_id"`
	ClaimGeneration int                 `json:"claim_generation"`
	UploadID        string              `json:"upload_id"`
	EvidenceKind    codingevidence.Kind `json:"evidence_kind"`
	SHA256          string              `json:"sha256"`
	SizeBytes       int64               `json:"size_bytes"`
}

// PendingRelease is the bounded local recovery authority for one finalized or
// still-finalizable terminal acknowledgement. It contains no bearer material.
type PendingRelease struct {
	RecordID               string
	TicketID               string
	TerminalEvidenceSHA256 string
	Reservation            ReleaseReservation
}

type PendingPublication struct {
	RecordID  string
	Binding   Binding
	Stage     PublicationStage
	Authority PublicationAuthority
	Request   PublicationArtifact
}

func (publication PendingPublication) String() string {
	return "CodingPendingPublication{stage=" + strconv.Quote(string(publication.Stage)) + "}"
}

func (publication PendingPublication) GoString() string { return publication.String() }

func (publication PendingPublication) LogValue() slog.Value {
	return slog.StringValue("coding-pending-publication-" + string(publication.Stage))
}

func (publication PendingPublication) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding pending publications cannot be serialized")
}

type FrozenMetadata struct {
	CodingContractVersion     int    `json:"coding_contract_version"`
	CaseID                    string `json:"case_id"`
	BaseTreeSHA256            string `json:"base_tree_sha256"`
	VisibleBundleSHA256       string `json:"visible_bundle_sha256"`
	FinalTreeSHA256           string `json:"final_tree_sha256"`
	FrozenPatchSHA256         string `json:"frozen_patch_sha256"`
	ChangedPathRoot           string `json:"changed_path_root"`
	AuthoringEventRoot        string `json:"authoring_event_root"`
	AuthoringTranscriptSHA256 string `json:"authoring_transcript_sha256"`
	AuthoringTranscriptBytes  int64  `json:"authoring_transcript_bytes"`
	ProtectedPathsIntact      bool   `json:"protected_paths_intact"`
}

type Record struct {
	Schema                string                      `json:"schema"`
	Generation            uint64                      `json:"generation"`
	ID                    string                      `json:"id"`
	Binding               Binding                     `json:"binding"`
	BindingSHA256         string                      `json:"binding_sha256"`
	Limits                SignedLimits                `json:"limits"`
	ReservedBytes         int64                       `json:"reserved_bytes"`
	State                 State                       `json:"state"`
	WriterNonce           string                      `json:"writer_nonce,omitempty"`
	StagingName           string                      `json:"staging_name,omitempty"`
	Transcript            *TranscriptArtifact         `json:"transcript,omitempty"`
	Frozen                *FrozenRecord               `json:"frozen,omitempty"`
	Failure               *codingrunner.FreezeFailure `json:"failure,omitempty"`
	OutcomeSHA256         string                      `json:"outcome_sha256,omitempty"`
	AuthoringPublication  *PublicationRecord          `json:"authoring_publication,omitempty"`
	TerminalPublication   *PublicationRecord          `json:"terminal_publication,omitempty"`
	ReleaseEvidenceSHA256 string                      `json:"release_evidence_sha256,omitempty"`
	ReleaseReservation    *ReleaseReservation         `json:"release_reservation,omitempty"`
	ReleaseFinalization   *ReleaseFinalization        `json:"release_finalization,omitempty"`
	CreatedAtUnixNano     int64                       `json:"created_at_unix_nano"`
	UpdatedAtUnixNano     int64                       `json:"updated_at_unix_nano"`
	SealedAtUnix          int64                       `json:"sealed_at_unix,omitempty"`
	ReleasedAtUnix        int64                       `json:"released_at_unix,omitempty"`
	ExpiredAtUnix         int64                       `json:"expired_at_unix,omitempty"`
	ChecksumSHA256        string                      `json:"checksum_sha256"`
}

type SweepReport struct {
	ExpiredRecords       int
	DeletedRecords       int
	DeletedObjects       int
	DeletedStagingFiles  int
	RemainingRecords     int
	RemainingReservation int64
	RemainingOrphanBytes int64
}

type Store struct {
	mu            sync.Mutex
	config        Config
	root          string
	dirs          directorySet
	rootLeaf      string
	rootDev       uint64
	rootIno       uint64
	records       map[string]*Record
	reserved      int64
	orphanBytes   int64
	physicalKnown bool
	lastNow       time.Time
	closed        bool
}

type Attempt struct {
	store *Store
	id    string
}

func (*Store) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding evidence outbox stores cannot be serialized")
}

func (store *Store) String() string {
	if store == nil {
		return "CodingEvidenceOutbox{nil=true}"
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	return "CodingEvidenceOutbox{records=" + strconv.Itoa(len(store.records)) +
		" closed=" + strconv.FormatBool(store.closed) + "}"
}

func (store *Store) GoString() string { return store.String() }

func (store *Store) LogValue() slog.Value {
	if store == nil {
		return slog.GroupValue(slog.Bool("nil", true))
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	return slog.GroupValue(slog.Int("records", len(store.records)), slog.Bool("closed", store.closed))
}

func (*Attempt) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding evidence outbox attempts cannot be serialized")
}

func (attempt *Attempt) String() string {
	if attempt == nil {
		return "CodingEvidenceAttempt{nil=true}"
	}
	return "CodingEvidenceAttempt{id=" + strconv.Quote(attempt.id) + "}"
}

func (attempt *Attempt) GoString() string { return attempt.String() }

func (attempt *Attempt) LogValue() slog.Value {
	if attempt == nil {
		return slog.GroupValue(slog.Bool("nil", true))
	}
	return slog.GroupValue(slog.String("id", attempt.id))
}

type TranscriptWriter interface {
	io.Writer
	Commit(context.Context, codingrunner.TranscriptIdentity) (TranscriptArtifact, error)
	Abort() error
}

type transcriptWriter struct {
	mu        sync.Mutex
	store     *Store
	recordID  string
	nonce     string
	file      *os.File
	name      string
	stageDev  uint64
	stageIno  uint64
	hash      hash.Hash
	written   int64
	events    uint64
	lineBytes int64
	invalid   bool
	sealed    bool
	installed bool
	committed *TranscriptArtifact
	abortDone bool
	abortErr  error
	closed    bool
}

type directorySet struct {
	parent    *os.File
	root      *os.File
	staging   *os.File
	records   *os.File
	objects   *os.File
	sha256Dir *os.File
}
