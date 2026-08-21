package codingartifacts

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testTicketID = "12345678-1234-1234-1234-123456789abc"

func digest(body []byte) string {
	value := sha256.Sum256(body)
	return hex.EncodeToString(value[:])
}

func signedCapability(serverURL string, body []byte, now time.Time) Capability {
	sha := digest(body)
	expiresAt := now.Add(5 * time.Minute)
	return Capability{
		TicketID: testTicketID, Phase: PhaseAuthoring, Kind: KindVisibleBundle,
		Audience: AudienceWorkspaceMaterializer, SHA256: sha, SizeBytes: int64(len(body)),
		URL: serverURL + "/private-coding/coding-artifacts/v1/visible-bundle/sha256/" + sha +
			"?AWSAccessKeyId=placeholder&Expires=" + fmt.Sprint(expiresAt.Unix()) + "&Signature=secret-query",
		ExpiresAt: expiresAt, TicketDeadline: now.Add(time.Hour),
	}
}

func testFetcher(t *testing.T, now func() time.Time) *Fetcher {
	t.Helper()
	fetcher, err := New(Config{
		RequestTimeout: 2 * time.Second, TemporaryDirectory: t.TempDir(),
		AllowLoopback: true, Now: now,
	})
	if err != nil {
		t.Fatal(err)
	}
	return fetcher
}

func TestOpenVerifiesPrivateFileAndFreshOpener(t *testing.T) {
	body := []byte("immutable-visible-bundle")
	now := time.Now().UTC().Truncate(time.Second)
	var requests atomic.Int32
	var headerProblem atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		requests.Add(1)
		if request.Header.Get("Accept-Encoding") != "identity" {
			headerProblem.Store(true)
		}
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	temporaryRoot := t.TempDir()
	fetcher, err := New(Config{
		RequestTimeout: 2 * time.Second, TemporaryDirectory: temporaryRoot,
		AllowLoopback: true, Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	opener := fetcher.Opener(t.Context(), signedCapability(server.URL, body, now))
	for call := 0; call < 2; call++ {
		artifact, err := opener()
		if err != nil {
			t.Fatal(err)
		}
		entries, err := os.ReadDir(temporaryRoot)
		if err != nil || len(entries) != 1 {
			t.Fatalf("staged files = %d, err = %v", len(entries), err)
		}
		info, err := entries[0].Info()
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("staged mode = %v, err = %v", info.Mode(), err)
		}
		got, err := io.ReadAll(artifact)
		if err != nil || string(got) != string(body) {
			t.Fatalf("read = %q, err = %v", got, err)
		}
		if err := artifact.Close(); err != nil {
			t.Fatal(err)
		}
		if err := artifact.Close(); err != nil {
			t.Fatalf("idempotent close: %v", err)
		}
		entries, err = os.ReadDir(temporaryRoot)
		if err != nil || len(entries) != 0 {
			t.Fatalf("files after close = %d, err = %v", len(entries), err)
		}
	}
	if requests.Load() != 2 || headerProblem.Load() {
		t.Fatalf("requests = %d, identity header problem = %v", requests.Load(), headerProblem.Load())
	}
}

func TestOpenRejectsCapabilityDriftBeforeRequest(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	base := signedCapability(server.URL, body, now)
	tests := map[string]func(*Capability){
		"ticket":            func(value *Capability) { value.TicketID = "not-a-ticket" },
		"phase":             func(value *Capability) { value.Phase = DeliveryPhase("unknown") },
		"zero ticket":       func(value *Capability) { value.TicketID = "00000000-0000-0000-0000-000000000000" },
		"kind":              func(value *Capability) { value.Kind = Kind("unknown") },
		"audience":          func(value *Capability) { value.Audience = AudienceProtectedGrader },
		"digest":            func(value *Capability) { value.SHA256 = strings.Repeat("f", 64) },
		"size":              func(value *Capability) { value.SizeBytes = 0 },
		"oversized kind":    func(value *Capability) { value.SizeBytes = (2 << 30) + 1 },
		"nanosecond expiry": func(value *Capability) { value.ExpiresAt = value.ExpiresAt.Add(time.Nanosecond) },
		"deadline":          func(value *Capability) { value.TicketDeadline = value.ExpiresAt.Add(-time.Second) },
		"unsigned":          func(value *Capability) { value.URL = strings.Split(value.URL, "?")[0] },
		"wrong path":        func(value *Capability) { value.URL = strings.Replace(value.URL, "visible-bundle", "grader-bundle", 1) },
		"encoded path": func(value *Capability) {
			value.URL = strings.Replace(value.URL, "coding-artifacts", "coding%2Dartifacts", 1)
		},
		"dot path": func(value *Capability) {
			value.URL = strings.Replace(value.URL, "/private-coding/", "/private-coding/../private-coding/", 1)
		},
		"expiry mismatch": func(value *Capability) { value.ExpiresAt = value.ExpiresAt.Add(-time.Second) },
		"ambiguous signature": func(value *Capability) {
			value.URL += "&X-Amz-Date=20260821T120000Z&X-Amz-Expires=300&X-Amz-Signature=second"
		},
		"non ascii URL": func(value *Capability) { value.URL += "&label=é" },
		"oversized URL": func(value *Capability) { value.URL += "&padding=" + strings.Repeat("a", maximumSignedURLBytes) },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			capability := base
			mutate(&capability)
			_, err := testFetcher(t, func() time.Time { return now }).Open(t.Context(), capability)
			if !errors.Is(err, ErrInvalidCapability) {
				t.Fatalf("error = %v", err)
			}
		})
	}
	if requests.Load() != 0 {
		t.Fatalf("invalid capabilities made %d requests", requests.Load())
	}
}

