package codingsupervisor

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"sync"

	"github.com/google/uuid"
)

const (
	defaultMaximumSessions = 1_024
	hardMaximumSessions    = 100_000
)

// PhaseRunner owns the future authoring, grading, cleanup, and durable
// recovery composition. Implementations must consume AuthoringInput
// synchronously, must not retain any field after Author returns, transfer
// exclusive ownership of returned buffers, and support concurrent attempts.
type PhaseRunner interface {
	Author(context.Context, AuthoringInput) (AuthoringOutcome, error)
	Grade(context.Context, Request) (GradingOutcome, error)
	AbortAuthoring(context.Context, Request) error
	AbortGrading(context.Context, Request) error
	Recover(context.Context, Request) (RecoveryOutcome, error)
}

// AuthoringInput gives the trusted phase runner the prepared broker authority.
// Its private key aliases a buffer that is zeroed as soon as Author returns.
type AuthoringInput struct {
	Request          Request
	SessionID        string
	BrokerPublicKey  string
	BrokerPrivateKey ed25519.PrivateKey
}

func (input AuthoringInput) String() string {
	return "CodingSupervisorAuthoringInput{private=true}"
}

func (input AuthoringInput) GoString() string { return input.String() }

func (input AuthoringInput) LogValue() slog.Value {
	return slog.StringValue("coding-supervisor-authoring-input-private")
}

func (AuthoringInput) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

// SessionBackendConfig supplies only the future phase runner and a bounded
// process-local table size. Production entropy is never caller-configurable.
type SessionBackendConfig struct {
	Runner          PhaseRunner
	MaximumSessions int
}

func (SessionBackendConfig) String() string { return "CodingSupervisorSessionBackendConfig{private}" }

func (config SessionBackendConfig) GoString() string { return config.String() }

func (SessionBackendConfig) LogValue() slog.Value {
	return slog.StringValue("coding-supervisor-session-backend-config-private")
}

func (SessionBackendConfig) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

type sessionState string

const (
	sessionPrepared    sessionState = "prepared"
	sessionAuthoring   sessionState = "authoring"
	sessionAuthored    sessionState = "authored"
	sessionGrading     sessionState = "grading"
	sessionGraded      sessionState = "graded"
	sessionAborting    sessionState = "aborting"
	sessionAborted     sessionState = "aborted"
	sessionPhaseFailed sessionState = "phase_failed"
)

type sessionRecord struct {
	preparation  PreparationOutcome
	leaseSHA256  [sha256.Size]byte
	deadlineUnix int64
	privateKey   ed25519.PrivateKey
	state        sessionState
	authoring    *AuthoringOutcome
	gradingLease [sha256.Size]byte
	gradingUntil int64
	grading      *GradingOutcome
	failed       Operation
	failure      error
}

// SessionBackend is an intentionally process-local supervisor state machine.
// It generates the broker key during prepare, lends it to one authoring call,
// then zeroes it. The real phase runner remains an injected, unwired port.
type SessionBackend struct {
	mu              sync.Mutex
	runner          PhaseRunner
	random          io.Reader
	newSessionID    func() (string, error)
	maximumSessions int
	sessions        map[string]*sessionRecord
	sessionIDs      map[string]struct{}
	publicKeys      map[string]struct{}
	active          int
	closed          bool
}

func NewSessionBackend(config SessionBackendConfig) (*SessionBackend, error) {
	return newSessionBackend(config, rand.Reader, func() (string, error) {
		identifier, err := uuid.NewRandom()
		return identifier.String(), err
	})
}

func newSessionBackend(
	config SessionBackendConfig,
	random io.Reader,
	newSessionID func() (string, error),
) (*SessionBackend, error) {
	if nilLike(config.Runner) || nilLike(random) || newSessionID == nil ||
		config.MaximumSessions < 0 || config.MaximumSessions > hardMaximumSessions {
		return nil, ErrInvalidConfig
	}
	if config.MaximumSessions == 0 {
		config.MaximumSessions = defaultMaximumSessions
	}
	return &SessionBackend{
		runner: config.Runner, random: random, newSessionID: newSessionID,
		maximumSessions: config.MaximumSessions, sessions: make(map[string]*sessionRecord),
		sessionIDs: make(map[string]struct{}), publicKeys: make(map[string]struct{}),
	}, nil
}

