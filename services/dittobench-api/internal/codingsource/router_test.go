package codingsource

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
)

type routerFixture struct {
	now      time.Time
	registry *Registry
	lease    *Lease
	router   *Router
	binding  HarnessBinding
	baseURL  string
}

func newRouterFixture(t *testing.T) *routerFixture {
	t.Helper()
	now := time.Date(2026, 8, 23, 18, 0, 0, 0, time.UTC)
	registry := NewRegistry(func() time.Time { return now })
	binding := fixtureSourceBinding(now, "route")
	lease, err := registry.Register(binding, "172.30.0.7")
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	baseURL := "http://host.docker.internal:" + port
	router, err := NewRouter(RouterConfig{
		Listener: listener, PublicBaseURL: baseURL, Registry: registry,
		NewToken: func() string { return strings.Repeat("a", 43) }, MaxRoutes: 4,
	})
	if err != nil {
		t.Fatal(err)
	}
	fixture := &routerFixture{now: now, registry: registry, lease: lease, router: router, binding: binding, baseURL: baseURL}
	t.Cleanup(func() {
		_ = lease.Close()
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = router.Close(ctx)
	})
	return fixture
}

func (fixture *routerFixture) request(t *testing.T, target, remote string) *http.Request {
	t.Helper()
	parsed, err := url.Parse(target)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, parsed.RequestURI(), nil)
	request.Host = strings.TrimPrefix(fixture.baseURL, "http://")
	request.RemoteAddr = remote
	return request
}

func TestWorkspaceAndInferenceRoutesAreSourceBoundAndPathScoped(t *testing.T) {
	fixture := newRouterFixture(t)
	workspaceCalls := 0
	workspace, err := fixture.router.WorkspacePublisher().Publish(t.Context(), codingcertifier.CapabilityBinding{
		HarnessInstanceID:   fixture.binding.HarnessInstanceID,
		AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		TicketID:            fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
	}, http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		workspaceCalls++
		if request.URL.Path != "/tool" {
			t.Fatalf("workspace inner path=%q", request.URL.Path)
		}
		response.WriteHeader(http.StatusNoContent)
	}))
	if err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	fixture.router.ServeHTTP(response, fixture.request(t, workspace.URL(), "172.30.0.7:43100"))
	if response.Code != http.StatusNoContent || workspaceCalls != 1 {
		t.Fatalf("workspace status/calls=%d/%d", response.Code, workspaceCalls)
	}
	publicURL, err := url.Parse(workspace.URL())
	if err != nil {
		t.Fatal(err)
	}
	publicURL.Host = "127.0.0.1:" + publicURL.Port()
	direct, err := http.NewRequestWithContext(t.Context(), http.MethodPost, publicURL.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	direct.Host = strings.TrimPrefix(fixture.baseURL, "http://")
	directResponse, err := (&http.Client{Transport: &http.Transport{Proxy: nil}}).Do(direct)
	if err != nil {
		t.Fatal(err)
	}
	_ = directResponse.Body.Close()
	if directResponse.StatusCode != http.StatusNotFound || workspaceCalls != 1 {
		t.Fatalf("host-origin status/calls=%d/%d", directResponse.StatusCode, workspaceCalls)
	}
	wrong := fixture.request(t, workspace.URL(), "172.30.0.8:43100")
	wrong.Header.Set("X-Forwarded-For", "172.30.0.7")
	response = httptest.NewRecorder()
	fixture.router.ServeHTTP(response, wrong)
	if response.Code != http.StatusNotFound || workspaceCalls != 1 {
		t.Fatalf("wrong source status/calls=%d/%d", response.Code, workspaceCalls)
	}

	inferenceCalls := 0
	inference, err := fixture.router.InferencePublisher().Publish(t.Context(), codinggateway.CapabilityBinding{
		HarnessInstanceID:   fixture.binding.HarnessInstanceID,
		AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		TicketID:            fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
		Deadline:            fixture.binding.Deadline,
	}, http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		inferenceCalls++
		if request.URL.Path != "/chat/completions" {
			t.Fatalf("inference inner path=%q", request.URL.Path)
		}
		response.WriteHeader(http.StatusNoContent)
	}))
	if err != nil {
		t.Fatal(err)
	}
	response = httptest.NewRecorder()
	fixture.router.ServeHTTP(
		response,
		fixture.request(t, inference.URL()+"/chat/completions", "172.30.0.7:43101"),
	)
	if response.Code != http.StatusNoContent || inferenceCalls != 1 {
		t.Fatalf("inference status/calls=%d/%d", response.Code, inferenceCalls)
	}
	for _, capability := range []interface {
		Revoke(context.Context) error
		Close() error
	}{workspace, inference} {
		if err := capability.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		if err := capability.Close(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestRouteRevocationStopsAdmissionAndWaitsForInflight(t *testing.T) {
	fixture := newRouterFixture(t)
	started := make(chan struct{})
	release := make(chan struct{})
	published, err := fixture.router.WorkspacePublisher().Publish(t.Context(), codingcertifier.CapabilityBinding{
		HarnessInstanceID:   fixture.binding.HarnessInstanceID,
		AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		TicketID:            fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
	}, http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		close(started)
		<-release
		response.WriteHeader(http.StatusNoContent)
	}))
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		response := httptest.NewRecorder()
		fixture.router.ServeHTTP(response, fixture.request(t, published.URL(), "172.30.0.7:43102"))
	}()
	<-started
	revokeContext, cancel := context.WithTimeout(t.Context(), 10*time.Millisecond)
	defer cancel()
	if err := published.Revoke(revokeContext); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("revoke err=%v", err)
	}
	response := httptest.NewRecorder()
	fixture.router.ServeHTTP(response, fixture.request(t, fixture.baseURL+workspacePrefix+strings.Repeat("a", 43)+"/tool", "172.30.0.7:43103"))
	if response.Code != http.StatusNotFound {
		t.Fatalf("post-revoke status=%d", response.Code)
	}
	close(release)
	<-done
	if err := published.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := published.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestRouterRejectsAuthorityDriftAndPrivateSerialization(t *testing.T) {
	fixture := newRouterFixture(t)
	binding := codingcertifier.CapabilityBinding{
		HarnessInstanceID:   fixture.binding.HarnessInstanceID,
		AgentArtifactSHA256: strings.Repeat("b", 64),
		TicketID:            fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
	}
	if _, err := fixture.router.WorkspacePublisher().Publish(t.Context(), binding, http.NotFoundHandler()); !errors.Is(err, ErrRoute) {
		t.Fatalf("drift err=%v", err)
	}
	for name, value := range map[string]any{
		"router":    fixture.router,
		"workspace": fixture.router.WorkspacePublisher(),
		"inference": fixture.router.InferencePublisher(),
	} {
		if _, err := json.Marshal(value); !errors.Is(err, ErrRouterConfig) {
			t.Fatalf("%s marshal err=%v", name, err)
		}
	}
}
