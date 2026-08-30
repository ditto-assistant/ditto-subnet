package codingharness

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

const defaultMaximumInstances = 16

func New(config Config) (*Factory, error) {
	if nilLike(config.Runtime) || config.Sources == nil {
		return nil, ErrInvalidConfig
	}
	transport := config.Transport
	if transport == nil {
		transport = &http.Transport{
			Proxy:             nil,
			DialContext:       (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			ForceAttemptHTTP2: true, MaxIdleConns: 4, MaxIdleConnsPerHost: 1, MaxConnsPerHost: 2,
			IdleConnTimeout: 30 * time.Second, TLSHandshakeTimeout: 10 * time.Second,
			ExpectContinueTimeout: time.Second, MaxResponseHeaderBytes: 32 << 10,
		}
	} else if nilLike(transport) {
		return nil, ErrInvalidConfig
	}
	now := config.Now
	if now == nil {
		now = time.Now
	}
	newInstance := config.NewInstance
	if newInstance == nil {
		newInstance = func() string { return "coding-harness-" + uuid.NewString() }
	}
	maximum := config.MaxInstances
	if maximum == 0 {
		maximum = defaultMaximumInstances
	}
	if maximum < 1 || maximum > 256 {
		return nil, ErrInvalidConfig
	}
	return &Factory{
		runtime: config.Runtime, sources: config.Sources,
		client: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
		now: now, newID: newInstance, maximum: maximum, instances: make(map[string]*Handle),
	}, nil
}

const (
	publicCanaryCaseID              = "PRACTICE-LEDGER-001"
	publicCanaryProfileCapabilityID = "public-certification-v1"
)

// CanaryBinding is one claimed certification lease plus a short-lived
// screened-image capability. It is not a private coding ticket.
type CanaryBinding struct {
	LeaseID                string
	AgentID                string
	AgentArtifactSHA256    string
	Deadline               time.Time
	BenchVersion           int
	ScreenedImageSHA256    string
	ScreenedImageSize      int64
	ScreenedImageID        string
	ScreenedImageRef       string
	ScreeningPolicyVersion int
	ImageURL               string
	ImageExpiresAt         time.Time
}

func (factory *Factory) Acquire(
	ctx context.Context,
	binding codingphase.HarnessBinding,
) (codingphase.Harness, error) {
	if factory == nil || ctx == nil || ctx.Err() != nil {
		return nil, ErrInvalid
	}
	now := factory.now().UTC()
	binding.Deadline = binding.Deadline.UTC()
	binding.ImageExpiresAt = binding.ImageExpiresAt.UTC()
	if !validHarnessBinding(binding, now) {
		return nil, ErrInvalid
	}
	return factory.acquireValidated(ctx, binding)
}

func (factory *Factory) AcquireCanary(
	ctx context.Context,
	binding CanaryBinding,
) (codingphase.Harness, error) {
	if factory == nil || ctx == nil || ctx.Err() != nil {
		return nil, ErrInvalid
	}
	now := factory.now().UTC()
	binding.Deadline = binding.Deadline.UTC()
	binding.ImageExpiresAt = binding.ImageExpiresAt.UTC()
	if !validCanaryBinding(binding, now) {
		return nil, ErrInvalid
	}
	return factory.acquireValidated(ctx, codingphase.HarnessBinding{
		ExecutionID: binding.LeaseID, AgentID: binding.AgentID, RunRowID: binding.LeaseID,
		AgentArtifactSHA256: binding.AgentArtifactSHA256, TicketID: binding.LeaseID,
		CaseID: publicCanaryCaseID, ProfileCapabilityID: publicCanaryProfileCapabilityID,
		Deadline: binding.Deadline, BenchVersion: binding.BenchVersion,
		ScreenedImageSHA256: binding.ScreenedImageSHA256, ScreenedImageSize: binding.ScreenedImageSize,
		ScreenedImageID: binding.ScreenedImageID, ScreenedImageRef: binding.ScreenedImageRef,
		ScreeningPolicyVersion: binding.ScreeningPolicyVersion, ImageURL: binding.ImageURL,
		ImageExpiresAt: binding.ImageExpiresAt,
	})
}

func (factory *Factory) acquireValidated(
	ctx context.Context,
	binding codingphase.HarnessBinding,
) (codingphase.Harness, error) {
	instanceID := factory.newID()
	if !validIdentifier(instanceID, 256) {
		return nil, ErrLifecycle
	}
	handle := &Handle{
		factory: factory, binding: binding, instanceID: instanceID,
		state: stateDormant, proxy: &lifecycleClient{},
	}
	handle.proxy.handle = handle
	factory.mu.Lock()
	if len(factory.instances) >= factory.maximum || factory.instances[instanceID] != nil {
		factory.mu.Unlock()
		return nil, ErrLifecycle
	}
	factory.instances[instanceID] = handle
	factory.mu.Unlock()
	if err := factory.runtime.Available(ctx); err != nil {
		return nil, errors.Join(ErrLifecycle, err, factory.releaseReservation(handle))
	}
	remaining := binding.Deadline.Sub(factory.now().UTC())
	if remaining <= 0 {
		return nil, errors.Join(ErrClosed, factory.releaseReservation(handle))
	}
	loadContext, cancelLoad := context.WithTimeout(ctx, remaining)
	image, err := factory.runtime.Load(loadContext, ImageSource{
		URL: binding.ImageURL, SHA256: binding.ScreenedImageSHA256,
		SizeBytes: binding.ScreenedImageSize, ImageID: binding.ScreenedImageID,
		ImageRef: binding.ScreenedImageRef, ArtifactSHA: binding.AgentArtifactSHA256,
	})
	cancelLoad()
	if err != nil || !validIdentifier(image, 256) {
		if image != "" {
			factory.runtime.Release(context.WithoutCancel(ctx), image)
		}
		return nil, errors.Join(ErrLifecycle, err, factory.releaseReservation(handle))
	}
	if !binding.Deadline.After(factory.now().UTC()) {
		factory.runtime.Release(context.WithoutCancel(ctx), image)
		return nil, errors.Join(ErrClosed, factory.releaseReservation(handle))
	}
	handle.image = image
	handle.binding.ImageURL = ""
	handle.binding.ImageExpiresAt = time.Time{}
	return handle, nil
}

func (factory *Factory) releaseReservation(handle *Handle) error {
	factory.mu.Lock()
	defer factory.mu.Unlock()
	if handle == nil || factory.instances[handle.instanceID] != handle {
		return ErrLifecycle
	}
	delete(factory.instances, handle.instanceID)
	return nil
}

func validCanaryBinding(binding CanaryBinding, now time.Time) bool {
	if !canonicalUUID(binding.LeaseID) || !canonicalUUID(binding.AgentID) ||
		!lowerSHA256(binding.AgentArtifactSHA256) || binding.BenchVersion < 7 || binding.BenchVersion > 1_000_000 ||
		!lowerSHA256(binding.ScreenedImageSHA256) || binding.ScreenedImageSize <= 0 || binding.ScreenedImageSize > 8<<30 ||
		binding.ScreenedImageID != "sha256:"+strings.TrimPrefix(binding.ScreenedImageID, "sha256:") ||
		!lowerSHA256(strings.TrimPrefix(binding.ScreenedImageID, "sha256:")) ||
		binding.ScreenedImageRef != "ditto-screen/"+binding.AgentID+":latest" ||
		binding.ScreeningPolicyVersion < 9 || binding.ScreeningPolicyVersion > 1_000_000 ||
		!validImageURL(binding.ImageURL) || now.IsZero() || !binding.Deadline.After(now) ||
		binding.Deadline.After(now.Add(2*time.Hour)) || !binding.ImageExpiresAt.After(now) ||
		binding.ImageExpiresAt.After(binding.Deadline) || binding.ImageExpiresAt.After(now.Add(6*time.Minute)) {
		return false
	}
	return true
}

func validHarnessBinding(binding codingphase.HarnessBinding, now time.Time) bool {
	if binding.ExecutionID != binding.TicketID || !canonicalUUID(binding.AgentID) ||
		!canonicalUUID(binding.RunRowID) || !canonicalUUID(binding.TicketID) ||
		!lowerSHA256(binding.AgentArtifactSHA256) || !validIdentifier(binding.CaseID, 256) ||
		!validIdentifier(binding.ProfileCapabilityID, 256) || binding.BenchVersion < 7 || binding.BenchVersion > 1_000_000 ||
		!lowerSHA256(binding.ScreenedImageSHA256) || binding.ScreenedImageSize <= 0 || binding.ScreenedImageSize > 8<<30 ||
		binding.ScreenedImageID != "sha256:"+strings.TrimPrefix(binding.ScreenedImageID, "sha256:") ||
		!lowerSHA256(strings.TrimPrefix(binding.ScreenedImageID, "sha256:")) ||
		binding.ScreenedImageRef != "ditto-screen/"+binding.AgentID+":latest" ||
		binding.ScreeningPolicyVersion < 9 || binding.ScreeningPolicyVersion > 1_000_000 ||
		!validImageURL(binding.ImageURL) || now.IsZero() || !binding.Deadline.After(now) ||
		binding.Deadline.After(now.Add(2*time.Hour)) || !binding.ImageExpiresAt.After(now) ||
		binding.ImageExpiresAt.After(binding.Deadline) || binding.ImageExpiresAt.After(now.Add(6*time.Minute)) {
		return false
	}
	return true
}

func validImageURL(value string) bool {
	if len(value) == 0 || len(value) > 16<<10 {
		return false
	}
	for _, character := range value {
		if character < 32 || character > 126 {
			return false
		}
	}
	parsed, err := url.ParseRequestURI(value)
	return err == nil && parsed.Scheme == "https" && parsed.Hostname() != "" && parsed.User == nil &&
		parsed.Path != "" && parsed.RawQuery != "" && parsed.Fragment == "" &&
		(parsed.Port() == "" || parsed.Port() == "443")
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

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func lowerSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func (client *lifecycleClient) delegate() (*codingcertifier.HTTPHarnessClient, time.Duration, error) {
	if client == nil || client.handle == nil {
		return nil, 0, ErrInactive
	}
	client.handle.mu.Lock()
	defer client.handle.mu.Unlock()
	if client.handle.state != stateActive || client.handle.client == nil {
		return nil, 0, ErrInactive
	}
	remaining := client.handle.binding.Deadline.Sub(client.handle.factory.now().UTC())
	if remaining <= 0 {
		return nil, 0, ErrInactive
	}
	return client.handle.client, remaining, nil
}

func (client *lifecycleClient) Health(ctx context.Context) (codingcertifier.HealthResponse, error) {
	if ctx == nil {
		return codingcertifier.HealthResponse{}, ErrInactive
	}
	delegate, remaining, err := client.delegate()
	if err != nil {
		return codingcertifier.HealthResponse{}, err
	}
	requestContext, cancel := boundedHarnessContext(ctx, remaining, 2*time.Minute)
	defer cancel()
	return delegate.Health(requestContext)
}

func (client *lifecycleClient) Seed(
	ctx context.Context,
	request codingcontract.SeedRequest,
) (codingcertifier.SeedResponse, error) {
	if ctx == nil {
		return codingcertifier.SeedResponse{}, ErrInactive
	}
	delegate, remaining, err := client.delegate()
	if err != nil {
		return codingcertifier.SeedResponse{}, err
	}
	requestContext, cancel := boundedHarnessContext(ctx, remaining, 2*time.Minute)
	defer cancel()
	return delegate.Seed(requestContext, request)
}

func (client *lifecycleClient) Run(
	ctx context.Context,
	request codingcontract.RunRequest,
) (codingcertifier.RunResponse, error) {
	if ctx == nil {
		return codingcertifier.RunResponse{}, ErrInactive
	}
	delegate, remaining, err := client.delegate()
	if err != nil {
		return codingcertifier.RunResponse{}, err
	}
	maximum := time.Duration(request.Budgets.WallTimeSeconds)*time.Second + time.Minute
	requestContext, cancel := boundedHarnessContext(ctx, remaining, maximum)
	defer cancel()
	return delegate.Run(requestContext, request)
}

func boundedHarnessContext(
	ctx context.Context,
	remaining time.Duration,
	maximum time.Duration,
) (context.Context, context.CancelFunc) {
	if remaining <= 0 || maximum <= 0 {
		cancelled, cancel := context.WithCancel(ctx)
		cancel()
		return cancelled, func() {}
	}
	if maximum < remaining {
		remaining = maximum
	}
	return context.WithTimeout(ctx, remaining)
}

func sourceBinding(handle *Handle) codingsource.HarnessBinding {
	return codingsource.HarnessBinding{
		HarnessInstanceID: handle.instanceID, AgentArtifactSHA256: handle.binding.AgentArtifactSHA256,
		TicketID: handle.binding.TicketID, CaseID: handle.binding.CaseID,
		ProfileCapabilityID: handle.binding.ProfileCapabilityID, Deadline: handle.binding.Deadline,
	}
}