func (backend *SessionBackend) Execute(ctx context.Context, request Request) (Response, error) {
	if backend == nil {
		return Response{}, ErrClosed
	}
	if err := ctx.Err(); err != nil {
		return Response{}, err
	}
	if err := backend.beginExecution(); err != nil {
		return Response{}, err
	}
	defer backend.endExecution()
	request = cloneRequest(request)
	switch request.Operation {
	case OperationPrepare:
		return backend.prepare(request)
	case OperationAuthor:
		return backend.author(ctx, request)
	case OperationGrade:
		return backend.grade(ctx, request)
	case OperationAbortAuthoring, OperationAbortGrading:
		return backend.abort(ctx, request)
	case OperationRecover:
		return backend.recover(ctx, request)
	default:
		return Response{}, ErrInvalid
	}
}

func (backend *SessionBackend) beginExecution() error {
	backend.mu.Lock()
	defer backend.mu.Unlock()
	if backend.closed {
		return ErrClosed
	}
	backend.active++
	return nil
}

func (backend *SessionBackend) endExecution() {
	backend.mu.Lock()
	backend.active--
	backend.mu.Unlock()
}

func (backend *SessionBackend) prepare(request Request) (Response, error) {
	digest, err := canonicalRawSHA256(request.Lease)
	if err != nil {
		return Response{}, ErrInvalid
	}
	key := sessionKey(request)
	backend.mu.Lock()
	defer backend.mu.Unlock()
	if backend.closed {
		return Response{}, ErrClosed
	}
	if record := backend.sessions[key]; record != nil {
		if record.leaseSHA256 != digest || record.deadlineUnix != request.Deadline.UnixNano() {
			return Response{}, ErrConflict
		}
		return preparationResponse(request, record.preparation), nil
	}
	if len(backend.sessions) >= backend.maximumSessions {
		return Response{}, ErrUnavailable
	}
	publicKey, privateKey, err := generateBrokerKey(backend.random)
	if err != nil {
		return Response{}, ErrUnavailable
	}
	sessionID, err := backend.newSessionID()
	if err != nil || !canonicalUUID(sessionID) {
		zero(privateKey)
		return Response{}, ErrUnavailable
	}
	preparation := PreparationOutcome{
		SessionID: sessionID, BrokerPublicKey: base64.RawURLEncoding.EncodeToString(publicKey),
	}
	if validatePreparation(preparation) != nil {
		zero(privateKey)
		return Response{}, ErrUnavailable
	}
	if _, exists := backend.sessionIDs[preparation.SessionID]; exists {
		zero(privateKey)
		return Response{}, ErrUnavailable
	}
	if _, exists := backend.publicKeys[preparation.BrokerPublicKey]; exists {
		zero(privateKey)
		return Response{}, ErrUnavailable
	}
	backend.sessions[key] = &sessionRecord{
		preparation: preparation, leaseSHA256: digest, deadlineUnix: request.Deadline.UnixNano(),
		privateKey: append(ed25519.PrivateKey(nil), privateKey...), state: sessionPrepared,
	}
	backend.sessionIDs[preparation.SessionID] = struct{}{}
	backend.publicKeys[preparation.BrokerPublicKey] = struct{}{}
	zero(privateKey)
	return preparationResponse(request, preparation), nil
}

func (backend *SessionBackend) author(ctx context.Context, request Request) (Response, error) {
	digest, err := canonicalRawSHA256(request.Lease)
	if err != nil {
		return Response{}, ErrInvalid
	}
	key := sessionKey(request)
	backend.mu.Lock()
	if backend.closed {
		backend.mu.Unlock()
		return Response{}, ErrClosed
	}
	record := backend.sessions[key]
	if record == nil || record.leaseSHA256 != digest || record.deadlineUnix != request.Deadline.UnixNano() {
		backend.mu.Unlock()
		return Response{}, ErrConflict
	}
	if record.authoring != nil {
		outcome := cloneAuthoringOutcome(*record.authoring)
		backend.mu.Unlock()
		return authoringResponse(request, outcome), nil
	}
	if record.state == sessionAuthoring || record.state == sessionGrading || record.state == sessionAborting {
		backend.mu.Unlock()
		return Response{}, ErrConcurrent
	}
	if record.state == sessionPhaseFailed && record.failed == OperationAuthor {
		failure := record.failure
		backend.mu.Unlock()
		return Response{}, failure
	}
	if record.state != sessionPrepared || len(record.privateKey) != ed25519.PrivateKeySize {
		backend.mu.Unlock()
		return Response{}, ErrConflict
	}
	privateKey := append(ed25519.PrivateKey(nil), record.privateKey...)
	input := AuthoringInput{
		Request: cloneRequest(request), SessionID: record.preparation.SessionID,
		BrokerPublicKey: record.preparation.BrokerPublicKey, BrokerPrivateKey: privateKey,
	}
	record.state = sessionAuthoring
	backend.mu.Unlock()

	runnerOutcome, runErr := backend.runAuthor(ctx, input)
	outcome := cloneAuthoringOutcome(runnerOutcome)
	zeroAuthoringOutcome(&runnerOutcome)
	if runErr == nil && validateAuthoring(outcome) != nil {
		runErr = ErrConflict
	}

	backend.mu.Lock()
	record = backend.sessions[key]
	if record == nil || record.state != sessionAuthoring {
		backend.mu.Unlock()
		return Response{}, ErrConflict
	}
	zero(record.privateKey)
	record.privateKey = nil
	if runErr != nil {
		record.state = sessionPhaseFailed
		record.failed = OperationAuthor
		record.failure = safePhaseError(runErr)
		failure := record.failure
		backend.mu.Unlock()
		return Response{}, failure
	}
	record.state = sessionAuthored
	record.authoring = &outcome
	record.failure = nil
	backend.mu.Unlock()
	return authoringResponse(request, outcome), nil
}