func TestOpenAcceptsV4SignatureExpiry(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	capability := signedCapability(server.URL, body, now)
	capability.URL = strings.Split(capability.URL, "?")[0] +
		"?X-Amz-Date=" + now.Format("20060102T150405Z") +
		"&X-Amz-Expires=300&X-Amz-Signature=secret-query"
	artifact, err := testFetcher(t, func() time.Time { return now }).Open(t.Context(), capability)
	if err != nil {
		t.Fatal(err)
	}
	if err := artifact.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestOpenRejectsExpiredCapabilityBeforeRequest(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		t.Error("expired capability reached storage")
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	capability := signedCapability(server.URL, body, now)
	_, err := testFetcher(t, func() time.Time { return capability.ExpiresAt }).Open(t.Context(), capability)
	if !errors.Is(err, ErrCapabilityExpired) {
		t.Fatalf("error = %v", err)
	}
}

func TestOpenRejectsPrivateNetworkWithoutLoopbackGate(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		t.Error("disabled loopback capability reached storage")
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	fetcher, err := New(Config{RequestTimeout: time.Second, Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	_, err = fetcher.Open(t.Context(), signedCapability(server.URL, body, now))
	if !errors.Is(err, ErrInvalidCapability) {
		t.Fatalf("error = %v", err)
	}
}

func TestOpenRefusesRedirect(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	var redirected atomic.Bool
	target := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		redirected.Store(true)
		_, _ = writer.Write(body)
	}))
	defer target.Close()
	origin := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		http.Redirect(writer, request, target.URL, http.StatusFound)
	}))
	defer origin.Close()
	_, err := testFetcher(t, func() time.Time { return now }).Open(t.Context(), signedCapability(origin.URL, body, now))
	if !errors.Is(err, ErrArtifactUnavailable) || redirected.Load() {
		t.Fatalf("error = %v, redirected = %v", err, redirected.Load())
	}
}

func TestOpenRejectsStatusAndContentLengthBeforeStaging(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	tests := map[string]struct {
		status        int
		contentLength string
		want          error
	}{
		"status": {status: http.StatusForbidden, want: ErrArtifactUnavailable},
		"length": {status: http.StatusOK, contentLength: "99", want: ErrArtifactIntegrity},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				if test.contentLength != "" {
					writer.Header().Set("Content-Length", test.contentLength)
				}
				writer.WriteHeader(test.status)
				_, _ = writer.Write(body)
			}))
			defer server.Close()
			_, err := testFetcher(t, func() time.Time { return now }).Open(t.Context(), signedCapability(server.URL, body, now))
			if !errors.Is(err, test.want) {
				t.Fatalf("error = %v", err)
			}
		})
	}
}

func TestOpenRejectsResponseIntegrityFailuresAndCleansUp(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	tests := map[string]struct {
		expected []byte
		served   []byte
	}{
		"truncated":    {expected: []byte("expected-body"), served: []byte("short")},
		"oversized":    {expected: []byte("short"), served: []byte("larger-body")},
		"wrong digest": {expected: []byte("expected"), served: []byte("differen")},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.Header().Set("Trailer", "X-Ditto-End")
				writer.WriteHeader(http.StatusOK)
				_, _ = writer.Write(test.served)
			}))
			defer server.Close()
			temporaryRoot := t.TempDir()
			fetcher, err := New(Config{
				RequestTimeout: 2 * time.Second, TemporaryDirectory: temporaryRoot,
				AllowLoopback: true, Now: func() time.Time { return now },
			})
			if err != nil {
				t.Fatal(err)
			}
			_, err = fetcher.Open(t.Context(), signedCapability(server.URL, test.expected, now))
			if !errors.Is(err, ErrArtifactIntegrity) {
				t.Fatalf("error = %v", err)
			}
			entries, readErr := os.ReadDir(temporaryRoot)
			if readErr != nil || len(entries) != 0 {
				t.Fatalf("partial files = %d, err = %v", len(entries), readErr)
			}
		})
	}
}

