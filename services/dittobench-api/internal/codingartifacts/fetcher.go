// Package codingartifacts fetches one ticket-bound coding artifact into a
// verified, private temporary file. It does not extract or interpret bytes.
package codingartifacts

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/netguard"
)

const (
	maximumSignedURLBytes = 16 << 10
	maximumRequestTimeout = 15 * time.Minute
	maximumTicketLifetime = 2 * time.Hour
	maximumCapabilityTTL  = 15 * time.Minute
)

// Kind identifies one of the four contract-v1 artifact classes.
type Kind string

const (
	KindVisibleBundle   Kind = "visible-bundle"
	KindMemoryBundle    Kind = "memory-bundle"
	KindResourceProfile Kind = "resource-profile"
	KindGraderBundle    Kind = "grader-bundle"
)

// Audience is the only trusted component allowed to receive an artifact.
type Audience string

const (
	AudienceWorkspaceMaterializer Audience = "workspace-materializer"
	AudienceMemorySeedProjector   Audience = "memory-seed-projector"
	AudienceResourceSupervisor    Audience = "resource-supervisor"
	AudienceProtectedGrader       Audience = "protected-grader"
)

var (
	ErrInvalidCapability   = errors.New("coding artifact capability is invalid")
	ErrCapabilityExpired   = errors.New("coding artifact capability expired")
	ErrArtifactUnavailable = errors.New("coding artifact is unavailable")
	ErrArtifactIntegrity   = errors.New("coding artifact integrity check failed")
)

// Capability is a trusted, audience-projected view of one Platform bearer URL.
// URL must never be logged, persisted, or sent to the miner or model context.
type Capability struct {
	TicketID       string
	Kind           Kind
	Audience       Audience
	SHA256         string
	SizeBytes      int64
	URL            string
	ExpiresAt      time.Time
	TicketDeadline time.Time
}

// String deliberately omits the bearer URL.
func (capability Capability) String() string {
	return fmt.Sprintf(
		"CodingArtifactCapability{ticket=%q kind=%q audience=%q size_bytes=%d expires_at=%q}",
		capability.TicketID, capability.Kind, capability.Audience,
		capability.SizeBytes, capability.ExpiresAt.UTC().Format(time.RFC3339),
	)
}

// GoString keeps %#v diagnostics on the same redacted projection.
func (capability Capability) GoString() string {
	return capability.String()
}

// LogValue keeps structured slog output on the same redacted projection.
func (capability Capability) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("ticket", capability.TicketID),
		slog.String("kind", string(capability.Kind)),
		slog.String("audience", string(capability.Audience)),
		slog.Int64("size_bytes", capability.SizeBytes),
		slog.Time("expires_at", capability.ExpiresAt.UTC()),
	)
}

// MarshalJSON fails closed because this is an internal consumer type, not the
// future validator wire model, and serializing it would persist the bearer URL.
func (Capability) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding artifact capabilities cannot be serialized")
}

// Config binds fetch lifetime, local-practice loopback, and private staging.
type Config struct {
	RequestTimeout     time.Duration
	TemporaryDirectory string
	AllowLoopback      bool
	Now                func() time.Time
}

// Fetcher downloads immutable bytes through guarded clients.
type Fetcher struct {
	timeout        time.Duration
	temporaryRoot  string
	allowLoopback  bool
	now            func() time.Time
	publicClient   *http.Client
	loopbackClient *http.Client
}

// New returns a fail-closed artifact fetcher.
func New(config Config) (*Fetcher, error) {
	if config.RequestTimeout <= 0 || config.RequestTimeout > maximumRequestTimeout ||
		config.RequestTimeout%time.Millisecond != 0 {
		return nil, errors.New("coding artifact request timeout is outside bounds")
	}
	if config.TemporaryDirectory != "" {
		if !filepath.IsAbs(config.TemporaryDirectory) {
			return nil, errors.New("coding artifact temporary directory must be absolute")
		}
		info, err := os.Lstat(config.TemporaryDirectory)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("coding artifact temporary directory is unavailable")
		}
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return newWithClients(
		config,
		guardedClient(false),
		guardedClient(true),
	), nil
}

func newWithClients(config Config, publicClient, loopbackClient *http.Client) *Fetcher {
	return &Fetcher{
		timeout: config.RequestTimeout, temporaryRoot: config.TemporaryDirectory,
		allowLoopback: config.AllowLoopback, now: config.Now,
		publicClient: publicClient, loopbackClient: loopbackClient,
	}
}