func (backend *SessionBackend) runAuthor(ctx context.Context, input AuthoringInput) (AuthoringOutcome, error) {
	defer func() {
		zero(input.BrokerPrivateKey)
		zero(input.Request.Lease)
		zero(input.Request.Authoring)
		zero(input.Request.Grant)
	}()
	return backend.runner.Author(ctx, input)
}

func (backend *SessionBackend) grade(ctx context.Context, request Request) (Response, error) {
	leaseDigest, err := canonicalRawSHA256(request.Lease)
	if err != nil {
		return Response{}, ErrInvalid
	}
	key := sessionKey(request)
	backend.mu.Lock()
	if backend.closed {
		backend.mu.Unlock()
		return Response{}, ErrClosed
	}
	record := backend.sessions[key]
	if record == nil || record.authoring == nil || !requestMatchesAuthoring(request.Authoring, *record.authoring) {
		backend.mu.Unlock()
		return Response{}, ErrConflict
	}
	if record.grading != nil {
		if record.gradingLease != leaseDigest || record.gradingUntil != request.Deadline.UnixNano() {
			backend.mu.Unlock()
			return Response{}, ErrConflict
		}
		outcome := cloneGradingOutcome(*record.grading)
		backend.mu.Unlock()
		return gradingResponse(request, outcome), nil
	}
	if record.state == sessionAuthoring || record.state == sessionGrading || record.state == sessionAborting {
		backend.mu.Unlock()
		return Response{}, ErrConcurrent
	}
	if record.state == sessionPhaseFailed && record.failed == OperationGrade {
		if record.gradingLease != leaseDigest || record.gradingUntil != request.Deadline.UnixNano() {
			backend.mu.Unlock()
			return Response{}, ErrConflict
		}
		failure := record.failure
		backend.mu.Unlock()
		return Response{}, failure
	}
	if record.state != sessionAuthored {
		backend.mu.Unlock()
		return Response{}, ErrConflict
	}
	record.gradingLease = leaseDigest
	record.gradingUntil = request.Deadline.UnixNano()
	record.state = sessionGrading
	backend.mu.Unlock()

	runnerOutcome, runErr := backend.runGrade(ctx, cloneRequest(request))
	outcome := cloneGradingOutcome(runnerOutcome)
	zeroGradingOutcome(&runnerOutcome)
	if runErr == nil && validateGrading(outcome, request.CodingRunID, request.TicketID) != nil {
		runErr = ErrConflict
	}

	backend.mu.Lock()
	record = backend.sessions[key]
	if record == nil || record.state != sessionGrading {
		backend.mu.Unlock()
		return Response{}, ErrConflict
	}
	if runErr != nil {
		record.state = sessionPhaseFailed
		record.failed = OperationGrade
		record.failure = safePhaseError(runErr)
		failure := record.failure
		backend.mu.Unlock()
		return Response{}, failure
	}
	record.state = sessionGraded
	record.grading = &outcome
	record.failure = nil
	backend.mu.Unlock()
	return gradingResponse(request, outcome), nil
}

func (backend *SessionBackend) runGrade(ctx context.Context, request Request) (GradingOutcome, error) {
	defer zeroRequest(&request)
	return backend.runner.Grade(ctx, request)
}

