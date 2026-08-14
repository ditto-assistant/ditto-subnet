// Package config parses the model-relay's environment.
//
// The Go relay is a drop-in replacement for the Python platform's
// DITTO_ROLE=relay process, so it reads EXACTLY the environment variable
// names the Python relay reads (sourced from /opt/ditto-subnet/apps/platform/.env
// and .env.deploy on the host). A host cutover must need no env changes.
//
// Requiredness policy:
//   - Variables the relay actually consumes are validated as strictly as the
//     Python parser validates them (missing required values and unparsable
//     numerics fail boot loudly; there are no placeholder defaults for
//     required values).
//   - Platform-only variables (STORAGE_*, pricing, screener, dashboard,
//     efficiency-bonus knobs, ...) are tolerated and ignored: the relay never
//     reads them, and their presence or absence never affects boot.
package config

import (
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
)

// Role values accepted by the relay binary. The Python api_server accepts
// "platform" too; this binary IS the relay, so "platform" is a deploy error.
const RoleRelay = "relay"

var ss58Pattern = regexp.MustCompile(`^[1-9A-HJ-NP-Za-km-z]{47,48}$`)

// Embedding identity is pinned: the v7 contract froze the embedding space,
// and the Python check_config fails boot on any deviation. Mirrored here.
const (
	PinnedEmbeddingModel      = "perplexity/pplx-embed-v1-0.6b"
	PinnedEmbeddingProfile    = "dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1"
	PinnedEmbeddingProvider   = "Perplexity"
	PinnedEmbeddingDimensions = 768
)

// Routing modes accepted by DITTO_INFERENCE_ROUTING_MODE.
const (
	RoutingModeAggregateThroughput = "aggregate_throughput"
	RoutingModeAdaptive            = "adaptive"
)

// Lookup resolves one environment variable, reporting presence. It matches
// os.LookupEnv so tests can substitute a map.
type Lookup func(key string) (string, bool)

// MapLookup adapts a map to a Lookup (test helper, but exported because the
// pg test harness uses it too).
func MapLookup(m map[string]string) Lookup {
	return func(key string) (string, bool) {
		v, ok := m[key]
		return v, ok
	}
}

// Config is everything the relay reads from the environment.
type Config struct {
	// Role is DITTO_ROLE. Empty or "relay" accepted; anything else fails boot.
	Role string

	// API server bind address: API_HOST / API_PORT — the same host/port env
	// the Python relay listens on.
	Host string
	Port int

	// LogLevel is API_LOG_LEVEL (a stdlib logging level name in Python;
	// mapped onto slog levels here). Invalid names fail boot.
	LogLevel string

	Postgres  PostgresConfig
	Chain     ChainConfig
	Inference InferenceProxyConfig
	Upload    UploadConfig
}

// UploadConfig is the narrow upload-admission surface served by the relay.
// Finalized-payment recovery is forwarded to the Python platform until the
// historical Substrate verifier is ported; ordinary checks stay in Go.
type UploadConfig struct {
	PaymentAddress      string
	MaxTarballSizeBytes int64
	LegacyBaseURL       string
}

// PostgresConfig mirrors ditto/db/config.py::parse_postgres_config_from_env.
type PostgresConfig struct {
	Host           string  // POSTGRES_HOST, default localhost
	Port           int     // POSTGRES_PORT, default 5432
	User           string  // POSTGRES_USER, REQUIRED
	Password       string  // POSTGRES_PASSWORD, REQUIRED
	Database       string  // POSTGRES_DB, REQUIRED
	PoolMinSize    int32   // POSTGRES_POOL_MIN_SIZE, default 2
	PoolMaxSize    int32   // POSTGRES_POOL_MAX_SIZE, default 10
	CommandTimeout float64 // POSTGRES_COMMAND_TIMEOUT seconds, default 30.0
}

