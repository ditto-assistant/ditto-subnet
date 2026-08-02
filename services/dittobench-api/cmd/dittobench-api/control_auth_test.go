package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const testControlToken = "test-control-token-not-the-compose-default"

func testControlAuth(mode controlAuthMode, token string) *controlAuth {
	return &controlAuth{mode: mode, token: token}
}

// probeMux stands in for the real control plane in the credential tests. It
// registers the allowlisted liveness route plus one protected route, and
// records whether the request reached the handler — which is what distinguishes
// shadow mode (rejected in the log, served anyway) from enforcement.
func probeMux(reached *bool) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		*reached = true
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /v1/dataset", func(w http.ResponseWriter, _ *http.Request) {
		*reached = true
		writeJSON(w, http.StatusOK, map[string]string{"dataset": "labeled"})
	})
	return mux
}

func doRequest(t *testing.T, auth *controlAuth, method, target, authorization string) (*httptest.ResponseRecorder, bool) {
	t.Helper()
	reached := false
	handler := auth.wrap(probeMux(&reached))
	req := httptest.NewRequest(method, target, nil)
	if authorization != "" {
		req.Header.Set("Authorization", authorization)
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec, reached
}

func TestControlAuthEnforceAcceptsValidCredential(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	rec, reached := doRequest(t, auth, http.MethodGet, "/v1/dataset", "Bearer "+testControlToken)
	if rec.Code != http.StatusOK {
		t.Fatalf("valid credential: got status %d, want %d", rec.Code, http.StatusOK)
	}
	if !reached {
		t.Fatal("valid credential: request did not reach the handler")
	}
}

// Absent, malformed, and unrecognized credentials must all be rejected, and the
// request must not reach the handler. "Expired" has no representation in a
// static bearer credential — it only becomes testable once the timestamped,
// validator-signed credential lands at the verifyControlCredential seam — so
// the stale case here is a credential that no longer matches the configured
// token, which is the rotation path an operator actually exercises.
func TestControlAuthEnforceRejectsBadCredentials(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	cases := []struct {
		name          string
		authorization string
	}{
		{"absent", ""},
		{"malformed: no scheme", testControlToken},
		{"malformed: wrong scheme", "Basic " + testControlToken},
		{"malformed: empty bearer value", "Bearer "},
		{"unrecognized", "Bearer some-other-token"},
		{"rotated away", "Bearer " + testControlToken + "-old"},
		{"prefix of the real token", "Bearer " + testControlToken[:10]},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec, reached := doRequest(t, auth, http.MethodGet, "/v1/dataset", tc.authorization)
			if rec.Code != http.StatusUnauthorized {
				t.Errorf("got status %d, want %d", rec.Code, http.StatusUnauthorized)
			}
			if reached {
				t.Error("request reached the handler despite a bad credential")
			}
			if body := rec.Body.String(); strings.Contains(body, testControlToken) {
				t.Errorf("response body leaked the configured token: %q", body)
			}
		})
	}
}

// A server with no credential configured must reject rather than admit
// everyone. newControlAuth cannot reach this state under enforcement because
// logStartup refuses to boot, but the check itself still fails closed.
func TestControlAuthEnforceWithNoTokenRejects(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, "")
	rec, reached := doRequest(t, auth, http.MethodGet, "/v1/dataset", "Bearer "+testControlToken)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unconfigured server: got status %d, want %d", rec.Code, http.StatusUnauthorized)
	}
	if reached {
		t.Fatal("unconfigured server admitted a request")
	}
}

// The published Compose default is not a secret and must not satisfy the check.
func TestInsecureDefaultTokenIsTreatedAsUnconfigured(t *testing.T) {
	t.Setenv("DITTOBENCH_CONTROL_TOKEN", "")
	t.Setenv("DITTOBENCH_BROKER_CONTROL_TOKEN", insecureDefaultControlToken)
	if token := controlTokenFromEnv(); token != "" {
		t.Fatalf("published default accepted as a credential: %q", token)
	}
}

func TestControlTokenPrefersDedicatedEnvVar(t *testing.T) {
	t.Setenv("DITTOBENCH_CONTROL_TOKEN", "dedicated")
	t.Setenv("DITTOBENCH_BROKER_CONTROL_TOKEN", "broker")
	if token := controlTokenFromEnv(); token != "dedicated" {
		t.Fatalf("got %q, want the dedicated control token", token)
	}
}

// Stage 1 must not change behavior: a request that enforcement would reject is
// logged and served.
func TestControlAuthShadowServesRejectedRequests(t *testing.T) {
	auth := testControlAuth(controlAuthShadow, testControlToken)
	rec, reached := doRequest(t, auth, http.MethodGet, "/v1/dataset", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("shadow mode: got status %d, want %d", rec.Code, http.StatusOK)
	}
	if !reached {
		t.Fatal("shadow mode blocked a request; it must only log")
	}
}

func TestControlAuthModeFromEnv(t *testing.T) {
	cases := map[string]controlAuthMode{
		"":         controlAuthShadow,
		"shadow":   controlAuthShadow,
		"enforce":  controlAuthEnforce,
		"ENFORCE":  controlAuthEnforce,
		" enforce": controlAuthEnforce,
		"typo":     controlAuthShadow,
		"off":      controlAuthShadow,
	}
	for value, want := range cases {
		if got := controlAuthModeFromEnv(value); got != want {
			t.Errorf("controlAuthModeFromEnv(%q) = %q, want %q", value, got, want)
		}
	}
}

