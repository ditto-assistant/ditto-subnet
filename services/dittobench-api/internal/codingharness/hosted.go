package codingharness

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

// HostedBinding is a trusted Platform projection. No field is accepted from a
// miner or validator request. ImageURL is transport-only and dropped after load.
type HostedBinding struct {
	EvaluationID, AttemptID, WorkerID, AssignmentSHA256    string
	AgentID, AgentArtifactSHA256, ProfileCapabilityID      string
	Deadline                                               time.Time
	ScreenedImageSHA256, ScreenedImageID, ScreenedImageRef string
	ScreenedImageSize                                      int64
	ScreeningPolicyVersion                                 int
	ImageURL                                               string
	ImageExpiresAt                                         time.Time
}

// HostedStartStore must atomically recheck the exact assignment, worker,
// artifact/image, phase and deadline and COMMIT its irreversible start before
// returning true. Existing starts (including same-worker replays) return false.
// A failed/lost acknowledgement must never permit another start. Implementations
// are trusted Platform dependencies; this package cannot certify a DB commit.
type HostedStartStore interface {
	CommitStart(context.Context, HostedBinding, string) (bool, error)
}

type HostedConfig struct {
	Config
	Starts HostedStartStore
}

type HostedFactory struct {
	base      *Factory
	starts    HostedStartStore
	mu        sync.Mutex
	instances map[string]*HostedHandle
}

// HostedHandle cannot satisfy the legacy phase Harness interface. Its methods
// expose only native v2 requests; no v1 ticket is synthesized or executed.
type HostedHandle struct {
	factory                            *HostedFactory
	binding                            HostedBinding
	instanceID                         string
	op                                 chan struct{}
	mu                                 sync.Mutex
	attempted, active, closed, cleaned bool
	runAttempted                       bool
	image                              string
	running                            Running
	lease                              *codingsource.Lease
	client                             *codingcertifier.HostedHTTPHarnessClient
	life                               context.Context
	cancel                             context.CancelFunc
}

func NewHosted(config HostedConfig) (*HostedFactory, error) {
	if nilLike(config.Starts) {
		return nil, ErrInvalidConfig
	}
	if _, legacy := config.Runtime.(*SandboxRuntime); legacy {
		return nil, ErrInvalidConfig
	}
	base, err := New(config.Config)
	if err != nil {
		return nil, err
	}
	// Validate transport before image loading or candidate execution.
	if _, err := codingcertifier.NewHostedHTTPHarnessClient("http://127.0.0.1:1", base.client); err != nil {
		return nil, ErrInvalidConfig
	}
	return &HostedFactory{base: base, starts: config.Starts, instances: make(map[string]*HostedHandle)}, nil
}

func (binding HostedBinding) valid(now time.Time) bool {
	return canonicalUUID(binding.EvaluationID) && canonicalUUID(binding.AttemptID) && canonicalUUID(binding.WorkerID) &&
		lowerSHA256(binding.AssignmentSHA256) && validIdentifier(binding.ProfileCapabilityID, 256) &&
		canonicalUUID(binding.AgentID) && lowerSHA256(binding.AgentArtifactSHA256) &&
		lowerSHA256(binding.ScreenedImageSHA256) && binding.ScreenedImageSize > 0 && binding.ScreenedImageSize <= 8<<30 &&
		strings.HasPrefix(binding.ScreenedImageID, "sha256:") && lowerSHA256(strings.TrimPrefix(binding.ScreenedImageID, "sha256:")) &&
		binding.ScreenedImageRef == "ditto-screen/"+binding.AgentID+":latest" &&
		binding.ScreeningPolicyVersion >= 9 && binding.ScreeningPolicyVersion <= 1_000_000 &&
		!now.IsZero() && binding.Deadline.After(now) && !binding.Deadline.After(now.Add(time.Hour)) &&
		validImageURL(binding.ImageURL) && binding.ImageExpiresAt.After(now) &&
		!binding.ImageExpiresAt.After(binding.Deadline) && !binding.ImageExpiresAt.After(now.Add(6*time.Minute))
}

