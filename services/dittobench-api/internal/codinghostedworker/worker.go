// Package codinghostedworker coordinates one native Platform authoring attempt.
// It never schedules work or exposes private material to a validator host.
package codinghostedworker

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"reflect"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

var ErrAttempt = errors.New("hosted authoring attempt did not complete")

// Control is implemented only inside the trusted Platform. Each method must
// recheck committed worker/assignment authority. No method accepts miner data
// as authority, and failures must not reopen an irreversible start.
type Control interface {
	Authoring(context.Context, codingsource.HostedBinding) (*codinghostedinput.Prepared, error)
	// Inference provisions the Python bridge with this exact source binding,
	// a policy-bound estimator and durable inference-evidence publisher. Return
	// cleanup functions even on partial failure. Revoke drains while keeping
	// the private command socket available; Close disposes it after cleanup.
	Inference(context.Context, codingsource.HostedBinding) (Bridge, error)
	CheckAuthoring(context.Context, codingsource.HostedBinding) error
	// Retain must durably seal the exact freeze AND transcript, plus any failed
	// run classification. It must verify complete inference evidence before
	// returning a successful commitment for CompletedRun=true. Exact replay
	// returns the same digest, never a newly encrypted or rerun identity.
	Retain(context.Context, codingsource.HostedBinding, Evidence) (string, error)
	// CommitFreeze commits the retained exact patch to Platform PostgreSQL and
	// returns independently reconstructed authority from the committed row.
	CommitFreeze(context.Context, codingsource.HostedBinding, []byte, string) (codingrunner.HostedReplayAuthority, error)
	// Abort removes private-object access, including after expiry/retirement.
	Abort(context.Context, codingsource.HostedBinding) error
}

type Bridge struct {
	GrantID, PolicySHA256, SocketPath string
	Token                             []byte
	ExpiresAt                         time.Time
	Revoke                            func(context.Context) error
	Close                             func(context.Context) error
}

// Evidence remains private. Transcript reads are synchronous and bounded by
// the runner; the receiver must finish reading before returning from Retain.
type Evidence struct {
	CompletedRun    bool
	Freeze          codingrunner.FreezeResult
	WriteTranscript func(io.Writer) (codingrunner.TranscriptIdentity, error)
}

type Config struct {
	Harnesses *codingharness.HostedFactory
	Executors *codingexecutor.PhaseFactory
	Router    *codingsource.Router
	Control   Control
}

type input interface {
	Authority() (codinghostedinput.Expected, error)
	SeedRequest() (codingcontract.HostedSeedRequest, error)
	RunRequest(string, string) (codingcontract.HostedRunRequest, error)
	Begin(context.Context, *codingexecutor.PhaseFactory) (*codingrunner.Session, error)
	Close()
}

type harness interface {
	Activate(context.Context) error
	Health(context.Context) (codingcertifier.HealthResponse, error)
	Seed(context.Context, codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error)
	Run(context.Context, codingcontract.HostedRunRequest) (codingcertifier.RunResponse, error)
	Destroy(context.Context) error
	SourceBinding() codingsource.HostedBinding
}

type inference interface {
	URL() string
	Revoke(context.Context) error
}

// dependencies are private: tests can exercise faults without exposing a
// public switch that substitutes candidate executors or source authorization.
type dependencies struct {
	control   Control
	executors *codingexecutor.PhaseFactory
	acquire   func(context.Context, codingharness.HostedBinding) (harness, error)
	load      func(context.Context, codingsource.HostedBinding) (input, error)
	workspace func(context.Context, codingsource.HostedBinding, http.Handler) (codingcertifier.PublishedCapability, error)
	relay     func(context.Context, codingsource.HostedBinding, Bridge) (inference, error)
}

type Worker struct{ deps dependencies }

func New(config Config) (*Worker, error) {
	if config.Harnesses == nil || config.Executors == nil || config.Router == nil || nilLike(config.Control) {
		return nil, ErrAttempt
	}
	return &Worker{dependencies{
		control: config.Control, executors: config.Executors,
		acquire: func(ctx context.Context, b codingharness.HostedBinding) (harness, error) {
			return config.Harnesses.Acquire(ctx, b)
		},
		load: func(ctx context.Context, b codingsource.HostedBinding) (input, error) {
			return config.Control.Authoring(ctx, b)
		},
		workspace: config.Router.PublishHostedWorkspace,
		relay: func(ctx context.Context, b codingsource.HostedBinding, bridge Bridge) (inference, error) {
			return codinghostedrelay.Publish(ctx, codinghostedrelay.Config{Router: config.Router, Source: b, GrantID: bridge.GrantID, PolicySHA256: bridge.PolicySHA256, SocketPath: bridge.SocketPath, Token: bridge.Token, ExpiresAt: bridge.ExpiresAt})
		},
	}}, nil
}

