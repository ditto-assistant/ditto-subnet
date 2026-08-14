package inference

import (
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/chain"
	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/server"
)

// Wire timing constants (endpoints/inference.py).
const (
	exchangeMaxAge = 2 * time.Minute
	proxyMaxAge    = 30 * time.Second

	embeddingMaxInputs = 256
)

// Deps carries everything the three inference handlers need. Upstream is
// the ONE shared HTTP client for chat + embeddings (mirroring the single
// httpx.AsyncClient); it must never follow redirects.
type Deps struct {
	Cfg      *config.Config
	Logger   *slog.Logger
	Pool     *pgxpool.Pool
	Queries  *postgres.Queries
	Permits  chain.PermitChecker
	Upstream *http.Client
	Settings *SettingsResolver

	// Sleep is the provider-retry backoff hook (tests replace it).
	Sleep sleepFunc
	// Now is the clock (tests replace it).
	Now func() time.Time
}

func (d *Deps) sleep() sleepFunc {
	if d.Sleep != nil {
		return d.Sleep
	}
	return defaultSleep
}

func (d *Deps) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now().UTC()
}

// NewUpstreamClient builds the shared provider client: no redirects, a
// connection pool sized to the chat global concurrency (matching the httpx
// Limits(max_connections=global_concurrency) client), and NO client-level
// deadline — per-attempt timeouts are applied in postOnce, mirroring the
// per-operation httpx.Timeout.
func NewUpstreamClient(cfg config.InferenceProxyConfig) *http.Client {
	transport := &http.Transport{
		MaxIdleConns:        cfg.GlobalConcurrency,
		MaxIdleConnsPerHost: cfg.GlobalConcurrency,
		MaxConnsPerHost:     cfg.GlobalConcurrency,
	}
	return &http.Client{
		Transport: transport,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			// follow_redirects=False: surface the redirect response itself.
			return http.ErrUseLastResponse
		},
	}
}

// NewHandlers builds the server registration struct for the three routes.
// The routes are always mounted; each handler answers 404 "inference proxy
// is disabled" itself when the proxy is off, exactly like the Python app.
func NewHandlers(deps *Deps) *server.InferenceHandlers {
	return &server.InferenceHandlers{
		Exchange:        http.HandlerFunc(deps.handleExchange),
		ChatCompletions: http.HandlerFunc(deps.handleChatCompletions),
		Embeddings:      http.HandlerFunc(deps.handleEmbeddings),
	}
}

// writeHTTPError renders an httpError as the 3002 envelope.
func writeHTTPError(w http.ResponseWriter, r *http.Request, err *httpError) {
	var headers http.Header
	if len(err.headers) > 0 {
		headers = http.Header{}
		for k, v := range err.headers {
			headers.Set(k, v)
		}
	}
	relayhttp.WriteHTTPError(w, r, err.status, err.message, headers)
}

// parseHeaderDatetime accepts the ISO-8601 shapes Pydantic parses for a
// datetime header. Naive datetimes are interpreted as server-local
// (mirroring .astimezone(UTC) on a naive value); the caller is told whether
// the value carried a zone.
func parseHeaderDatetime(value string) (t time.Time, aware bool, err error) {
	value = strings.TrimSpace(value)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05Z07:00"} {
		if parsed, perr := time.Parse(layout, value); perr == nil {
			return parsed, true, nil
		}
	}
	for _, layout := range []string{"2006-01-02T15:04:05.999999999", "2006-01-02T15:04:05"} {
		if parsed, perr := time.ParseInLocation(layout, value, time.Local); perr == nil {
			return parsed, false, nil
		}
	}
	return time.Time{}, false, &httpError{status: 422, message: "request validation failed"}
}

// proxyHeaders are the six per-call auth headers of the two proxy routes.
type proxyHeaders struct {
	grant         uuid.UUID
	generation    int64
	nonce         uuid.UUID
	requestedAt   time.Time
	proof         string
	authorization string

	hasGrant, hasGeneration, hasNonce, hasRequestedAt, hasProof, hasAuthorization bool
}

func (h *proxyHeaders) anyMissing() bool {
	return !h.hasGrant || !h.hasGeneration || !h.hasNonce || !h.hasRequestedAt || !h.hasProof || !h.hasAuthorization
}

// parseProxyHeaders type-parses the headers that are present. A present but
// malformed value is a 422 (FastAPI parses headers before the endpoint body
// runs, so this precedes even the proxy-disabled 404); missing headers are
// reported via the presence flags and answered 401 by the caller.
func parseProxyHeaders(r *http.Request) (*proxyHeaders, bool) {
	h := &proxyHeaders{}
	if values, ok := r.Header["X-Ditto-Grant"]; ok && len(values) > 0 {
		parsed, err := uuid.Parse(strings.TrimSpace(values[0]))
		if err != nil {
			return nil, false
		}
		h.grant = parsed
		h.hasGrant = true
	}
	if values, ok := r.Header["X-Ditto-Generation"]; ok && len(values) > 0 {
		parsed, err := strconv.ParseInt(strings.TrimSpace(values[0]), 10, 64)
		if err != nil {
			return nil, false
		}
		h.generation = parsed
		h.hasGeneration = true
	}
	if values, ok := r.Header["X-Ditto-Nonce"]; ok && len(values) > 0 {
		parsed, err := uuid.Parse(strings.TrimSpace(values[0]))
		if err != nil {
			return nil, false
		}
		h.nonce = parsed
		h.hasNonce = true
	}
	if values, ok := r.Header["X-Ditto-Requested-At"]; ok && len(values) > 0 {
		parsed, _, err := parseHeaderDatetime(values[0])
		if err != nil {
			return nil, false
		}
		h.requestedAt = parsed
		h.hasRequestedAt = true
	}
	if values, ok := r.Header["X-Ditto-Proof"]; ok && len(values) > 0 {
		h.proof = strings.TrimSpace(values[0])
		h.hasProof = true
	}
	if values, ok := r.Header["Authorization"]; ok && len(values) > 0 {
		h.authorization = values[0]
		h.hasAuthorization = true
	}
	return h, true
}