// DSN renders the plain (non-SQLAlchemy) connection string with
// user/password/database percent-encoded, exactly like the Python
// parse_postgres_config_from_env (urllib.parse.quote(value, safe="")).
func (p PostgresConfig) DSN() string {
	esc := func(s string) string { return url.QueryEscape(s) }
	// url.QueryEscape encodes " " as "+"; Python's quote uses %20. Normalize.
	fix := func(s string) string { return strings.ReplaceAll(s, "+", "%20") }
	return fmt.Sprintf("postgresql://%s:%s@%s:%d/%s",
		fix(esc(p.User)), fix(esc(p.Password)), p.Host, p.Port, fix(esc(p.Database)))
}

// ChainConfig is the Pylon chain-client configuration; the relay needs it for
// the /health chain probe and the /exchange validator-permit check.
type ChainConfig struct {
	PylonURL string // PYLON_URL, default http://localhost:8001
	Netuid   int    // NETUID, default 118
	Network  string // SUBTENSOR_NETWORK, default finney

	// Auth: PYLON_OPEN_ACCESS_TOKEN, or the pair PYLON_IDENTITY_NAME +
	// PYLON_IDENTITY_TOKEN. One of the two forms is REQUIRED, matching the
	// Python boot behavior.
	OpenAccessToken string
	IdentityName    string
	IdentityToken   string

	// DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR (dev-only permit bypass; the
	// exchange path must refuse it on finney/mainnet).
	DevAllowUnpermittedValidator bool
}

// InferenceProxyConfig mirrors the Python InferenceProxyConfig (section 10 of
// the DB contract / section 9 of the API contract). All limits validated with
// the same bounds; non-numeric numerics fail boot.
type InferenceProxyConfig struct {
	Enabled       bool   // DITTO_INFERENCE_PROXY_ENABLED, default false
	Required      bool   // DITTO_INFERENCE_PROXY_REQUIRED, default false
	PublicBaseURL string // DITTO_INFERENCE_PUBLIC_BASE_URL, default http://localhost:8000

	OpenRouterAPIKey string // OPENROUTER_API_KEY; REQUIRED when Enabled
	PerplexityAPIKey string // PERPLEXITY_API_KEY; optional

	UpstreamURL          string // DITTO_INFERENCE_UPSTREAM_URL
	EmbeddingUpstreamURL string // DITTO_EMBEDDING_UPSTREAM_URL
	EmbeddingFallbackURL string // DITTO_EMBEDDING_FALLBACK_URL

	AllowedModels []string // DITTO_INFERENCE_ALLOWED_MODELS csv, 1..4 entries
	Provider      string   // DITTO_INFERENCE_PROVIDER, default nebius
	RoutingMode   string   // DITTO_INFERENCE_ROUTING_MODE

	RequestBudget int   // DITTO_INFERENCE_REQUEST_BUDGET, default 8192, max 16384
	TokenBudget   int64 // DITTO_INFERENCE_TOKEN_BUDGET, default 25_000_000, max 100_000_000

	TicketConcurrency    int // DITTO_INFERENCE_TICKET_CONCURRENCY, default 8
	ValidatorConcurrency int // DITTO_INFERENCE_VALIDATOR_CONCURRENCY, default 24
	GlobalConcurrency    int // DITTO_INFERENCE_GLOBAL_CONCURRENCY, default 72, cap 128
	TicketRPM            int // DITTO_INFERENCE_TICKET_RPM, default 240
	ValidatorRPM         int // DITTO_INFERENCE_VALIDATOR_RPM, default 960
	GlobalRPM            int // DITTO_INFERENCE_GLOBAL_RPM, default 2880, cap 100000

	RequestBodyBytes  int64 // DITTO_INFERENCE_REQUEST_BODY_BYTES, default 262144, max 1 MiB
	ResponseBodyBytes int64 // DITTO_INFERENCE_RESPONSE_BODY_BYTES, default 2 MiB, max 8 MiB
	TimeoutSeconds    int   // DITTO_INFERENCE_TIMEOUT_SECONDS, default 90, range [1,120]
	MaxOutputTokens   int   // DITTO_INFERENCE_MAX_OUTPUT_TOKENS, default 8192, max 32768

	EmbeddingModel      string // pinned, see PinnedEmbedding*
	EmbeddingProfile    string
	EmbeddingProvider   string
	EmbeddingDimensions int

	EmbeddingRequestBudget int   // DITTO_EMBEDDING_REQUEST_BUDGET, default+max 100000
	EmbeddingTokenBudget   int64 // DITTO_EMBEDDING_TOKEN_BUDGET, default+max 1e9

	EmbeddingTicketConcurrency    int // DITTO_EMBEDDING_TICKET_CONCURRENCY, default 12
	EmbeddingValidatorConcurrency int // DITTO_EMBEDDING_VALIDATOR_CONCURRENCY, default 48
	EmbeddingGlobalConcurrency    int // DITTO_EMBEDDING_GLOBAL_CONCURRENCY, default 96, cap 128
	EmbeddingTicketRPM            int // DITTO_EMBEDDING_TICKET_RPM, default 10000
	EmbeddingValidatorRPM         int // DITTO_EMBEDDING_VALIDATOR_RPM, default 40000
	EmbeddingGlobalRPM            int // DITTO_EMBEDDING_GLOBAL_RPM, default+cap 100000

	EmbeddingRequestBodyBytes  int64 // DITTO_EMBEDDING_REQUEST_BODY_BYTES, default+max 1 MiB
	EmbeddingResponseBodyBytes int64 // DITTO_EMBEDDING_RESPONSE_BODY_BYTES, default+max 16 MiB
}