func guardedClient(allowPrivate bool) *http.Client {
	client := netguard.Client(allowPrivate)
	client.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	if transport, ok := client.Transport.(*http.Transport); ok {
		transport.Proxy = nil
		transport.DisableCompression = true
		transport.MaxResponseHeaderBytes = 1 << 20
	}
	return client
}

// Opener adapts one capability to codingcertifier.BundleOpener without making
// this transport package depend on the certifier package.
func (fetcher *Fetcher) Opener(ctx context.Context, capability Capability) func() (io.ReadCloser, error) {
	return func() (io.ReadCloser, error) {
		return fetcher.Open(ctx, capability)
	}
}

// Open downloads and verifies one capability. Every call fetches fresh bytes;
// the returned reader deletes its private temporary file on Close.
func (fetcher *Fetcher) Open(ctx context.Context, capability Capability) (io.ReadCloser, error) {
	if ctx == nil {
		return nil, errors.New("coding artifact fetch context is required")
	}
	requestContext, cancelRequest := context.WithTimeout(ctx, fetcher.timeout)
	defer cancelRequest()
	now := fetcher.now().UTC()
	parsed, client, err := fetcher.validate(requestContext, capability, now)
	if err != nil {
		if ctx.Err() != nil {
			return nil, fmt.Errorf("coding artifact fetch interrupted: %w", ctx.Err())
		}
		if requestContext.Err() != nil {
			return nil, fail(ErrArtifactUnavailable, "capability validation timed out")
		}
		return nil, err
	}
	transportDeadline := capability.ExpiresAt
	if capability.TicketDeadline.Before(transportDeadline) {
		transportDeadline = capability.TicketDeadline
	}
	if !fetcher.now().UTC().Before(transportDeadline) {
		return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
	}
	requestContext, cancelDeadline := context.WithDeadline(requestContext, transportDeadline)
	defer cancelDeadline()
	request, err := http.NewRequestWithContext(requestContext, http.MethodGet, parsed.String(), nil)
	if err != nil {
		return nil, fail(ErrInvalidCapability, "request construction failed")
	}
	request.Header.Set("Accept-Encoding", "identity")
	response, err := client.Do(request)
	if err != nil {
		if ctx.Err() != nil {
			return nil, fmt.Errorf("coding artifact fetch interrupted: %w", ctx.Err())
		}
		if !fetcher.now().UTC().Before(transportDeadline) {
			return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
		}
		return nil, fail(ErrArtifactUnavailable, "request failed")
	}
	defer response.Body.Close()
	if !fetcher.now().UTC().Before(transportDeadline) {
		return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
	}
	if response.StatusCode != http.StatusOK {
		return nil, fail(ErrArtifactUnavailable, "storage returned a non-success status")
	}
	if response.ContentLength >= 0 && response.ContentLength != capability.SizeBytes {
		return nil, fail(ErrArtifactIntegrity, "declared response size disagrees")
	}
	handle, err := os.CreateTemp(fetcher.temporaryRoot, ".dittobench-coding-artifact-*")
	if err != nil {
		return nil, fail(ErrArtifactUnavailable, "private staging failed")
	}
	path := handle.Name()
	cleanup := true
	defer func() {
		if cleanup {
			_ = handle.Close()
			_ = os.Remove(path)
		}
	}()
	if err := handle.Chmod(0o600); err != nil {
		return nil, fail(ErrArtifactUnavailable, "private staging permissions failed")
	}
	hasher := sha256.New()
	limited := &io.LimitedReader{R: response.Body, N: capability.SizeBytes + 1}
	written, err := io.Copy(io.MultiWriter(handle, hasher), limited)
	if err != nil {
		if ctx.Err() != nil {
			return nil, fmt.Errorf("coding artifact fetch interrupted: %w", ctx.Err())
		}
		if !fetcher.now().UTC().Before(transportDeadline) {
			return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
		}
		return nil, fail(ErrArtifactUnavailable, "response stream failed")
	}
	if written != capability.SizeBytes {
		return nil, fail(ErrArtifactIntegrity, "downloaded size disagrees")
	}
	if !fetcher.now().UTC().Before(transportDeadline) {
		return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
	}
	if hex.EncodeToString(hasher.Sum(nil)) != capability.SHA256 {
		return nil, fail(ErrArtifactIntegrity, "downloaded digest disagrees")
	}
	if err := handle.Sync(); err != nil {
		return nil, fail(ErrArtifactUnavailable, "private staging sync failed")
	}
	if _, err := handle.Seek(0, io.SeekStart); err != nil {
		return nil, fail(ErrArtifactUnavailable, "private staging rewind failed")
	}
	if ctx.Err() != nil {
		return nil, fmt.Errorf("coding artifact fetch interrupted: %w", ctx.Err())
	}
	if requestContext.Err() != nil {
		if !fetcher.now().UTC().Before(transportDeadline) {
			return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
		}
		return nil, fail(ErrArtifactUnavailable, "request timeout elapsed")
	}
	if !fetcher.now().UTC().Before(transportDeadline) {
		return nil, fail(ErrCapabilityExpired, "transport deadline elapsed")
	}
	cleanup = false
	return &verifiedFile{file: handle, path: path, remove: os.Remove}, nil
}