func TestOpenRejectsExpiryDuringTransfer(t *testing.T) {
	body := []byte("artifact")
	base := time.Now().UTC().Truncate(time.Second)
	var current atomic.Int64
	current.Store(base.UnixNano())
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		current.Store(base.Add(5 * time.Minute).UnixNano())
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	fetcher := testFetcher(t, func() time.Time { return time.Unix(0, current.Load()).UTC() })
	_, err := fetcher.Open(t.Context(), signedCapability(server.URL, body, base))
	if !errors.Is(err, ErrCapabilityExpired) {
		t.Fatalf("error = %v", err)
	}
}

func TestOpenHonorsCancellationAndTimeout(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
		<-request.Context().Done()
	}))
	defer server.Close()
	capability := signedCapability(server.URL, body, now)
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := testFetcher(t, func() time.Time { return now }).Open(cancelled, capability)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel error = %v", err)
	}
	fetcher, newErr := New(Config{
		RequestTimeout: 20 * time.Millisecond, TemporaryDirectory: t.TempDir(),
		AllowLoopback: true, Now: time.Now,
	})
	if newErr != nil {
		t.Fatal(newErr)
	}
	capability = signedCapability(server.URL, body, time.Now().UTC().Truncate(time.Second))
	_, err = fetcher.Open(t.Context(), capability)
	if !errors.Is(err, ErrArtifactUnavailable) {
		t.Fatalf("timeout error = %v", err)
	}
}

func TestOpenRedactsSignedURLFromTransportError(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	capability := signedCapability("http://127.0.0.1:1", body, now)
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		return nil, fmt.Errorf("transport exposed %s", request.URL.String())
	})}
	fetcher := newWithClients(Config{
		RequestTimeout: time.Second, TemporaryDirectory: t.TempDir(),
		AllowLoopback: true, Now: func() time.Time { return now },
	}, client, client)
	_, err := fetcher.Open(t.Context(), capability)
	if !errors.Is(err, ErrArtifactUnavailable) {
		t.Fatalf("error = %v", err)
	}
	rendered := fmt.Sprintf("%+v", err)
	if strings.Contains(rendered, "secret-query") || strings.Contains(rendered, capability.URL) {
		t.Fatalf("signed URL leaked: %s", rendered)
	}
}

func TestOpenRedactsResponseStreamErrorAndCleansPartialFile(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	capability := signedCapability("http://127.0.0.1:1", body, now)
	temporaryRoot := t.TempDir()
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK, ContentLength: -1,
			Body:   io.NopCloser(errorReader("stream exposed secret-query")),
			Header: make(http.Header),
		}, nil
	})}
	fetcher := newWithClients(Config{
		RequestTimeout: time.Second, TemporaryDirectory: temporaryRoot,
		AllowLoopback: true, Now: func() time.Time { return now },
	}, client, client)
	_, err := fetcher.Open(t.Context(), capability)
	if !errors.Is(err, ErrArtifactUnavailable) || strings.Contains(fmt.Sprintf("%+v", err), "secret-query") {
		t.Fatalf("error = %v", err)
	}
	entries, readErr := os.ReadDir(temporaryRoot)
	if readErr != nil || len(entries) != 0 {
		t.Fatalf("partial files = %d, err = %v", len(entries), readErr)
	}
}

func TestCapabilityFormattingRedactsBearerURL(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	capability := signedCapability("http://127.0.0.1:1", body, now)
	for _, rendered := range []string{
		fmt.Sprint(capability),
		fmt.Sprintf("%+v", capability),
		fmt.Sprintf("%#v", capability),
	} {
		if strings.Contains(rendered, "secret-query") || strings.Contains(rendered, capability.URL) {
			t.Fatalf("signed URL leaked: %s", rendered)
		}
	}
	var structured bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&structured, nil))
	logger.Info("capability", "value", capability)
	if strings.Contains(structured.String(), "secret-query") || strings.Contains(structured.String(), capability.URL) {
		t.Fatalf("structured log leaked signed URL: %s", structured.String())
	}
	if encoded, err := json.Marshal(capability); err == nil || strings.Contains(string(encoded), "secret-query") {
		t.Fatalf("capability serialized: %q, err = %v", encoded, err)
	}
}