func (factory *HostedFactory) Acquire(ctx context.Context, binding HostedBinding) (*HostedHandle, error) {
	if factory == nil || factory.base == nil || ctx == nil || ctx.Err() != nil || !binding.valid(factory.base.now().UTC()) {
		return nil, ErrInvalid
	}
	binding.Deadline, binding.ImageExpiresAt = binding.Deadline.UTC(), binding.ImageExpiresAt.UTC()
	id := factory.base.newID()
	if !validIdentifier(id, 256) {
		return nil, ErrInvalid
	}
	handle := &HostedHandle{factory: factory, binding: binding, instanceID: id, op: make(chan struct{}, 1)}
	factory.mu.Lock()
	if len(factory.instances) >= factory.base.maximum {
		factory.mu.Unlock()
		return nil, ErrLifecycle
	}
	for _, existing := range factory.instances {
		if existing.instanceID == id || existing.binding.EvaluationID == binding.EvaluationID || existing.binding.AttemptID == binding.AttemptID {
			factory.mu.Unlock()
			return nil, ErrLifecycle
		}
	}
	factory.instances[id] = handle
	factory.mu.Unlock()
	load, cancel := context.WithTimeout(ctx, binding.Deadline.Sub(factory.base.now().UTC()))
	defer cancel()
	err := factory.base.runtime.Available(load)
	if err == nil && load.Err() == nil {
		handle.image, err = factory.base.runtime.Load(load, ImageSource{
			URL: binding.ImageURL, SHA256: binding.ScreenedImageSHA256, SizeBytes: binding.ScreenedImageSize,
			ImageID: binding.ScreenedImageID, ImageRef: binding.ScreenedImageRef, ArtifactSHA: binding.AgentArtifactSHA256,
		})
	}
	if err != nil || load.Err() != nil || !validIdentifier(handle.image, 256) || !binding.Deadline.After(factory.base.now().UTC()) {
		cleanup, done := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Minute)
		defer done()
		factory.base.runtime.Release(cleanup, handle.image)
		factory.remove(handle)
		return nil, ErrLifecycle
	}
	handle.binding.ImageURL, handle.binding.ImageExpiresAt = "", time.Time{}
	handle.life, handle.cancel = context.WithTimeout(context.Background(), binding.Deadline.Sub(factory.base.now().UTC()))
	go func() {
		<-handle.life.Done()
		cleanup, done := context.WithTimeout(context.Background(), 2*time.Minute)
		defer done()
		// Failed cleanup retains the handle/reservation for explicit recovery.
		_ = handle.Destroy(cleanup)
	}()
	return handle, nil
}

func (factory *HostedFactory) remove(handle *HostedHandle) {
	factory.mu.Lock()
	defer factory.mu.Unlock()
	if factory.instances[handle.instanceID] == handle {
		delete(factory.instances, handle.instanceID)
	}
}

