package codingsupervisor

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"log/slog"
	"sync"
	"time"
)

const (
	RequestSchema  = "dittobench-coding-attempt-supervisor-request-v1"
	ResponseSchema = "dittobench-coding-attempt-supervisor-response-v1"

	maximumRequestBytes    = 8 << 20
	maximumResponseBytes   = 8 << 20
	maximumLeaseBytes      = 6 << 20
	maximumOutcomeBytes    = 6 << 20
	defaultTimeout         = 15 * time.Minute
	maximumTimeout         = 30 * time.Minute
	maximumBindingLifetime = 2 * time.Hour
)

var (
	ErrInvalidConfig = errors.New("coding attempt supervisor configuration is invalid")
	ErrInvalid       = errors.New("coding attempt supervisor request is invalid")
	ErrUnauthorized  = errors.New("coding attempt supervisor authorization failed")
	ErrConcurrent    = errors.New("coding attempt supervisor operation is already active")
	ErrConflict      = errors.New("coding attempt supervisor authority conflicts")
	ErrUnavailable   = errors.New("coding attempt supervisor backend is unavailable")
	ErrDeadline      = errors.New("coding attempt supervisor deadline expired")
	ErrClosed        = errors.New("coding attempt supervisor is closed")
	ErrPrivate       = errors.New("coding attempt supervisor private state cannot be serialized")
)

type Operation string

const (
	OperationAuthor         Operation = "author"
	OperationGrade          Operation = "grade"
	OperationAbortAuthoring Operation = "abort_authoring"
	OperationAbortGrading   Operation = "abort_grading"
	OperationRecover        Operation = "recover"
)

type Request struct {
	Schema      string          `json:"schema"`
	Operation   Operation       `json:"operation"`
	OperationID string          `json:"operation_id"`
	TicketID    string          `json:"ticket_id"`
	CodingRunID string          `json:"coding_run_id"`
	Deadline    time.Time       `json:"deadline"`
	Lease       json.RawMessage `json:"lease"`
	Authoring   json.RawMessage `json:"authoring"`
}

type AuthoringOutcome struct {
	Evidence                      json.RawMessage `json:"evidence"`
	AuthoringTranscriptObjectKey  string          `json:"authoring_transcript_object_key"`
	AuthoringTranscriptBytes      int64           `json:"authoring_transcript_bytes"`
	AuthoringEventCount           uint64          `json:"authoring_event_count"`
	FrozenSubmissionObjectKey     string          `json:"frozen_submission_object_key"`
	CapabilitiesRevoked           bool            `json:"capabilities_revoked"`
	AuthoringEnvironmentDestroyed bool            `json:"authoring_environment_destroyed"`
}

type GradingOutcome struct {
	TaskEvidence                []json.RawMessage `json:"task_evidence"`
	GradingEnvironmentDestroyed bool              `json:"grading_environment_destroyed"`
}

type RecoveryOutcome struct {
	State            string  `json:"state"`
	PublicationStage *string `json:"publication_stage"`
	RequestSHA256    *string `json:"request_sha256"`
}

type Response struct {
	Schema      string            `json:"schema"`
	Operation   Operation         `json:"operation"`
	OperationID string            `json:"operation_id"`
	TicketID    string            `json:"ticket_id"`
	CodingRunID string            `json:"coding_run_id"`
	Authoring   *AuthoringOutcome `json:"authoring"`
	Grading     *GradingOutcome   `json:"grading"`
	Recovery    *RecoveryOutcome  `json:"recovery"`
	Aborted     bool              `json:"aborted"`
}

func (response Response) String() string {
	return "CodingAttemptSupervisorResponse{operation=" + string(response.Operation) + "}"
}

func (response Response) GoString() string { return response.String() }

func (response Response) LogValue() slog.Value {
	return slog.StringValue("coding-attempt-supervisor-response-" + string(response.Operation))
}

type Backend interface {
	Execute(context.Context, Request) (Response, error)
}

type Config struct {
	ControlToken     string
	Backend          Backend
	OperationTimeout time.Duration
	Now              func() time.Time
}

type Service struct {
	mu      sync.Mutex
	backend Backend
	now     func() time.Time
	timeout time.Duration
	token   [sha256.Size]byte
	lastNow time.Time
	active  map[string]struct{}
	closed  bool
}

func (config Config) String() string { return "CodingAttemptSupervisorConfig{private}" }

func (config Config) GoString() string { return config.String() }

func (config Config) LogValue() slog.Value {
	return slog.StringValue("coding-attempt-supervisor-config")
}

func (config Config) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

func (request Request) String() string {
	return "CodingAttemptSupervisorRequest{operation=" + string(request.Operation) + "}"
}

func (request Request) GoString() string { return request.String() }

func (request Request) LogValue() slog.Value {
	return slog.StringValue("coding-attempt-supervisor-request-" + string(request.Operation))
}

func (request Request) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

func (service *Service) String() string {
	if service == nil {
		return "CodingAttemptSupervisor{nil=true}"
	}
	return "CodingAttemptSupervisor{private}"
}

func (service *Service) GoString() string { return service.String() }

func (service *Service) LogValue() slog.Value {
	return slog.StringValue("coding-attempt-supervisor")
}

func (service *Service) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

var _ json.Marshaler = Config{}
var _ json.Marshaler = Request{}
var _ json.Marshaler = (*Service)(nil)