func (backend *SessionBackend) abort(ctx context.Context, request Request) (Response, error) {
	key := sessionKey(request)
	backend.mu.Lock()
	if backend.closed {
		backend.mu.Unlock()
		return Response{}, ErrClosed
	}
	record := backend.sessions[key]
	if record != nil {
		if record.state == sessionAuthoring || record.state == sessionGrading || record.state == sessionAborting {
			backend.mu.Unlock()
			return Response{}, ErrConcurrent
		}
		if record.state == sessionAborted {
			backend.mu.Unlock()
			return abortedResponse(request), nil
		}
		zero(record.privateKey)
		record.privateKey = nil
		record.state = sessionAborting
	}
	backend.mu.Unlock()

	var runErr error
	if request.Operation == OperationAbortAuthoring {
		runErr = backend.runAbortAuthoring(ctx, cloneRequest(request))
	} else {
		runErr = backend.runAbortGrading(ctx, cloneRequest(request))
	}

	backend.mu.Lock()
	if record != nil {
		if runErr != nil {
			record.state = sessionPhaseFailed
			record.failed = request.Operation
			record.failure = safePhaseError(runErr)
		} else {
			record.state = sessionAborted
			zeroAuthoringOutcome(record.authoring)
			zeroGradingOutcome(record.grading)
			record.authoring = nil
			record.grading = nil
			record.failure = nil
		}
	}
	backend.mu.Unlock()
	if runErr != nil {
		return Response{}, safePhaseError(runErr)
	}
	return abortedResponse(request), nil
}

func (backend *SessionBackend) runAbortAuthoring(ctx context.Context, request Request) error {
	defer zeroRequest(&request)
	return backend.runner.AbortAuthoring(ctx, request)
}

func (backend *SessionBackend) runAbortGrading(ctx context.Context, request Request) error {
	defer zeroRequest(&request)
	return backend.runner.AbortGrading(ctx, request)
}

func (backend *SessionBackend) recover(ctx context.Context, request Request) (Response, error) {
	backend.mu.Lock()
	if backend.closed {
		backend.mu.Unlock()
		return Response{}, ErrClosed
	}
	if record := backend.sessions[sessionKey(request)]; record != nil &&
		(record.state == sessionAuthoring || record.state == sessionGrading || record.state == sessionAborting) {
		backend.mu.Unlock()
		return Response{}, ErrConcurrent
	}
	backend.mu.Unlock()
	outcome, err := backend.runRecover(ctx, cloneRequest(request))
	if err != nil {
		return Response{}, safePhaseError(err)
	}
	if validateRecovery(outcome) != nil {
		return Response{}, ErrConflict
	}
	return recoveryResponse(request, outcome), nil
}

func (backend *SessionBackend) runRecover(ctx context.Context, request Request) (RecoveryOutcome, error) {
	defer zeroRequest(&request)
	return backend.runner.Recover(ctx, request)
}

func (backend *SessionBackend) Close() error {
	if backend == nil {
		return nil
	}
	backend.mu.Lock()
	defer backend.mu.Unlock()
	if backend.closed {
		return nil
	}
	if backend.active != 0 {
		return ErrConcurrent
	}
	for _, record := range backend.sessions {
		if record.state == sessionAuthoring || record.state == sessionGrading || record.state == sessionAborting {
			return ErrConcurrent
		}
	}
	for _, record := range backend.sessions {
		zero(record.privateKey)
		record.privateKey = nil
		zeroAuthoringOutcome(record.authoring)
		zeroGradingOutcome(record.grading)
		record.authoring = nil
		record.grading = nil
	}
	clear(backend.sessions)
	clear(backend.sessionIDs)
	clear(backend.publicKeys)
	backend.runner = nil
	backend.sessions = nil
	backend.random = nil
	backend.newSessionID = nil
	backend.sessionIDs = nil
	backend.publicKeys = nil
	backend.closed = true
	return nil
}

func (backend *SessionBackend) String() string {
	if backend == nil {
		return "CodingSupervisorSessionBackend{nil=true}"
	}
	return "CodingSupervisorSessionBackend{private=true}"
}

func (backend *SessionBackend) GoString() string { return backend.String() }

func (backend *SessionBackend) LogValue() slog.Value {
	return slog.StringValue("coding-supervisor-session-backend-private")
}

func (*SessionBackend) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

func preparationResponse(request Request, outcome PreparationOutcome) Response {
	return baseResponse(request, &outcome, nil, nil, nil, false)
}

func authoringResponse(request Request, outcome AuthoringOutcome) Response {
	outcome = cloneAuthoringOutcome(outcome)
	return baseResponse(request, nil, &outcome, nil, nil, false)
}

