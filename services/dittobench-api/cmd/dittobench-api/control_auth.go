package main

import (
	"crypto/subtle"
	"log"
	"net/http"
	"os"
	"strings"
)

// The control plane on :8000 serves validator-internal data. Until this file
// existed the only middleware on that router was request logging, so the sole
// control keeping a benchmarked container away from it was the sandbox egress
// allowlist in ditto-subnet's entrypoint — a network control with no
// application-layer backstop. This adds the backstop. The iptables allowlist
// stays exactly as it is; it is now defense in depth rather than the control.

// newControlPlaneMux registers the operator/validator control-plane routes.
//
// It is a method rather than inline setup in main so the auth tests can assert
// against the real route table: controlPlaneRoutesDefaultToProtected walks
// every pattern registered here and fails if one is reachable without a
// credential and is not in publicControlRoutes. Adding a route below therefore
// either lands protected or trips that test.
func (s *server) newControlPlaneMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /v1/relay-preflight", s.handleRelayPreflight)
	mux.HandleFunc("GET /v1/capabilities", s.handleCapabilities)
	mux.HandleFunc("GET /v1/dataset", s.handleDataset)
	mux.HandleFunc("GET /v1/sample", s.handleSample)
	mux.HandleFunc("GET /v1/catalog", s.handleCatalog)
	mux.HandleFunc("POST /v1/submit", s.handleSubmit)
	mux.HandleFunc("POST /v2/score", s.handleVersionedScore)
	mux.HandleFunc("GET /v1/runs/{id}", s.handleGetRun)
	mux.HandleFunc("GET /v1/runs/{id}/transcript", s.handleGetTranscript)
	mux.HandleFunc("DELETE /v1/runs/{id}", s.handleCancelRun)
	mux.HandleFunc("POST /v1/inference/session", s.broker.prepare)
	mux.HandleFunc("POST /v1/inference/session/{id}/activate", s.broker.activate)
	mux.HandleFunc("POST /v1/inference/session/{id}/activate-confirmation", s.broker.activateConfirmation)
	mux.HandleFunc("DELETE /v1/inference/session/{id}", s.broker.cancel)
	mux.HandleFunc("GET /v1/confirmation/readiness", s.handleConfirmationReadiness)
	mux.HandleFunc("GET /v1/confirmation/progress", s.handleConfirmationProgress)
	mux.HandleFunc("POST /v1/confirmation/execute", s.handleConfirmationExecute)
	mux.HandleFunc("POST /v1/coding/supervisor/{operation}", s.handleCodingSupervisor)
	mux.HandleFunc("POST /v1/coding/publications/{operation}", s.handleCodingPublication)
	mux.HandleFunc("POST /v1/coding/evidence/open", s.handleCodingEvidence)
	mux.HandleFunc("POST /v1/coding/evidence/manifest", s.handleCodingEvidence)
	mux.HandleFunc("POST /v1/coding/certifier/canary", s.handleCodingCanary)
	return mux
}

// controlPlaneRoutes lists every pattern newControlPlaneMux registers, in the
// same order. The test suite pins these two in sync, so this is a faithful
// enumeration rather than a comment that can rot.
var controlPlaneRoutes = []string{
	"GET /health",
	"GET /v1/relay-preflight",
	"GET /v1/capabilities",
	"GET /v1/dataset",
	"GET /v1/sample",
	"GET /v1/catalog",
	"POST /v1/submit",
	"POST /v2/score",
	"GET /v1/runs/{id}",
	"GET /v1/runs/{id}/transcript",
	"DELETE /v1/runs/{id}",
	"POST /v1/inference/session",
	"POST /v1/inference/session/{id}/activate",
	"POST /v1/inference/session/{id}/activate-confirmation",
	"DELETE /v1/inference/session/{id}",
	"GET /v1/confirmation/readiness",
	"GET /v1/confirmation/progress",
	"POST /v1/confirmation/execute",
	"POST /v1/coding/supervisor/{operation}",
	"POST /v1/coding/publications/{operation}",
	"POST /v1/coding/evidence/open",
	"POST /v1/coding/evidence/manifest",
	"POST /v1/coding/certifier/canary",
}

func (s *server) handleCodingSupervisor(response http.ResponseWriter, request *http.Request) {
	if s == nil || s.codingHost == nil {
		http.NotFound(response, request)
		return
	}
	s.codingHost.SupervisorHandler().ServeHTTP(response, request)
}

func (s *server) handleCodingPublication(response http.ResponseWriter, request *http.Request) {
	if s == nil || s.codingHost == nil {
		http.NotFound(response, request)
		return
	}
	s.codingHost.PublicationHandler().ServeHTTP(response, request)
}