func (fetcher *Fetcher) validate(ctx context.Context, capability Capability, now time.Time) (*url.URL, *http.Client, error) {
	maximum, audience, knownKind := kindPolicy(capability.Kind)
	if !knownKind || audience != capability.Audience ||
		!validUUID(capability.TicketID) || !lowerSHA256(capability.SHA256) ||
		capability.SizeBytes <= 0 || capability.SizeBytes > maximum ||
		capability.ExpiresAt.IsZero() || capability.ExpiresAt.Nanosecond() != 0 ||
		capability.TicketDeadline.IsZero() || capability.TicketDeadline.Before(capability.ExpiresAt) ||
		capability.TicketDeadline.After(now.Add(maximumTicketLifetime)) ||
		capability.ExpiresAt.After(now.Add(maximumCapabilityTTL)) {
		return nil, nil, fail(ErrInvalidCapability, "known fields disagree")
	}
	if !now.Before(capability.ExpiresAt) || !now.Before(capability.TicketDeadline) {
		return nil, nil, fail(ErrCapabilityExpired, "ticket or URL lifetime elapsed")
	}
	parsed, loopback, err := fetcher.validateURL(ctx, capability)
	if err != nil {
		return nil, nil, err
	}
	if loopback {
		return parsed, fetcher.loopbackClient, nil
	}
	return parsed, fetcher.publicClient, nil
}

func (fetcher *Fetcher) validateURL(ctx context.Context, capability Capability) (*url.URL, bool, error) {
	if len(capability.URL) == 0 || len(capability.URL) > maximumSignedURLBytes ||
		strings.IndexFunc(capability.URL, func(character rune) bool {
			return character < 32 || character > 126
		}) >= 0 {
		return nil, false, fail(ErrInvalidCapability, "URL is outside bounds")
	}
	parsed, err := url.Parse(capability.URL)
	if err != nil || !parsed.IsAbs() || parsed.Hostname() == "" || parsed.User != nil ||
		parsed.Fragment != "" || parsed.RawQuery == "" || parsed.RawPath != "" ||
		path.Clean(parsed.Path) != parsed.Path || strings.Contains(parsed.Path, "//") {
		return nil, false, fail(ErrInvalidCapability, "URL structure is invalid")
	}
	if port := parsed.Port(); port != "" {
		number, portErr := strconv.Atoi(port)
		if portErr != nil || number < 1 || number > 65_535 {
			return nil, false, fail(ErrInvalidCapability, "URL port is invalid")
		}
	}
	loopback := isLoopbackHost(parsed.Hostname())
	if parsed.Scheme != "https" && !(fetcher.allowLoopback && loopback && parsed.Scheme == "http") {
		return nil, false, fail(ErrInvalidCapability, "URL transport is invalid")
	}
	if loopback {
		if !fetcher.allowLoopback {
			return nil, false, fail(ErrInvalidCapability, "loopback transport is disabled")
		}
	} else if err := netguard.ValidateURLContext(ctx, capability.URL, false); err != nil {
		return nil, false, fail(ErrInvalidCapability, "URL destination is invalid")
	}
	key := "coding-artifacts/v1/" + string(capability.Kind) + "/sha256/" + capability.SHA256
	if !strings.HasSuffix(parsed.Path, "/"+key) {
		return nil, false, fail(ErrInvalidCapability, "URL path disagrees")
	}
	query, err := url.ParseQuery(parsed.RawQuery)
	if err != nil {
		return nil, false, fail(ErrInvalidCapability, "URL query is invalid")
	}
	expiresAt, err := signedURLExpiry(query)
	if err != nil || !expiresAt.Equal(capability.ExpiresAt.UTC()) {
		return nil, false, fail(ErrInvalidCapability, "URL expiry disagrees")
	}
	return parsed, loopback, nil
}

