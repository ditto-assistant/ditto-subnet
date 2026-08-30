// Package codingharness loads one exact screened image into the dedicated
// rootless validator daemon and returns a dormant lifecycle handle. Candidate
// code starts only when Activate is called after the durable attempt marker.
package codingharness

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

var (
	ErrInvalidConfig = errors.New("coding harness controller configuration is invalid")
	ErrInvalid       = errors.New("coding harness authority is invalid")
	ErrLifecycle     = errors.New("coding harness lifecycle failed")
	ErrInactive      = errors.New("coding harness is not active")
	ErrClosed        = errors.New("coding harness is closed")
)

type ImageSource struct {
	URL         string
	SHA256      string
	SizeBytes   int64
	ImageID     string
	ImageRef    string
	ArtifactSHA string
}

type Running interface {
	ContainerID() string
	BaseURL() string
	SourceIP() string
	ImageRef() string
}

// Runtime is the narrow screened-image/sandbox port. Its production adapter
// refuses a rootful, shared, or unrestricted Docker daemon.
type Runtime interface {
	Available(context.Context) error
	Load(context.Context, ImageSource) (string, error)
	Start(context.Context, string) (Running, error)
	Stop(context.Context, Running) error
	Release(context.Context, string)
}

type Config struct {
	Runtime      Runtime
	Sources      *codingsource.Registry
	Transport    http.RoundTripper
	Now          func() time.Time
	NewInstance  func() string
	MaxInstances int
}

type Factory struct {
	mu sync.Mutex

	runtime   Runtime
	sources   *codingsource.Registry
	client    *http.Client
	now       func() time.Time
	newID     func() string
	maximum   int
	instances map[string]*Handle
}

type lifecycleState uint8

const (
	stateDormant lifecycleState = iota + 1
	stateActivating
	stateActive
	stateStopping
	stateTerminal
	stateDestroyed
)

type Handle struct {
	mu sync.Mutex

	factory        *Factory
	binding        codingphase.HarnessBinding
	instanceID     string
	image          string
	state          lifecycleState
	activationDone chan struct{}
	activationErr  error
	destroyDone    chan struct{}
	running        Running
	sourceLease    *codingsource.Lease
	client         *codingcertifier.HTTPHarnessClient
	proxy          *lifecycleClient
}

type lifecycleClient struct{ handle *Handle }

func (handle *Handle) InstanceID() string {
	if handle == nil {
		return ""
	}
	return handle.instanceID
}

func (handle *Handle) Client() codingcertifier.HarnessClient {
	if handle == nil {
		return (*lifecycleClient)(nil)
	}
	return handle.proxy
}

func (handle *Handle) String() string        { return "CodingHarnessHandle{private}" }
func (handle *Handle) GoString() string      { return handle.String() }
func (handle *Handle) LogValue() slog.Value  { return slog.StringValue("coding-harness-handle") }
func (*Handle) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

func (factory *Factory) String() string       { return "CodingHarnessFactory{private}" }
func (factory *Factory) GoString() string     { return factory.String() }
func (factory *Factory) LogValue() slog.Value { return slog.StringValue("coding-harness-factory") }
func (*Factory) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

func (ImageSource) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }
func (source ImageSource) String() string        { return "CodingHarnessImageSource{private}" }
func (source ImageSource) GoString() string      { return source.String() }
func (source ImageSource) LogValue() slog.Value {
	return slog.StringValue("coding-harness-image-source")
}

var _ codingphase.HarnessFactory = (*Factory)(nil)
var _ codingphase.Harness = (*Handle)(nil)
var _ codingcertifier.HarnessClient = (*lifecycleClient)(nil)
var _ json.Marshaler = ImageSource{}
var _ json.Marshaler = (*Factory)(nil)
var _ json.Marshaler = (*Handle)(nil)