func (s *server) handleCodingEvidence(response http.ResponseWriter, request *http.Request) {
	if s == nil || s.codingHost == nil {
		http.NotFound(response, request)
		return
	}
	s.codingHost.EvidenceHandler().ServeHTTP(response, request)
}

func (s *server) handleCodingCanary(response http.ResponseWriter, request *http.Request) {
	if s == nil || s.codingHost == nil {
		http.NotFound(response, request)
		return
	}
	s.codingHost.CanaryHandler().ServeHTTP(response, request)
}

// controlAuthMode selects what the control plane does with a request that fails
// the credential check.
type controlAuthMode string

const (
	// controlAuthShadow evaluates the credential, logs the verdict, and serves
	// the request anyway. This is stage 1 of the rollout and the default: it
	// lets an operator confirm from the logs that their validator presents a
	// good credential on every call path *before* rejection turns on. Shipping
	// enforcement as the default would be a flag day, and the validator fleet
	// does not yet send a credential on most of these routes.
	controlAuthShadow controlAuthMode = "shadow"
	// controlAuthEnforce rejects every request that fails the check. Stage 2.
	controlAuthEnforce controlAuthMode = "enforce"
)

// publicControlRoutes is an ALLOWLIST of the control-plane routes served
// without a credential.
//
// It is deliberately an allowlist and not a denylist of protected routes: a
// route registered on the mux later is protected by default, so a new endpoint
// that nobody remembered to classify fails closed instead of silently joining
// the public surface. controlAuthDefaultsProtected in the tests pins that
// property so it cannot regress.
//
// Membership requires that a route expose no dataset content, no run detail, no
// transcript, and no counter that varies with what the validator is doing. A
// liveness bit and nothing more. GET /health qualifies: handleHealth writes a
// constant {"status":"ok"} and reads nothing off the server. (A sibling
// advisory covers a health endpoint that leaked per-run counters, which is why
// that is spelled out rather than assumed.)
var publicControlRoutes = map[string]struct{}{
	"GET /health": {},
}

// insecureDefaultControlToken is the placeholder ditto-subnet's Compose file
// substitutes when an operator has not set DITTOBENCH_BROKER_CONTROL_TOKEN. It
// is a literal in a public repository, so it is worth nothing as a secret.
// Treating it as "unconfigured" keeps enforcement from being theater: an
// operator who never set a token gets a startup failure telling them to set
// one, not a control plane that appears authenticated and is not.
const insecureDefaultControlToken = "ditto-sn118-stack-broker-control"

// controlAuth authorizes callers of the control plane.
//
// The credential is the bearer token the validator already presents on the
// inference-session routes (DITTOBENCH_BROKER_CONTROL_TOKEN on this side,
// VALIDATOR_DITTOBENCH_CONTROL_TOKEN on the validator, bound to one Compose
// anchor). Reusing it rather than minting a second scheme is what makes stage 2
// a configuration change instead of a protocol migration.
//
// Note what this does and does not assert. It proves the caller holds the
// per-stack secret. It does not prove *which* validator hotkey is calling, does
// not consult the chain for a validator permit, and carries no timestamp, so it
// has no expiry or replay window. The platform gets all four at its validator
// boundary by verifying an sr25519 signature over a canonical string; see
// verifyControlCredential for why that is not what runs here yet.
type controlAuth struct {
	mode  controlAuthMode
	token string
}

func newControlAuth() *controlAuth {
	return &controlAuth{
		mode:  controlAuthModeFromEnv(os.Getenv("DITTOBENCH_CONTROL_AUTH_MODE")),
		token: controlTokenFromEnv(),
	}
}

// controlAuthModeFromEnv defaults to shadow. An unrecognized value is also
// shadow rather than a startup failure: a typo in the mode should not take the
// scoring plane down, and shadow still surfaces the misconfiguration in logs.
func controlAuthModeFromEnv(value string) controlAuthMode {
	if strings.EqualFold(strings.TrimSpace(value), string(controlAuthEnforce)) {
		return controlAuthEnforce
	}
	return controlAuthShadow
}

// controlTokenFromEnv reads the control credential. DITTOBENCH_CONTROL_TOKEN
// wins when set so a deployment can separate the control-plane secret from the
// inference-broker one later; otherwise it falls back to the broker token that
// is already wired to the validator.
func controlTokenFromEnv() string {
	token := strings.TrimSpace(os.Getenv("DITTOBENCH_CONTROL_TOKEN"))
	if token == "" {
		token = strings.TrimSpace(os.Getenv("DITTOBENCH_BROKER_CONTROL_TOKEN"))
	}
	if token == insecureDefaultControlToken {
		return ""
	}
	return token
}