// The allowlisted liveness route stays reachable with no credential, which is
// what keeps the container healthcheck working under enforcement.
func TestHealthIsPublicUnderEnforcement(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	rec, reached := doRequest(t, auth, http.MethodGet, "/health", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("health: got status %d, want %d", rec.Code, http.StatusOK)
	}
	if !reached {
		t.Fatal("health did not reach the handler without a credential")
	}
}

// handleHealth must stay a constant liveness bit. A sibling advisory covers a
// health endpoint that grew per-run counters; if that happens here, the route
// has to leave publicControlRoutes, and this test is the tripwire.
func TestHealthExposesOnlyLiveness(t *testing.T) {
	rec := httptest.NewRecorder()
	(&server{}).handleHealth(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	if got := strings.TrimSpace(rec.Body.String()); got != `{"status":"ok"}` {
		t.Fatalf("health body = %s; it must expose no counters or per-run state", got)
	}
}

// The regression test the whole design turns on: a route registered on the
// control plane and NOT named in publicControlRoutes must require a credential.
// Adding a route to newControlPlaneMux without classifying it fails here rather
// than silently joining the public surface.
func TestControlPlaneRoutesDefaultToProtected(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	mux := (&server{broker: newInferenceBroker(1, 1)}).newControlPlaneMux()
	handler := auth.wrap(mux)

	for _, pattern := range controlPlaneRoutes {
		method, path, ok := strings.Cut(pattern, " ")
		if !ok {
			t.Fatalf("route %q is not in \"METHOD /path\" form", pattern)
		}
		// Wildcards need a concrete value to match the pattern.
		path = strings.NewReplacer("{id}", "00000000-0000-0000-0000-000000000000").Replace(path)

		t.Run(pattern, func(t *testing.T) {
			req := httptest.NewRequest(method, path, nil)
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)

			if _, public := publicControlRoutes[pattern]; public {
				if rec.Code == http.StatusUnauthorized {
					t.Fatalf("allowlisted route was rejected (status %d)", rec.Code)
				}
				return
			}
			if rec.Code != http.StatusUnauthorized {
				t.Fatalf(
					"route is reachable without a credential (status %d); "+
						"either protect it or add it to publicControlRoutes with a "+
						"justification that it exposes no dataset, run, or transcript data",
					rec.Code,
				)
			}
		})
	}
}

// controlPlaneRoutes must stay in sync with what newControlPlaneMux registers,
// otherwise the coverage above silently stops covering a route. mux.Handler
// reports the pattern each probe actually matched, so a route missing from the
// list shows up as a pattern no probe produced.
func TestControlPlaneRouteListMatchesMux(t *testing.T) {
	mux := (&server{broker: newInferenceBroker(1, 1)}).newControlPlaneMux()
	for _, pattern := range controlPlaneRoutes {
		method, path, _ := strings.Cut(pattern, " ")
		path = strings.NewReplacer("{id}", "00000000-0000-0000-0000-000000000000").Replace(path)
		_, matched := mux.Handler(httptest.NewRequest(method, path, nil))
		if matched != pattern {
			t.Errorf("probe for %q matched pattern %q; the route list is stale", pattern, matched)
		}
	}
}

// An unregistered path must also require a credential, so probing the plane
// cannot enumerate which routes exist.
func TestUnregisteredPathIsProtected(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	rec, _ := doRequest(t, auth, http.MethodGet, "/v1/does-not-exist", "")
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("unregistered path: got status %d, want %d", rec.Code, http.StatusUnauthorized)
	}
}

// Authorization is the only thing that admits a caller. Loopback, a spoofed
// X-Forwarded-For, and a private source address must not.
func TestNetworkPositionDoesNotAuthorize(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	cases := []struct {
		name       string
		remoteAddr string
		forwarded  string
	}{
		{"loopback", "127.0.0.1:54321", ""},
		{"ipv6 loopback", "[::1]:54321", ""},
		{"private network", "10.0.0.5:54321", ""},
		{"spoofed forwarded-for", "10.0.0.5:54321", "127.0.0.1"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reached := false
			handler := auth.wrap(probeMux(&reached))
			req := httptest.NewRequest(http.MethodGet, "/v1/dataset", nil)
			req.RemoteAddr = tc.remoteAddr
			if tc.forwarded != "" {
				req.Header.Set("X-Forwarded-For", tc.forwarded)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)

			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("network position authorized the caller (status %d)", rec.Code)
			}
			if reached {
				t.Fatal("request reached the handler on network position alone")
			}
		})
	}
}

// Rejections must not be cached by any intermediary, and must advertise the
// scheme so a caller can tell what it failed to present.
func TestRejectionHeaders(t *testing.T) {
	auth := testControlAuth(controlAuthEnforce, testControlToken)
	rec, _ := doRequest(t, auth, http.MethodGet, "/v1/dataset", "")
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store", got)
	}
	if got := rec.Header().Get("WWW-Authenticate"); got != "Bearer" {
		t.Errorf("WWW-Authenticate = %q, want Bearer", got)
	}
}