// Attempt owns every partial cleanup handle. Keep it on any error and call
// Cleanup again when cleanup was not verified. Run is irrevocably single-use.
type Attempt struct {
	mu                                                                          sync.Mutex
	op                                                                          sync.Mutex
	worker                                                                      *Worker
	binding                                                                     codingharness.HostedBinding
	source                                                                      codingsource.HostedBinding
	ran, completedRun, quiescent, retained, committed, aborted                  bool
	cancel                                                                      context.CancelFunc
	harness                                                                     harness
	inputs                                                                      input
	session                                                                     *codingrunner.Session
	workspace                                                                   codingcertifier.PublishedCapability
	relay                                                                       inference
	bridge                                                                      Bridge
	evidenceSHA                                                                 string
	freeze                                                                      *codingrunner.FreezeResult
	authority                                                                   codingrunner.HostedReplayAuthority
	gradingAttempted                                                            bool
	terminalBody                                                                []byte
	workspaceRevoked, relayRevoked, bridgeRevoked, harnessStopped, inputsClosed bool
}

func (w *Worker) Attempt(binding codingharness.HostedBinding) (*Attempt, error) {
	if w == nil || w.deps.control == nil || binding.Deadline.IsZero() || !binding.Deadline.After(time.Now()) || binding.Deadline.After(time.Now().Add(time.Hour)) {
		return nil, ErrAttempt
	}
	return &Attempt{worker: w, binding: binding}, nil
}