type envReader struct {
	lookup Lookup
	errs   []string
}

func (r *envReader) fail(format string, args ...any) {
	r.errs = append(r.errs, fmt.Sprintf(format, args...))
}

func (r *envReader) raw(key string) (string, bool) {
	v, ok := r.lookup(key)
	if !ok {
		return "", false
	}
	return v, true
}

// str returns a trimmed value or the default when unset/blank.
func (r *envReader) str(key, def string) string {
	v, ok := r.raw(key)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	return strings.TrimSpace(v)
}

// required returns a trimmed value; unset or blank records a boot error.
func (r *envReader) required(key string) string {
	v, ok := r.raw(key)
	if !ok || strings.TrimSpace(v) == "" {
		r.fail("missing required environment variable %s", key)
		return ""
	}
	return strings.TrimSpace(v)
}

func (r *envReader) intval(key string, def int) int {
	v, ok := r.raw(key)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	n, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil {
		r.fail("%s must be an integer, got %q", key, v)
		return def
	}
	return n
}

func (r *envReader) int64val(key string, def int64) int64 {
	v, ok := r.raw(key)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
	if err != nil {
		r.fail("%s must be an integer, got %q", key, v)
		return def
	}
	return n
}

func (r *envReader) floatval(key string, def float64) float64 {
	v, ok := r.raw(key)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	n, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil {
		r.fail("%s must be a number, got %q", key, v)
		return def
	}
	return n
}

