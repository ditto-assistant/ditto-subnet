package codingsupervisor

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
)

const fixtureSessionID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

type phaseRunnerFuncs struct {
	author         func(context.Context, AuthoringInput) (AuthoringOutcome, error)
	grade          func(context.Context, Request) (GradingOutcome, error)
	abortAuthoring func(context.Context, Request) error
	abortGrading   func(context.Context, Request) error
	recover        func(context.Context, Request) (RecoveryOutcome, error)
}

func (runner *phaseRunnerFuncs) Author(ctx context.Context, input AuthoringInput) (AuthoringOutcome, error) {
	if runner.author == nil {
		return AuthoringOutcome{}, ErrUnavailable
	}
	return runner.author(ctx, input)
}

func (runner *phaseRunnerFuncs) Grade(ctx context.Context, request Request) (GradingOutcome, error) {
	if runner.grade == nil {
		return GradingOutcome{}, ErrUnavailable
	}
	return runner.grade(ctx, request)
}

func (runner *phaseRunnerFuncs) AbortAuthoring(ctx context.Context, request Request) error {
	if runner.abortAuthoring == nil {
		return ErrUnavailable
	}
	return runner.abortAuthoring(ctx, request)
}

func (runner *phaseRunnerFuncs) AbortGrading(ctx context.Context, request Request) error {
	if runner.abortGrading == nil {
		return ErrUnavailable
	}
	return runner.abortGrading(ctx, request)
}

func (runner *phaseRunnerFuncs) Recover(ctx context.Context, request Request) (RecoveryOutcome, error) {
	if runner.recover == nil {
		return RecoveryOutcome{}, ErrUnavailable
	}
	return runner.recover(ctx, request)
}

func fixtureBackend(t *testing.T, runner PhaseRunner, maximum int) *SessionBackend {
	t.Helper()
	backend, err := newSessionBackend(
		SessionBackendConfig{Runner: runner, MaximumSessions: maximum},
		bytes.NewReader(bytes.Repeat([]byte{0x42}, 4*ed25519.SeedSize)),
		func() (string, error) { return fixtureSessionID, nil },
	)
	if err != nil {
		t.Fatal(err)
	}
	return backend
}

func backendRequest(t *testing.T, vector fixtureVector, operation Operation, authoring *AuthoringOutcome) Request {
	t.Helper()
	var document map[string]any
	if err := json.Unmarshal(vector.Requests[string(operation)], &document); err != nil {
		t.Fatal(err)
	}
	if authoring != nil {
		body, err := json.Marshal(authoring)
		if err != nil {
			t.Fatal(err)
		}
		var value any
		if err := json.Unmarshal(body, &value); err != nil {
			t.Fatal(err)
		}
		document["authoring"] = value
	}
	body, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	request, err := parseRequest(
		body, operation, time.Date(2026, 8, 23, 6, 0, 0, 0, time.UTC),
	)
	if err != nil {
		t.Fatal(err)
	}
	return request
}