func (a *Attempt) Run(ctx context.Context) (codingrunner.HostedReplayAuthority, error) {
	if a == nil || a.worker == nil || ctx == nil {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	a.mu.Lock()
	if a.ran {
		a.mu.Unlock()
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	a.ran = true
	call, cancel := context.WithDeadline(ctx, a.binding.Deadline)
	a.cancel = cancel
	a.mu.Unlock()
	defer cancel()
	a.op.Lock()
	defer a.op.Unlock()
	err := a.author(call)
	// Always try every cleanup boundary. One failed revoke must not leave the
	// candidate running, and failed physical cleanup must never permit freeze.
	clean := a.quiesce()
	a.completedRun = err == nil && clean == nil && call.Err() == nil
	if clean != nil {
		_ = a.abort()
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	if a.session == nil {
		_ = a.abort()
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	if a.capture() != nil {
		_ = a.abort()
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	if !a.completedRun || a.freeze.Submission == nil {
		_ = a.retain(context.Background())
		_ = a.abort()
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	return a.finalize(ctx)
}

func (a *Attempt) author(ctx context.Context) error {
	d := a.worker.deps
	var err error
	a.harness, err = d.acquire(ctx, a.binding)
	if err != nil || nilLike(a.harness) {
		return ErrAttempt
	}
	a.source = a.harness.SourceBinding()
	if a.source.EvaluationID != a.binding.EvaluationID || a.source.AttemptID != a.binding.AttemptID || a.source.WorkerID != a.binding.WorkerID || a.source.AssignmentSHA256 != a.binding.AssignmentSHA256 || a.source.AgentArtifactSHA256 != a.binding.AgentArtifactSHA256 || a.source.ProfileCapabilityID != a.binding.ProfileCapabilityID || !a.source.Deadline.Equal(a.binding.Deadline) {
		a.source = codingsource.HostedBinding{}
		return ErrAttempt
	}
	if a.harness.Activate(ctx) != nil {
		return ErrAttempt
	}
	check, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-check.Done():
				return
			case <-ticker.C:
				err := a.check(check)
				if err != nil {
					cancel()
					return
				}
			}
		}
	}()
	defer func() { cancel(); <-done }()
	if a.check(check) != nil {
		return ErrAttempt
	}
	health, err := a.harness.Health(check)
	if err != nil || !health.SupportsCodingV2() {
		return ErrAttempt
	}
	a.inputs, err = d.load(check, a.source)
	if err != nil || nilLike(a.inputs) {
		return ErrAttempt
	}
	expected, err := a.inputs.Authority()
	if err != nil || expected.EvaluationID != a.source.EvaluationID || expected.AttemptID != a.source.AttemptID || expected.WorkerID != a.source.WorkerID || expected.AssignmentSHA256 != a.source.AssignmentSHA256 || expected.DeadlineUnix != a.source.Deadline.Unix() {
		return ErrAttempt
	}
	a.session, err = a.inputs.Begin(check, d.executors)
	if err != nil || a.session == nil {
		return ErrAttempt
	}
	seed, err := a.inputs.SeedRequest()
	if err != nil {
		return ErrAttempt
	}
	seeded, err := a.harness.Seed(check, seed)
	if err != nil || seeded.CaseID != seed.CaseID || seeded.ProfileCapabilityID != seed.ProfileCapabilityID || seeded.MemoryBundleSHA256 != seed.MemoryBundleSHA256 || seeded.MemoryCount != len(seed.Memories) || seeded.IdempotentReplay {
		return ErrAttempt
	}
	a.workspace, err = d.workspace(check, a.source, a.session.Handler())
	if err != nil || nilLike(a.workspace) {
		return ErrAttempt
	}
	a.bridge, err = d.control.Inference(check, a.source)
	if err != nil || a.bridge.Close == nil || a.bridge.Revoke == nil {
		return ErrAttempt
	}
	a.relay, err = d.relay(check, a.source, a.bridge)
	if err != nil || nilLike(a.relay) {
		return ErrAttempt
	}
	run, err := a.inputs.RunRequest(a.workspace.URL(), a.relay.URL())
	if err != nil || run.Validate() != nil {
		return ErrAttempt
	}
	if a.check(check) != nil {
		return ErrAttempt
	}
	runContext, stopRun := context.WithTimeout(check, time.Duration(run.Budgets.WallTimeSeconds)*time.Second)
	defer stopRun()
	result, err := a.harness.Run(runContext, run)
	if err != nil || result.CaseID != a.source.AttemptID || runContext.Err() != nil {
		return ErrAttempt
	}
	if a.check(check) != nil || check.Err() != nil {
		return ErrAttempt
	}
	return nil
}

func (a *Attempt) check(ctx context.Context) error {
	probe, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if a.worker.deps.control.CheckAuthoring(probe, a.source) != nil || probe.Err() != nil {
		return ErrAttempt
	}
	return nil
}

func bounded(f func(context.Context) error) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	return f(ctx)
}

func (a *Attempt) quiesce() error {
	if a.quiescent {
		return nil
	}
	ok := true
	if !nilLike(a.workspace) && !a.workspaceRevoked {
		a.workspaceRevoked = bounded(a.workspace.Revoke) == nil
		ok = a.workspaceRevoked && ok
	}
	if !nilLike(a.relay) && !a.relayRevoked {
		a.relayRevoked = bounded(a.relay.Revoke) == nil
		ok = a.relayRevoked && ok
	}
	if a.bridge.Revoke != nil && !a.bridgeRevoked {
		a.bridgeRevoked = bounded(a.bridge.Revoke) == nil
		ok = a.bridgeRevoked && ok
	}
	if !nilLike(a.harness) && !a.harnessStopped {
		a.harnessStopped = bounded(a.harness.Destroy) == nil
		ok = a.harnessStopped && ok
	}
	if !nilLike(a.inputs) && !a.inputsClosed {
		a.inputs.Close()
		a.inputsClosed = true
	}
	if !ok {
		return ErrAttempt
	}
	// Never dispose the command socket while another cleanup handle needs it.
	if a.bridge.Close != nil && bounded(a.bridge.Close) != nil {
		return ErrAttempt
	}
	a.quiescent = true
	return nil
}

func (a *Attempt) capture() error {
	if !a.quiescent || a.session == nil {
		return ErrAttempt
	}
	if a.freeze == nil {
		freeze := a.session.Freeze()
		a.freeze = &freeze
	}
	return nil
}

func (a *Attempt) retain(ctx context.Context) error {
	if a.retained {
		return nil
	}
	if a.freeze == nil || a.session == nil {
		return ErrAttempt
	}
	call, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	sha, err := a.worker.deps.control.Retain(call, a.source, Evidence{a.completedRun, a.session.Freeze(), a.session.WriteTranscript})
	if err != nil || call.Err() != nil || !digest(sha) {
		return ErrAttempt
	}
	a.evidenceSHA, a.retained = sha, true
	return nil
}

func (a *Attempt) finalize(ctx context.Context) (codingrunner.HostedReplayAuthority, error) {
	if !a.completedRun || !a.quiescent || a.freeze == nil || a.freeze.Submission == nil || a.aborted || a.retain(ctx) != nil {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	if !a.committed {
		call, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		patch := append([]byte(nil), a.freeze.Submission.Patch...)
		committed, err := a.worker.deps.control.CommitFreeze(call, a.source, patch, a.evidenceSHA)
		clear(patch)
		if err != nil || call.Err() != nil || committed.EvaluationID != a.source.EvaluationID || committed.AttemptID != a.source.AttemptID || committed.AssignmentSHA256 != a.source.AssignmentSHA256 || committed.FrozenPatchSHA256 != a.freeze.Submission.FrozenPatchSHA256 {
			return codingrunner.HostedReplayAuthority{}, ErrAttempt
		}
		a.authority, a.committed = committed, true
	}
	if a.session.Close() != nil {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	if !nilLike(a.workspace) && a.workspace.Close() != nil {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	return a.authority, nil
}

// RetryFinalization retries only already-captured exact evidence and its freeze
// acknowledgement. It never starts candidate code, invokes inference or grades.
// A process restart requires the separately durable Platform evidence adapter.
func (a *Attempt) RetryFinalization(ctx context.Context) (codingrunner.HostedReplayAuthority, error) {
	if a == nil || a.worker == nil || ctx == nil {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	a.op.Lock()
	defer a.op.Unlock()
	return a.finalize(ctx)
}

func (a *Attempt) abort() error {
	if a.source.EvaluationID == "" {
		return nil
	}
	if bounded(func(ctx context.Context) error { return a.worker.deps.control.Abort(ctx, a.source) }) != nil {
		return ErrAttempt
	}
	a.aborted = true
	return nil
}

// Cleanup cancels authoring, retries all cleanup handles and removes private
// grants. Failed retention preserves the frozen workspace for operator recovery.
func (a *Attempt) Cleanup() error {
	if a == nil {
		return nil
	}
	a.mu.Lock()
	a.ran = true
	if a.cancel != nil {
		a.cancel()
	}
	a.mu.Unlock()
	a.op.Lock()
	defer a.op.Unlock()
	clean, closed := a.quiesce(), a.abort()
	if clean != nil || closed != nil {
		return ErrAttempt
	}
	if a.session != nil {
		if a.capture() != nil || a.retain(context.Background()) != nil {
			return ErrAttempt
		}
		if a.session.Close() != nil {
			return ErrAttempt
		}
	}
	if !nilLike(a.workspace) && a.workspace.Close() != nil {
		return ErrAttempt
	}
	return nil
}

func digest(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}
func nilLike(v any) bool {
	if v == nil {
		return true
	}
	r := reflect.ValueOf(v)
	switch r.Kind() {
	case reflect.Pointer, reflect.Interface, reflect.Func:
		return r.IsNil()
	}
	return false
}
func (*Attempt) String() string               { return "HostedAuthoringAttempt{private}" }
func (a *Attempt) GoString() string           { return a.String() }
func (a *Attempt) LogValue() slog.Value       { return slog.StringValue(a.String()) }
func (*Attempt) MarshalJSON() ([]byte, error) { return nil, ErrAttempt }
func (Bridge) String() string                 { return "HostedInferenceBridge{private}" }
func (b Bridge) GoString() string             { return b.String() }
func (Bridge) MarshalJSON() ([]byte, error)   { return nil, ErrAttempt }
func (Evidence) String() string               { return "HostedAuthoringEvidence{private}" }
func (e Evidence) GoString() string           { return e.String() }
func (Evidence) MarshalJSON() ([]byte, error) { return nil, ErrAttempt }

func (Config) String() string                { return "HostedAuthoringWorkerConfig{private}" }
func (c Config) GoString() string            { return c.String() }
func (c Config) LogValue() slog.Value        { return slog.StringValue(c.String()) }
func (Config) MarshalJSON() ([]byte, error)  { return nil, ErrAttempt }
func (*Worker) String() string               { return "HostedAuthoringWorker{private}" }
func (w *Worker) GoString() string           { return w.String() }
func (w *Worker) LogValue() slog.Value       { return slog.StringValue(w.String()) }
func (*Worker) MarshalJSON() ([]byte, error) { return nil, ErrAttempt }
func (b Bridge) LogValue() slog.Value        { return slog.StringValue(b.String()) }
func (e Evidence) LogValue() slog.Value      { return slog.StringValue(e.String()) }