func (handle *HostedHandle) lock(ctx context.Context) error {
	if handle == nil || handle.life == nil || ctx == nil || ctx.Err() != nil {
		return ErrClosed
	}
	select {
	case handle.op <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (handle *HostedHandle) Activate(ctx context.Context) (result error) {
	if err := handle.lock(ctx); err != nil {
		return err
	}
	defer func() { <-handle.op }()
	defer func() {
		if result != nil {
			handle.cancel()
		}
	}()
	handle.mu.Lock()
	if handle.closed || handle.life.Err() != nil || !handle.binding.Deadline.After(handle.factory.base.now().UTC()) {
		handle.mu.Unlock()
		return ErrClosed
	}
	if handle.active {
		handle.mu.Unlock()
		return nil
	}
	if handle.attempted {
		handle.mu.Unlock()
		return ErrClosed
	}
	handle.attempted = true
	handle.mu.Unlock()
	call, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(handle.life, cancel)
	defer stop()
	defer cancel()
	fresh, err := handle.factory.starts.CommitStart(call, handle.binding, handle.instanceID)
	if err != nil || !fresh || call.Err() != nil || handle.life.Err() != nil || !handle.binding.Deadline.After(handle.factory.base.now().UTC()) {
		return ErrClosed
	}
	running, err := handle.factory.base.runtime.Start(call, handle.image)
	// Retain even a partially started container so cleanup errors cannot lose it.
	handle.running = running
	if err != nil || nilLike(running) || !validRunning(running, handle.image) || call.Err() != nil || handle.life.Err() != nil {
		return ErrLifecycle
	}
	handle.lease, err = handle.factory.base.sources.RegisterHosted(handle.SourceBinding(), running.SourceIP())
	if err != nil {
		return ErrLifecycle
	}
	client, err := codingcertifier.NewHostedHTTPHarnessClient(running.BaseURL(), handle.factory.base.client)
	if err != nil {
		return ErrLifecycle
	}
	handle.mu.Lock()
	defer handle.mu.Unlock()
	if handle.closed || handle.life.Err() != nil {
		return ErrClosed
	}
	handle.client, handle.active = client, true
	return nil
}

// Destroy immediately denies new calls, cancels admitted HTTP requests, removes
// source authority, and stops the exact container. Stop failure is NOT success:
// the running handle is retained and Destroy may be retried, Activate may not.
// The caller must also revoke/drain workspace and inference handlers before freeze.
func (handle *HostedHandle) Destroy(ctx context.Context) error {
	if handle == nil {
		return nil
	}
	if ctx == nil || handle.cancel == nil {
		return ErrInvalid
	}
	handle.mu.Lock()
	handle.closed, handle.active, handle.client = true, false, nil
	handle.cancel()
	handle.mu.Unlock()
	if err := handle.lock(ctx); err != nil {
		return err
	}
	defer func() { <-handle.op }()
	if handle.cleaned {
		return nil
	}
	if handle.lease != nil {
		if err := handle.lease.Close(); err != nil {
			return ErrLifecycle
		}
		handle.lease = nil
	}
	if !nilLike(handle.running) {
		if err := handle.factory.base.runtime.Stop(ctx, handle.running); err != nil {
			return ErrLifecycle
		}
		handle.running = nil
	} else {
		handle.factory.base.runtime.Release(ctx, handle.image)
	}
	handle.image, handle.cleaned = "", true
	handle.factory.remove(handle)
	return nil
}

func (handle *HostedHandle) SourceBinding() codingsource.HostedBinding {
	if handle == nil {
		return codingsource.HostedBinding{}
	}
	binding := handle.binding
	return codingsource.HostedBinding{
		HarnessInstanceID: handle.instanceID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		EvaluationID: binding.EvaluationID, AttemptID: binding.AttemptID, WorkerID: binding.WorkerID,
		AssignmentSHA256: binding.AssignmentSHA256, ProfileCapabilityID: binding.ProfileCapabilityID, Deadline: binding.Deadline,
	}
}

func (HostedBinding) MarshalJSON() ([]byte, error)  { return nil, ErrInvalid }
func (HostedBinding) String() string                { return "HostedHarnessBinding{private}" }
func (v HostedBinding) GoString() string            { return v.String() }
func (v HostedBinding) LogValue() slog.Value        { return slog.StringValue(v.String()) }
func (*HostedFactory) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }
func (*HostedFactory) String() string               { return "HostedHarnessFactory{private}" }
func (v *HostedFactory) GoString() string           { return v.String() }
func (v *HostedFactory) LogValue() slog.Value       { return slog.StringValue(v.String()) }
func (*HostedHandle) MarshalJSON() ([]byte, error)  { return nil, ErrInvalid }
func (*HostedHandle) String() string                { return "HostedHarnessHandle{private}" }
func (v *HostedHandle) GoString() string            { return v.String() }
func (v *HostedHandle) LogValue() slog.Value        { return slog.StringValue(v.String()) }
