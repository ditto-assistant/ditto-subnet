// Package server wires the relay's HTTP surface: GET /health, GET /metrics,
// and the /api/v1/inference/* registration point. The mounted surface (and
// nothing else) mirrors the Python api_server factory under DITTO_ROLE=relay.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/ditto-assistant/model-relay/internal/chain"
	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

// InferenceHandlers is the registration point for the inference plane. The
// handlers are nil in the foundation; the follow-up inference-proxy change
// supplies them and they are mounted at EXACTLY these routes:
//
//	POST /api/v1/inference/exchange
//	POST /api/v1/inference/chat/completions
//	POST /api/v1/inference/embeddings
//	POST /api/v1/inference/confirmation/chat/completions
//	POST /api/v1/inference/confirmation/embeddings
//
// Handlers are plain http.Handlers; they receive the request-ID context from
// the middleware stack and are expected to write relayhttp envelopes for
// every error path.
type InferenceHandlers struct {
	Exchange                    http.Handler
	ChatCompletions             http.Handler
	Embeddings                  http.Handler
	ConfirmationChatCompletions http.Handler
	ConfirmationEmbeddings      http.Handler
}

// Server owns the relay HTTP surface and its dependencies.
type Server struct {
	cfg       *config.Config
	logger    *slog.Logger
	pool      *pgxpool.Pool
	prober    chain.Prober
	commit    string
	revisions *revisionCache
	inference *InferenceHandlers
}

// Option customizes a Server.
type Option func(*Server)

// WithInferenceHandlers mounts the inference plane (used by the follow-up
// proxy change and by tests).
func WithInferenceHandlers(h *InferenceHandlers) Option {
	return func(s *Server) { s.inference = h }
}

// WithCheckedOutResolver overrides how the on-disk revision is re-read
// (tests).
func WithCheckedOutResolver(resolve func() string) Option {
	return func(s *Server) { s.revisions = newRevisionCache(resolve) }
}

// New builds the Server. commit is the running process's revision, resolved
// once at boot (ResolveCommitHash).
func New(cfg *config.Config, logger *slog.Logger, pool *pgxpool.Pool, prober chain.Prober, commit string, opts ...Option) *Server {
	s := &Server{
		cfg:       cfg,
		logger:    logger,
		pool:      pool,
		prober:    prober,
		commit:    commit,
		revisions: newRevisionCache(ResolveCommitHash),
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// Handler builds the full middleware stack:
//
//	RequestID (outermost) → AuthPassThrough → Recover → SizedGzip → mux
//
// mirroring the Python registration order (RequestIDMiddleware outermost,
// AuthPassThroughMiddleware a literal no-op, SizedGZipMiddleware innermost).
// The Python PublicCache middleware is deliberately absent: it is inert on
// every relay route (GET /api/v1/public/* only), so only its pass-through
// exists here — which is no code at all.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	// methodByPath drives the envelope fallback below: FastAPI routes EVERY
	// unmatched path and method through the error envelope (error_code 3002)
	// and redirects trailing slashes with 307 (redirect_slashes), so a bare
	// Go ServeMux plain-text 404/405 would change the wire for clients that
	// parse the numeric error_code.
	methodByPath := map[string]string{}
	register := func(method, path string, h http.Handler) {
		mux.Handle(method+" "+path, h)
		methodByPath[path] = method
	}
	register(http.MethodGet, "/health", http.HandlerFunc(s.handleHealth))
	register(http.MethodGet, "/metrics", promhttp.HandlerFor(prometheus.DefaultGatherer, promhttp.HandlerOpts{}))
	s.registerInferenceRoutes(register)
	mux.Handle("/", envelopeFallback(methodByPath))

	var h http.Handler = mux
	h = relayhttp.SizedGzipMiddleware(h)
	h = relayhttp.RecoverMiddleware(s.logger, h)
	h = relayhttp.AuthPassThroughMiddleware(h)
	h = relayhttp.RequestIDMiddleware(s.logger, h)
	return h
}

// registerInferenceRoutes mounts the inference plane when handlers were
// supplied. The paths are part of the cross-role contract — never change
// them.
func (s *Server) registerInferenceRoutes(register func(method, path string, h http.Handler)) {
	if s.inference == nil {
		return
	}
	if s.inference.Exchange != nil {
		register(http.MethodPost, "/api/v1/inference/exchange", s.inference.Exchange)
	}
	if s.inference.ChatCompletions != nil {
		register(http.MethodPost, "/api/v1/inference/chat/completions", s.inference.ChatCompletions)
	}
	if s.inference.Embeddings != nil {
		register(http.MethodPost, "/api/v1/inference/embeddings", s.inference.Embeddings)
	}
	if s.inference.ConfirmationChatCompletions != nil {
		register(http.MethodPost, "/api/v1/inference/confirmation/chat/completions", s.inference.ConfirmationChatCompletions)
	}
	if s.inference.ConfirmationEmbeddings != nil {
		register(http.MethodPost, "/api/v1/inference/confirmation/embeddings", s.inference.ConfirmationEmbeddings)
	}
}

// envelopeFallback answers everything the method-qualified patterns did not
// match, mirroring Starlette's router: a known path with the wrong method is
// 405 "Method Not Allowed" (with Allow) in the JSON envelope, a known path
// with a trailing slash is a 307 redirect (FastAPI redirect_slashes), and
// everything else is a 404 "Not Found" envelope.
func envelopeFallback(methodByPath map[string]string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if method, known := methodByPath[path]; known {
			w.Header().Set("Allow", method)
			relayhttp.WriteHTTPError(w, r, http.StatusMethodNotAllowed, "Method Not Allowed", nil)
			return
		}
		if trimmed := strings.TrimSuffix(path, "/"); trimmed != path {
			if _, known := methodByPath[trimmed]; known {
				redirect := *r.URL
				redirect.Path = trimmed
				w.Header().Set("Location", redirect.String())
				w.WriteHeader(http.StatusTemporaryRedirect)
				return
			}
		}
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "Not Found", nil)
	})
}