func TestOpenConcurrentCapabilitiesRemainIsolated(t *testing.T) {
	body := []byte("artifact")
	now := time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		_, _ = writer.Write(body)
	}))
	defer server.Close()
	fetcher := testFetcher(t, func() time.Time { return now })
	capability := signedCapability(server.URL, body, now)
	const count = 8
	var wait sync.WaitGroup
	errorsSeen := make(chan error, count)
	for index := 0; index < count; index++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			artifact, err := fetcher.Open(t.Context(), capability)
			if err != nil {
				errorsSeen <- err
				return
			}
			got, err := io.ReadAll(artifact)
			closeErr := artifact.Close()
			if err != nil || closeErr != nil || string(got) != string(body) {
				errorsSeen <- fmt.Errorf("read=%q read_err=%v close_err=%v", got, err, closeErr)
			}
		}()
	}
	wait.Wait()
	close(errorsSeen)
	for err := range errorsSeen {
		t.Error(err)
	}
}

func TestNewRejectsUnsafeConfiguration(t *testing.T) {
	tests := map[string]Config{
		"zero timeout":                 {},
		"fractional timeout":           {RequestTimeout: time.Microsecond},
		"long timeout":                 {RequestTimeout: maximumRequestTimeout + time.Millisecond},
		"relative temporary directory": {RequestTimeout: time.Second, TemporaryDirectory: "relative"},
		"missing temporary directory":  {RequestTimeout: time.Second, TemporaryDirectory: filepath.Join(t.TempDir(), "missing")},
	}
	for name, config := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := New(config); err == nil {
				t.Fatal("expected configuration rejection")
			}
		})
	}
}

func TestKindPoliciesAreExhaustive(t *testing.T) {
	tests := map[Kind]struct {
		maximum  int64
		audience Audience
	}{
		KindVisibleBundle:   {maximum: 2 << 30, audience: AudienceWorkspaceMaterializer},
		KindMemoryBundle:    {maximum: 64 << 20, audience: AudienceMemorySeedProjector},
		KindResourceProfile: {maximum: 4 << 20, audience: AudienceResourceSupervisor},
		KindGraderBundle:    {maximum: 512 << 20, audience: AudienceProtectedGrader},
	}
	for kind, expected := range tests {
		maximum, audience, ok := kindPolicy(kind)
		if !ok || maximum != expected.maximum || audience != expected.audience {
			t.Fatalf("kind %q policy = %d, %q, %v", kind, maximum, audience, ok)
		}
	}
	if _, _, ok := kindPolicy(Kind("unknown")); ok {
		t.Fatal("unknown kind has a policy")
	}
}

func TestDeliveryPhasesRejectCrossBoundaryArtifacts(t *testing.T) {
	allowed := map[DeliveryPhase]map[Kind]bool{
		PhaseAuthoring: {
			KindVisibleBundle: true, KindMemoryBundle: true, KindResourceProfile: true,
		},
		PhaseGrading: {
			KindVisibleBundle: true, KindResourceProfile: true, KindGraderBundle: true,
		},
	}
	for _, phase := range []DeliveryPhase{PhaseAuthoring, PhaseGrading, DeliveryPhase("unknown")} {
		for _, kind := range []Kind{KindVisibleBundle, KindMemoryBundle, KindResourceProfile, KindGraderBundle} {
			if phaseAllows(phase, kind) != allowed[phase][kind] {
				t.Fatalf("phase %q kind %q allowance drifted", phase, kind)
			}
		}
	}
}

func TestGuardedClientsIgnoreAmbientProxyAndRefuseRedirects(t *testing.T) {
	client := guardedClient(false)
	transport, ok := client.Transport.(*http.Transport)
	if !ok || transport.Proxy != nil || !transport.DisableCompression ||
		transport.MaxResponseHeaderBytes != 1<<20 || client.CheckRedirect == nil {
		t.Fatalf("guarded transport is not pinned: %#v", client.Transport)
	}
}

func TestVerifiedFileRetriesRemovalFailure(t *testing.T) {
	handle, err := os.CreateTemp(t.TempDir(), "artifact-*")
	if err != nil {
		t.Fatal(err)
	}
	path := handle.Name()
	var attempts atomic.Int32
	artifact := &verifiedFile{
		file: handle, path: path,
		remove: func(value string) error {
			if attempts.Add(1) == 1 {
				return errors.New("transient remove failure")
			}
			return os.Remove(value)
		},
	}
	if err := artifact.Close(); !errors.Is(err, ErrArtifactUnavailable) {
		t.Fatalf("first close error = %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("path was not retained for retry: %v", err)
	}
	if err := artifact.Close(); err != nil {
		t.Fatalf("retry close error = %v", err)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("path survived retry: %v", err)
	}
	if err := artifact.Close(); err != nil {
		t.Fatalf("idempotent close error = %v", err)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

type errorReader string

func (reader errorReader) Read([]byte) (int, error) {
	return 0, errors.New(string(reader))
}