// truthy mirrors the Python parser: {1, true, yes, on} (case-insensitive).
func truthy(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func (r *envReader) boolval(key string, def bool) bool {
	v, ok := r.raw(key)
	if !ok || strings.TrimSpace(v) == "" {
		return def
	}
	return truthy(v)
}

var logLevels = map[string]struct{}{
	"CRITICAL": {}, "FATAL": {}, "ERROR": {}, "WARN": {}, "WARNING": {},
	"INFO": {}, "DEBUG": {}, "NOTSET": {},
}

// LoadFromEnv parses the process environment.
func LoadFromEnv() (*Config, error) { return Load(os.LookupEnv) }

// Load parses configuration through the given lookup. It collects every
// problem it finds and fails with all of them at once, so a botched host env
// is diagnosed in one boot attempt.
func Load(lookup Lookup) (*Config, error) {
	r := &envReader{lookup: lookup}
	cfg := &Config{}

	// Role: this binary only serves the relay role.
	role := strings.ToLower(r.str("DITTO_ROLE", RoleRelay))
	if role != RoleRelay {
		r.fail("DITTO_ROLE must be %q (or unset) for the model-relay binary, got %q", RoleRelay, role)
	}
	cfg.Role = RoleRelay

	cfg.Host = r.str("API_HOST", "0.0.0.0")
	cfg.Port = r.intval("API_PORT", 8000)
	if cfg.Port < 1 || cfg.Port > 65535 {
		r.fail("API_PORT out of range: %d", cfg.Port)
	}
	cfg.LogLevel = strings.ToUpper(r.str("API_LOG_LEVEL", "INFO"))
	if _, ok := logLevels[cfg.LogLevel]; !ok {
		r.fail("API_LOG_LEVEL is not a recognized level name: %q", cfg.LogLevel)
	}

	cfg.Postgres = PostgresConfig{
		Host:           r.str("POSTGRES_HOST", "localhost"),
		Port:           r.intval("POSTGRES_PORT", 5432),
		User:           r.required("POSTGRES_USER"),
		Password:       r.required("POSTGRES_PASSWORD"),
		Database:       r.required("POSTGRES_DB"),
		PoolMinSize:    int32(r.intval("POSTGRES_POOL_MIN_SIZE", 2)),
		PoolMaxSize:    int32(r.intval("POSTGRES_POOL_MAX_SIZE", 10)),
		CommandTimeout: r.floatval("POSTGRES_COMMAND_TIMEOUT", 30.0),
	}
	if cfg.Postgres.PoolMinSize < 0 || cfg.Postgres.PoolMaxSize < 1 ||
		cfg.Postgres.PoolMinSize > cfg.Postgres.PoolMaxSize {
		r.fail("POSTGRES_POOL_MIN_SIZE/POSTGRES_POOL_MAX_SIZE invalid: min=%d max=%d",
			cfg.Postgres.PoolMinSize, cfg.Postgres.PoolMaxSize)
	}

	cfg.Chain = ChainConfig{
		PylonURL:                     r.str("PYLON_URL", "http://localhost:8001"),
		Netuid:                       r.intval("NETUID", 118),
		Network:                      r.str("SUBTENSOR_NETWORK", "finney"),
		OpenAccessToken:              r.str("PYLON_OPEN_ACCESS_TOKEN", ""),
		IdentityName:                 r.str("PYLON_IDENTITY_NAME", ""),
		IdentityToken:                r.str("PYLON_IDENTITY_TOKEN", ""),
		DevAllowUnpermittedValidator: r.boolval("DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR", false),
	}
	if (cfg.Chain.IdentityName == "") != (cfg.Chain.IdentityToken == "") {
		r.fail("PYLON_IDENTITY_NAME and PYLON_IDENTITY_TOKEN must be provided together")
	}
	hasIdentityPair := cfg.Chain.IdentityName != "" && cfg.Chain.IdentityToken != ""
	if cfg.Chain.OpenAccessToken == "" && !hasIdentityPair {
		r.fail("Pylon auth is required: set PYLON_OPEN_ACCESS_TOKEN or both PYLON_IDENTITY_NAME and PYLON_IDENTITY_TOKEN")
	}

	cfg.Upload = UploadConfig{
		PaymentAddress:      r.required("DITTO_UPLOAD_PAYMENT_ADDRESS"),
		MaxTarballSizeBytes: r.int64val("DITTO_MAX_TARBALL_SIZE_BYTES", 20*1024*1024),
		LegacyBaseURL:       strings.TrimRight(r.str("DITTO_UPLOAD_LEGACY_BASE_URL", "http://127.0.0.1:8000"), "/"),
	}
	if cfg.Upload.PaymentAddress != "" && !ss58Pattern.MatchString(cfg.Upload.PaymentAddress) {
		r.fail("DITTO_UPLOAD_PAYMENT_ADDRESS does not look like an SS58 address")
	}
	if cfg.Upload.MaxTarballSizeBytes < 1 {
		r.fail("DITTO_MAX_TARBALL_SIZE_BYTES must be positive, got %d", cfg.Upload.MaxTarballSizeBytes)
	}
	if u, err := url.Parse(cfg.Upload.LegacyBaseURL); err != nil || !u.IsAbs() || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		r.fail("DITTO_UPLOAD_LEGACY_BASE_URL must be an absolute http/https URL, got %q", cfg.Upload.LegacyBaseURL)
	}

	cfg.Inference = loadInferenceProxy(r)

	if len(r.errs) > 0 {
		return nil, fmt.Errorf("config: %s", strings.Join(r.errs, "; "))
	}
	return cfg, nil
}