// controlAuthVerdict is the outcome of a credential check. reason is for the
// operator log only and is never written to the response, so a caller probing
// the plane cannot tell "wrong token" from "server has no token configured".
type controlAuthVerdict struct {
	ok     bool
	reason string
}

// verifyControlCredential checks the presented credential.
//
// Every failure path returns not-ok: absent, malformed, unrecognized, and
// unconfigured-server all reject. There is no branch that admits a caller
// because of where the request came from — no loopback exemption and no source
// IP check. That is deliberate. The v7 inference broker's own controlAuthorized
// still admits loopback, and source-IP trust is the pattern this change moves
// away from, so it is not carried onto the control plane.
//
// A timestamped, per-hotkey credential (sr25519 over a canonical string, chain
// permit lookup, nonce store — what the platform verifies at its own validator
// boundary) is the stronger end state and would add an expiry dimension this
// bearer does not have. It needs the validator to sign these calls, which it
// does not do today, so it is a follow-up rather than something shipped here as
// dead code. This function is the single seam where that verifier lands.
func (a *controlAuth) verifyControlCredential(r *http.Request) controlAuthVerdict {
	if a.token == "" {
		return controlAuthVerdict{reason: "server has no control credential configured"}
	}
	header := strings.TrimSpace(r.Header.Get("Authorization"))
	if header == "" {
		return controlAuthVerdict{reason: "absent credential"}
	}
	provided, ok := strings.CutPrefix(header, "Bearer ")
	if !ok {
		return controlAuthVerdict{reason: "malformed credential: expected a Bearer scheme"}
	}
	if provided = strings.TrimSpace(provided); provided == "" {
		return controlAuthVerdict{reason: "malformed credential: empty bearer value"}
	}
	if subtle.ConstantTimeCompare([]byte(provided), []byte(a.token)) != 1 {
		return controlAuthVerdict{reason: "unrecognized credential"}
	}
	return controlAuthVerdict{ok: true}
}

// routeIsPublic reports whether the request resolves to an allowlisted route.
//
// The pattern comes from the mux itself rather than from re-parsing the path,
// so the classification is keyed on the same string the route was registered
// with and cannot drift from it. An unmatched path yields an empty pattern and
// is treated as protected: a 404 is served through the credential check like
// anything else, so probing the plane does not enumerate which routes exist.
func routeIsPublic(mux *http.ServeMux, r *http.Request) bool {
	_, pattern := mux.Handler(r)
	if pattern == "" {
		return false
	}
	_, ok := publicControlRoutes[pattern]
	return ok
}

// wrap installs the credential check in front of mux.
func (a *controlAuth) wrap(mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if routeIsPublic(mux, r) {
			mux.ServeHTTP(w, r)
			return
		}
		verdict := a.verifyControlCredential(r)
		if verdict.ok {
			mux.ServeHTTP(w, r)
			return
		}
		if a.mode == controlAuthShadow {
			// Stage 1: report what enforcement would have done and serve the
			// request unchanged. An operator watching these lines go quiet
			// knows the fleet is ready for stage 2.
			log.Printf(
				"control-plane auth (shadow, would reject): %s %s from %s: %s",
				r.Method, r.URL.Path, clientIP(r), verdict.reason,
			)
			mux.ServeHTTP(w, r)
			return
		}
		log.Printf(
			"control-plane auth rejected: %s %s from %s: %s",
			r.Method, r.URL.Path, clientIP(r), verdict.reason,
		)
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("WWW-Authenticate", "Bearer")
		writeError(w, http.StatusUnauthorized, "control plane requires a valid validator credential")
	})
}

// logStartup records the posture once at boot, and refuses to start in the one
// configuration that would silently brick the plane: enforcing with no
// credential to enforce against, which would 401 every validator call. Failing
// to start is the closed outcome and is unreachable by default, since the
// default mode is shadow.
func (a *controlAuth) logStartup() {
	if a.mode == controlAuthEnforce && a.token == "" {
		log.Fatalf(
			"DITTOBENCH_CONTROL_AUTH_MODE=enforce requires a control credential: " +
				"set DITTOBENCH_BROKER_CONTROL_TOKEN (or DITTOBENCH_CONTROL_TOKEN) " +
				"to a per-host secret, not the published Compose default",
		)
	}
	if a.token == "" {
		log.Printf(
			"control-plane auth: mode=shadow, NO CREDENTIAL CONFIGURED — every " +
				"protected route would be rejected under enforcement. Set " +
				"DITTOBENCH_BROKER_CONTROL_TOKEN to a per-host secret.",
		)
		return
	}
	log.Printf("control-plane auth: mode=%s, credential configured", a.mode)
}