func gradingResponse(request Request, outcome GradingOutcome) Response {
	outcome = cloneGradingOutcome(outcome)
	return baseResponse(request, nil, nil, &outcome, nil, false)
}

func recoveryResponse(request Request, outcome RecoveryOutcome) Response {
	response := baseResponse(request, nil, nil, nil, &outcome, false)
	return cloneResponse(response)
}

func abortedResponse(request Request) Response {
	return baseResponse(request, nil, nil, nil, nil, true)
}

func baseResponse(
	request Request,
	preparation *PreparationOutcome,
	authoring *AuthoringOutcome,
	grading *GradingOutcome,
	recovery *RecoveryOutcome,
	aborted bool,
) Response {
	return Response{
		Schema: ResponseSchema, Operation: request.Operation, OperationID: request.OperationID,
		TicketID: request.TicketID, CodingRunID: request.CodingRunID,
		Preparation: preparation, Authoring: authoring, Grading: grading, Recovery: recovery, Aborted: aborted,
	}
}

func cloneAuthoringOutcome(outcome AuthoringOutcome) AuthoringOutcome {
	outcome.Evidence = append(json.RawMessage(nil), outcome.Evidence...)
	return outcome
}

func cloneGradingOutcome(outcome GradingOutcome) GradingOutcome {
	outcome.TaskEvidence = append([]json.RawMessage(nil), outcome.TaskEvidence...)
	for index := range outcome.TaskEvidence {
		outcome.TaskEvidence[index] = append(json.RawMessage(nil), outcome.TaskEvidence[index]...)
	}
	return outcome
}

func zeroRequest(request *Request) {
	if request == nil {
		return
	}
	zero(request.Lease)
	zero(request.Authoring)
	zero(request.Grant)
}

func zeroAuthoringOutcome(outcome *AuthoringOutcome) {
	if outcome != nil {
		zero(outcome.Evidence)
	}
}

func zeroGradingOutcome(outcome *GradingOutcome) {
	if outcome == nil {
		return
	}
	for index := range outcome.TaskEvidence {
		zero(outcome.TaskEvidence[index])
	}
}

func requestMatchesAuthoring(body json.RawMessage, expected AuthoringOutcome) bool {
	var actual AuthoringOutcome
	if err := json.Unmarshal(body, &actual); err != nil || validateAuthoring(actual) != nil {
		return false
	}
	actualDigest, actualErr := canonicalRawSHA256(actual.Evidence)
	expectedDigest, expectedErr := canonicalRawSHA256(expected.Evidence)
	return actualErr == nil && expectedErr == nil && actualDigest == expectedDigest &&
		actual.AuthoringTranscriptObjectKey == expected.AuthoringTranscriptObjectKey &&
		actual.AuthoringTranscriptBytes == expected.AuthoringTranscriptBytes &&
		actual.AuthoringEventCount == expected.AuthoringEventCount &&
		actual.FrozenSubmissionObjectKey == expected.FrozenSubmissionObjectKey &&
		actual.CapabilitiesRevoked == expected.CapabilitiesRevoked &&
		actual.AuthoringEnvironmentDestroyed == expected.AuthoringEnvironmentDestroyed
}

func canonicalRawSHA256(body json.RawMessage) ([sha256.Size]byte, error) {
	var zero [sha256.Size]byte
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return zero, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return zero, ErrInvalid
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return zero, err
	}
	return sha256.Sum256(canonical), nil
}

func generateBrokerKey(source io.Reader) (ed25519.PublicKey, ed25519.PrivateKey, error) {
	seed := make([]byte, ed25519.SeedSize)
	defer zero(seed)
	if _, err := io.ReadFull(source, seed); err != nil {
		return nil, nil, err
	}
	privateKey := ed25519.NewKeyFromSeed(seed)
	publicKey := append(ed25519.PublicKey(nil), privateKey[ed25519.SeedSize:]...)
	return publicKey, privateKey, nil
}

func safePhaseError(err error) error {
	switch {
	case errors.Is(err, context.Canceled):
		return context.Canceled
	case errors.Is(err, context.DeadlineExceeded), errors.Is(err, ErrDeadline):
		return ErrDeadline
	case errors.Is(err, ErrConflict):
		return ErrConflict
	default:
		return ErrUnavailable
	}
}

func sessionKey(request Request) string { return request.TicketID + "\x00" + request.CodingRunID }

func zero(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

var _ Backend = (*SessionBackend)(nil)
var _ json.Marshaler = AuthoringInput{}
var _ json.Marshaler = SessionBackendConfig{}
var _ json.Marshaler = (*SessionBackend)(nil)