func loadInferenceProxy(r *envReader) InferenceProxyConfig {
	ip := InferenceProxyConfig{
		Enabled:       r.boolval("DITTO_INFERENCE_PROXY_ENABLED", false),
		Required:      r.boolval("DITTO_INFERENCE_PROXY_REQUIRED", false),
		PublicBaseURL: strings.TrimRight(r.str("DITTO_INFERENCE_PUBLIC_BASE_URL", "http://localhost:8000"), "/"),

		OpenRouterAPIKey: r.str("OPENROUTER_API_KEY", ""),
		PerplexityAPIKey: r.str("PERPLEXITY_API_KEY", ""),

		UpstreamURL:          r.str("DITTO_INFERENCE_UPSTREAM_URL", "https://openrouter.ai/api/v1/chat/completions"),
		EmbeddingUpstreamURL: r.str("DITTO_EMBEDDING_UPSTREAM_URL", "https://openrouter.ai/api/v1/embeddings"),
		EmbeddingFallbackURL: r.str("DITTO_EMBEDDING_FALLBACK_URL", "https://api.perplexity.ai/v1/embeddings"),

		Provider:    r.str("DITTO_INFERENCE_PROVIDER", "nebius"),
		RoutingMode: r.str("DITTO_INFERENCE_ROUTING_MODE", RoutingModeAggregateThroughput),

		RequestBudget: r.intval("DITTO_INFERENCE_REQUEST_BUDGET", 8192),
		TokenBudget:   r.int64val("DITTO_INFERENCE_TOKEN_BUDGET", 25_000_000),

		TicketConcurrency:    r.intval("DITTO_INFERENCE_TICKET_CONCURRENCY", 8),
		ValidatorConcurrency: r.intval("DITTO_INFERENCE_VALIDATOR_CONCURRENCY", 24),
		GlobalConcurrency:    r.intval("DITTO_INFERENCE_GLOBAL_CONCURRENCY", 72),
		TicketRPM:            r.intval("DITTO_INFERENCE_TICKET_RPM", 240),
		ValidatorRPM:         r.intval("DITTO_INFERENCE_VALIDATOR_RPM", 960),
		GlobalRPM:            r.intval("DITTO_INFERENCE_GLOBAL_RPM", 2880),

		RequestBodyBytes:  r.int64val("DITTO_INFERENCE_REQUEST_BODY_BYTES", 262144),
		ResponseBodyBytes: r.int64val("DITTO_INFERENCE_RESPONSE_BODY_BYTES", 2*1024*1024),
		TimeoutSeconds:    r.intval("DITTO_INFERENCE_TIMEOUT_SECONDS", 90),
		MaxOutputTokens:   r.intval("DITTO_INFERENCE_MAX_OUTPUT_TOKENS", 8192),

		EmbeddingModel:      r.str("DITTO_EMBEDDING_MODEL", PinnedEmbeddingModel),
		EmbeddingProfile:    r.str("DITTO_EMBEDDING_PROFILE", PinnedEmbeddingProfile),
		EmbeddingProvider:   r.str("DITTO_EMBEDDING_PROVIDER", PinnedEmbeddingProvider),
		EmbeddingDimensions: r.intval("DITTO_EMBEDDING_DIMENSIONS", PinnedEmbeddingDimensions),

		EmbeddingRequestBudget: r.intval("DITTO_EMBEDDING_REQUEST_BUDGET", 100_000),
		EmbeddingTokenBudget:   r.int64val("DITTO_EMBEDDING_TOKEN_BUDGET", 1_000_000_000),

		EmbeddingTicketConcurrency:    r.intval("DITTO_EMBEDDING_TICKET_CONCURRENCY", 12),
		EmbeddingValidatorConcurrency: r.intval("DITTO_EMBEDDING_VALIDATOR_CONCURRENCY", 48),
		EmbeddingGlobalConcurrency:    r.intval("DITTO_EMBEDDING_GLOBAL_CONCURRENCY", 96),
		EmbeddingTicketRPM:            r.intval("DITTO_EMBEDDING_TICKET_RPM", 10_000),
		EmbeddingValidatorRPM:         r.intval("DITTO_EMBEDDING_VALIDATOR_RPM", 40_000),
		EmbeddingGlobalRPM:            r.intval("DITTO_EMBEDDING_GLOBAL_RPM", 100_000),

		EmbeddingRequestBodyBytes:  r.int64val("DITTO_EMBEDDING_REQUEST_BODY_BYTES", 1*1024*1024),
		EmbeddingResponseBodyBytes: r.int64val("DITTO_EMBEDDING_RESPONSE_BODY_BYTES", 16*1024*1024),
	}

	models := strings.Split(r.str("DITTO_INFERENCE_ALLOWED_MODELS", "qwen/qwen3-32b,openai/gpt-oss-20b"), ",")
	for _, m := range models {
		if m = strings.TrimSpace(m); m != "" {
			ip.AllowedModels = append(ip.AllowedModels, m)
		}
	}

	validateInferenceProxy(r, &ip)
	return ip
}