func TestSessionBackendLifecycleCachesOutcomesAndZerosBrokerKey(t *testing.T) {
	vector := loadFixtureVector(t)
	expectedAuthoring := *vectorResponse(t, vector, "author").Authoring
	expectedGrading := *vectorResponse(t, vector, "grade").Grading

	var mu sync.Mutex
	authorCalls := 0
	gradeCalls := 0
	var runnerKey ed25519.PrivateKey
	var runnerGrant json.RawMessage
	var runnerHarness json.RawMessage
	runner := &phaseRunnerFuncs{
		author: func(_ context.Context, input AuthoringInput) (AuthoringOutcome, error) {
			mu.Lock()
			defer mu.Unlock()
			authorCalls++
			if input.SessionID != fixtureSessionID || input.Request.Operation != OperationAuthor ||
				len(input.Request.Grant) == 0 || len(input.Request.Harness) == 0 {
				t.Fatal("author input lost prepared authority")
			}
			publicKey, err := base64.RawURLEncoding.DecodeString(input.BrokerPublicKey)
			if err != nil || !bytes.Equal(publicKey, input.BrokerPrivateKey.Public().(ed25519.PublicKey)) {
				t.Fatal("author input keypair disagrees")
			}
			runnerKey = input.BrokerPrivateKey
			runnerGrant = input.Request.Grant
			runnerHarness = input.Request.Harness
			return cloneAuthoringOutcome(expectedAuthoring), nil
		},
		grade: func(_ context.Context, request Request) (GradingOutcome, error) {
			mu.Lock()
			defer mu.Unlock()
			gradeCalls++
			if !requestMatchesAuthoring(request.Authoring, expectedAuthoring) {
				t.Fatal("grade did not receive the exact authoring outcome")
			}
			return cloneGradingOutcome(expectedGrading), nil
		},
	}
	backend := fixtureBackend(t, runner, 4)

	prepare := backendRequest(t, vector, OperationPrepare, nil)
	prepared, err := backend.Execute(t.Context(), prepare)
	if err != nil || prepared.Preparation == nil {
		t.Fatalf("prepare response=%#v err=%v", prepared, err)
	}
	retryPrepare := prepare
	retryPrepare.OperationID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	replayed, err := backend.Execute(t.Context(), retryPrepare)
	if err != nil || replayed.Preparation == nil ||
		*replayed.Preparation != *prepared.Preparation || replayed.OperationID != retryPrepare.OperationID {
		t.Fatalf("prepare replay response=%#v err=%v", replayed, err)
	}

	author := backendRequest(t, vector, OperationAuthor, nil)
	authored, err := backend.Execute(t.Context(), author)
	if err != nil || authored.Authoring == nil {
		t.Fatalf("author response=%#v err=%v", authored, err)
	}
	if !allZero(runnerKey) {
		t.Fatal("phase runner retained a live broker private-key alias")
	}
	if !allZero(runnerGrant) {
		t.Fatal("phase runner retained a live inference-grant alias")
	}
	if !allZero(runnerHarness) {
		t.Fatal("phase runner retained a live harness-capability alias")
	}
	record := backend.sessions[sessionKey(author)]
	if record.privateKey != nil || record.state != sessionAuthored {
		t.Fatalf("stored key/state = %x/%s", record.privateKey, record.state)
	}
	authored.Authoring.Evidence[0] ^= 1
	retryAuthor := author
	retryAuthor.OperationID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
	authoredAgain, err := backend.Execute(t.Context(), retryAuthor)
	if err != nil || authoredAgain.Authoring == nil || validateAuthoring(*authoredAgain.Authoring) != nil ||
		authoredAgain.OperationID != retryAuthor.OperationID {
		t.Fatalf("author replay response=%#v err=%v", authoredAgain, err)
	}

	grade := backendRequest(t, vector, OperationGrade, &expectedAuthoring)
	graded, err := backend.Execute(t.Context(), grade)
	if err != nil || graded.Grading == nil {
		t.Fatalf("grade response=%#v err=%v", graded, err)
	}
	graded.Grading.TaskEvidence[0][0] ^= 1
	retryGrade := grade
	retryGrade.OperationID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
	gradedAgain, err := backend.Execute(t.Context(), retryGrade)
	if err != nil || gradedAgain.Grading == nil ||
		validateGrading(*gradedAgain.Grading, grade.CodingRunID, grade.TicketID) != nil {
		t.Fatalf("grade replay response=%#v err=%v", gradedAgain, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if authorCalls != 1 || gradeCalls != 1 {
		t.Fatalf("runner calls author=%d grade=%d", authorCalls, gradeCalls)
	}
}

func TestSessionBackendNeverRetriesFailedGrade(t *testing.T) {
	vector := loadFixtureVector(t)
	expectedAuthoring := *vectorResponse(t, vector, "author").Authoring
	gradeCalls := 0
	runner := &phaseRunnerFuncs{
		author: func(context.Context, AuthoringInput) (AuthoringOutcome, error) {
			return cloneAuthoringOutcome(expectedAuthoring), nil
		},
		grade: func(context.Context, Request) (GradingOutcome, error) {
			gradeCalls++
			return GradingOutcome{}, errors.New("private grader path")
		},
	}
	backend := fixtureBackend(t, runner, 2)
	prepare := backendRequest(t, vector, OperationPrepare, nil)
	author := backendRequest(t, vector, OperationAuthor, nil)
	grade := backendRequest(t, vector, OperationGrade, &expectedAuthoring)
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	if _, err := backend.Execute(t.Context(), author); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if _, err := backend.Execute(t.Context(), grade); !errors.Is(err, ErrUnavailable) ||
			strings.Contains(fmt.Sprint(err), "grader") {
			t.Fatalf("grade err=%v", err)
		}
	}
	if gradeCalls != 1 {
		t.Fatalf("failed grade was rerun %d times", gradeCalls)
	}
	if _, err := backend.Execute(t.Context(), author); err != nil {
		t.Fatalf("cached author unavailable after grade failure: %v", err)
	}
}

func TestSessionBackendRejectsOrderingAndAuthorityDrift(t *testing.T) {
	vector := loadFixtureVector(t)
	expectedAuthoring := *vectorResponse(t, vector, "author").Authoring
	runner := &phaseRunnerFuncs{
		author: func(context.Context, AuthoringInput) (AuthoringOutcome, error) {
			return cloneAuthoringOutcome(expectedAuthoring), nil
		},
		grade: func(context.Context, Request) (GradingOutcome, error) {
			return *vectorResponse(t, vector, "grade").Grading, nil
		},
	}
	backend := fixtureBackend(t, runner, 2)
	prepare := backendRequest(t, vector, OperationPrepare, nil)
	author := backendRequest(t, vector, OperationAuthor, nil)
	grade := backendRequest(t, vector, OperationGrade, &expectedAuthoring)

	if _, err := backend.Execute(t.Context(), author); !errors.Is(err, ErrConflict) {
		t.Fatalf("author before prepare err=%v", err)
	}
	if _, err := backend.Execute(t.Context(), grade); !errors.Is(err, ErrConflict) {
		t.Fatalf("grade before author err=%v", err)
	}
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	driftedPrepare := prepare
	driftedPrepare.Lease = json.RawMessage(`{"different":true}`)
	if _, err := backend.Execute(t.Context(), driftedPrepare); !errors.Is(err, ErrConflict) {
		t.Fatalf("prepare authority drift err=%v", err)
	}
	driftedAuthor := author
	driftedAuthor.Deadline = author.Deadline.Add(time.Second)
	if _, err := backend.Execute(t.Context(), driftedAuthor); !errors.Is(err, ErrConflict) {
		t.Fatalf("author deadline drift err=%v", err)
	}
	if _, err := backend.Execute(t.Context(), author); err != nil {
		t.Fatal(err)
	}
	driftedGrade := grade
	var payload map[string]any
	if err := json.Unmarshal(driftedGrade.Authoring, &payload); err != nil {
		t.Fatal(err)
	}
	payload["authoring_event_count"] = float64(expectedAuthoring.AuthoringEventCount + 1)
	driftedGrade.Authoring, _ = json.Marshal(payload)
	if _, err := backend.Execute(t.Context(), driftedGrade); !errors.Is(err, ErrConflict) {
		t.Fatalf("grade authoring drift err=%v", err)
	}
	if _, err := backend.Execute(t.Context(), grade); err != nil {
		t.Fatal(err)
	}
	driftedGrade = grade
	driftedGrade.Lease = json.RawMessage(`{"schema":"different-grading-lease"}`)
	if _, err := backend.Execute(t.Context(), driftedGrade); !errors.Is(err, ErrConflict) {
		t.Fatalf("cached grade lease drift err=%v", err)
	}
}

func TestSessionBackendNeverRetriesFailedAuthorAndAbortIsIdempotent(t *testing.T) {
	vector := loadFixtureVector(t)
	authorCalls := 0
	abortCalls := 0
	var runnerKey ed25519.PrivateKey
	runner := &phaseRunnerFuncs{
		author: func(_ context.Context, input AuthoringInput) (AuthoringOutcome, error) {
			authorCalls++
			runnerKey = input.BrokerPrivateKey
			return AuthoringOutcome{}, errors.New("provider secret must not escape")
		},
		abortAuthoring: func(context.Context, Request) error {
			abortCalls++
			return nil
		},
	}
	backend := fixtureBackend(t, runner, 2)
	prepare := backendRequest(t, vector, OperationPrepare, nil)
	author := backendRequest(t, vector, OperationAuthor, nil)
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	if _, err := backend.Execute(t.Context(), author); !errors.Is(err, ErrUnavailable) ||
		strings.Contains(fmt.Sprint(err), "secret") {
		t.Fatalf("first author err=%v", err)
	}
	if !allZero(runnerKey) {
		t.Fatal("failed author retained broker private key")
	}
	if _, err := backend.Execute(t.Context(), author); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("failed author replay err=%v", err)
	}
	if authorCalls != 1 {
		t.Fatalf("failed author was rerun %d times", authorCalls)
	}
	abort := backendRequest(t, vector, OperationAbortAuthoring, nil)
	if _, err := backend.Execute(t.Context(), abort); err != nil {
		t.Fatal(err)
	}
	if _, err := backend.Execute(t.Context(), abort); err != nil {
		t.Fatal(err)
	}
	if abortCalls != 1 {
		t.Fatalf("abort calls=%d", abortCalls)
	}
	if _, err := backend.Execute(t.Context(), author); !errors.Is(err, ErrConflict) {
		t.Fatalf("aborted author could retry err=%v", err)
	}
}