func signedURLExpiry(values url.Values) (time.Time, error) {
	normalized := make(map[string][]string, len(values))
	fields := 0
	for name, entries := range values {
		key := strings.ToLower(name)
		normalized[key] = append(normalized[key], entries...)
		fields += len(entries)
	}
	if fields > 64 {
		return time.Time{}, errors.New("too many signed URL fields")
	}
	versionFour := normalized["x-amz-signature"]
	versionTwo := normalized["signature"]
	if (len(versionFour) > 0) == (len(versionTwo) > 0) {
		return time.Time{}, errors.New("ambiguous signed URL")
	}
	if len(versionFour) > 0 {
		dateValues := normalized["x-amz-date"]
		durationValues := normalized["x-amz-expires"]
		if len(versionFour) != 1 || versionFour[0] == "" || len(dateValues) != 1 || len(durationValues) != 1 {
			return time.Time{}, errors.New("invalid v4 signature fields")
		}
		signedAt, err := time.Parse("20060102T150405Z", dateValues[0])
		if err != nil {
			return time.Time{}, err
		}
		durationSeconds, err := strconv.ParseInt(durationValues[0], 10, 64)
		if err != nil || durationSeconds <= 0 || durationSeconds > int64(maximumCapabilityTTL/time.Second) {
			return time.Time{}, errors.New("invalid v4 expiry")
		}
		return signedAt.Add(time.Duration(durationSeconds) * time.Second).UTC(), nil
	}
	expiryValues := normalized["expires"]
	if len(versionTwo) != 1 || versionTwo[0] == "" || len(expiryValues) != 1 {
		return time.Time{}, errors.New("invalid v2 signature fields")
	}
	seconds, err := strconv.ParseInt(expiryValues[0], 10, 64)
	if err != nil {
		return time.Time{}, err
	}
	return time.Unix(seconds, 0).UTC(), nil
}

func validUUID(value string) bool {
	if len(value) != 36 || value == "00000000-0000-0000-0000-000000000000" {
		return false
	}
	for index, character := range value {
		if index == 8 || index == 13 || index == 18 || index == 23 {
			if character != '-' {
				return false
			}
			continue
		}
		if !strings.ContainsRune("0123456789abcdef", character) {
			return false
		}
	}
	return true
}

func lowerSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	parsed := net.ParseIP(host)
	return parsed != nil && parsed.IsLoopback()
}

func kindPolicy(kind Kind) (int64, Audience, bool) {
	switch kind {
	case KindVisibleBundle:
		return 2 << 30, AudienceWorkspaceMaterializer, true
	case KindMemoryBundle:
		return 64 << 20, AudienceMemorySeedProjector, true
	case KindResourceProfile:
		return 4 << 20, AudienceResourceSupervisor, true
	case KindGraderBundle:
		return 512 << 20, AudienceProtectedGrader, true
	default:
		return 0, "", false
	}
}

func fail(kind error, message string) error {
	return fmt.Errorf("%w: %s", kind, message)
}

type verifiedFile struct {
	mu          sync.Mutex
	file        *os.File
	path        string
	remove      func(string) error
	closeFailed bool
}

func (artifact *verifiedFile) Read(buffer []byte) (int, error) {
	artifact.mu.Lock()
	defer artifact.mu.Unlock()
	if artifact.file == nil {
		return 0, os.ErrClosed
	}
	return artifact.file.Read(buffer)
}

func (artifact *verifiedFile) Close() error {
	artifact.mu.Lock()
	defer artifact.mu.Unlock()
	if artifact.file != nil {
		if err := artifact.file.Close(); err != nil {
			artifact.closeFailed = true
		}
		artifact.file = nil
	}
	if artifact.path != "" {
		remove := artifact.remove
		if remove == nil {
			remove = os.Remove
		}
		if err := remove(artifact.path); err != nil {
			return fail(ErrArtifactUnavailable, "private staging cleanup failed")
		}
		artifact.path = ""
	}
	if artifact.closeFailed {
		return fail(ErrArtifactUnavailable, "private staging cleanup failed")
	}
	return nil
}