// healthResponse mirrors the Python HealthResponse model field-for-field.
type healthResponse struct {
	Status           string `json:"status"`
	DB               string `json:"db"`
	Chain            string `json:"chain"`
	Commit           string `json:"commit"`
	CheckedOutCommit string `json:"checked_out_commit"`
	CommitDrift      bool   `json:"commit_drift"`
}

// handleHealth probes DB (SELECT 1) and chain (latest block) per request;
// 503 when either is down, body always returned. Failures are logged as
// warnings without stack traces (Prometheus scrape rate would flood logs).
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	dbStatus := "ok"
	if _, err := s.pool.Exec(ctx, "SELECT 1"); err != nil {
		s.logger.Warn("health probe: db unreachable", slog.String("error", err.Error()))
		dbStatus = "down"
	}

	chainStatus := "ok"
	if err := s.prober.ProbeLatestBlock(ctx); err != nil {
		s.logger.Warn("health probe: chain unreachable", slog.String("error", err.Error()))
		chainStatus = "down"
	}

	overall := "ok"
	status := http.StatusOK
	if dbStatus != "ok" || chainStatus != "ok" {
		overall = "down"
		status = http.StatusServiceUnavailable
	}

	checkedOut := s.revisions.checkedOut()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(healthResponse{
		Status:           overall,
		DB:               dbStatus,
		Chain:            chainStatus,
		Commit:           s.commit,
		CheckedOutCommit: checkedOut,
		CommitDrift:      commitsDiverged(s.commit, checkedOut),
	})
}

// Run serves until ctx is canceled, then drains with a bounded graceful
// shutdown. It returns nil on a clean shutdown.
func (s *Server) Run(ctx context.Context) error {
	addr := net.JoinHostPort(s.cfg.Host, strconv.Itoa(s.cfg.Port))
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		s.logger.Info("model-relay listening", slog.String("addr", addr))
		errCh <- srv.ListenAndServe()
	}()

	select {
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-ctx.Done():
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), s.drainTimeout())
	defer cancel()
	s.logger.Info("model-relay shutting down",
		slog.Duration("drain_timeout", s.drainTimeout()))
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return err
	}
	return nil
}

// drainTimeout bounds the graceful shutdown. Provider reads can legitimately
// run for the configured inference timeout (up to 120s), and the pm2 slot
// roll budgets kill_timeout=135000 in ecosystem.config.js precisely so a
// draining slot can finish its in-flight inference requests while Caddy sends
// new calls to the sibling. The drain therefore outlasts the inference
// timeout (plus margin), floored at the Python relay's
// timeout_graceful_shutdown=30, and stays inside pm2's 135s SIGKILL budget.
func (s *Server) drainTimeout() time.Duration {
	d := time.Duration(s.cfg.Inference.TimeoutSeconds)*time.Second + 5*time.Second
	if d < 30*time.Second {
		return 30 * time.Second
	}
	return d
}
