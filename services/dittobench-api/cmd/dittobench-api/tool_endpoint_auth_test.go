package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

func serveTestBroker(t *testing.T, broker *inferenceBroker) (int, func()) {
	return serveTestBrokerOn(t, broker, "127.0.0.1:0")
}

func serveTestBrokerOn(t *testing.T, broker *inferenceBroker, address string) (int, func()) {
	t.Helper()
	listener, err := net.Listen("tcp4", address)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/tools/{id}/tool", broker.handleTool)
	mux.HandleFunc("POST /v1/tools/{id}/tool", broker.handleTool)
	server := &http.Server{Handler: mux}
	go func() { _ = server.Serve(listener) }()
	return listener.Addr().(*net.TCPAddr).Port, func() {
		_ = server.Close()
	}
}

func localToolURL(t *testing.T, endpoint string) string {
	t.Helper()
	parsed, err := url.Parse(endpoint)
	if err != nil {
		t.Fatal(err)
	}
	parsed.Host = "127.0.0.1:" + parsed.Port()
	return parsed.String()
}

func postToolRequest(t *testing.T, endpoint, caseID, userID string) (int, protocol.ToolExecResponse) {
	t.Helper()
	body, err := json.Marshal(protocol.ToolExecRequest{
		CaseID: caseID,
		UserID: userID,
		Name:   "search_web",
		Args:   json.RawMessage(`{"query":"veltrix"}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.Post(endpoint, "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var result protocol.ToolExecResponse
	if raw, readErr := io.ReadAll(response.Body); readErr != nil {
		t.Fatal(readErr)
	} else if response.StatusCode == http.StatusOK {
		if err := json.Unmarshal(raw, &result); err != nil {
			t.Fatalf("decode successful tool response: %v; body=%s", err, raw)
		}
	}
	return response.StatusCode, result
}

func TestLocalSandboxToolEndpointSurvivesDockerDesktopNAT(t *testing.T) {
	broker := newInferenceBroker(1)
	port, stopBroker := serveTestBroker(t, broker)
	defer stopBroker()
	t.Setenv("DITTOBENCH_BROKER_PORT", strconv.Itoa(port))

	toolServer := toolexec.NewServer()
	toolCase := protocol.ToolCase{
		ID:            "wire-case-a",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
	toolServer.Register(toolCase.ID, toolexec.BuildFixture(42, toolCase))
	server := &server{broker: broker, allowPrivate: true}
	endpoint, stopTool, err := server.startToolServer(toolServer, "172.30.0.2", protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	defer stopTool()

	// The real request arrives from this test process as loopback, modeling the
	// outer Docker Desktop VM/gateway address rather than docker inspect's inner
	// 172.30.0.2 identity. The case capability is the local fallback authority.
	caseEndpoint := localToolURL(t, endpoint.forCase(toolCase.ID, "wire-user-a"))
	status, result := postToolRequest(t, caseEndpoint, toolCase.ID, "wire-user-a")
	if status != http.StatusOK || result.Result == "" || result.Error != "" {
		t.Fatalf("NAT-routed tool status=%d result=%+v", status, result)
	}
	observed := toolServer.Observed(toolCase.ID)
	if len(observed) != 1 || observed[0].Name != "search_web" {
		t.Fatalf("observed=%+v", observed)
	}
}

func TestV8SandboxToolEndpointPreservesFrozenBareRoute(t *testing.T) {
	broker := newInferenceBroker(1)
	port, stopBroker := serveTestBroker(t, broker)
	defer stopBroker()
	t.Setenv("DITTOBENCH_BROKER_PORT", strconv.Itoa(port))

	toolServer := toolexec.NewServer()
	toolCase := protocol.ToolCase{
		ID:            "v8-wire-case",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
	toolServer.Register(toolCase.ID, toolexec.BuildFixture(8, toolCase))
	server := &server{broker: broker, allowPrivate: true}
	endpoint, stopTool, err := server.startToolServer(
		toolServer,
		"127.0.0.1",
		protocol.BenchVersionV8,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stopTool()

	caseEndpoint := endpoint.forCase(toolCase.ID, "new-v9-user-field")
	if caseEndpoint != endpoint.baseURL {
		t.Fatalf("v8 endpoint changed from frozen bare route: got %q want %q", caseEndpoint, endpoint.baseURL)
	}
	if other := endpoint.forCase("other-case", "other-user"); other != caseEndpoint {
		t.Fatalf("v8 endpoint became case-specific: first=%q other=%q", caseEndpoint, other)
	}
	parsed, err := url.Parse(caseEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.RawQuery != "" {
		t.Fatalf("v8 endpoint unexpectedly requires query capability: %q", parsed.RawQuery)
	}

	// A frozen v8 harness may populate its own user identity rather than echo a
	// new scorer-side value. It remains authorized by the inspected container
	// source and reaches the old handler without case/user coupling.
	status, result := postToolRequest(t, localToolURL(t, caseEndpoint), toolCase.ID, "legacy-default-user")
	if status != http.StatusOK || result.Result == "" || result.Error != "" {
		t.Fatalf("frozen v8 tool status=%d result=%+v", status, result)
	}
	if observed := toolServer.Observed(toolCase.ID); len(observed) != 1 || observed[0].Name != "search_web" {
		t.Fatalf("frozen v8 observation=%+v", observed)
	}
}

func TestDockerDesktopNATToolObservationSmoke(t *testing.T) {
	if os.Getenv("DITTOBENCH_DOCKER_NAT_TEST") != "1" {
		t.Skip("set DITTOBENCH_DOCKER_NAT_TEST=1 for the Docker Desktop smoke")
	}
	broker := newInferenceBroker(1)
	port, stopBroker := serveTestBrokerOn(t, broker, "0.0.0.0:0")
	defer stopBroker()
	t.Setenv("DITTOBENCH_BROKER_PORT", strconv.Itoa(port))

	toolServer := toolexec.NewServer()
	toolCase := protocol.ToolCase{
		ID:            "docker-nat-case",
		Category:      "web_search",
		ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}},
		MaxToolCalls:  1,
	}
	toolServer.Register(toolCase.ID, toolexec.BuildFixture(42, toolCase))
	var observedRemoteAddr string
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		observedRemoteAddr = r.RemoteAddr
		toolServer.ServeHTTP(w, r)
	})
	server := &server{broker: broker, allowPrivate: true}
	const inspectedContainerIP = "172.30.0.2"
	endpoint, stopTool, err := server.startToolServer(handler, inspectedContainerIP, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	defer stopTool()

	caseEndpoint := endpoint.forCase(toolCase.ID, "docker-user")
	body, err := json.Marshal(protocol.ToolExecRequest{
		CaseID: toolCase.ID, UserID: "docker-user", Name: "search_web",
		Args: json.RawMessage(`{"query":"veltrix"}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	dockerPost := func(target string) string {
		t.Helper()
		output, runErr := exec.Command(
			"docker", "run", "--rm", "--pull=never", "--network=bridge",
			"curlimages/curl:8.16.0", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
			"-H", "Content-Type: application/json", "--data-binary", string(body), target,
		).Output()
		if runErr != nil {
			t.Fatalf("Docker Desktop tool POST failed: %v", runErr)
		}
		return string(output)
	}

	parsed, err := url.Parse(caseEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	query := parsed.Query()
	query.Set("cap", "cross-container-spoof")
	parsed.RawQuery = query.Encode()
	if status := dockerPost(parsed.String()); status != strconv.Itoa(http.StatusUnauthorized) {
		t.Fatalf("spoof status=%s", status)
	}
	if got := toolServer.Observed(toolCase.ID); got != nil {
		t.Fatalf("spoof was observed: %+v", got)
	}

	if status := dockerPost(caseEndpoint); status != strconv.Itoa(http.StatusOK) {
		t.Fatalf("authorized NAT status=%s", status)
	}
	if sourceIP(observedRemoteAddr) == inspectedContainerIP {
		t.Fatalf("smoke did not traverse Docker Desktop NAT: remote=%q", observedRemoteAddr)
	}
	if got := toolServer.Observed(toolCase.ID); len(got) != 1 || got[0].Name != "search_web" {
		t.Fatalf("authorized NAT observation=%+v", got)
	}
}

func TestProductionToolEndpointDoesNotTreatCapabilityAsSourceBypass(t *testing.T) {
	broker := newInferenceBroker(1)
	port, stopBroker := serveTestBroker(t, broker)
	defer stopBroker()
	t.Setenv("DITTOBENCH_BROKER_PORT", strconv.Itoa(port))

	toolServer := toolexec.NewServer()
	toolCase := protocol.ToolCase{ID: "case-a", ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}}
	toolServer.Register(toolCase.ID, toolexec.BuildFixture(1, toolCase))
	server := &server{broker: broker, allowPrivate: false}
	endpoint, stopTool, err := server.startToolServer(toolServer, "172.30.0.2", protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	defer stopTool()

	status, _ := postToolRequest(t, localToolURL(t, endpoint.forCase(toolCase.ID, "user-a")), toolCase.ID, "user-a")
	if status != http.StatusUnauthorized {
		t.Fatalf("production NAT mismatch status=%d", status)
	}
	if got := toolServer.Observed(toolCase.ID); got != nil {
		t.Fatalf("rejected source became observed: %+v", got)
	}
}

func TestCaseCapabilityCannotAttributeCallToSiblingCase(t *testing.T) {
	broker := newInferenceBroker(1)
	port, stopBroker := serveTestBroker(t, broker)
	defer stopBroker()
	t.Setenv("DITTOBENCH_BROKER_PORT", strconv.Itoa(port))

	toolServer := toolexec.NewServer()
	for _, caseID := range []string{"case-a", "case-b"} {
		toolCase := protocol.ToolCase{ID: caseID, ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}}
		toolServer.Register(caseID, toolexec.BuildFixture(1, toolCase))
	}
	server := &server{broker: broker, allowPrivate: true}
	endpoint, stopTool, err := server.startToolServer(toolServer, "172.30.0.2", protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	defer stopTool()

	caseAEndpoint := localToolURL(t, endpoint.forCase("case-a", "user-a"))
	status, _ := postToolRequest(t, caseAEndpoint, "case-b", "user-a")
	if status != http.StatusUnauthorized {
		t.Fatalf("cross-case status=%d", status)
	}
	if gotA, gotB := toolServer.Observed("case-a"), toolServer.Observed("case-b"); gotA != nil || gotB != nil {
		t.Fatalf("cross-case request was attributed: a=%+v b=%+v", gotA, gotB)
	}
}

func TestToolEndpointHealthAndNoCallLeaveTranscriptEmpty(t *testing.T) {
	broker := newInferenceBroker(1)
	port, stopBroker := serveTestBroker(t, broker)
	defer stopBroker()
	t.Setenv("DITTOBENCH_BROKER_PORT", strconv.Itoa(port))

	toolServer := toolexec.NewServer()
	toolCase := protocol.ToolCase{ID: "cc9-no-call", ExpectedTools: []protocol.ToolSpec{{Name: "search_web"}}}
	toolServer.Register(toolCase.ID, toolexec.BuildFixture(9, toolCase))
	server := &server{broker: broker, allowPrivate: true}
	endpoint, stopTool, err := server.startToolServer(toolServer, "172.30.0.2", protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	defer stopTool()

	if !endpoint.available() || !strings.Contains(endpoint.forCase(toolCase.ID, "user-a"), "cap=") {
		t.Fatalf("missing advertised case capability at %q", endpoint.baseURL)
	}
	if got := toolServer.Observed(toolCase.ID); got != nil {
		t.Fatalf("authenticated health probe invented cc9 tool calls: %+v", got)
	}
}

func TestDirectPracticeEndpointRemainsCaseAgnostic(t *testing.T) {
	endpoint := observedToolEndpoint{baseURL: "http://127.0.0.1:1234/tool"}
	if got := endpoint.forCase("opaque/case?", "opaque/user?"); got != endpoint.baseURL {
		t.Fatalf("direct endpoint changed: %q", got)
	}
	if !endpoint.available() || (observedToolEndpoint{}).available() {
		t.Fatal("endpoint availability mismatch")
	}
}

// A v10+ sandboxed tool route must be bound to the run's inference session so
// the broker can enforce session-scoped model-emission provenance; without a
// session the run fails before any case starts rather than serving unprovenanced
// tool credit. Pre-v10 routes keep their frozen, session-free wire.
func TestStartToolServerForSessionRequiresInferenceSessionForV10(t *testing.T) {
	broker := newInferenceBroker(1)
	server := &server{broker: broker, allowPrivate: true}
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	if _, stop, err := server.startToolServerForSession(handler, "172.30.0.2", protocol.BenchVersionV10, "", 4); err == nil {
		stop()
		t.Fatal("v10 sandboxed tool route without an inference session must fail")
	}
	if _, stop, err := server.startToolServerForSession(handler, "172.30.0.2", protocol.BenchVersionV12, "", 4); err == nil {
		stop()
		t.Fatal("v12 sandboxed tool route without an inference session must fail")
	}
	// Route registration itself (the health probe is exercised elsewhere) binds
	// the provenance session for v10 and leaves it empty for v9.
	route, stop, err := broker.registerToolRoute(handler, "172.30.0.2", false, true, "session-a", 4)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	broker.mu.RLock()
	bound := broker.tools[route.id].provenanceSessionID
	broker.mu.RUnlock()
	if bound != "session-a" {
		t.Fatalf("provenance session bound=%q want session-a", bound)
	}
}
