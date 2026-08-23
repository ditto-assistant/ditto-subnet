package codingharness

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/url"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

type SandboxRuntime struct{ docker *sandbox.LocalDocker }

type sandboxRunning struct{ handle *sandbox.Handle }

func NewSandboxRuntime(docker *sandbox.LocalDocker) (*SandboxRuntime, error) {
	if docker == nil || !docker.RequireRootless || !docker.RequireIsolatedDaemon || !docker.Harden ||
		strings.TrimSpace(docker.EgressNetwork) == "" || !validProxyURL(docker.EgressProxy) {
		return nil, ErrInvalidConfig
	}
	return &SandboxRuntime{docker: docker}, nil
}

func validProxyURL(value string) bool {
	parsed, err := url.ParseRequestURI(strings.TrimSpace(value))
	return err == nil && (parsed.Scheme == "http" || parsed.Scheme == "https") &&
		parsed.Hostname() != "" && parsed.User == nil && parsed.RawQuery == "" && parsed.Fragment == ""
}

func (runtime *SandboxRuntime) Available(ctx context.Context) error {
	if runtime == nil || runtime.docker == nil {
		return ErrInvalidConfig
	}
	return runtime.docker.Available(ctx)
}

func (runtime *SandboxRuntime) Load(ctx context.Context, source ImageSource) (string, error) {
	if runtime == nil || runtime.docker == nil || !validImageURL(source.URL) ||
		!lowerSHA256(source.SHA256) || !lowerSHA256(source.ArtifactSHA) || source.SizeBytes <= 0 ||
		source.SizeBytes > 8<<30 || !strings.HasPrefix(source.ImageID, "sha256:") ||
		!lowerSHA256(strings.TrimPrefix(source.ImageID, "sha256:")) ||
		!strings.HasPrefix(source.ImageRef, "ditto-screen/") || !strings.HasSuffix(source.ImageRef, ":latest") {
		return "", ErrInvalidConfig
	}
	image, _, err := runtime.docker.LoadScreenedImage(ctx, sandbox.Source{
		ScreenedImageURL: source.URL, ScreenedImageSHA256: source.SHA256,
		ScreenedImageSize: source.SizeBytes, ScreenedImageID: source.ImageID,
		ScreenedImageRef: source.ImageRef,
	})
	return image, err
}

func (runtime *SandboxRuntime) Start(ctx context.Context, image string) (Running, error) {
	if runtime == nil || runtime.docker == nil {
		return nil, ErrInvalidConfig
	}
	handle, err := runtime.docker.Run(ctx, image, map[string]string{})
	if err != nil || handle == nil {
		return nil, errors.Join(err)
	}
	return &sandboxRunning{handle: handle}, nil
}

func (runtime *SandboxRuntime) Stop(ctx context.Context, running Running) error {
	value, ok := running.(*sandboxRunning)
	if runtime == nil || runtime.docker == nil || !ok || value == nil || value.handle == nil {
		return ErrInvalid
	}
	err := runtime.docker.StopRetainingImage(ctx, value.handle)
	if err == nil {
		runtime.docker.Release(context.WithoutCancel(ctx), value.handle.ImageRef)
	}
	return err
}

func (runtime *SandboxRuntime) Release(ctx context.Context, image string) {
	if runtime != nil && runtime.docker != nil {
		runtime.docker.Release(ctx, image)
	}
}

func (running *sandboxRunning) ContainerID() string {
	if running == nil || running.handle == nil {
		return ""
	}
	return running.handle.ContainerID
}
func (running *sandboxRunning) BaseURL() string {
	if running == nil || running.handle == nil {
		return ""
	}
	return running.handle.BaseURL
}
func (running *sandboxRunning) SourceIP() string {
	if running == nil || running.handle == nil {
		return ""
	}
	return running.handle.SourceIP
}
func (running *sandboxRunning) ImageRef() string {
	if running == nil || running.handle == nil {
		return ""
	}
	return running.handle.ImageRef
}

func (runtime *SandboxRuntime) String() string   { return "CodingHarnessSandboxRuntime{private}" }
func (runtime *SandboxRuntime) GoString() string { return runtime.String() }
func (runtime *SandboxRuntime) LogValue() slog.Value {
	return slog.StringValue("coding-harness-sandbox-runtime")
}
func (*SandboxRuntime) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

func (running *sandboxRunning) String() string   { return "CodingHarnessSandboxRunning{private}" }
func (running *sandboxRunning) GoString() string { return running.String() }
func (running *sandboxRunning) LogValue() slog.Value {
	return slog.StringValue("coding-harness-sandbox-running")
}
func (*sandboxRunning) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

var _ Runtime = (*SandboxRuntime)(nil)
var _ json.Marshaler = (*SandboxRuntime)(nil)
var _ json.Marshaler = (*sandboxRunning)(nil)