func validateInferenceProxy(r *envReader, ip *InferenceProxyConfig) {
	if ip.Enabled && ip.OpenRouterAPIKey == "" {
		r.fail("OPENROUTER_API_KEY is required when DITTO_INFERENCE_PROXY_ENABLED is true")
	}
	if ip.Required && !ip.Enabled {
		r.fail("DITTO_INFERENCE_PROXY_REQUIRED requires DITTO_INFERENCE_PROXY_ENABLED")
	}
	if u, err := url.Parse(ip.PublicBaseURL); err != nil || !u.IsAbs() || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		r.fail("DITTO_INFERENCE_PUBLIC_BASE_URL must be an absolute http/https URL, got %q", ip.PublicBaseURL)
	}
	validateProviderURL := func(name, raw, host, path, description string) {
		u, err := url.Parse(raw)
		if err != nil || u.Scheme != "https" || !strings.EqualFold(u.Hostname(), host) || u.Path != path {
			r.fail("%s must be %s, got %q", name, description, raw)
		}
	}
	// These pins are credential boundaries, not just convenient defaults. The
	// relay attaches provider bearer tokens to these URLs, so a typo or hostile
	// environment override must fail boot instead of forwarding a secret to an
	// arbitrary host. Keep this identical to apps/platform check_config().
	validateProviderURL("DITTO_INFERENCE_UPSTREAM_URL", ip.UpstreamURL,
		"openrouter.ai", "/api/v1/chat/completions", "OpenRouter chat completions")
	validateProviderURL("DITTO_EMBEDDING_UPSTREAM_URL", ip.EmbeddingUpstreamURL,
		"openrouter.ai", "/api/v1/embeddings", "OpenRouter embeddings")
	validateProviderURL("DITTO_EMBEDDING_FALLBACK_URL", ip.EmbeddingFallbackURL,
		"api.perplexity.ai", "/v1/embeddings", "Perplexity embeddings")
	if ip.RoutingMode != RoutingModeAggregateThroughput && ip.RoutingMode != RoutingModeAdaptive {
		r.fail("DITTO_INFERENCE_ROUTING_MODE must be %q or %q, got %q",
			RoutingModeAggregateThroughput, RoutingModeAdaptive, ip.RoutingMode)
	}
	if n := len(ip.AllowedModels); n < 1 || n > 4 {
		r.fail("DITTO_INFERENCE_ALLOWED_MODELS must list 1-4 models, got %d", n)
	}

	pos := func(name string, v int64, maxv int64) {
		if v < 1 || v > maxv {
			r.fail("%s out of range: %d (must be 1..%d)", name, v, maxv)
		}
	}
	pos("DITTO_INFERENCE_REQUEST_BUDGET", int64(ip.RequestBudget), 16384)
	pos("DITTO_INFERENCE_TOKEN_BUDGET", ip.TokenBudget, 100_000_000)
	pos("DITTO_INFERENCE_REQUEST_BODY_BYTES", ip.RequestBodyBytes, 1*1024*1024)
	pos("DITTO_INFERENCE_RESPONSE_BODY_BYTES", ip.ResponseBodyBytes, 8*1024*1024)
	pos("DITTO_INFERENCE_MAX_OUTPUT_TOKENS", int64(ip.MaxOutputTokens), 32768)
	if ip.TimeoutSeconds < 1 || ip.TimeoutSeconds > 120 {
		r.fail("DITTO_INFERENCE_TIMEOUT_SECONDS out of range: %d (must be 1..120)", ip.TimeoutSeconds)
	}

	hierarchy := func(names [3]string, a, b, c, cap int) {
		if a < 1 || b < 1 || c < 1 || a > b || b > c || c > cap {
			r.fail("%s <= %s <= %s <= %d violated: %d/%d/%d", names[0], names[1], names[2], cap, a, b, c)
		}
	}
	hierarchy([3]string{"DITTO_INFERENCE_TICKET_CONCURRENCY", "DITTO_INFERENCE_VALIDATOR_CONCURRENCY", "DITTO_INFERENCE_GLOBAL_CONCURRENCY"},
		ip.TicketConcurrency, ip.ValidatorConcurrency, ip.GlobalConcurrency, 128)
	hierarchy([3]string{"DITTO_INFERENCE_TICKET_RPM", "DITTO_INFERENCE_VALIDATOR_RPM", "DITTO_INFERENCE_GLOBAL_RPM"},
		ip.TicketRPM, ip.ValidatorRPM, ip.GlobalRPM, 100_000)
	hierarchy([3]string{"DITTO_EMBEDDING_TICKET_CONCURRENCY", "DITTO_EMBEDDING_VALIDATOR_CONCURRENCY", "DITTO_EMBEDDING_GLOBAL_CONCURRENCY"},
		ip.EmbeddingTicketConcurrency, ip.EmbeddingValidatorConcurrency, ip.EmbeddingGlobalConcurrency, 128)
	hierarchy([3]string{"DITTO_EMBEDDING_TICKET_RPM", "DITTO_EMBEDDING_VALIDATOR_RPM", "DITTO_EMBEDDING_GLOBAL_RPM"},
		ip.EmbeddingTicketRPM, ip.EmbeddingValidatorRPM, ip.EmbeddingGlobalRPM, 100_000)

	pos("DITTO_EMBEDDING_REQUEST_BUDGET", int64(ip.EmbeddingRequestBudget), 100_000)
	pos("DITTO_EMBEDDING_TOKEN_BUDGET", ip.EmbeddingTokenBudget, 1_000_000_000)
	pos("DITTO_EMBEDDING_REQUEST_BODY_BYTES", ip.EmbeddingRequestBodyBytes, 1*1024*1024)
	pos("DITTO_EMBEDDING_RESPONSE_BODY_BYTES", ip.EmbeddingResponseBodyBytes, 16*1024*1024)

	// Embedding identity is frozen by the v7 contract; any deviation fails
	// boot exactly like the Python check_config.
	if ip.EmbeddingModel != PinnedEmbeddingModel {
		r.fail("DITTO_EMBEDDING_MODEL is pinned to %q, got %q", PinnedEmbeddingModel, ip.EmbeddingModel)
	}
	if ip.EmbeddingProfile != PinnedEmbeddingProfile {
		r.fail("DITTO_EMBEDDING_PROFILE is pinned to %q, got %q", PinnedEmbeddingProfile, ip.EmbeddingProfile)
	}
	if ip.EmbeddingProvider != PinnedEmbeddingProvider {
		r.fail("DITTO_EMBEDDING_PROVIDER is pinned to %q, got %q", PinnedEmbeddingProvider, ip.EmbeddingProvider)
	}
	if ip.EmbeddingDimensions != PinnedEmbeddingDimensions {
		r.fail("DITTO_EMBEDDING_DIMENSIONS is pinned to %d, got %d", PinnedEmbeddingDimensions, ip.EmbeddingDimensions)
	}
}