func TestSessionBackendConcurrentPhaseBlocksCloseAndDuplicate(t *testing.T) {
	vector := loadFixtureVector(t)
	expectedAuthoring := *vectorResponse(t, vector, "author").Authoring
	entered := make(chan struct{})
	release := make(chan struct{})
	runner := &phaseRunnerFuncs{author: func(ctx context.Context, _ AuthoringInput) (AuthoringOutcome, error) {
		close(entered)
		select {
		case <-release:
			return cloneAuthoringOutcome(expectedAuthoring), nil
		case <-ctx.Done():
			return AuthoringOutcome{}, ctx.Err()
		}
	}}
	backend := fixtureBackend(t, runner, 2)
	prepare := backendRequest(t, vector, OperationPrepare, nil)
	author := backendRequest(t, vector, OperationAuthor, nil)
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := backend.Execute(context.Background(), author)
		done <- err
	}()
	<-entered
	if _, err := backend.Execute(t.Context(), author); !errors.Is(err, ErrConcurrent) {
		t.Fatalf("concurrent author err=%v", err)
	}
	if err := backend.Close(); !errors.Is(err, ErrConcurrent) {
		t.Fatalf("close during author err=%v", err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := backend.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := backend.Execute(t.Context(), author); !errors.Is(err, ErrClosed) {
		t.Fatalf("execute after close err=%v", err)
	}
}

func TestSessionBackendActiveRecoveryBlocksClose(t *testing.T) {
	vector := loadFixtureVector(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	runner := &phaseRunnerFuncs{recover: func(ctx context.Context, _ Request) (RecoveryOutcome, error) {
		close(entered)
		select {
		case <-release:
			return RecoveryOutcome{State: "none"}, nil
		case <-ctx.Done():
			return RecoveryOutcome{}, ctx.Err()
		}
	}}
	backend := fixtureBackend(t, runner, 2)
	recoverRequest := backendRequest(t, vector, OperationRecover, nil)
	done := make(chan error, 1)
	go func() {
		_, err := backend.Execute(context.Background(), recoverRequest)
		done <- err
	}()
	<-entered
	if err := backend.Close(); !errors.Is(err, ErrConcurrent) {
		t.Fatalf("close during recovery err=%v", err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := backend.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSessionBackendRecoveryDelegatesWithoutRunningPhases(t *testing.T) {
	vector := loadFixtureVector(t)
	recoverCalls := 0
	authorCalls := 0
	gradeCalls := 0
	stage := "terminal_result"
	digest := strings.Repeat("a", 64)
	runner := &phaseRunnerFuncs{
		author: func(context.Context, AuthoringInput) (AuthoringOutcome, error) {
			authorCalls++
			return AuthoringOutcome{}, nil
		},
		grade: func(context.Context, Request) (GradingOutcome, error) {
			gradeCalls++
			return GradingOutcome{}, nil
		},
		recover: func(context.Context, Request) (RecoveryOutcome, error) {
			recoverCalls++
			return RecoveryOutcome{State: "terminal_pending", PublicationStage: &stage, RequestSHA256: &digest}, nil
		},
	}
	backend := fixtureBackend(t, runner, 2)
	request := backendRequest(t, vector, OperationRecover, nil)
	for range 2 {
		response, err := backend.Execute(t.Context(), request)
		if err != nil || response.Recovery == nil || response.Recovery.State != "terminal_pending" {
			t.Fatalf("recover response=%#v err=%v", response, err)
		}
	}
	if recoverCalls != 2 || authorCalls != 0 || gradeCalls != 0 {
		t.Fatalf("calls recover=%d author=%d grade=%d", recoverCalls, authorCalls, gradeCalls)
	}
}

func TestSessionBackendCapacityConstructionAndDiagnosticsFailClosed(t *testing.T) {
	vector := loadFixtureVector(t)
	runner := &phaseRunnerFuncs{}
	for name, config := range map[string]SessionBackendConfig{
		"typed nil":       {Runner: (*phaseRunnerFuncs)(nil)},
		"negative limit":  {Runner: runner, MaximumSessions: -1},
		"excessive limit": {Runner: runner, MaximumSessions: hardMaximumSessions + 1},
	} {
		if _, err := NewSessionBackend(config); !errors.Is(err, ErrInvalidConfig) {
			t.Fatalf("%s err=%v", name, err)
		}
	}
	if _, err := newSessionBackend(
		SessionBackendConfig{Runner: runner}, (*bytes.Reader)(nil),
		func() (string, error) { return fixtureSessionID, nil },
	); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("typed nil entropy err=%v", err)
	}
	backend := fixtureBackend(t, runner, 1)
	prepare := backendRequest(t, vector, OperationPrepare, nil)
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	storedKey := backend.sessions[sessionKey(prepare)].privateKey
	second := prepare
	second.TicketID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
	second.CodingRunID = "coding-run-supervisor-002"
	if _, err := backend.Execute(t.Context(), second); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("capacity err=%v", err)
	}
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatalf("existing replay at capacity err=%v", err)
	}
	config := SessionBackendConfig{Runner: runner}
	for name, value := range map[string]any{
		"config":  config,
		"backend": backend,
		"input":   AuthoringInput{BrokerPrivateKey: ed25519.PrivateKey(bytes.Repeat([]byte{0x7f}, ed25519.PrivateKeySize))},
	} {
		body, err := json.Marshal(value)
		if !errors.Is(err, ErrPrivate) || body != nil {
			t.Fatalf("%s marshal body=%q err=%v", name, body, err)
		}
		if strings.Contains(fmt.Sprintf("%v %#v", value, value), "7f7f") {
			t.Fatalf("%s diagnostics exposed private bytes", name)
		}
	}
	if slog.AnyValue(backend).String() == "" {
		t.Fatal("backend log projection is empty")
	}
	if err := backend.Close(); err != nil {
		t.Fatal(err)
	}
	if !allZero(storedKey) {
		t.Fatal("close did not zero the retained prepared key")
	}
}

func TestSessionBackendRejectsSessionAndKeyCollisions(t *testing.T) {
	vector := loadFixtureVector(t)
	prepare := backendRequest(t, vector, OperationPrepare, nil)
	second := prepare
	second.TicketID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
	second.CodingRunID = "coding-run-supervisor-002"

	backend := fixtureBackend(t, &phaseRunnerFuncs{}, 2)
	if _, err := backend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	if _, err := backend.Execute(t.Context(), second); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("duplicate session ID err=%v", err)
	}
	if len(backend.sessions) != 1 {
		t.Fatalf("collision created %d sessions", len(backend.sessions))
	}

	identifiers := []string{
		"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
	}
	index := 0
	keyBackend, err := newSessionBackend(
		SessionBackendConfig{Runner: &phaseRunnerFuncs{}, MaximumSessions: 2},
		bytes.NewReader(bytes.Repeat([]byte{0x42}, 2*ed25519.SeedSize)),
		func() (string, error) {
			value := identifiers[index]
			index++
			return value, nil
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := keyBackend.Execute(t.Context(), prepare); err != nil {
		t.Fatal(err)
	}
	if _, err := keyBackend.Execute(t.Context(), second); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("duplicate public key err=%v", err)
	}
}

func TestSessionBackendRunsThroughPrivateHTTPBoundary(t *testing.T) {
	vector := loadFixtureVector(t)
	expectedAuthoring := *vectorResponse(t, vector, "author").Authoring
	expectedGrading := *vectorResponse(t, vector, "grade").Grading
	runner := &phaseRunnerFuncs{
		author: func(context.Context, AuthoringInput) (AuthoringOutcome, error) {
			return cloneAuthoringOutcome(expectedAuthoring), nil
		},
		grade: func(context.Context, Request) (GradingOutcome, error) {
			return cloneGradingOutcome(expectedGrading), nil
		},
	}
	backend := fixtureBackend(t, runner, 2)
	service := fixtureService(t, backend)
	for _, operation := range []Operation{OperationPrepare, OperationAuthor} {
		response := invoke(
			t, service.Handler(), operationPath(operation),
			vector.Requests[string(operation)], fixtureToken,
		)
		if response.Code != 200 {
			t.Fatalf("%s status=%d body=%s", operation, response.Code, response.Body.String())
		}
	}
	grade := backendRequest(t, vector, OperationGrade, &expectedAuthoring)
	gradeBody, err := marshalPrivateRequestForTest(grade)
	if err != nil {
		t.Fatal(err)
	}
	response := invoke(t, service.Handler(), operationPath(OperationGrade), gradeBody, fixtureToken)
	if response.Code != 200 {
		t.Fatalf("grade status=%d body=%s", response.Code, response.Body.String())
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
	if err := backend.Close(); err != nil {
		t.Fatal(err)
	}
}

func operationPath(operation Operation) string {
	for path, candidate := range operationPaths {
		if candidate == operation {
			return path
		}
	}
	return ""
}

func marshalPrivateRequestForTest(request Request) ([]byte, error) {
	type requestAlias Request
	return json.Marshal(requestAlias(request))
}

func allZero(value []byte) bool {
	if len(value) == 0 {
		return false
	}
	for _, item := range value {
		if item != 0 {
			return false
		}
	}
	return true
}
