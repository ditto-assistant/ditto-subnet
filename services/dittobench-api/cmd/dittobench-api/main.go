// Command dittobench-api is the off-chain DittoBench *practice* validator for
// Bittensor SN118. It mirrors the on-chain run+score loop minus TAO/chain:
// miners pull a fresh, randomized small dataset, run their harness against it,
// and get a DittoBench score — without overfitting risk (the seed rotates on
// every request).
package main

import (
	"context"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/netguard"
	"github.com/ditto-assistant/dittobench-api/internal/pprofserver"
	"github.com/ditto-assistant/dittobench-api/internal/ratelimit"
	"github.com/ditto-assistant/dittobench-api/internal/release"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

const defaultN = 30

// sandboxHealthTimeout bounds how long we wait for a freshly built container to
// answer /health before giving up on the submission.
const sandboxHealthTimeout = 3 * time.Minute

// Abuse guards for the public submit endpoint.
const (
	maxSubmitBody    = 64 << 10        // request body cap (submissions are tiny)
	submitsPerWindow = 15              // per-IP submissions...
	submitWindow     = 5 * time.Minute // ...per this window
)

// maxConcurrentRuns is the global cap on in-flight run_size jobs.
var (
	// Defaults to 1: one full miner sandbox has a 3 GiB cgroup cap and 512 MiB
	// writable tmpfs within it, and the validator host also runs Ollama, the
	// relay, Docker, Pylon, and the worker, so concurrent in-process full runs
	// overcommit the documented 16 GiB host (#31). Raise it only on a host with
	// headroom to spare.
	maxConcurrentRuns = envIntDefault("DITTOBENCH_MAX_CONCURRENT_RUNS", 1)
	// LongMemEval is a separate, slower execution lane. Its semaphore never
	// consumes ordinary run slots and defaults to half the ordinary pool.
	maxConcurrentConfirmations = envIntDefault("DITTOBENCH_MAX_CONCURRENT_CONFIRMATIONS", max(1, (maxConcurrentRuns+1)/2))
	// Local embeddings are a separate finite resource from hosted chat
	// inference. Default to one memory phase so raising run capacity cannot turn
	// concurrent seed waves into deterministic Ollama timeouts; capable hosts may
	// raise this independently after qualification.
	maxConcurrentMemoryPhases = envIntDefault("DITTOBENCH_MAX_CONCURRENT_MEMORY_PHASES", 1)
	// How many v8 cases one run may execute concurrently.
	v8CaseConcurrency = envIntDefault("DITTOBENCH_V8_CASE_CONCURRENCY", 4)
	// How many embedding calls ONE v8 run may have in flight. Two per concurrent
	// case, so a case doing a retrieve burst is not blocked by its siblings, and
	// so a harness that parallelises its own /seed ingestion gets some benefit
	// even while cases are sequential.
	//
	// Deliberately below the platform's per-ticket ceiling (12). The binding
	// limit should be this local semaphore, not a network round trip that comes
	// back as a decline.
	v8EmbeddingSessionConcurrency = envIntDefault("DITTOBENCH_V8_EMBEDDING_CONCURRENCY", 8)
)

// envIntDefault reads a positive int from key, returning def when unset or invalid.
func envIntDefault(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func envFloatDefault(key string, def float64) float64 {
	if value := os.Getenv(key); value != "" {
		if parsed, err := strconv.ParseFloat(value, 64); err == nil && parsed > 0 && !math.IsInf(parsed, 0) && !math.IsNaN(parsed) {
			return parsed
		}
	}
	return def
}

// runBounded runs fn for each index in [0,n) with at most `concurrency` calls in
// flight, and returns once all have finished. fn must be safe for concurrent use;
// callers write per-index results into a preallocated slice so output order is
// independent of completion order. Acquisition honors ctx cancellation so a
// timed-out or cancelled run stops scheduling new cases promptly.
func runBounded(ctx context.Context, n, concurrency int, fn func(i int)) {
	if concurrency < 1 {
		concurrency = 1
	}
	sem := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		select {
		case <-ctx.Done():
			wg.Wait()
			return
		case sem <- struct{}{}:
		}
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			defer func() { <-sem }()
			// A panic in one worker must not kill the whole process (the
			// pipeline's own recover only covers runSizeJob's goroutine, not
			// these). The case keeps its zero-value score, which grades as a
			// miss, matching how a failed /run call is treated.
			defer func() {
				if rec := recover(); rec != nil {
					log.Printf("bounded worker %d panicked: %v", i, rec)
				}
			}()
			fn(i)
		}(i)
	}
	wg.Wait()
}

func activeCaseConcurrency() int {
	return v8CaseConcurrency
}

type server struct {
	store   *store.Store
	sandbox sandbox.Sandbox
	// allowPrivate relaxes caller-supplied URL checks for local development.
	// Validator-owned loopback sandboxes use a separate trusted client.
	allowPrivate           bool
	allowScreenedImages    bool
	requireTicketInference bool
	softwareVersion        string
	sourceRevision         string
	// sourceRevisionOrigin records whether sourceRevision was derived from the
	// compiled binary or merely asserted by the environment, and
	// sourceRevisionMismatch records that the two disagreed. Both are reported
	// so a validator can tell a trustworthy deployment from a stale one.
	sourceRevisionOrigin   release.Origin
	sourceRevisionMismatch bool
	softwareVersionOrigin  release.Origin
	limiter                *ratelimit.Limiter
	runSlots               chan struct{} // bounds concurrent run_size jobs
	memorySlots            chan struct{} // bounds embedding-heavy memory phases
	broker                 *inferenceBroker
	// relayRunMu isolates scored runs that share this server's model relay.
	// The relay exposes process-wide monotonic failure counters, so overlapping
	// scored runs could otherwise attribute one run's provider failure to another.
	relayRunMu        sync.Mutex
	cancelMu          sync.Mutex
	runCancels        map[string]context.CancelFunc
	confirmationSlots chan struct{}
	// confirmation is nil until an exact server-owned v9 profile and every
	// trusted LongMem/ablation dependency have been installed. Nil is the
	// production-safe default and is independent of public v8 capabilities.
	confirmation confirmationExecutor
}

func main() {
	port := flag.Int("port", 8000, "HTTP listen port (ditto-subnet API convention)")
	printVersion := flag.Bool("version", false, "print this binary's release identity and exit")
	flag.Parse()

	// Release identity is answerable without a server, a Docker daemon, or a
	// network: `docker run <image> version` must tell an operator what a
	// container actually IS. The default ENTRYPOINT already carries `-port 8000`,
	// so the subcommand arrives as a trailing argument after flag parsing.
	identity := release.Resolve(os.Getenv)
	if *printVersion || versionCommandRequested(flag.Args()) {
		if err := writeVersion(os.Stdout, identity, versionJSONRequested(flag.Args())); err != nil {
			log.Fatalf("write version: %v", err)
		}
		return
	}

	if maxConcurrentRuns > 8 {
		log.Fatalf("DITTOBENCH_MAX_CONCURRENT_RUNS exceeds the supported safety bound of 8")
	}
	if maxConcurrentConfirmations > 4 {
		log.Fatalf("DITTOBENCH_MAX_CONCURRENT_CONFIRMATIONS exceeds the supported safety bound of 4")
	}
	if maxConcurrentMemoryPhases > maxConcurrentRuns {
		maxConcurrentMemoryPhases = maxConcurrentRuns
	}

	// Cloud Run (and most PaaS) inject the listen port via $PORT; honor it over
	// the flag default so the same binary runs locally and hosted.
	if p := os.Getenv("PORT"); p != "" {
		if v, err := strconv.Atoi(p); err == nil {
			*port = v
		}
	}
	pprofserver.Start(context.Background(), "dittobench-api", *port)

	// SSRF guard is ON by default; opt out for local dev / sandbox containers.
	allowPrivate := envBool("DITTOBENCH_ALLOW_PRIVATE_HARNESS")
	allowScreenedImages := envBool("DITTOBENCH_ALLOW_SCREENED_IMAGES")
	requireTicketInference := envBool("DITTOBENCH_REQUIRE_TICKET_INFERENCE")
	logReleaseIdentity(identity)
	runner.Configure(allowPrivate)
	if allowPrivate {
		log.Printf("WARNING: DITTOBENCH_ALLOW_PRIVATE_HARNESS set — SSRF guard relaxed (local/dev only)")
	}
	if allowScreenedImages {
		log.Printf("DITTOBENCH_ALLOW_SCREENED_IMAGES set — trusted validator image path enabled")
	}
	sandboxRuntime := sandbox.NewLocalDocker()
	cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cleanupCancel()
	if err := sandboxRuntime.CleanupStale(cleanupCtx); err != nil {
		if allowScreenedImages {
			log.Fatalf("stale sandbox recovery failed: %v", err)
		}
		log.Printf("optional practice sandbox cleanup skipped: %v", err)
	}

	memorySlots := make(chan struct{}, maxConcurrentMemoryPhases)
	s := &server{
		store:                  store.New(),
		sandbox:                sandboxRuntime,
		allowPrivate:           allowPrivate,
		allowScreenedImages:    allowScreenedImages,
		requireTicketInference: requireTicketInference,
		softwareVersion:        identity.SoftwareVersion,
		sourceRevision:         identity.SourceRevision,
		sourceRevisionOrigin:   identity.SourceRevisionOrigin,
		sourceRevisionMismatch: identity.SourceRevisionMismatch,
		softwareVersionOrigin:  identity.SoftwareVersionOrigin,
		limiter:                ratelimit.New(submitsPerWindow, submitWindow),
		runSlots:               make(chan struct{}, maxConcurrentRuns),
		memorySlots:            memorySlots,
		broker:                 newInferenceBroker(maxConcurrentRuns, cap(memorySlots)),
		runCancels:             make(map[string]context.CancelFunc),
		confirmationSlots:      make(chan struct{}, maxConcurrentConfirmations),
	}
	s.broker.relayWait = s.store.SetRelayWaiting
	s.broker.terminalAgentFailure = s.failAgentInferenceRun
	confirmationRuntime, err := confirmationExecutorFromEnvironment(os.Getenv, sandboxRuntime, s.broker)
	if err != nil {
		log.Fatalf("v9 confirmation installation failed: %v", err)
	}
	s.confirmation = confirmationRuntime

	mux := s.newControlPlaneMux()

	// Untrusted harnesses reach a dedicated listener that exposes only the
	// ticket-bound inference route. Keeping this off the control-plane mux means
	// a harness cannot probe submit, cancel, run, or session-management APIs.
	brokerMux := http.NewServeMux()
	brokerMux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusNoContent)
	})
	brokerMux.HandleFunc("GET /v1/inference/{rest...}", s.broker.handle)
	brokerMux.HandleFunc("POST /v1/inference/{rest...}", s.broker.handle)
	brokerMux.HandleFunc("POST /api/embed", s.broker.handleEmbedding)
	brokerMux.HandleFunc("GET /v1/tools/{id}/tool", s.broker.handleTool)
	brokerMux.HandleFunc("POST /v1/tools/{id}/tool", s.broker.handleTool)
	brokerPort := envIntDefault("DITTOBENCH_BROKER_PORT", 11436)
	if brokerPort < 1024 || brokerPort > 65535 || brokerPort == *port {
		log.Fatalf("invalid DITTOBENCH_BROKER_PORT: must be an unprivileged port distinct from the API port")
	}
	go func() {
		brokerAddr := "0.0.0.0:" + strconv.Itoa(brokerPort)
		log.Printf("trusted inference broker listening on %s", brokerAddr)
		listener, err := listenInferenceBrokerTCP4(brokerAddr)
		if err != nil {
			log.Fatalf("inference broker listen error: %v", err)
		}
		if err := newInferenceBrokerHTTPServer("", brokerMux, s.broker).Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("inference broker error: %v", err)
		}
	}()
	openRouterShimCABundlePath := strings.TrimSpace(os.Getenv("DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH"))
	if openRouterShimCABundlePath != "" {
		openRouterShimPort := envIntDefault("DITTOBENCH_OPENROUTER_SHIM_PORT", 11437)
		if openRouterShimPort < 1024 || openRouterShimPort > 65535 ||
			openRouterShimPort == *port || openRouterShimPort == brokerPort {
			log.Fatalf("invalid DITTOBENCH_OPENROUTER_SHIM_PORT: must be an unprivileged port distinct from API and broker ports")
		}
		if err := startOpenRouterShim(s.broker, openRouterShimCABundlePath, openRouterShimPort); err != nil {
			log.Fatalf("OpenRouter compatibility shim unavailable: %v", err)
		}
		log.Printf("OpenRouter compatibility shim listening on :%d", openRouterShimPort)
	}

	addr := ":" + strconv.Itoa(*port)
	// Everything on this mux except the allowlisted liveness route requires a
	// validator credential. See control_auth.go for the route classification
	// and the shadow/enforce rollout.
	controlAuth := newControlAuth()
	controlAuth.logStartup()

	log.Printf("dittobench-api (off-chain practice validator) listening on %s", addr)
	if err := http.ListenAndServe(addr, logging(controlAuth.wrap(mux))); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

// ---- middleware & helpers ----

func logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.RequestURI(), time.Since(start).Round(time.Millisecond))
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("encode response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// ---- handlers ----

func (s *server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type capabilitiesResponse struct {
	SoftwareVersion        string                          `json:"software_version"`
	SourceRevision         string                          `json:"source_revision"`
	SupportedBenchVersions []int                           `json:"supported_bench_versions"`
	Features               []string                        `json:"features"`
	FullRunCapacity        int                             `json:"full_run_capacity"`
	MemoryPhaseCapacity    int                             `json:"memory_phase_capacity"`
	V8Readiness            efficiency.CalibrationReadiness `json:"v8_readiness"`
	// SourceRevisionOrigin is "binary" when source_revision was compiled into
	// the running binary and "env" when it was only asserted by the process
	// environment. Additive: an older scorer omits it, which a consumer should
	// read as unknown provenance.
	SourceRevisionOrigin release.Origin `json:"source_revision_origin,omitempty"`
	// SourceRevisionMismatch is true when the binary and the environment both
	// named a revision and disagreed — the signature of a container recreated
	// against a cached image. The binary-derived value is still reported; the
	// deployment should be treated as untrustworthy until the image is
	// refreshed.
	SourceRevisionMismatch bool `json:"source_revision_mismatch"`
	// SoftwareVersionOrigin mirrors SourceRevisionOrigin for software_version.
	SoftwareVersionOrigin release.Origin `json:"software_version_origin,omitempty"`
}

// supportedBenchVersions is the capability set this build can administer. It is
// shared with the version command so an operator can ask an unstarted container
// exactly what a validator would negotiate with it.
func supportedBenchVersions() []int {
	if !efficiency.ValidV8Readiness(efficiency.V8Readiness()) {
		return nil
	}
	versions := make([]int, 0, 4)
	for _, version := range []int{protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11} {
		if protocol.SupportedBenchVersion(version) && efficiency.ProductionReadyForVersion(version) {
			versions = append(versions, version)
		}
	}
	return versions
}

type v8IsolationReporter interface {
	V8IsolationReady(context.Context) error
}

// executesUntrustedImages reports whether this deployment can be asked to launch
// a miner container at all. Only two paths start one: a screener-built image,
// accepted solely under the narrow DITTOBENCH_ALLOW_SCREENED_IMAGES opt-in that
// marks a validator-owned sandbox, and a validator-side source build, which the
// v8 contract retires. A deployment with neither — the hosted practice endpoint
// — serves v8 only over harness_url, driving and scoring a harness the miner
// already runs themselves, so it has no isolation boundary to make ready.
//
// The source-build half is derived from validateBenchmarkImageContract rather
// than restated, so a future version that re-enables source builds re-arms the
// isolation gate here instead of silently leaving it exempt.
func (s *server) executesUntrustedImages() bool {
	if s.allowScreenedImages {
		return true
	}
	sourceBuild := submitRequest{BenchVersion: protocol.BenchVersionV8, TarballURL: "https://example.invalid/source.tgz"}
	return validateBenchmarkImageContract(sourceBuild) == ""
}

// runtimeSupportedBenchVersions intersects the immutable scorer contract with
// the live untrusted-execution boundary. A code-only `version` report can still
// prove that the image contains V8, while the validator-facing capability omits
// V8 until the configured executor is reachable and satisfies its selected
// isolation policy.
//
// The intersection applies only where that boundary exists. Gating every
// deployment on a reachable Docker daemon made the hosted practice endpoint
// advertise an empty version set while /v1/submit accepted v8 — the capability
// document contradicting the endpoint it describes.
func (s *server) runtimeSupportedBenchVersions(ctx context.Context) []int {
	versions := supportedBenchVersions()
	if s.sandbox == nil || !s.executesUntrustedImages() {
		return versions
	}
	reporter, ok := s.sandbox.(v8IsolationReporter)
	if ok && reporter.V8IsolationReady(ctx) == nil {
		return versions
	}
	return nil
}

// handleCapabilities reports public release metadata to a co-located validator.
// Identity is derived from the compiled binary (see internal/release), falling
// back to the deploy-time environment only for an image that embedded nothing,
// and the validator accepts a v3 claim only when both fields match the immutable
// release descriptor.
// A bearer token would not authenticate the scorer: the scorer itself would hold
// the token and could use it while lying. Keeping this read-only response
// secretless avoids an operator cutover without weakening the identity binding.
func (s *server) handleCapabilities(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if s.softwareVersion == "" || !canonicalSourceRevision(s.sourceRevision) {
		writeError(w, http.StatusServiceUnavailable, "scorer release identity unavailable")
		return
	}
	writeJSON(w, http.StatusOK, capabilitiesResponse{
		SoftwareVersion:        s.softwareVersion,
		SourceRevision:         s.sourceRevision,
		SupportedBenchVersions: s.runtimeSupportedBenchVersions(r.Context()),
		Features:               []string{"git_subdir"},
		FullRunCapacity:        maxConcurrentRuns,
		MemoryPhaseCapacity:    maxConcurrentMemoryPhases,
		V8Readiness:            efficiency.V8Readiness(),
		SourceRevisionOrigin:   s.sourceRevisionOrigin,
		SourceRevisionMismatch: s.sourceRevisionMismatch,
		SoftwareVersionOrigin:  s.softwareVersionOrigin,
	})
}

func canonicalSourceRevision(value string) bool {
	if len(value) != 40 || value != strings.ToLower(value) {
		return false
	}
	if value == strings.Repeat("0", 40) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func canonicalSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func verifyDatasetHash(expected, actual string, hashErr error) error {
	if hashErr != nil {
		return fmt.Errorf("dataset hashing failed for platform-issued dataset: %w", hashErr)
	}
	if expected != actual {
		return fmt.Errorf(
			"dataset_sha256 mismatch: platform issued %s but this validator regenerated %s (generator/bench_version drift)",
			expected,
			actual,
		)
	}
	return nil
}

func practiceBenchVersion(r *http.Request) (int, string) {
	value := strings.TrimSpace(r.URL.Query().Get("bench_version"))
	if value == "" {
		return protocol.BenchVersionV9, ""
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, "bench_version must be an integer"
	}
	return requestedPracticeBenchVersion(parsed)
}

func (s *server) handleCatalog(w http.ResponseWriter, r *http.Request) {
	benchVersion, msg := practiceBenchVersion(r)
	if msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	w.Header().Set("X-Bench-Version", strconv.Itoa(benchVersion))
	writeJSON(w, http.StatusOK, catalog.CatalogForVersion(benchVersion))
}

// handleDataset returns a fresh dataset for practice. Seed is random unless
// pinned via ?seed=, n defaults to 30 (clamped in datagen).
func (s *server) handleDataset(w http.ResponseWriter, r *http.Request) {
	benchVersion, msg := practiceBenchVersion(r)
	if msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	n := parseIntDefault(r.URL.Query().Get("n"), defaultN)
	seed := freshSeed()
	if sv := r.URL.Query().Get("seed"); sv != "" {
		if parsed, err := strconv.ParseInt(sv, 10, 64); err == nil {
			seed = parsed
		}
	}
	ds, err := datagen.GenerateForVersion(seed, n, benchVersion)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	w.Header().Set("X-Bench-Version", strconv.Itoa(benchVersion))
	// The oracle (expected tools/answers, category labels, grading params) is
	// validator-internal grading data. On the public hosted deployment we serve
	// only what the harness itself sees at scoring time (the prompt/question), so
	// this endpoint cannot be scraped into a prompt->answer training set. Trusted
	// local mode (DITTOBENCH_ALLOW_PRIVATE_HARNESS) returns the full labeled
	// dataset for calibration and self-hosted practice.
	if !s.allowPrivate {
		writeJSON(w, http.StatusOK, redactDataset(ds))
		return
	}
	writeJSON(w, http.StatusOK, ds)
}

// publicDataset is the answer-free view of a Dataset served on the public
// practice endpoint: only the harness-visible prompt/question, never the
// expected tools/answers, category label, or grading params.
type publicDataset struct {
	Seed        int64              `json:"seed"`
	GeneratedAt string             `json:"generated_at"`
	ToolCases   []publicToolCase   `json:"tool_cases"`
	MemoryCases []publicMemoryCase `json:"memory_cases,omitempty"`
}

type publicToolCase struct {
	ID     string `json:"id"`
	Prompt string `json:"prompt"`
}

type publicMemoryCase struct {
	ID       string `json:"id"`
	Question string `json:"question"`
}

// redactDataset strips validator-internal grading data, leaving only what the
// harness sees. See the "validator-internal" comments on ToolCase/MemoryCase in
// the dittobench-datagen protocol module.
func redactDataset(ds protocol.Dataset) publicDataset {
	pub := publicDataset{Seed: ds.Seed, GeneratedAt: ds.GeneratedAt}
	for _, c := range ds.ToolCases {
		pub.ToolCases = append(pub.ToolCases, publicToolCase{ID: c.ID, Prompt: c.Prompt})
	}
	for _, c := range ds.MemoryCases {
		pub.MemoryCases = append(pub.MemoryCases, publicMemoryCase{ID: c.ID, Question: c.Question})
	}
	return pub
}

// submitRequest carries an explicit executable benchmark contract and exactly
// one harness source. Public practice currently selects v9; canonical scoring
// remains explicit and continues to accept the supported v8 compatibility path.
type submitRequest struct {
	// BenchVersion selects the deterministic dataset/scoring contract. The
	// field is always explicit on scoring and practice requests.
	BenchVersion int    `json:"bench_version,omitempty"`
	HarnessURL   string `json:"harness_url,omitempty"`
	GitURL       string `json:"git_url,omitempty"`
	GitRef       string `json:"git_ref,omitempty"`
	// GitSubdir selects a repository-relative Docker context after cloning a
	// monorepo. It is valid only with GitURL and is resolved without allowing
	// absolute paths, parent traversal, or symlink escape.
	GitSubdir string `json:"git_subdir,omitempty"`
	// TarballURL is a presigned https URL of a gzipped tar of the harness. The
	// SN118 platform stores miner uploads as tarballs, so the validator hands us
	// the platform's short-lived download URL instead of a git repo. Mutually
	// exclusive with harness_url / git_url; built in the Docker sandbox like
	// git_url. (mode B — platform-tarball ingest.)
	TarballURL string `json:"tarball_url,omitempty"`
	// TarballSHA256 optionally pins the tarball's SHA-256 (hex). When present the
	// sandbox re-verifies the fetched bytes — the platform already checks it at
	// upload, so this is defense in depth against a swapped/corrupted blob.
	TarballSHA256 string `json:"tarball_sha256,omitempty"`
	// ScreenedImageURL is a short-lived URL for a `docker save` archive
	// produced by the trusted screener from this exact tarball. It is
	// accepted only together with TarballURL: the source is still materialized
	// for structural anti-copy fingerprinting, but the Rust crate is not rebuilt.
	ScreenedImageURL string `json:"screened_image_url,omitempty"`
	// ScreenedImageSHA256 pins the archive bytes. ScreenedImageID pins
	// the Docker image loaded from those bytes, preventing an archive/tag swap.
	ScreenedImageSHA256 string            `json:"screened_image_sha256,omitempty"`
	ScreenedImageID     string            `json:"screened_image_id,omitempty"`
	ScreenedImageRef    string            `json:"screened_image_ref,omitempty"`
	ScreenedImageSize   int64             `json:"screened_image_size_bytes,omitempty"`
	Env                 map[string]string `json:"env,omitempty"`
	N                   int               `json:"n"`
	// RunSize selects the full SN118 pipeline (build → generate fresh anti-cheat
	// dataset → seed haystack → run tool+memory cases → judge → score). One of
	// "small" | "medium" | "full". When set, this path takes precedence.
	RunSize string `json:"run_size,omitempty"`
	// Seed pins the dataset seed (0 = fresh crypto-random per submission).
	Seed int64 `json:"seed,omitempty"`
	// ExpectedDatasetSHA256 pins the exact dataset the caller intends to score.
	// When set, the run regenerates the dataset from Seed (generation is
	// deterministic) and FAILS if the regenerated dataset_sha256 does not match —
	// tamper-evidence for the canonical validator path (POST /v2/score): the
	// platform issues (seed, dataset_sha256) with the ticket, and this guarantees
	// the validator scored precisely that dataset. Empty on the practice path.
	ExpectedDatasetSHA256 string `json:"dataset_sha256,omitempty"`
	// InferenceSessionID selects a trusted, memory-only platform capability
	// prepared by the validator. It is an opaque broker routing id, not a bearer.
	InferenceSessionID string `json:"inference_session_id,omitempty"`
	// The v8 validator echoes the immutable platform ticket identity delivered
	// during trusted session activation. The broker compares these fields before
	// atomically assigning the session to this API-generated run id.
	InferenceGrantID        string    `json:"inference_grant_id,omitempty"`
	InferenceAgentID        string    `json:"inference_agent_id,omitempty"`
	InferenceSlotID         string    `json:"inference_slot_id,omitempty"`
	InferenceTicketDeadline time.Time `json:"inference_ticket_deadline,omitempty"`
	// BenchmarkRuntime is an additive v10 execution policy stamped onto the
	// Platform lease. Absence preserves the deployed serial/off behavior.
	BenchmarkRuntime *benchmarkRuntimeSettings `json:"benchmark_runtime,omitempty"`
}

type benchmarkRuntimeSettings struct {
	CaseConcurrency            int                  `json:"case_concurrency"`
	RelayDelayFingerprintMode  delayFingerprintMode `json:"relay_delay_fingerprint_mode"`
	RelayDelayFingerprintMinMS int                  `json:"relay_delay_fingerprint_min_ms"`
	RelayDelayFingerprintMaxMS int                  `json:"relay_delay_fingerprint_max_ms"`
}

func (r *benchmarkRuntimeSettings) validate(benchVersion int) error {
	if r == nil {
		return nil
	}
	if benchVersion < protocol.BenchVersionV10 {
		return fmt.Errorf("benchmark_runtime requires bench_version 10 or newer")
	}
	if r.CaseConcurrency < 1 || r.CaseConcurrency > 16 {
		return fmt.Errorf("benchmark_runtime.case_concurrency must be between 1 and 16")
	}
	if r.RelayDelayFingerprintMode != delayFingerprintOff &&
		r.RelayDelayFingerprintMode != delayFingerprintShadow {
		return fmt.Errorf("benchmark_runtime.relay_delay_fingerprint_mode must be off or shadow")
	}
	if r.RelayDelayFingerprintMinMS < 0 || r.RelayDelayFingerprintMaxMS < 0 ||
		r.RelayDelayFingerprintMinMS > r.RelayDelayFingerprintMaxMS ||
		r.RelayDelayFingerprintMaxMS > 5000 {
		return fmt.Errorf("benchmark_runtime relay delay range must satisfy 0 <= min <= max <= 5000")
	}
	return nil
}

func (r *benchmarkRuntimeSettings) delayConfig() delayFingerprintConfig {
	if r == nil {
		return delayFingerprintConfig{mode: delayFingerprintOff}
	}
	return delayFingerprintConfig{
		mode:  r.RelayDelayFingerprintMode,
		minMS: r.RelayDelayFingerprintMinMS,
		maxMS: r.RelayDelayFingerprintMaxMS,
	}
}

func inferenceTicketIdentity(req submitRequest) brokerTicketIdentity {
	return brokerTicketIdentity{
		GrantID: req.InferenceGrantID, AgentID: req.InferenceAgentID,
		SlotID: req.InferenceSlotID, TicketDeadline: req.InferenceTicketDeadline,
	}
}

// sourceFromReq builds the sandbox Source for a build submission. git_url and
// tarball_url are mutually exclusive (enforced in handleSubmit); GitRef and
// GitSubdir apply only to the git path.
func sourceFromReq(req submitRequest) sandbox.Source {
	return sandbox.Source{
		GitURL:              req.GitURL,
		GitRef:              req.GitRef,
		GitSubdir:           req.GitSubdir,
		TarballURL:          req.TarballURL,
		TarballSHA256:       req.TarballSHA256,
		ScreenedImageURL:    req.ScreenedImageURL,
		ScreenedImageSHA256: req.ScreenedImageSHA256,
		ScreenedImageID:     req.ScreenedImageID,
		ScreenedImageRef:    req.ScreenedImageRef,
		ScreenedImageSize:   req.ScreenedImageSize,
	}
}

func validateGitSourceOptions(req submitRequest) string {
	if req.GitSubdir != "" && req.GitURL == "" {
		return "git_subdir requires git_url"
	}
	return ""
}

func validateScreenedImage(req submitRequest) string {
	fieldsSet := req.ScreenedImageURL != "" || req.ScreenedImageSHA256 != "" ||
		req.ScreenedImageID != "" || req.ScreenedImageRef != "" || req.ScreenedImageSize != 0
	if !fieldsSet {
		return ""
	}
	if req.TarballURL == "" {
		return "screened image requires tarball_url for source fingerprinting"
	}
	if req.ScreenedImageURL == "" || req.ScreenedImageSHA256 == "" ||
		req.ScreenedImageID == "" || req.ScreenedImageRef == "" || req.ScreenedImageSize <= 0 {
		return "screened image url, sha256, id, ref, and positive size are required together"
	}
	if req.ScreenedImageSize > 8<<30 {
		return "screened_image_size_bytes exceeds the 8 GiB limit"
	}
	if len(req.ScreenedImageSHA256) != 64 {
		return "screened_image_sha256 must be 64 lowercase hex characters"
	}
	if _, err := hex.DecodeString(req.ScreenedImageSHA256); err != nil ||
		req.ScreenedImageSHA256 != strings.ToLower(req.ScreenedImageSHA256) {
		return "screened_image_sha256 must be 64 lowercase hex characters"
	}
	if !strings.HasPrefix(req.ScreenedImageID, "sha256:") || len(req.ScreenedImageID) != 71 {
		return "screened_image_id must be a sha256 Docker image id"
	}
	imageHex := strings.TrimPrefix(req.ScreenedImageID, "sha256:")
	if _, err := hex.DecodeString(imageHex); err != nil || imageHex != strings.ToLower(imageHex) {
		return "screened_image_id must be a sha256 Docker image id"
	}
	const refPrefix = "ditto-screen/"
	const refSuffix = ":latest"
	refID := strings.TrimSuffix(strings.TrimPrefix(req.ScreenedImageRef, refPrefix), refSuffix)
	parsedRefID, refErr := uuid.Parse(refID)
	if !strings.HasPrefix(req.ScreenedImageRef, refPrefix) || !strings.HasSuffix(req.ScreenedImageRef, refSuffix) ||
		refErr != nil || parsedRefID.String() != refID {
		return "screened_image_ref must be the screener-owned ditto-screen reference"
	}
	return ""
}

func validateScreenedImageAccess(req submitRequest, allowScreenedImages bool) string {
	if req.ScreenedImageURL != "" && !allowScreenedImages {
		return "screened images are only accepted by the validator sandbox"
	}
	return ""
}

func validateBenchmarkImageContract(req submitRequest) string {
	if req.BenchVersion >= protocol.BenchVersionV8 && req.ScreenedImageURL == "" {
		return fmt.Sprintf(
			"benchmark version %d requires a screener-built image; source builds are disabled",
			req.BenchVersion,
		)
	}
	return ""
}

// sandboxStartInfraFailure classifies a container-start error that is the
// validator's OWN executor fault — most importantly a missing ditto-sandbox
// egress network — rather than anything about the miner submission. The scorer
// must surface these as retryable validator_infrastructure so the validator
// ends its sweep and backs off, instead of blaming the agent and re-leasing it
// in a tight resubmit loop (which floods this endpoint with 429s). The match is
// deliberately narrow: an ordinary harness crash stays a submission failure. The
// text it reads is Docker's own daemon error, which carries no miner source.
func sandboxStartInfraFailure(err error) *store.Failure {
	if err == nil {
		return nil
	}
	msg := strings.ToLower(err.Error())
	if strings.Contains(msg, "dittobench-sub:") &&
		(strings.Contains(msg, "unable to find image") ||
			strings.Contains(msg, "no such image") ||
			strings.Contains(msg, "pull access denied for dittobench-sub")) {
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "sandbox_image_unavailable",
			Retryable: true,
		}
	}
	if !strings.Contains(msg, "network") || !strings.Contains(msg, "not found") {
		return nil
	}
	return &store.Failure{
		Kind:      "validator_infrastructure",
		Code:      "sandbox_network_unavailable",
		Retryable: true,
	}
}

// stopSandboxForRestart removes the current compatibility container and its
// private network while retaining Build's request-scoped image tag. Stop owns
// that tag by default, but the v8 adapter fallback must start the same verified
// image a second time. The replacement handle resumes normal ownership and
// releases the tag at the end of the run; Run also releases it on restart
// failure, so every exit remains bounded.
func (s *server) stopSandboxForRestart(handle *sandbox.Handle) error {
	if handle == nil {
		return errors.New("compatibility sandbox identity is unavailable")
	}
	containerOnly := *handle
	containerOnly.ImageRef = ""
	cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	err := s.sandbox.StopRetainingImage(cleanupCtx, &containerOnly)
	cancel()
	return err
}

func (s *server) cleanupPartialSandboxStart(handle *sandbox.Handle, image string) error {
	if err := s.stopSandboxForRestart(handle); err != nil {
		return err
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	s.sandbox.Release(cleanupCtx, image)
	cancel()
	return nil
}

// screenedImageInfraFailure classifies a build-path error that is the
// validator's OWN fault for a reason the miner cannot influence: it could not
// ACQUIRE the screened image the platform produced and attested. The image is
// platform output, fetched over the platform's own URL onto this validator's
// disk, and this happens strictly before `docker run` -- the harness has not
// executed a single instruction. There is no path by which an artifact can
// steer a run into this bin.
//
// That last property is the safety argument, and it is what distinguishes this
// from the failure mode that caused tonight's fleet starvation. The mnemox
// family looped because a RUNNING artifact reliably triggered a no-fault
// classification: its own ~60-minute hang minted the grant, raised the attempt
// cap, and re-leased itself, reaching 10 attempts on a budget of 2 with zero
// scores. A self-sustaining loop needs the artifact's behaviour in the loop.
// Here the artifact is not running and its bytes have not been read.
//
// The complementary half of the guarantee lives in sandbox.loadScreenedImage:
// only transient-by-construction acquisition failures carry
// ErrScreenedImageUnavailable. Every DETERMINISTIC failure -- sha256/size/id
// mismatch, malformed URL, 4xx other than 429, archive or attestation
// rejection, and `docker image load` refusing a verified archive -- is left on
// the terminal default on purpose, because it will fail identically forever and
// a no-fault verdict on it would re-lease a permanently broken image without
// bound.
//
// Returns nil for everything else. The generic
// sandbox_failure/sandbox_runtime/retryable=false default is deliberately NOT
// touched: a merely-unrecognised failure must never become no-fault.
func screenedImageInfraFailure(err error) *store.Failure {
	if err == nil || !errors.Is(err, sandbox.ErrScreenedImageUnavailable) {
		return nil
	}
	return &store.Failure{
		Kind:      "validator_infrastructure",
		Code:      "screened_image_unavailable",
		Retryable: true,
	}
}

// acceptedResponse is returned by the sandbox (asynchronous) path; poll
// GET /v1/runs/{id} for status + report.
type acceptedResponse struct {
	RunID        string `json:"run_id"`
	Status       string `json:"status"`
	Poll         string `json:"poll"`
	BenchVersion int    `json:"bench_version"`
}

func (s *server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	// Abuse guard: per-IP rate limit on the expensive submit endpoint. Skipped
	// in local/dev mode (DITTOBENCH_ALLOW_PRIVATE_HARNESS), where a calibration
	// loop legitimately submits in bulk from a single IP.
	if !s.allowPrivate && !s.limiter.Allow(clientIP(r)) {
		writeError(w, http.StatusTooManyRequests, "rate limit exceeded; slow down and retry shortly")
		return
	}

	var req submitRequest
	r.Body = http.MaxBytesReader(w, r.Body, maxSubmitBody)
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid or oversized JSON body")
		return
	}
	version, msg := requestedPracticeBenchVersion(req.BenchVersion)
	if msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	req.BenchVersion = version
	// Exactly one submission source: a running harness (direct), a git repo, or
	// a presigned platform tarball (mode B). git_url + tarball_url both build in
	// the Docker sandbox.
	sources := 0
	for _, set := range []bool{req.HarnessURL != "", req.GitURL != "", req.TarballURL != ""} {
		if set {
			sources++
		}
	}
	if sources == 0 {
		writeError(w, http.StatusBadRequest, "one of harness_url (direct), git_url, or tarball_url (sandbox/run_size) is required")
		return
	}
	if sources > 1 {
		writeError(w, http.StatusBadRequest, "provide only one of harness_url, git_url, or tarball_url")
		return
	}
	if msg := validateGitSourceOptions(req); msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	if msg := validateScreenedImage(req); msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	// Screened-image metadata is an integrity pin, not caller authentication.
	// Only validator-owned/private deployments may bypass the source build; the
	// public unauthenticated practice API must always build submitted source.
	if msg := validateScreenedImageAccess(req, s.allowScreenedImages); msg != "" {
		writeError(w, http.StatusForbidden, msg)
		return
	}
	// The screened-image contract bans validator-side SOURCE BUILDS (git_url /
	// tarball_url). A direct harness_url run never builds anything — the miner runs
	// their own already-built harness and the API only drives + scores it — so the
	// contract is inapplicable there. The exemption is keyed on the SOURCE KIND,
	// not on the deployment: gating it on allowPrivate instead took hosted
	// practice offline once v8 became the only supported version, because
	// harness_url is the only submission path the public practice endpoint has
	// (it runs without a Docker daemon and rejects screened images), so every
	// practice submission answered 403. Build modes stay gated everywhere, and
	// the canonical validator path (handleScoreRequest) enforces the contract
	// unconditionally — including for harness_url.
	if req.HarnessURL == "" {
		if msg := validateBenchmarkImageContract(req); msg != "" {
			writeError(w, http.StatusForbidden, msg)
			return
		}
	}

	// SSRF guard: a caller-supplied harness_url / tarball_url must be a public
	// http(s) endpoint (relaxed for local dev via DITTOBENCH_ALLOW_PRIVATE_HARNESS).
	if req.HarnessURL != "" {
		if err := netguard.ValidateURL(req.HarnessURL, s.allowPrivate); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	if req.TarballURL != "" {
		if err := netguard.ValidateURL(req.TarballURL, s.allowPrivate); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	if req.ScreenedImageURL != "" {
		if err := netguard.ValidateURL(req.ScreenedImageURL, s.allowPrivate); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}

	if req.RunSize == "" {
		writeError(w, http.StatusBadRequest, "run_size is required (small|medium|full)")
		return
	}
	s.submitRunSize(w, r, req)
}

// handleVersionedScore is the canonical v8 scoring path. The request must pin
// the platform-issued dataset and explicitly select benchmark version 8.
func (s *server) handleVersionedScore(w http.ResponseWriter, r *http.Request) {
	s.handleScoreRequest(w, r)
}

func (s *server) handleScoreRequest(w http.ResponseWriter, r *http.Request) {
	if !s.allowPrivate && !s.limiter.Allow(clientIP(r)) {
		writeError(w, http.StatusTooManyRequests, "rate limit exceeded; slow down and retry shortly")
		return
	}
	var req submitRequest
	r.Body = http.MaxBytesReader(w, r.Body, maxSubmitBody)
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid or oversized JSON body")
		return
	}
	version, msg := requestedBenchVersion(req.BenchVersion)
	if msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	req.BenchVersion = version
	if (s.requireTicketInference || req.BenchVersion >= protocol.BenchVersionV7) && req.InferenceSessionID == "" {
		writeError(
			w,
			http.StatusServiceUnavailable,
			"ticket inference session is required for canonical scoring",
		)
		return
	}
	// Validator-specific preconditions (beyond what submitRunSize enforces).
	if req.Seed == 0 {
		writeError(w, http.StatusBadRequest, "seed is required (the platform-issued dataset seed) for canonical scoring")
		return
	}
	if strings.TrimSpace(req.ExpectedDatasetSHA256) == "" {
		writeError(w, http.StatusBadRequest, "dataset_sha256 is required (the platform-issued dataset hash) for canonical scoring")
		return
	}
	if !canonicalSHA256(req.ExpectedDatasetSHA256) {
		writeError(w, http.StatusBadRequest, "dataset_sha256 must be 64 lowercase hex characters")
		return
	}
	if req.RunSize == "" {
		writeError(w, http.StatusBadRequest, "run_size is required (small|medium|full) for canonical scoring")
		return
	}
	// Exactly one harness source; SSRF-guard any caller-supplied URL (same rules
	// as /v1/submit).
	sources := 0
	for _, set := range []bool{req.HarnessURL != "", req.GitURL != "", req.TarballURL != ""} {
		if set {
			sources++
		}
	}
	if sources != 1 {
		writeError(w, http.StatusBadRequest, "provide exactly one of harness_url, git_url, or tarball_url")
		return
	}
	if msg := validateGitSourceOptions(req); msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	if msg := validateScreenedImage(req); msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	if msg := validateScreenedImageAccess(req, s.allowScreenedImages); msg != "" {
		writeError(w, http.StatusForbidden, msg)
		return
	}
	if msg := validateBenchmarkImageContract(req); msg != "" {
		writeError(w, http.StatusForbidden, msg)
		return
	}
	if req.HarnessURL != "" {
		if err := netguard.ValidateURL(req.HarnessURL, s.allowPrivate); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	if req.TarballURL != "" {
		if err := netguard.ValidateURL(req.TarballURL, s.allowPrivate); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	if req.ScreenedImageURL != "" {
		if err := netguard.ValidateURL(req.ScreenedImageURL, s.allowPrivate); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	// Delegate to the shared run_size pipeline; the dataset_sha256 pin is enforced
	// inside runSizeJob after regeneration.
	s.submitRunSize(w, r, req)
}

func requestedBenchVersion(requested int) (int, string) {
	if requested == 0 {
		return 0, "bench_version is required (supported: 8, 9, 10, 11)"
	}
	for _, version := range supportedBenchVersions() {
		if requested == version {
			return requested, ""
		}
	}
	return 0, "unsupported bench_version (supported: 8, 9, 10, 11)"
}

func toolPrerequisiteWave(toolCases []protocol.ToolCase) (protocol.SeedRequest, error) {
	wave := protocol.SeedRequest{UserID: "miner"}
	seen := make(map[string]bool)
	for _, tc := range toolCases {
		for _, pair := range tc.PrerequisitePairs {
			if pair.PairID == "" || seen[pair.PairID] {
				return protocol.SeedRequest{}, fmt.Errorf("invalid or duplicate tool prerequisite pair")
			}
			seen[pair.PairID] = true
			wave.Pairs = append(wave.Pairs, pair)
		}
	}
	return wave, nil
}

// validateV8EvidenceAvailability proves that every generator-declared memory
// dependency is present in the ordered seed boundary for that user graph. V8's
// tool prerequisites are the initial shared world; later memory waves extend
// that same live harness store rather than replacing or resetting it.
func validateV8EvidenceAvailability(toolCases []protocol.ToolCase, memoryCases []gen.StagedCase, waves []protocol.SeedRequest) error {
	available := map[string]map[string]bool{"miner": {}}
	add := func(user string, pairs []protocol.MemoryPair) {
		if user == "" {
			user = "miner"
		}
		if available[user] == nil {
			available[user] = map[string]bool{}
		}
		for _, pair := range pairs {
			available[user][pair.PairID] = true
		}
	}
	for _, tc := range toolCases {
		add("miner", tc.PrerequisitePairs)
	}
	for _, wave := range waves {
		add(wave.UserID, wave.Pairs)
	}
	for _, staged := range memoryCases {
		user := staged.UserID
		if user == "" {
			user = "miner"
		}
		for _, pairID := range staged.RequiredPairIDs {
			if pairID == "" || !available[user][pairID] {
				return fmt.Errorf("v8 memory evidence is unavailable through the ordered seed boundary")
			}
		}
	}
	return nil
}

// submitRunSize validates a run_size submission, requires an OpenRouter key,
// and kicks off the full SN118 pipeline asynchronously (returns 202 + run_id).
func (s *server) submitRunSize(w http.ResponseWriter, r *http.Request, req submitRequest) {
	if err := req.BenchmarkRuntime.validate(req.BenchVersion); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.GitURL == "" && req.HarnessURL == "" && req.TarballURL == "" {
		writeError(w, http.StatusBadRequest, "run_size requires git_url / tarball_url (build in Docker) or harness_url (point at an already-running harness, for local dev)")
		return
	}
	prof, ok := gen.ProfileForVersion(req.RunSize, req.BenchVersion)
	if !ok {
		writeError(w, http.StatusBadRequest, "run_size must be one of: small, medium, full")
		return
	}
	// Sandbox is only needed when we build the crate ourselves. The local
	// harness_url path runs the full generate→seed→run→score pipeline against an
	// already-running harness, so it does not require Docker.
	if req.GitURL != "" || req.TarballURL != "" {
		if s.sandbox == nil {
			writeError(w, http.StatusNotImplemented, "git_url (Docker build) is not available on this server; submit a reachable harness_url instead")
			return
		}
		if err := s.sandbox.Available(r.Context()); err != nil {
			// The hosted practice API has no Docker daemon — the on-chain
			// validator owns crate builds. Miners practice by exposing a
			// reachable harness and submitting harness_url.
			writeError(w, http.StatusServiceUnavailable, "git_url (Docker build) is unavailable here ("+err.Error()+"); submit a reachable harness_url to practice")
			return
		}
	}
	// Generation is deterministic, scoring is judge-free, and the harness talks
	// only to the locked-model gateway, so no LLM key exists anywhere in a run.

	if !s.acquireRunSlot(w) {
		return
	}

	seed := pinnedOrFreshSeed(req.Seed)
	runID := uuid.NewString()
	if req.InferenceSessionID != "" &&
		!s.broker.claimRun(req.InferenceSessionID, runID, inferenceTicketIdentity(req), req.BenchVersion) {
		<-s.runSlots
		writeError(w, http.StatusConflict, "ticket inference session is unavailable")
		return
	}
	if req.InferenceSessionID != "" && req.BenchmarkRuntime != nil &&
		!s.broker.configureRun(req.InferenceSessionID, runID, req.BenchmarkRuntime.delayConfig()) {
		<-s.runSlots
		s.broker.removeRun(req.InferenceSessionID, runID)
		writeError(w, http.StatusConflict, "ticket inference runtime policy is unavailable")
		return
	}
	s.store.Create(runID, "run_size", store.StatusQueued, seed, prof.Tools+prof.Mem)
	s.store.SetRunSize(runID, req.RunSize)
	s.store.SetBenchVersion(runID, req.BenchVersion)
	log.Printf("run %s: run_size=%s bench_version=%d seed=%d tools=%d mem=%d",
		runID, req.RunSize, req.BenchVersion, seed, prof.Tools, prof.Mem)

	runCtx, cancel := context.WithCancel(context.Background())
	s.registerRunCancel(runID, cancel)
	go s.runSizeJob(runCtx, runID, req, prof, seed)

	writeJSON(w, http.StatusAccepted, acceptedResponse{
		RunID:        runID,
		Status:       string(store.StatusQueued),
		Poll:         "/v1/runs/" + runID,
		BenchVersion: req.BenchVersion,
	})
}

func (s *server) acquireRunSlot(w http.ResponseWriter) bool {
	select {
	case s.runSlots <- struct{}{}:
		return true
	default:
		writeError(w, http.StatusTooManyRequests, "validator at capacity; retry shortly")
		return false
	}
}

// beginMemoryPhase opens the source-bound broker session for the lifetime of
// the memory phase. Historical local-embedding runs additionally share the
// global Ollama slot; hosted v7 runs are isolated per ticket and bypass it.
func (s *server) beginMemoryPhase(
	ctx context.Context,
	inferenceSessionID string,
	runID string,
	localEmbedding bool,
) (func(), bool) {
	if localEmbedding && s.memorySlots != nil {
		select {
		case s.memorySlots <- struct{}{}:
		case <-ctx.Done():
			return func() {}, false
		}
	}
	releaseSlot := func() {
		if localEmbedding && s.memorySlots != nil {
			<-s.memorySlots
		}
	}
	if inferenceSessionID != "" &&
		(s.broker == nil || !s.broker.beginEmbeddingPhase(inferenceSessionID, runID)) {
		releaseSlot()
		return func() {}, false
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			if inferenceSessionID != "" {
				s.broker.endEmbeddingPhase(inferenceSessionID, runID)
			}
			releaseSlot()
		})
	}, true
}

// runScope classifies a run_size request as SCORED or PRACTICE. The canonical
// on-chain path (POST /v1/score) pins the exact dataset the platform issued with
// its ticket (dataset_sha256), so its report feeds the KOTH ledger and scoring is
// trustless-strict: observed execution is mandatory and the parser free points
// close. A run_size PRACTICE submission (POST /v1/submit, no dataset_sha256) keeps
// the lenient self-hostable scoring. The scope is a pure property of the request,
// so any third party re-deriving (dataset, transcript, scope) reproduces the
// score — no validator secret enters the score path.
func runScope(req submitRequest) scorer.Scope {
	if strings.TrimSpace(req.ExpectedDatasetSHA256) != "" {
		return scorer.ScopeScored
	}
	return scorer.ScopePractice
}

func requestedPracticeBenchVersion(version int) (int, string) {
	if version == 0 {
		version = protocol.BenchVersionV9
	}
	return requestedBenchVersion(version)
}

// requiresV9BaseEvidence separates an ordinary practice report from a
// signature-bound validator report. Practice has no platform-issued artifact
// or provider-accounting ticket, so manufacturing a v9 evidence root would
// either fail at finalization or falsely claim canonical provenance. The same
// v9 dataset and grader still run; only the activation/confirmation evidence is
// intentionally absent.
func requiresV9BaseEvidence(req submitRequest) bool {
	return req.BenchVersion >= protocol.BenchVersionV9 && runScope(req) == scorer.ScopeScored
}

// runSizeJob is the full SN118 pipeline: building → generating → seeding →
// running (with a reversible relay-wait pause) → scoring → done. Every stage is
// deterministic; the only LLM in the loop is the locked model the harness
// itself talks to.
func (s *server) runSizeJob(ctx context.Context, runID string, req submitRequest, prof gen.Profile, seed int64) {
	defer func() { <-s.runSlots }() // release the concurrency slot
	defer s.unregisterRunCancel(runID)
	if req.InferenceSessionID != "" {
		defer s.broker.removeRun(req.InferenceSessionID, runID)
	}
	defer func() {
		if rec := recover(); rec != nil {
			s.store.Fail(runID, "internal panic during run_size job")
			log.Printf("run_size job %s panicked: %v", runID, rec)
		}
	}()

	total := prof.Tools + prof.Mem
	scope := runScope(req)

	// 1. building — build the crate in the Docker sandbox. Skipped on the local
	//    harness_url path (the miner is already running their harness).
	var image string
	var structuralFP *protocol.CodeFingerprint
	if req.GitURL != "" || req.TarballURL != "" {
		s.store.SetStage(runID, store.StatusBuilding, 0, total)
		img, buildLog, fp, err := s.sandbox.Build(ctx, sourceFromReq(req))
		if err != nil {
			s.store.FailWith(runID, "build failed: "+err.Error(), screenedImageInfraFailure(err))
			log.Printf("run %s build failed: %v\n%s", runID, err, buildLog)
			return
		}
		image = img
		structuralFP = fp
		defer s.sandbox.Release(context.Background(), image)
	}

	// 2. generating — fresh, anti-cheat dataset (tools + memory haystack).
	s.store.SetStage(runID, store.StatusGenerating, 0, total)
	rng, rngErr := gen.NewRNGForVersion(seed, req.BenchVersion)
	if rngErr != nil {
		s.store.Fail(runID, "unsupported bench_version during generation")
		return
	}
	toolCases, toolPara := gen.GenerateToolsForVersion(rng, seed, prof.Tools, req.BenchVersion)
	// v2 memory engine (bench_version 2): a fresh procedural persona universe
	// per seed replaces the static LongMemEval fixture. Generation is fully
	// non-LLM and a pure function of the master `seed`; selection shares the run
	// rng. The suite lays cases out across seeding tiers (A prepared, B raw-pairs)
	// and staged Tier-C waves.
	// Always pass the version explicitly. The un-suffixed wrappers default to
	// the FROZEN v2 contract, so an implicit call here would score the run
	// against the pre-hardening dataset while the report claimed v3.
	memSuite, memErr := gen.GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, req.BenchVersion)
	if memErr != nil {
		s.store.Fail(runID, "memory dataset generation failed for requested bench_version")
		return
	}
	// Multi-graph isolation: seed a second persona under a different
	// user_id and add cross-user isolation cases. The secondary graph is template-
	// rendered. Cases carry the user_id they must be answered under; they merge
	// into the primary staged-case stream.
	iso, isoErr := gen.GenerateIsolationForVersion(seed, prof.Mem, prof.Waves, prof.IsoCases, req.BenchVersion)
	if isoErr != nil {
		s.store.Fail(runID, "isolation dataset generation failed for requested bench_version")
		return
	}
	memSuite.Cases = append(memSuite.Cases, iso.Cases...)
	memWaves := gen.MergeMemoryWaves(memSuite.Waves, iso.SecondaryWave)
	if req.BenchVersion >= protocol.BenchVersionV8 {
		if err := validateV8EvidenceAvailability(toolCases, memSuite.Cases, memWaves); err != nil {
			s.store.Fail(runID, "v8 memory evidence is unavailable through the ordered seed boundary")
			return
		}
	}
	para := toolPara
	para.Add(memSuite.Stats)
	totalPairs, totalSubjects := 0, 0
	for _, w := range memSuite.Waves {
		totalPairs += len(w.Pairs)
		totalSubjects += len(w.Subjects)
	}
	log.Printf("run %s generated: %d tool cases, %d memory cases, %d haystack pairs (%d subjects) across %d wave(s), %d Tier-B cases; paraphrase attempted=%d applied=%d retried=%d fallback=%d",
		runID, len(toolCases), len(memSuite.Cases), totalPairs, totalSubjects, memSuite.SeedingWaves, memSuite.TierBCases,
		para.Attempted, para.Applied, para.Retried, para.Fallback)
	total = prof.Tools + len(memSuite.Cases) // actual case count for progress

	// Dataset hashing + optional artifact persistence:
	// hash the fully-rendered dataset so a dispute can be pinned to exact bytes;
	// when DITTOBENCH_ARTIFACT_DIR is set, persist the artifact keyed by run_id
	// (the local-file form of the "upload rendered dataset" persistence; a
	// platform bucket is the drop-in replacement for os.WriteFile here).
	// Observed execution: derive each tool case's live mock-tool environment (the Fixtures
	// back the mock endpoint stood up below). The hashed artifact — assembled by
	// gen.BuildArtifact so the run path and the generate service produce identical
	// bytes for a seed — recomputes the fixture digests from the same (seed, case).
	toolFixtureByInternalID := make(map[string]toolexec.Fixture, len(toolCases))
	for _, c := range toolCases {
		toolFixtureByInternalID[c.ID] = toolexec.BuildFixture(seed, c)
	}
	// The hashed artifact covers the secondary isolation graph too (when present),
	// so a dispute re-scores the exact multi-graph seeding.
	artifact, artifactErr := gen.BuildArtifactForVersion(seed, req.BenchVersion, toolCases, memSuite.Cases, memWaves)
	if artifactErr != nil {
		s.store.Fail(runID, "dataset generation failed for requested bench_version")
		return
	}
	if artifact.BenchVersion != req.BenchVersion {
		s.store.Fail(runID, fmt.Sprintf("bench_version mismatch: requested %d but generated artifact reports %d", req.BenchVersion, artifact.BenchVersion))
		return
	}
	datasetHash, artifactBytes, hashErr := artifact.SHA256Hex()
	if hashErr != nil {
		log.Printf("run %s: dataset hashing failed: %v", runID, hashErr)
	}
	// Canonical validator path (/v2/score): the platform pins the dataset it
	// issued with the ticket. Regeneration is deterministic, so a mismatch means
	// the generator/bench_version drifted from what the platform shipped — fail
	// loudly rather than score a different dataset than the one under dispute.
	if want := strings.TrimSpace(req.ExpectedDatasetSHA256); want != "" {
		if err := verifyDatasetHash(want, datasetHash, hashErr); err != nil {
			s.store.Fail(runID, err.Error())
			log.Printf("run %s: %v", runID, err)
			return
		}
	}
	if dir := strings.TrimSpace(os.Getenv("DITTOBENCH_ARTIFACT_DIR")); dir != "" && artifactBytes != nil {
		if err := os.WriteFile(filepath.Join(dir, runID+".json"), artifactBytes, 0o644); err != nil {
			log.Printf("run %s: artifact persist failed: %v", runID, err)
		}
	}

	// V9 projects canonical dataset identities into per-run opaque capabilities
	// only after the canonical dataset artifact is complete. The grader and report restore
	// canonical IDs; the harness sees only this projected view.
	var harnessProjection *gen.HarnessProjection
	if req.BenchVersion >= protocol.BenchVersionV9 {
		blindingKey := make([]byte, sha256.Size)
		if _, artifactErr = cryptorand.Read(blindingKey); artifactErr != nil {
			s.store.Fail(runID, "v9 harness projection entropy unavailable")
			return
		}
		harnessProjection, artifactErr = gen.BuildHarnessProjection(seed, blindingKey, req.BenchVersion, toolCases, memSuite.Cases, memWaves)
		if artifactErr != nil {
			s.store.Fail(runID, "v9 harness projection failed")
			return
		}
		primaryWaveCount := len(memSuite.Waves)
		toolCases = harnessProjection.ToolCases
		memSuite.Cases = harnessProjection.MemoryCases
		memSuite.Waves = harnessProjection.Waves[:primaryWaveCount]
		if len(harnessProjection.Waves) > primaryWaveCount {
			iso.SecondaryWave = harnessProjection.Waves[primaryWaveCount]
		}
	}

	inferenceSessionID := req.InferenceSessionID

	// The relay exposes process-wide monotonic counters. Serialize the entire
	// scored lifetime only for the direct-harness development path, which cannot
	// be source-bound to a private broker session. Production sandbox runs use
	// the per-run broker above and therefore do not serialize on this mutex.
	if scope == scorer.ScopeScored && inferenceSessionID == "" {
		unlockRelayRun := s.lockScoredRelayRun()
		defer unlockRelayRun()
	}

	// 3. start the container. On the local harness_url path we skip the container
	//    and target the miner's already-running harness.
	harnessURL := req.HarnessURL
	var handle *sandbox.Handle
	var runErr error
	var sourceCapability string
	strictCleanupOnly := false
	if image != "" {
		// Register ownership before Sandbox.Run: an implementation may return a
		// partial handle with an error, and that process must be strictly removed
		// before its request-scoped image can be released.
		defer func() {
			if handle == nil {
				return
			}
			if sourceCapability != "" {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
			}
			if strictCleanupOnly {
				_ = s.cleanupPartialSandboxStart(handle, image)
				return
			}
			s.finishSandboxRun(runID, handle)
		}()
		env := harnessSandboxEnv(req.Env, req.BenchVersion, inferenceSessionID)
		if inferenceSessionID != "" {
			sourceCapability, runErr = s.broker.installSourceCapability(inferenceSessionID, runID)
			if runErr != nil {
				s.store.Fail(runID, "inference session capability is unavailable")
				return
			}
			env, runErr = harnessSandboxEnvWithCapability(
				req.Env, req.BenchVersion, platformLockedProvider, inferenceSessionID, sourceCapability,
			)
			if runErr != nil {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
				s.store.Fail(runID, "inference session capability is unavailable")
				return
			}
		}
		handle, runErr = s.sandbox.Run(ctx, image, env)
		if runErr != nil {
			if sourceCapability != "" {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
				sourceCapability = ""
			}
			if handle != nil {
				if cleanupErr := s.cleanupPartialSandboxStart(handle, image); cleanupErr != nil {
					strictCleanupOnly = true
					s.store.Fail(runID, "partial container start isolation unavailable")
					return
				}
				handle = nil
			}
			s.store.FailWith(runID, "container start failed: "+runErr.Error(), sandboxStartInfraFailure(runErr))
			return
		}
		if inferenceSessionID != "" {
			if !s.broker.bindSourceCapability(inferenceSessionID, runID, handle.SourceIP, sourceCapability) {
				s.store.Fail(runID, "inference session is unavailable")
				return
			}
		}
		harnessURL = handle.BaseURL
		ctx = runner.TrustSandbox(ctx)
	} else if inferenceSessionID != "" {
		// The local practice path deliberately skips the sandbox, but ticket
		// inference is still source-bound. Only admit a literal loopback harness:
		// this preserves the broker's one-source invariant without allowing a
		// caller-selected remote host to inherit the ticket capability.
		sourceIP, ok := loopbackHarnessSourceIP(harnessURL)
		if !ok || !s.broker.bindSource(inferenceSessionID, runID, sourceIP) {
			s.store.Fail(runID, "local ticket inference requires a loopback harness")
			return
		}
	}

	var healthErr error
	if handle != nil {
		healthErr = s.waitSandboxHealthy(ctx, handle, sandboxHealthTimeout)
	} else {
		healthErr = runner.WaitHealthy(ctx, harnessURL, sandboxHealthTimeout)
	}
	if healthErr != nil {
		s.store.Fail(runID, "harness never became healthy: "+healthErr.Error())
		return
	}
	tools := catalog.CatalogForVersion(req.BenchVersion)

	// V8 harnesses may embed before any model turn, including the route probe
	// below. Admit the ticket-bound embedding lane before probing so a working
	// adapter is not mistaken for a zero-call adapter merely because its
	// retrieval prelude was denied.
	endEmbeddingPhase := func() {}
	if req.BenchVersion >= protocol.BenchVersionV7 {
		endMemoryPhase, admitted := s.beginMemoryPhase(ctx, inferenceSessionID, runID, false)
		if !admitted {
			if ctx.Err() == nil {
				s.store.Fail(runID, "embedding phase admission failed")
			}
			return
		}
		endEmbeddingPhase = endMemoryPhase
		defer endEmbeddingPhase()
	}

	// Probe the independent validator relay dependency before spending the
	// dataset, then snapshot its monotonic health counters. The end snapshot
	// prevents a mid-run provider outage from being persisted as an indefensible
	// low score even when a harness masks that outage behind HTTP 200 + an empty
	// response.
	var relayStart relayHealthSnapshot
	var routeProbeAttempts uint64
	var routeProbeRouted uint64
	if scope == scorer.ScopeScored {
		var ok bool
		relayStart, ok = s.relayRunStart(ctx, runID, req.BenchVersion, req.RunSize, harnessGateway(inferenceSessionID), inferenceSessionID)
		if !ok {
			return
		}
	}

	// v0.44 originally selected only the new `platform` adapter. Older images
	// passed admission under the generic OpenAI-compatible adapter, so
	// they stayed healthy and answered all 351 cases without ever reaching the
	// broker. Detect that incompatibility with one discarded, isolated probe
	// before seeding or scoring, then restart the same screened image once with
	// the compatibility selector. The CAS source move keeps the ticket bound to
	// one stopped-or-live container throughout.
	if image != "" && scope == scorer.ScopeScored && req.BenchVersion >= protocol.BenchVersionV8 {
		afterProbe, routed, probeErr := s.probeHarnessModelRoute(ctx, harnessURL, inferenceSessionID, relayStart, tools, req.BenchVersion)
		routeProbeAttempts++
		if routed {
			routeProbeRouted++
		}
		if probeErr != nil {
			s.failRelayUnavailableForContext(ctx, runID, probeErr)
			return
		}
		if !routed {
			oldHandle := handle
			replacementCapability, retiredSourceEpoch, capabilityErr := s.broker.rotateSourceCapability(
				inferenceSessionID, runID, sourceCapability,
			)
			if capabilityErr != nil {
				s.store.Fail(runID, "inference session compatibility capability is unavailable")
				return
			}
			sourceCapability = replacementCapability
			if err := s.stopSandboxForRestart(oldHandle); err != nil {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
				sourceCapability = ""
				strictCleanupOnly = true
				s.store.Fail(runID, "compatibility sandbox isolation unavailable")
				return
			}
			drainCtx, drainCancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
			drainErr := s.broker.waitSourceEpochDrained(drainCtx, inferenceSessionID, retiredSourceEpoch)
			drainCancel()
			if drainErr != nil {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
				sourceCapability = ""
				s.store.Fail(runID, "compatibility inference drain unavailable")
				return
			}
			compatEnv, capabilityErr := harnessSandboxEnvWithCapability(
				req.Env, req.BenchVersion, v8CompatLockedProvider, inferenceSessionID, sourceCapability,
			)
			if capabilityErr != nil {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
				sourceCapability = ""
				s.store.Fail(runID, "compatibility inference capability is unavailable")
				return
			}
			replacement, err := s.sandbox.Run(ctx, image, compatEnv)
			if err != nil {
				_ = s.broker.revokeSourceCapability(inferenceSessionID, runID, sourceCapability)
				sourceCapability = ""
				if replacement != nil {
					handle = replacement
					if cleanupErr := s.cleanupPartialSandboxStart(replacement, image); cleanupErr != nil {
						strictCleanupOnly = true
						s.store.Fail(runID, "partial compatibility container isolation unavailable")
						return
					}
					handle = nil
				}
				s.store.FailWith(runID, "compatibility container start failed: "+err.Error(), sandboxStartInfraFailure(err))
				return
			}
			if !s.broker.replaceBoundSourceCapability(
				inferenceSessionID, runID, oldHandle.SourceIP, replacement.SourceIP, sourceCapability,
			) {
				handle = replacement
				s.store.Fail(runID, "inference session could not move to compatibility sandbox")
				return
			}
			handle = replacement
			harnessURL = replacement.BaseURL
			if err := s.waitSandboxHealthy(ctx, replacement, sandboxHealthTimeout); err != nil {
				s.store.Fail(runID, "compatibility harness never became healthy: "+err.Error())
				return
			}
			afterProbe, routed, probeErr = s.probeHarnessModelRoute(ctx, harnessURL, inferenceSessionID, relayStart, tools, req.BenchVersion)
			routeProbeAttempts++
			if routed {
				routeProbeRouted++
			}
			if probeErr != nil {
				s.failRelayUnavailableForContext(ctx, runID, probeErr)
				return
			}
			if !routed && req.BenchVersion == protocol.BenchVersionV8 {
				s.store.FailWith(
					runID,
					"benchmark v8 requires the harness response path to use the locked model",
					relayFinalizeFailure(errAgentModelUseMissing),
				)
				return
			}
			if routed {
				log.Printf("run %s selected compatibility inference adapter after zero-call platform probe", runID)
			} else {
				log.Printf("run %s exhausted both supported v9 inference selectors without a broker call", runID)
			}
		}
		relayStart = afterProbe // route probes are not benchmark usage
	}

	perCase := make([]protocol.CaseScore, 0, total)

	// Observed tool execution: stand up the validator's mock tool
	// endpoint. It serves deterministic, seed-derived results for external-world
	// tools AND records the real trajectory, so tool cases are scored on what the
	// validator observed rather than the harness's self-report.
	// Registered for every case (tool + memory) so a harness may route any
	// non-memory call through it during any case.
	toolSrv := toolexec.NewServer()
	for _, c := range toolCases {
		internalID := c.ID
		if harnessProjection != nil {
			var reverseErr error
			internalID, reverseErr = harnessProjection.InternalCaseID(c.ID)
			if reverseErr != nil {
				s.store.Fail(runID, "v9 tool capability reverse mapping failed")
				return
			}
		}
		toolSrv.Register(c.ID, toolFixtureByInternalID[internalID])
	}
	for _, sc := range memSuite.Cases {
		internalID := sc.Case.ID
		if harnessProjection != nil {
			var reverseErr error
			internalID, reverseErr = harnessProjection.InternalCaseID(sc.Case.ID)
			if reverseErr != nil {
				s.store.Fail(runID, "v9 memory capability reverse mapping failed")
				return
			}
		}
		toolSrv.Register(sc.Case.ID, toolexec.BuildFixture(seed, protocol.ToolCase{ID: internalID}))
	}
	toolSourceIP := ""
	if handle != nil {
		toolSourceIP = handle.SourceIP
	}
	toolEndpoint, stopToolSrv, err := s.startToolServerForSession(
		toolSrv, toolSourceIP, req.BenchVersion, inferenceSessionID,
	)
	if err != nil {
		s.store.FailWith(runID, "tool endpoint start failed: "+err.Error(), toolEndpointInfrastructureFailure())
		return
	}
	defer stopToolSrv()

	if !toolEndpoint.available() {
		if scope == scorer.ScopeScored {
			// Observed execution is mandatory on the scored path: without the mock
			// endpoint reachable, observable tool cases can never be watched execute
			// and would all score 0, which is not a defensible score. The on-chain
			// validator always builds+runs the harness in Docker (endpoint served via
			// host.docker.internal), so this only trips on a misconfigured scored run
			// (e.g. a remote harness_url that cannot reach our loopback). Fail loudly
			// rather than emit a zeroed report.
			s.store.Fail(runID, "scored run cannot observe tool execution: tool_endpoint unreachable from the harness (build+run in the Docker sandbox, or use a locally reachable harness). Observed execution is mandatory on the scored path.")
			log.Printf("run %s: scored run aborted — tool_endpoint not advertised (harness cannot be observed)", runID)
			return
		}
		log.Printf("run %s: tool_endpoint not advertised (remote harness unreachable); observable tool cases scored capped (practice)", runID)
	}

	// V8 tool routing is intentionally derived from fresh, seed-bound state.
	// Load only the validator-internal prerequisite facts before tool execution;
	// they are absent from v7 artifacts, so the frozen v7 ordering and bytes are
	// untouched. Any seed failure is validator infrastructure and fails closed.
	if req.BenchVersion >= protocol.BenchVersionV8 {
		prerequisites, err := toolPrerequisiteWave(toolCases)
		if err != nil {
			s.store.Fail(runID, "invalid v8 tool prerequisite dataset")
			return
		}
		if len(prerequisites.Pairs) > 0 {
			if harnessProjection != nil {
				prerequisites.UserID = harnessProjection.WireUserID(gen.PrimaryUser)
			}
			s.store.SetStage(runID, store.StatusSeeding, 0, total)
			if _, err := runner.SeedForVersion(ctx, harnessURL, prerequisites, req.BenchVersion); err != nil {
				s.failV7Seeding(runID, "seeding v8 tool routing state failed: ", err)
				return
			}
		}
	}
	effectiveCaseConcurrency := activeCaseConcurrency()
	caseScopedInference := false
	// Existing v9/v10 harnesses have one run-wide inference URL, so they retain
	// non-overlapping attribution windows. An upgraded v10 harness explicitly
	// advertises the additive per-case URL and can consume the operator-selected
	// concurrency without changing dataset or scoring semantics.
	if scope == scorer.ScopeScored && req.BenchVersion >= protocol.BenchVersionV9 {
		effectiveCaseConcurrency = 1
		if req.BenchVersion >= protocol.BenchVersionV10 && req.BenchmarkRuntime != nil &&
			req.BenchmarkRuntime.CaseConcurrency > 1 {
			if runner.SupportsCaseScopedInference(ctx, harnessURL) {
				effectiveCaseConcurrency = req.BenchmarkRuntime.CaseConcurrency
				caseScopedInference = true
			} else {
				log.Printf(
					"run %s: harness lacks case_scoped_inference_v1; retaining serial attribution",
					runID,
				)
			}
		}
	}
	toolRunUserID := ""
	if harnessProjection != nil {
		toolRunUserID = harnessProjection.WireUserID(gen.PrimaryUser)
		if toolRunUserID == "" {
			s.store.Fail(runID, "v9 primary user capability projection failed")
			return
		}
	}

	// 4. tool cases — share V8's already-seeded world but remain independent of
	//    each other, so run with bounded per-case concurrency. Results are
	//    written per index so the report order is identical to sequential
	//    execution regardless of completion order.
	observedTool, cappedTool := 0, 0
	s.store.SetStage(runID, store.StatusRunning, 0, total)
	toolResults := make([]protocol.CaseScore, len(toolCases))
	toolWasObserved := make([]bool, len(toolCases))
	toolWasCapped := make([]bool, len(toolCases))
	toolTranscripts := make([]transcriptCase, len(toolCases))
	var projectionFailure error
	var projectionFailureOnce sync.Once
	recordProjectionFailure := func(err error) {
		if err != nil {
			projectionFailureOnce.Do(func() { projectionFailure = err })
		}
	}
	runBounded(ctx, len(toolCases), effectiveCaseConcurrency, func(i int) {
		c := toolCases[i]
		caseToolEndpoint := toolEndpoint.forCase(c.ID, toolRunUserID)
		resp, execution, runErr := s.runCaseWithModelAttribution(ctx, inferenceSessionID, harnessURL, c.ID, c.Prompt, tools, runner.CaseOptions{ToolEndpoint: caseToolEndpoint, UserID: toolRunUserID, BenchVersion: req.BenchVersion, CaseScopedInference: caseScopedInference})
		observed := toolSrv.Observed(c.ID)
		cs := scorer.ScoreToolCaseObservedForVersion(c, resp, runErr == nil, observed, scope, req.BenchVersion)
		cs = applyV10ToolProvenance(req.BenchVersion, scope, cs, resp, observed, execution)
		fixture := toolFixtureByInternalID[c.ID]
		if harnessProjection != nil {
			internalID, reverseErr := harnessProjection.InternalCaseID(c.ID)
			if reverseErr != nil {
				recordProjectionFailure(reverseErr)
				return
			}
			fixture = toolFixtureByInternalID[internalID]
		}
		if datagen.IsResultUsage(c.Category) {
			// Result-usage: trajectory + whether the answer carried the served
			// needle value (a fabricated value only the executed tool could
			// reveal). An answer carrying the served DECOY (a plausible number
			// fished from the wrong tool's result) zeros the usage half too.
			// Under v7 the composition is multiplicative and a decoy zeroes the
			// whole case (ComposeResultUsageForVersion).
			cs = scorer.ComposeResultUsageForVersion(req.BenchVersion, cs, resp.FinalText,
				fixture.NeedleValue(), fixture.DecoyValue())
		} else {
			cs = scorer.FinishTool(cs)
		}
		// Exact v7+ trajectories retain the fabricated-self-report penalty.
		// Outcome-driven v8 fuzzy cases rely on the authoritative observed calls
		// and may legitimately summarize a longer exploratory trace.
		cs = scorer.ApplyTrajectoryMismatchForCase(req.BenchVersion, c, cs, resp.ToolCalls, observed)
		switch {
		case len(observed) > 0:
			toolWasObserved[i] = true
		case toolexec.Observable(c):
			// Unobserved observable case: capped in practice (0.5 pre-v7, 0.05
			// under v7), 0 when scored.
			cs = scorer.CapUnobservedForVersion(cs, scope, req.BenchVersion)
			toolWasCapped[i] = true
		}
		transcriptID := c.ID
		transcriptObserved := observed
		transcriptResponse := resp
		if harnessProjection != nil {
			var reverseErr error
			transcriptID, reverseErr = harnessProjection.InternalCaseID(c.ID)
			if reverseErr != nil {
				recordProjectionFailure(reverseErr)
				return
			}
			projected := projectHarnessCase(harnessProjection, resp, observed)
			transcriptObserved = projected.Observed
			transcriptResponse = projected.Response
			if projected.Invalid {
				cs = zeroInvalidV9CapabilityScore(cs, len(observed) > 0, false)
			}
			cs.CaseID = transcriptID
		}
		toolTranscripts[i] = transcriptCase{CaseID: transcriptID, Kind: protocol.KindTool, Response: transcriptResponse, Observed: transcriptObserved, Execution: execution}
		toolResults[i] = cs
		s.store.AppendPartial(runID, cs) // store append is mutex-guarded
	})
	// A cancelled context aborts runBounded early, leaving unexecuted slots as
	// zero-value CaseScores. Never fold those into a report: the cancel handler
	// has already failed the run, so just abandon it.
	if ctx.Err() != nil {
		log.Printf("run %s: cancelled during tool cases; abandoning without a report", runID)
		return
	}
	if projectionFailure != nil {
		s.store.Fail(runID, "v9 tool capability reverse mapping failed")
		return
	}
	for i, cs := range toolResults {
		perCase = append(perCase, cs)
		if toolWasObserved[i] {
			observedTool++
		} else if toolWasCapped[i] {
			cappedTool++
		}
	}
	transcripts := append(make([]transcriptCase, 0, total), toolTranscripts...)

	// Historical local-embedding versions retain their frozen boundary: tool
	// cases may overlap, then the embedding-heavy seed/query phase is admitted.
	if req.BenchVersion < protocol.BenchVersionV7 {
		endMemoryPhase, admitted := s.beginMemoryPhase(ctx, inferenceSessionID, runID, true)
		if !admitted {
			if ctx.Err() == nil {
				s.store.Fail(runID, "embedding phase admission failed")
			}
			return
		}
		endEmbeddingPhase = endMemoryPhase
		defer endEmbeddingPhase()
	}

	// 5. memory cases — staged Tier-C ingestion: seed a wave,
	//    then run the cases it unlocks (all their evidence is now seeded), then
	//    the next wave. A single-wave run degrades to seed-then-run-all.
	casesByWave := make([][]gen.StagedCase, memSuite.SeedingWaves)
	for _, sc := range memSuite.Cases {
		w := sc.RunAfterWave
		if w < 0 {
			w = 0
		}
		if w >= memSuite.SeedingWaves {
			w = memSuite.SeedingWaves - 1
		}
		casesByWave[w] = append(casesByWave[w], sc)
	}
	// Seed the secondary isolation graph up front (a distinct user_id), so cross-
	// user isolation cases can run in any wave.
	if len(iso.SecondaryWave.Pairs) > 0 {
		s.store.SetStage(runID, store.StatusSeeding, len(perCase), total)
		if _, err := runner.SeedForVersion(ctx, harnessURL, iso.SecondaryWave, req.BenchVersion); err != nil {
			if req.BenchVersion >= protocol.BenchVersionV7 {
				s.failV7Seeding(runID, "seeding secondary isolation graph failed: ", err)
			} else {
				s.store.Fail(runID, "seeding secondary isolation graph failed: "+err.Error())
			}
			return
		}
	}
	for w, wave := range memSuite.Waves {
		if len(wave.Pairs) > 0 {
			s.store.SetStage(runID, store.StatusSeeding, len(perCase), total)
			if _, err := runner.SeedForVersion(ctx, harnessURL, wave, req.BenchVersion); err != nil {
				if req.BenchVersion >= protocol.BenchVersionV7 {
					s.failV7Seeding(runID, fmt.Sprintf("seeding haystack wave %d failed: ", w), err)
				} else {
					s.store.Fail(runID, fmt.Sprintf("seeding haystack wave %d failed: %s", w, err.Error()))
				}
				return
			}
		}
		s.store.SetStage(runID, store.StatusRunning, len(perCase), total)
		// Cases within one wave are independent: their evidence is fully seeded
		// (this wave and all prior waves), lifecycle WRITE cases live only in wave
		// 0 and their READ cases in a later wave, and same-wave writes target
		// distinct keys — so they run with bounded concurrency. The wave boundary
		// stays a barrier: seed wave w, run its cases, then seed wave w+1.
		waveCases := casesByWave[w]
		waveResults := make([]protocol.CaseScore, len(waveCases))
		waveTranscripts := make([]transcriptCase, len(waveCases))
		runBounded(ctx, len(waveCases), effectiveCaseConcurrency, func(i int) {
			sc := waveCases[i]
			mc := sc.Case
			// Scope the query to the case's memory graph: isolation cases carry an
			// explicit user_id; all others default to the primary wave's user.
			uid := sc.UserID
			if uid == "" {
				uid = wave.UserID
			}
			caseToolEndpoint := toolEndpoint.forCase(mc.ID, uid)
			resp, execution, runErr := s.runCaseWithModelAttribution(ctx, inferenceSessionID, harnessURL, mc.ID, mc.Question, tools, runner.CaseOptions{ToolEndpoint: caseToolEndpoint, UserID: uid, BenchVersion: req.BenchVersion, CaseScopedInference: caseScopedInference})
			observedCalls := toolSrv.Observed(mc.ID)
			resp = withObservedTrajectory(resp, observedCalls)
			gradedResp := resp
			projected := projectedHarnessCase{Response: resp, Observed: observedCalls}
			if harnessProjection != nil {
				projected = projectHarnessCase(harnessProjection, resp, observedCalls)
				gradedResp = projected.Response
			}
			cs := gradeProjectedMemoryCase(mc, resp, projected, len(observedCalls) > 0)
			cs = applyV10ToolProvenance(
				req.BenchVersion, scope, cs, resp, observedCalls, execution,
			)
			if runErr != nil {
				// The case still scores 0 on its own accuracy (an empty response
				// grades 0); this only tells the group metrics to drop it, so a
				// timeout is never read as phrasing brittleness or a failed audit
				// pair. The tool loop has captured runErr all along; memory did not.
				cs.Undelivered = true
				cs.Notes = append(cs.Notes, "no response from harness (error or timeout)")
			}
			if len(observedCalls) > 0 {
				// The graded response's ToolCalls (and thus cs.Called) are the
				// validator-observed trajectory, not the harness self-report. Mark it
				// so a consumer/auditor reading the report — e.g. reviewing a BaitTool
				// zero — knows cs.Called is authoritative, matching the tool-case path.
				cs.Observed = true
				cs.Notes = append(cs.Notes, "called reflects the validator-observed trajectory")
			}
			transcriptID, transcriptUser := mc.ID, uid
			transcriptObserved := projected.Observed
			transcriptResponse := gradedResp
			if harnessProjection != nil {
				var reverseErr error
				transcriptID, reverseErr = harnessProjection.InternalCaseID(mc.ID)
				if reverseErr == nil {
					transcriptUser, reverseErr = harnessProjection.InternalUserID(uid)
				}
				if reverseErr != nil {
					recordProjectionFailure(reverseErr)
					return
				}
				// User graph provenance is already pinned in the private dataset and
				// projection artifacts; do not publish role labels in the transcript.
				transcriptUser = ""
				cs.CaseID = transcriptID
			}
			waveTranscripts[i] = transcriptCase{CaseID: transcriptID, Kind: protocol.KindMemory, UserID: transcriptUser, Response: transcriptResponse, Observed: transcriptObserved, Execution: execution}
			waveResults[i] = cs
			s.store.AppendPartial(runID, cs) // store append is mutex-guarded
		})
		// Same zero-value guard as the tool loop: a cancellation mid-wave must
		// not fold half-empty results into a report.
		if ctx.Err() != nil {
			log.Printf("run %s: cancelled during memory wave %d; abandoning without a report", runID, w)
			return
		}
		if projectionFailure != nil {
			s.store.Fail(runID, "v9 memory capability reverse mapping failed")
			return
		}
		perCase = append(perCase, waveResults...)
		transcripts = append(transcripts, waveTranscripts...)
	}
	// Close broker access before scoring/accounting. The once-guarded deferred
	// cleanup still handles every early return, cancel, and panic above.
	endEmbeddingPhase()

	// The relay owns authoritative provider-delivery evidence. Check it before
	// scoring or persistence: any upstream infrastructure failure during this
	// run invalidates the whole attempt and lets the validator retry later. This
	// intentionally does not inspect response content or score magnitude, so a
	// legitimately weak harness still receives its legitimate low score.
	var tokenUsage protocol.TokenUsage
	var relayExecution relayExecutionSummary
	if scope == scorer.ScopeScored {
		var ok bool
		tokenUsage, relayExecution, ok = s.relayRunResult(
			ctx,
			runID,
			relayStart,
			harnessGateway(inferenceSessionID),
			inferenceSessionID,
		)
		if !ok {
			return
		}
		relayExecution.RouteProbeAttempts = routeProbeAttempts
		relayExecution.RouteProbeRouted = routeProbeRouted
		if err := s.requireCompleteModelUsageAfterRun(
			ctx,
			harnessURL,
			inferenceSessionID,
			relayStart,
			tools,
			req.BenchVersion,
			tokenUsage,
			relayExecution,
		); err != nil {
			s.failRelayUnavailable(runID, err)
			return
		}
	}

	// 6. scoring — aggregate + finish. Protocol reachability probes are
	// mechanical checks, not benchmark questions. They are currently discarded
	// at their call sites; filter again at the population boundary so a future
	// compatibility path cannot put a hard-coded preflight answer or tool call
	// into any mean, uncertainty estimate, or gate.
	perCase = scorer.ScoredPopulation(perCase)
	s.store.SetStage(runID, store.StatusScoring, len(perCase), total)
	// Score under the contract this run was GENERATED for, not the module's
	// current release: a v2 run's composite is pure accuracy, and the v3+ gate
	// factors must not retroactively apply to it.
	report := scorer.AggregateForVersion(runID, perCase, req.BenchVersion)
	report.Seed = seed
	report.StructuralFingerprint = structuralFP
	injections := injectionAttemptCount(perCase)
	report.Details = &protocol.RunDetails{
		BenchVersion:      req.BenchVersion,
		RunSize:           req.RunSize,
		DatasetSHA256:     datasetHash,
		Paraphrase:        &para,
		InjectionAttempts: injections,
		ToolMean:          report.ToolMean,
		MemoryMean:        report.MemoryMean,
		SeedingWaves:      memSuite.SeedingWaves,
		RawPairsCases:     memSuite.TierBCases,
		ObservedToolCases: observedTool,
		CappedToolCases:   cappedTool,
		ToolProvenance:    summarizeV10ToolProvenance(perCase),
		IsolationCases:    len(iso.Cases),
		LifecycleCases:    memSuite.LifecycleCases,
		ToolEfficiency:    scorer.ToolEfficiencyFactorForVersion(perCase, req.BenchVersion),
		// Generation and grading are both deterministic and non-LLM; the only
		// model in a run is the locked one the harness talks to.
		Models: &protocol.ModelInfo{
			Harness: llm.HarnessModelForVersion(req.BenchVersion),
		},
		PerCategory: report.PerCategory,
	}
	if scope == scorer.ScopeScored {
		report.Details.TokenUsage = &tokenUsage
	}
	if memSuite.LexicalGap.Questions > 0 {
		lg := memSuite.LexicalGap
		report.Details.LexicalGap = &lg
	}
	// v5 conversational-sanity metric (first-class on the report; mirrored into the
	// details blob so it survives the platform wire alongside the other factors).
	report.Details.ConversationalSanity = report.ConversationalSanity
	report.Details.MetamorphicConsistency = scorer.MetamorphicConsistency(perCase)
	if tr, pairs := scorer.TransformRobustness(perCase); tr != nil {
		report.Details.TransformRobustness = tr
		report.Details.AuditCaseCount = pairs
	}
	// The pair COUNTS are what a brittleness verdict is computed from; the
	// robustness ratio above stays for continuity and human reading. Counts pool
	// across runs and validators, and they preserve the direction of a split,
	// which is the only part of the signal that separates a brittle harness from
	// a noisy honest one.
	if ap := scorer.AuditPairs(perCase); ap.Total() > 0 {
		report.Details.AuditPairs = &ap
	}
	if brier, cn := scorer.CalibrationBrier(perCase); brier != nil {
		report.Details.CalibrationBrier = brier
		report.Details.CalibrationN = cn
	}
	report = applyTokenContract(report, req.BenchVersion, req.RunSize, tokenUsage)
	if injections > 0 {
		log.Printf("run %s: %d injection-compliance case(s) flagged", runID, injections)
	}
	if err := validateBenchVersionResult(req.BenchVersion, artifact.BenchVersion, report.Details); err != nil {
		s.store.Fail(runID, err.Error())
		return
	}

	// Offline reproducibility: content-address the transcript artifact (the
	// graded per-case inputs) and attach it to the run. The grader is public and
	// deterministic, so (dataset regenerated from seed) + (this transcript)
	// re-grades to the same numbers; the digest travels with the run status so
	// the platform can bind it into the signed score payload and publish the
	// bytes. A hashing failure is logged, never fatal: the score itself does not
	// depend on the artifact.
	tArtifact := transcriptArtifact{
		RunID:         runID,
		Seed:          seed,
		BenchVersion:  artifact.BenchVersion,
		DatasetSHA256: datasetHash,
		Cases:         transcripts,
		ModelRelay:    relayExecution,
	}
	if harnessProjection != nil {
		projectionSHA, projectionBody, projectionErr := projectionReplayArtifact(harnessProjection.Manifest)
		if projectionErr != nil {
			s.store.Fail(runID, "v9 private projection artifact encoding failed")
			return
		}
		tArtifact.ProjectionSHA256 = projectionSHA
		privateDir := strings.TrimSpace(os.Getenv("DITTOBENCH_PRIVATE_ARTIFACT_DIR"))
		if err := writePrivateProjectionArtifact(privateDir, runID, projectionBody); err != nil {
			s.store.Fail(runID, "v9 private projection artifact persistence failed")
			return
		}
	}
	if tSHA, tBody, tErr := tArtifact.canonicalBytes(); tErr != nil {
		if req.BenchVersion >= protocol.BenchVersionV9 {
			s.store.Fail(runID, "v9 transcript evidence hashing failed")
			return
		}
		log.Printf("run %s: transcript hashing failed: %v", runID, tErr)
	} else {
		if requiresV9BaseEvidence(req) {
			report, tErr = applyV9BaseEvidence(report, req, perCase, transcripts, tokenUsage, relayExecution, tSHA)
			if tErr != nil {
				s.store.Fail(runID, tErr.Error())
				return
			}
		}
		s.store.SetTranscript(runID, tSHA, tBody)
		if dir := strings.TrimSpace(os.Getenv("DITTOBENCH_ARTIFACT_DIR")); dir != "" {
			if err := os.WriteFile(filepath.Join(dir, runID+".transcript.json"), tBody, 0o644); err != nil {
				log.Printf("run %s: transcript persist failed: %v", runID, err)
			}
		}
		log.Printf("run %s: transcript_sha256=%s (%d cases)", runID, tSHA, len(transcripts))
	}
	s.store.Finish(runID, report)
	log.Printf("run %s done: bench_version=%d composite=%.3f tool_mean=%.3f memory_mean=%.3f observed=%d capped=%d",
		runID, req.BenchVersion, report.Composite, report.ToolMean, report.MemoryMean, observedTool, cappedTool)
}

func loopbackHarnessSourceIP(rawURL string) (string, bool) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return "", false
	}
	ip := net.ParseIP(parsed.Hostname())
	if ip == nil || !ip.IsLoopback() {
		return "", false
	}
	return ip.String(), true
}

// applyTokenContract applies the bench version's token contract to a scored
// report:
//
//   - v5/v6: the absolute p90 token-waste transform (efficiency.Apply) may
//     discount the composite, exactly as historically.
//   - v7+ (quality-only contract): the composite is NEVER moved by token
//     usage. A neutral TokenEfficiency record still lands in the report so
//     the audited observed usage is first-class alongside the quality score
//     (details.token_usage carries the full metered block independently).
//   - pre-v5: untouched.
func applyTokenContract(report protocol.ScoreReport, benchVersion int, runSize string, tokenUsage protocol.TokenUsage) protocol.ScoreReport {
	if benchVersion < protocol.BenchVersionV5 {
		return report
	}
	rawComposite := report.Composite
	rawStderr := report.CompositeStderr
	var baseline *efficiency.Baseline
	if found, ok := efficiency.LookupForVersion(benchVersion, runSize, tokenUsage); ok {
		baseline = &found
	}
	decision := efficiency.ApplyForVersion(benchVersion, report, tokenUsage, baseline)
	decision.RawCompositeStderr = rawStderr
	decision.AdjustedCompositeStderr = math.Round(rawStderr*decision.Multiplier*1e6) / 1e6
	report.RawComposite = rawComposite
	report.Composite = decision.AdjustedComposite
	report.CompositeStderr = decision.AdjustedCompositeStderr
	report.Details.TokenEfficiency = &decision
	return report
}

func validateBenchVersionResult(requested, artifactVersion int, details *protocol.RunDetails) error {
	if requested != artifactVersion {
		return fmt.Errorf("bench_version contradiction: request=%d artifact=%d", requested, artifactVersion)
	}
	if details == nil || details.BenchVersion != requested {
		reported := 0
		if details != nil {
			reported = details.BenchVersion
		}
		return fmt.Errorf("bench_version contradiction: request=%d report=%d", requested, reported)
	}
	return nil
}

// finishSandboxRun runs deferred, after the scored path has returned. Its job is
// to attach post-mortem sandbox diagnostics to a failed run and stop the
// container -- NOT to decide whose fault the failure was. The scored path is the
// only code with the context to classify: it knows whether the relay preflight
// failed, whether the platform revoked this run's inference grant, or whether
// the harness itself misbehaved, and it records that verdict via FailWith before
// returning.
//
// So a classification already on the job is authoritative and is preserved here.
// This used to overwrite it unconditionally with the generic
// sandbox_failure/sandbox_runtime/retryable=false envelope, which silently
// converted every validator-side fault into an agent fault. Downstream that is
// not cosmetic: ditto-subnet's _sandbox_infrastructure_failure_code() accepts a
// failure only when kind == "validator_infrastructure" AND retryable is true, so
// a clobbered run fell through to DittobenchError -> fail_job("scoring_error"),
// which spends one of the miner's finite attempts and imposes a 6h cooldown for
// an outage the miner did not cause. A run killed by a platform grant denial --
// correctly diagnosed by #103 as "lease revoked ... not an upstream provider
// fault" -- was still being billed to the agent, because the diagnosis was
// discarded three frames later on this path.
//
// Direct sandbox resource evidence (OOM, tmpfs exhaustion) still wins over a
// prior verdict. That is not a downgrade: those codes are themselves
// validator_infrastructure, and physical evidence from the cgroup is strictly
// better than an inference drawn mid-run.
func (s *server) finishSandboxRun(runID string, handle *sandbox.Handle) {
	job, ok := s.store.Get(runID)
	if ok && job.Status == store.StatusFailed {
		diagnostics := s.sandbox.Diagnostics(context.Background(), handle)
		// Read the container's own output BEFORE the Stop below removes it.
		// `docker logs` cannot read a removed container, so every prior failed
		// benchmark run destroyed this evidence on the way out. It is the whole
		// reason a submission could die 12 seconds into a 90-minute lease across
		// four validators and leave neither the miner nor an operator anything to
		// read but "exit_code=1 oom_killed=false".
		logTail := s.sandbox.Logs(context.Background(), handle)
		failure := &store.Failure{
			Kind:      "sandbox_failure",
			Code:      "sandbox_runtime",
			Retryable: false,
			Diagnostics: map[string]any{
				"running":       diagnostics.Running,
				"oom_killed":    diagnostics.OOMKilled,
				"exit_code":     diagnostics.ExitCode,
				"memory_events": diagnostics.MemoryEvents,
			},
		}
		if diagnostics.MemoryPeakBytes != nil {
			failure.Diagnostics["memory_peak_bytes"] = *diagnostics.MemoryPeakBytes
		}
		if diagnostics.TmpfsUsedBytes != nil {
			failure.Diagnostics["tmpfs_used_bytes"] = *diagnostics.TmpfsUsedBytes
		}
		if diagnostics.TmpfsCapacityBytes != nil {
			failure.Diagnostics["tmpfs_capacity_bytes"] = *diagnostics.TmpfsCapacityBytes
		}
		message := job.Error
		if code := diagnostics.InfrastructureCode(); code != "" {
			failure.Kind = "validator_infrastructure"
			failure.Code = code
			failure.Retryable = true
			message = "validator sandbox resource envelope exhausted"
			log.Printf(
				"run %s validator infrastructure failure code=%s oom=%t exit=%d",
				runID, code, diagnostics.OOMKilled, diagnostics.ExitCode,
			)
		} else if prior := job.Failure; prior != nil && prior.Kind != "" {
			// The scored path already reached a verdict. Keep it, and keep the
			// message that explains it; only fold in the sandbox diagnostics
			// this path exists to collect.
			failure.Kind = prior.Kind
			failure.Code = prior.Code
			failure.Retryable = prior.Retryable
			for key, value := range prior.Diagnostics {
				failure.Diagnostics[key] = value
			}
			log.Printf(
				"run %s preserving scored-path classification kind=%s code=%s retryable=%t",
				runID, prior.Kind, prior.Code, prior.Retryable,
			)
		}
		// Attached last so it survives the prior-verdict merge above regardless of
		// what the scored path put in its own diagnostics map, and so it rides
		// EVERY failed sandbox run -- pre-health exits, mid-run crashes, and
		// classified platform faults alike.
		//
		// It goes in the structured envelope rather than into `message`: the
		// message becomes ditto-subnet's DittobenchError text, which
		// errors.failure_detail truncates to 200 chars on its way to the ticket.
		// Prepending 2 KB of log there would push the exit code and code name off
		// the wire and turn the one machine-groupable field back into prose. The
		// envelope has no such cap and is what an operator reads from
		// GET /v1/runs/{id}.
		if logTail != "" {
			failure.Diagnostics["container_log_tail"] = logTail
			log.Printf("run %s container log tail (%d bytes):\n%s", runID, len(logTail), logTail)
		}
		s.store.FailWith(runID, message, failure)
	}
	s.sandbox.Stop(context.Background(), handle)
}

// withObservedTrajectory substitutes the validator-OBSERVED tool trajectory for
// the harness's self-reported ToolCalls before a memory case is graded. This is
// what makes injection-bait compliance unscrubable: a bait action-tool call
// recorded by the mock endpoint reaches the grader even if the harness deleted
// every trace of it from its response text. When nothing was observed (a
// harness that stubs tools locally, or a pure recall answer that called no
// served tool), the self-report is left untouched, so honest harnesses are
// unaffected.
func withObservedTrajectory(resp protocol.RunResponse, observed []protocol.ObservedToolCall) protocol.RunResponse {
	if len(observed) > 0 {
		resp.ToolCalls = observed
	}
	return resp
}

// startToolServer stands up the observed-execution mock tool endpoint on an
// dedicated broker route (Docker) or an ephemeral host port (local practice),
// serving h at POST /tool. It returns the URL the harness
// should use (reachable from where the harness runs) and a stop func. The URL is
// empty when the harness cannot reach our loopback port — a remote hosted
// harness_url with the SSRF guard on — in which case observable tool cases are
// scored capped (the harness simply won't call it). Listens on all interfaces so
// a Docker-sandboxed container reaches it via host.docker.internal.
type observedToolEndpoint struct {
	baseURL     string
	brokerRoute *registeredToolRoute
}

func (e observedToolEndpoint) available() bool { return e.baseURL != "" }

func (e observedToolEndpoint) forCase(caseID, userID string) string {
	if e.brokerRoute == nil || e.baseURL == "" {
		return e.baseURL
	}
	return e.brokerRoute.endpoint(e.baseURL, caseID, userID)
}

// Bench v8 and earlier keep the frozen source-IP-bound broker wire contract:
// one bare endpoint for the whole run, with no case capability in the URL or
// request body. Bench v9 adds the case/user capability that safely authorizes
// Docker Desktop's source-NAT fallback for local scoring.
func (s *server) startToolServer(
	h http.Handler,
	sandboxSourceIP string,
	benchVersion int,
) (endpoint observedToolEndpoint, stop func(), err error) {
	return s.startToolServerForSession(h, sandboxSourceIP, benchVersion, "")
}

func (s *server) startToolServerForSession(
	h http.Handler,
	sandboxSourceIP string,
	benchVersion int,
	inferenceSessionID string,
) (endpoint observedToolEndpoint, stop func(), err error) {
	if sandboxSourceIP != "" {
		requireCaseCapability := benchVersion >= protocol.BenchVersionV9
		provenanceSessionID := ""
		if benchVersion >= protocol.BenchVersionV10 {
			if inferenceSessionID == "" {
				return observedToolEndpoint{}, func() {}, fmt.Errorf("v10 tool provenance session unavailable")
			}
			provenanceSessionID = inferenceSessionID
		}
		route, unregister, registerErr := s.broker.registerToolWithProvenance(
			h, sandboxSourceIP, s.allowPrivate, requireCaseCapability, provenanceSessionID,
		)
		if registerErr != nil {
			return observedToolEndpoint{}, func() {}, registerErr
		}
		port := envIntDefault("DITTOBENCH_BROKER_PORT", 11436)
		endpoint = observedToolEndpoint{
			baseURL:     fmt.Sprintf("http://host.docker.internal:%d/v1/tools/%s/tool", port, route.id),
			brokerRoute: &route,
		}
		checkBaseURL := fmt.Sprintf("http://127.0.0.1:%d/v1/tools/%s/tool", port, route.id)
		if err := verifyToolEndpoint(route.healthEndpoint(checkBaseURL)); err != nil {
			unregister()
			return observedToolEndpoint{}, func() {}, err
		}
		return endpoint, unregister, nil
	}
	// Bind IPv4 explicitly: a container reaching the host via host.docker.internal
	// (Docker Desktop's host-gateway) connects over IPv4, and a Go dual-stack "[::]"
	// listener is not reliably reachable that way on Docker Desktop/WSL2. tcp4 makes
	// the tool endpoint reachable for a containerized harness; Docker networking is
	// IPv4, so this loses nothing.
	ln, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		return observedToolEndpoint{}, func() {}, err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	mux := http.NewServeMux()
	mux.HandleFunc("GET /tool", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.Handle("POST /tool", h)
	srv := &http.Server{Handler: mux}
	go func() {
		if serveErr := srv.Serve(ln); serveErr != nil && serveErr != http.ErrServerClosed {
			log.Printf("tool endpoint serve error: %v", serveErr)
		}
	}()
	stop = func() { _ = srv.Close() }

	switch {
	case s.allowPrivate:
		// Local dev: the harness runs on the same host as the validator. A
		// CONTAINERIZED local harness (e.g. a miner practicing their own image via
		// harness_url) cannot reach the validator's 127.0.0.1, so DITTOBENCH_TOOL_HOST
		// lets the operator advertise a container-reachable host (host.docker.internal)
		// for tool observation. Defaults to loopback for a same-host process harness.
		host := envOr("DITTOBENCH_TOOL_HOST", "127.0.0.1")
		endpoint.baseURL = fmt.Sprintf("http://%s:%d/tool", host, port)
	default:
		// Hosted practice with a remote harness_url: it cannot reach our loopback
		// port. Leave the endpoint unadvertised; observable cases score capped.
		endpoint.baseURL = ""
	}
	if err := verifyToolEndpoint(fmt.Sprintf("http://127.0.0.1:%d/tool", port)); err != nil {
		stop()
		return observedToolEndpoint{}, func() {}, err
	}
	return endpoint, stop, nil
}

// verifyToolEndpoint checks the validator-owned listener without asking the
// miner harness to implement or execute a synthetic probe case. A failed check
// is a platform fault and aborts the run; a healthy endpoint that receives no
// scored-case calls is the harness's own observable result.
func verifyToolEndpoint(endpoint string) error {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(endpoint)
	if err != nil {
		return fmt.Errorf("tool endpoint self-check failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("tool endpoint self-check returned HTTP %d", resp.StatusCode)
	}
	return nil
}

func toolEndpointInfrastructureFailure() *store.Failure {
	// Reuse the fleet-recognized network code. Older validators deliberately
	// reject unknown infrastructure codes, which would charge the miner for a
	// scorer rollout they predate.
	return &store.Failure{
		Kind:      "validator_infrastructure",
		Code:      "sandbox_network_unavailable",
		Retryable: true,
	}
}

func (s *server) registerRunCancel(runID string, cancel context.CancelFunc) {
	s.cancelMu.Lock()
	defer s.cancelMu.Unlock()
	if s.runCancels == nil {
		s.runCancels = make(map[string]context.CancelFunc)
	}
	s.runCancels[runID] = cancel
}

func (s *server) unregisterRunCancel(runID string) {
	s.cancelMu.Lock()
	defer s.cancelMu.Unlock()
	delete(s.runCancels, runID)
}

// failAgentInferenceRun ends the benchmark as soon as the platform's typed
// response proves the harness spent its immutable request/token allowance.
// Waiting for final relay accounting used to let every remaining case send the
// same doomed request, producing hundreds of declines and occupying a scorer
// slot long after the run could no longer succeed.
func (s *server) failAgentInferenceRun(runID string) {
	failure := relayFinalizeFailure(errAgentInferenceDeclined)
	_, _, transitioned := s.store.FailIfActive(
		runID,
		"harness exhausted its inference allowance: the platform refused an agent-attributable inference request",
		failure,
	)
	if !transitioned {
		return
	}
	s.cancelMu.Lock()
	cancel := s.runCancels[runID]
	s.cancelMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *server) handleCancelRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job, ok := s.store.Get(id)
	if !ok {
		writeError(w, http.StatusNotFound, "run not found")
		return
	}
	if job.Status == store.StatusDone || job.Status == store.StatusFailed {
		writeJSON(w, http.StatusOK, job)
		return
	}

	s.cancelMu.Lock()
	cancel := s.runCancels[id]
	s.cancelMu.Unlock()
	if cancel == nil {
		// The worker unregisters its cancel function after publishing its terminal
		// state. Re-read so a finish racing this DELETE remains an idempotent 200.
		if job, ok = s.store.Get(id); ok && (job.Status == store.StatusDone || job.Status == store.StatusFailed) {
			writeJSON(w, http.StatusOK, job)
			return
		}
		writeError(w, http.StatusConflict, "run is not cancellable")
		return
	}
	job, _, transitioned := s.store.CancelIfActive(id, "run cancelled by client")
	if !transitioned {
		writeJSON(w, http.StatusOK, job)
		return
	}
	cancel()
	writeJSON(w, http.StatusAccepted, job)
}

func (s *server) handleGetRun(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job, ok := s.store.Get(id)
	if !ok {
		writeError(w, http.StatusNotFound, "run not found")
		return
	}
	writeJSON(w, http.StatusOK, job)
}

// handleGetTranscript serves a completed run's canonical transcript artifact —
// the graded per-case inputs whose SHA-256 is published as the run's
// transcript_sha256. Anyone holding these bytes plus the seed-regenerated
// dataset can re-run the public grader and reproduce the score offline.
func (s *server) handleGetTranscript(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job, ok := s.store.Get(id)
	if !ok {
		writeError(w, http.StatusNotFound, "run not found")
		return
	}
	if len(job.Transcript) == 0 {
		writeError(w, http.StatusNotFound, "run has no transcript (not finished, failed, or pre-transcript)")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Transcript-SHA256", job.TranscriptSHA256)
	w.WriteHeader(http.StatusOK)
	w.Write(job.Transcript)
}

// ---- small utils ----

func parseIntDefault(s string, def int) int {
	if s == "" {
		return def
	}
	if v, err := strconv.Atoi(s); err == nil {
		return v
	}
	return def
}

// freshSeed returns a non-deterministic seed for a new evaluation. Uses both
// wall-clock and a random component so concurrent requests don't collide.
func freshSeed() int64 {
	return time.Now().UnixNano() ^ int64(rand.Uint64())
}

// pinnedOrFreshSeed returns the request's pinned seed when non-zero (so a run is
// reproducible via {"seed":N}), else a fresh non-negative crypto-random seed.
// Shared by the direct, sandbox, and run_size submit paths so seed semantics are
// identical across them.
func pinnedOrFreshSeed(pinned int64) int64 {
	if pinned != 0 {
		return pinned
	}
	return gen.FreshSeed()
}

// envBool reports whether an env var is set to a truthy value.
func envBool(name string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// envOr returns the env var value or def when unset/blank.
func envOr(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

func (s *server) waitSandboxHealthy(
	ctx context.Context, handle *sandbox.Handle, timeout time.Duration,
) error {
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		if err := ctx.Err(); err != nil {
			return err
		}
		if last = runner.Health(ctx, handle.BaseURL); last == nil {
			return nil
		}
		diagnostics := s.sandbox.Diagnostics(ctx, handle)
		if diagnostics.StateKnown && !diagnostics.Running {
			return fmt.Errorf(
				"harness exited before health: exit_code=%d oom_killed=%t",
				diagnostics.ExitCode,
				diagnostics.OOMKilled,
			)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
	if last == nil {
		last = fmt.Errorf("timeout")
	}
	return fmt.Errorf("harness not healthy after %s: %w", timeout, last)
}

const (
	platformLockedProvider = "platform"
	// v8CompatLockedProvider is the pre-cutover starter harness's generic
	// OpenAI-compatible adapter name. Older already-admitted Bench v8 images
	// implement this arm, while current images implement `platform`.
	// The name does not select the public Chutes service: every URL below points
	// at the ticket-bound local broker and the key is a non-secret placeholder.
	v8CompatLockedProvider = "chutes"
	brokerPlaceholderKey   = "ticket"
)

var v8CompatBaseURLKeys = []string{
	"OPENAI_BASE_URL",
	"OPENAI_API_BASE",
	"OPENROUTER_BASE_URL",
}

// lockedEnvKeys are the sandbox env vars the model lock owns. A caller-supplied
// req.Env may not set any of these — otherwise a miner could route around the
// locked model (point at OpenRouter, Chutes, or any OpenAI-compatible host, swap
// the model id, or redirect the gateway URL). Every provider selector any
// supported crate honors must be listed here.
var lockedEnvKeys = map[string]bool{
	"OPENROUTER_API_KEY":            true,
	"DITTOBENCH_PROVIDER":           true,
	"DITTOBENCH_MODEL":              true,
	"OLLAMA_BASE_URL":               true,
	"CHUTES_API_KEY":                true,
	"CHUTES_BASE_URL":               true,
	"OPENAI_API_KEY":                true,
	"OPENAI_BASE_URL":               true,
	"OPENAI_API_BASE":               true,
	"OPENROUTER_BASE_URL":           true,
	"DITTOBENCH_INFERENCE_BASE_URL": true,
	"DITTOBENCH_DB":                 true,
}

// sandboxRuntimeEnv applies filesystem invariants shared by practice and
// canonical scoring without changing the practice endpoint's provider env.
func sandboxRuntimeEnv(reqEnv map[string]string) map[string]string {
	env := make(map[string]string, len(reqEnv)+1)
	for key, value := range reqEnv {
		if key != "DITTOBENCH_DB" {
			env[key] = value
		}
	}
	env["DITTOBENCH_DB"] = "/tmp/dittobench.db"
	return env
}

// harnessSandboxEnv builds the env for the miner sandbox container.
//
// The harness is scored against ONE locked open-weight model (llm.HarnessModel)
// served by the host gateway. No provider key is forwarded — the sandbox cannot
// reach any LLM but the locked gateway (the egress allowlist admits only the
// gateway upstream), so model choice is not an attack surface and the median of
// the k=3 validators' scores is comparable. The locked provider/model/gateway
// are applied AFTER the caller-supplied env and the caller's attempts to set any
// lockedEnvKey are dropped, so req.Env can never override the lock.
//
// V8 starts with the current starter kit's explicit platform adapter. The
// scored path may retry the SAME screened image once with the compatibility
// adapter when a bounded model-route probe proves the image made zero broker
// calls. That preserves already-admitted v8 images without restoring any
// pre-v8 benchmark path.
func harnessSandboxEnv(reqEnv map[string]string, benchVersion int, inferenceSessionID ...string) map[string]string {
	return harnessSandboxEnvForProvider(reqEnv, benchVersion, platformLockedProvider, inferenceSessionID...)
}

func harnessSandboxEnvForProvider(reqEnv map[string]string, benchVersion int, provider string, inferenceSessionID ...string) map[string]string {
	embeddingGateway := envOr("HARNESS_EMBED_URL", "http://host.docker.internal:11434")
	gateway := envOr("HARNESS_GATEWAY_URL", "http://host.docker.internal:11434")
	if len(inferenceSessionID) > 0 && inferenceSessionID[0] != "" {
		gateway = harnessGateway(inferenceSessionID[0])
		brokerPort := envIntDefault("DITTOBENCH_BROKER_PORT", 11436)
		embeddingGateway = "http://host.docker.internal:" + strconv.Itoa(brokerPort)
	}
	env := map[string]string{}
	if benchVersion < protocol.BenchVersionV9 {
		for k, v := range sandboxRuntimeEnv(reqEnv) {
			if lockedEnvKeys[k] {
				continue // the lock owns these; callers cannot set them
			}
			env[k] = v
		}
	}
	// The lock is applied last so it wins over caller env. No platform credential
	// enters the sandbox; the source-bound broker route is the capability.
	env["DITTOBENCH_PROVIDER"] = provider
	env["DITTOBENCH_INFERENCE_BASE_URL"] = gateway
	// Expose every historical OpenAI-compatible environment spelling in both
	// selector modes. They are aliases of the same source-bound ticket broker,
	// not additional providers, and the placeholder keys authorize nothing.
	// This lets harnesses that read generic SDK env directly route correctly on
	// the first boot; only images that actually branch on the singular provider
	// selector need the bounded compatibility restart below.
	env["CHUTES_BASE_URL"] = gateway
	env["CHUTES_API_KEY"] = brokerPlaceholderKey
	for _, key := range v8CompatBaseURLKeys {
		env[key] = gateway
	}
	env["OPENAI_API_KEY"] = brokerPlaceholderKey
	env["OPENROUTER_API_KEY"] = brokerPlaceholderKey
	env["DITTOBENCH_MODEL"] = llm.HarnessModelForVersion(benchVersion)
	env["OLLAMA_BASE_URL"] = embeddingGateway
	// The production sandbox has a read-only root and exposes exactly one
	// bounded writable filesystem at /tmp. Force the standard harness database
	// there so an image cannot pass screening as root and then fail to boot as
	// the validator's unprivileged UID.
	env["DITTOBENCH_DB"] = "/tmp/dittobench.db"
	return env
}

func harnessSandboxEnvWithCapability(
	reqEnv map[string]string,
	benchVersion int,
	provider string,
	inferenceSessionID string,
	capability string,
) (map[string]string, error) {
	if strings.TrimSpace(inferenceSessionID) == "" || !canonicalBrokerSourceCapability(capability) {
		return nil, errors.New("broker source capability is unavailable")
	}
	host, err := brokerSourceCapabilityHost(capability)
	if err != nil {
		return nil, errors.New("broker source capability is unavailable")
	}
	port := envIntDefault("DITTOBENCH_BROKER_PORT", 11436)
	base := "http://" + host + ":" + strconv.Itoa(port)
	env := harnessSandboxEnvForProvider(reqEnv, benchVersion, provider, inferenceSessionID)
	gateway := base + "/v1/inference"
	env["DITTOBENCH_INFERENCE_BASE_URL"] = gateway
	env["CHUTES_BASE_URL"] = gateway
	for _, key := range v8CompatBaseURLKeys {
		env[key] = gateway
	}
	env["CHUTES_API_KEY"] = capability
	env["OPENAI_API_KEY"] = capability
	env["OPENROUTER_API_KEY"] = capability
	env["OLLAMA_BASE_URL"] = base
	return env, nil
}

// probeHarnessModelRoute sends one isolated, discarded request through the
// harness and asks the ticket broker whether the harness actually reached it.
// A healthy /run response is not sufficient: the regression this guards
// against returned plausible deterministic answers while making zero model
// calls. The broker counters are validator-owned, so miner output cannot forge
// a positive route result.
func (s *server) probeHarnessModelRoute(
	ctx context.Context,
	harnessURL string,
	inferenceSessionID string,
	start relayHealthSnapshot,
	tools []protocol.ToolDefinition,
	benchVersion int,
) (relayHealthSnapshot, bool, error) {
	_, _, _ = runner.RunCaseWithTelemetry(
		ctx,
		harnessURL,
		scorer.ModelRoutePreflightCaseID,
		"Use the configured chat model and reply with exactly OK.",
		tools,
		runner.CaseOptions{UserID: scorer.ModelRoutePreflightCaseID, BenchVersion: benchVersion},
	)
	end, err := s.broker.snapshot(inferenceSessionID)
	if err != nil {
		return relayHealthSnapshot{}, false, fmt.Errorf("model-route broker snapshot unavailable: %w", err)
	}
	if err := relayDegradedSince(start, end); err != nil {
		return relayHealthSnapshot{}, false, err
	}
	return end, end.Requests > start.Requests, nil
}

// requireCompleteModelUsageAfterRun closes the only ambiguous v8 accounting
// shape: a complete interval with no broker-observed chat attempt. Sessionless
// development runs use the shared relay's start/end health snapshots directly;
// there is no ticket broker route to re-probe, so their original terminal
// agent attribution is already complete.
//
// Ticket-scoped runs repeat the discarded route challenge after an all-zero
// scored interval. An observed broker degradation remains retryable validator
// infrastructure. A healthy broker snapshot with no challenge request does
// not: the harness controls whether it honors /run and could otherwise mint a
// no-fault retry merely by recognizing and withholding the documented probe.
func (s *server) requireCompleteModelUsageAfterRun(
	ctx context.Context,
	harnessURL string,
	inferenceSessionID string,
	start relayHealthSnapshot,
	tools []protocol.ToolDefinition,
	benchVersion int,
	usage protocol.TokenUsage,
	execution relayExecutionSummary,
) error {
	usageErr := requireCompleteV7Usage(benchVersion, usage, execution)
	if !errors.Is(usageErr, errAgentModelUseMissing) {
		return usageErr
	}
	if inferenceSessionID == "" {
		return usageErr
	}
	_, routed, probeErr := s.probeHarnessModelRoute(
		ctx,
		harnessURL,
		inferenceSessionID,
		start,
		tools,
		benchVersion,
	)
	if probeErr != nil {
		return fmt.Errorf("post-run model-route probe failed: %w", probeErr)
	}
	if !routed {
		return fmt.Errorf(
			"%w: post-run model-route probe did not reach the healthy ticket broker",
			usageErr,
		)
	}
	return usageErr
}

func harnessGateway(inferenceSessionID string) string {
	if inferenceSessionID != "" {
		brokerPort := envIntDefault("DITTOBENCH_BROKER_PORT", 11436)
		return "http://host.docker.internal:" + strconv.Itoa(brokerPort) + "/v1/inference"
	}
	return envOr("HARNESS_GATEWAY_URL", "http://host.docker.internal:11434")
}

// clientIP returns the caller's IP for rate-limiting, honoring the first hop of
// X-Forwarded-For (set by Cloud Run / proxies) and falling back to RemoteAddr.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i >= 0 {
			return strings.TrimSpace(xff[:i])
		}
		return strings.TrimSpace(xff)
	}
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}
