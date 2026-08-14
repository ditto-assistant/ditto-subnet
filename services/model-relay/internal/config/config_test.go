package config

import (
	"strings"
	"testing"
)

// minimalEnv is the smallest environment that boots: the required Postgres
// triple plus Pylon auth.
func minimalEnv() map[string]string {
	return map[string]string{
		"POSTGRES_USER":           "ditto",
		"POSTGRES_PASSWORD":       "secret",
		"POSTGRES_DB":             "ditto_platform",
		"PYLON_OPEN_ACCESS_TOKEN": "token",
	}
}

func TestLoadMinimalEnvGetsDefaults(t *testing.T) {
	cfg, err := Load(MapLookup(minimalEnv()))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Host != "0.0.0.0" || cfg.Port != 8000 {
		t.Errorf("API defaults: got %s:%d", cfg.Host, cfg.Port)
	}
	if cfg.Postgres.Host != "localhost" || cfg.Postgres.Port != 5432 {
		t.Errorf("postgres defaults: got %s:%d", cfg.Postgres.Host, cfg.Postgres.Port)
	}
	if cfg.Postgres.PoolMinSize != 2 || cfg.Postgres.PoolMaxSize != 10 || cfg.Postgres.CommandTimeout != 30.0 {
		t.Errorf("pool defaults wrong: %+v", cfg.Postgres)
	}
	if cfg.Chain.Netuid != 118 || cfg.Chain.Network != "finney" || cfg.Chain.PylonURL != "http://localhost:8001" {
		t.Errorf("chain defaults wrong: %+v", cfg.Chain)
	}
	ip := cfg.Inference
	if ip.Enabled {
		t.Error("proxy must default disabled")
	}
	if ip.RequestBudget != 8192 || ip.TokenBudget != 25_000_000 {
		t.Errorf("budget defaults wrong: %d/%d", ip.RequestBudget, ip.TokenBudget)
	}
	if ip.TicketConcurrency != 8 || ip.ValidatorConcurrency != 24 || ip.GlobalConcurrency != 72 {
		t.Errorf("chat concurrency defaults wrong: %+v", ip)
	}
	if ip.TicketRPM != 240 || ip.ValidatorRPM != 960 || ip.GlobalRPM != 2880 {
		t.Errorf("chat rpm defaults wrong: %+v", ip)
	}
	if ip.EmbeddingTicketConcurrency != 12 || ip.EmbeddingValidatorConcurrency != 48 || ip.EmbeddingGlobalConcurrency != 96 {
		t.Errorf("embedding concurrency defaults wrong: %+v", ip)
	}
	if ip.RequestBodyBytes != 262144 || ip.ResponseBodyBytes != 2*1024*1024 {
		t.Errorf("chat body caps wrong: %d/%d", ip.RequestBodyBytes, ip.ResponseBodyBytes)
	}
	if ip.EmbeddingRequestBodyBytes != 1024*1024 || ip.EmbeddingResponseBodyBytes != 16*1024*1024 {
		t.Errorf("embedding body caps wrong: %d/%d", ip.EmbeddingRequestBodyBytes, ip.EmbeddingResponseBodyBytes)
	}
	if ip.TimeoutSeconds != 90 || ip.MaxOutputTokens != 8192 {
		t.Errorf("timeout/max tokens wrong: %d/%d", ip.TimeoutSeconds, ip.MaxOutputTokens)
	}
	if len(ip.AllowedModels) != 2 || ip.AllowedModels[0] != "qwen/qwen3-32b" || ip.AllowedModels[1] != "openai/gpt-oss-20b" {
		t.Errorf("allowed models default wrong: %v", ip.AllowedModels)
	}
	if ip.EmbeddingModel != PinnedEmbeddingModel || ip.EmbeddingDimensions != 768 {
		t.Errorf("embedding identity wrong: %s/%d", ip.EmbeddingModel, ip.EmbeddingDimensions)
	}
	if ip.RoutingMode != RoutingModeAggregateThroughput {
		t.Errorf("routing mode default wrong: %s", ip.RoutingMode)
	}
}

func TestMissingRequiredPostgresVarsFailBoot(t *testing.T) {
	for _, missing := range []string{"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} {
		env := minimalEnv()
		delete(env, missing)
		_, err := Load(MapLookup(env))
		if err == nil {
			t.Fatalf("missing %s must fail boot", missing)
		}
		if !strings.Contains(err.Error(), missing) {
			t.Errorf("error must name %s, got: %v", missing, err)
		}
	}
}

func TestBlankRequiredVarFailsBoot(t *testing.T) {
	env := minimalEnv()
	env["POSTGRES_PASSWORD"] = "   "
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("blank required var must fail boot (no placeholder defaults)")
	}
}

func TestMissingPylonAuthFailsBoot(t *testing.T) {
	env := minimalEnv()
	delete(env, "PYLON_OPEN_ACCESS_TOKEN")
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("no pylon auth must fail boot")
	}

	// The identity pair is the alternative auth form.
	env["PYLON_IDENTITY_NAME"] = "relay"
	env["PYLON_IDENTITY_TOKEN"] = "id-token"
	if _, err := Load(MapLookup(env)); err != nil {
		t.Fatalf("identity pair must satisfy pylon auth: %v", err)
	}

	// Half a pair is no pair.
	delete(env, "PYLON_IDENTITY_TOKEN")
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("identity name without token must fail boot")
	}
}

func TestRoleValidation(t *testing.T) {
	env := minimalEnv()
	env["DITTO_ROLE"] = "relay"
	if _, err := Load(MapLookup(env)); err != nil {
		t.Fatalf("DITTO_ROLE=relay must be accepted: %v", err)
	}

	// The Python api_server also accepts "platform", but this binary IS the
	// relay: booting it with the platform role is a deploy error.
	for _, bad := range []string{"platform", "worker", "bogus"} {
		env["DITTO_ROLE"] = bad
		if _, err := Load(MapLookup(env)); err == nil {
			t.Errorf("DITTO_ROLE=%s must fail boot", bad)
		}
	}
}

func TestNonNumericLimitFailsBoot(t *testing.T) {
	env := minimalEnv()
	env["DITTO_INFERENCE_TICKET_CONCURRENCY"] = "eight"
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("non-numeric numeric var must fail boot")
	}

	env = minimalEnv()
	env["API_PORT"] = "80a0"
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("non-numeric API_PORT must fail boot")
	}
}

func TestProxyEnabledRequiresOpenRouterKey(t *testing.T) {
	env := minimalEnv()
	env["DITTO_INFERENCE_PROXY_ENABLED"] = "true"
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("enabled proxy without OPENROUTER_API_KEY must fail boot")
	}
	env["OPENROUTER_API_KEY"] = "sk-or-xxx"
	cfg, err := Load(MapLookup(env))
	if err != nil {
		t.Fatalf("enabled proxy with key must boot: %v", err)
	}
	if !cfg.Inference.Enabled {
		t.Fatal("proxy must be enabled")
	}
}

func TestConcurrencyHierarchyValidated(t *testing.T) {
	env := minimalEnv()
	env["DITTO_INFERENCE_TICKET_CONCURRENCY"] = "50"
	env["DITTO_INFERENCE_VALIDATOR_CONCURRENCY"] = "24" // ticket > validator
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("ticket > validator concurrency must fail boot")
	}

	env = minimalEnv()
	env["DITTO_EMBEDDING_GLOBAL_RPM"] = "200000" // above the 100000 cap
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("rpm above cap must fail boot")
	}
}

func TestEmbeddingIdentityIsPinned(t *testing.T) {
	env := minimalEnv()
	env["DITTO_EMBEDDING_MODEL"] = "some/other-model"
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("deviating embedding model must fail boot")
	}

	env = minimalEnv()
	env["DITTO_EMBEDDING_DIMENSIONS"] = "1024"
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("deviating embedding dimensions must fail boot")
	}
}

func TestPlatformOnlyVariablesAreIgnored(t *testing.T) {
	env := minimalEnv()
	// A relay host env carries the full platform inventory; none of it may
	// affect the relay's boot.
	env["STORAGE_ENDPOINT_URL"] = "http://minio:9000"
	env["DITTO_UPLOAD_PAYMENT_ADDRESS"] = "5F3sa2TJAWMqDhXG6jhV4N8ko9SxwGy8TpaNS1repo5EYjQX"
	env["DITTO_EFFICIENCY_BONUS_ENABLED"] = "definitely-not-a-bool"
	env["SCREENER_GARBAGE"] = "\x00"
	cfg, err := Load(MapLookup(env))
	if err != nil {
		t.Fatalf("platform-only vars must be tolerated and ignored: %v", err)
	}
	if cfg.Role != RoleRelay {
		t.Fatalf("role: %s", cfg.Role)
	}
}

func TestDSNPercentEncoding(t *testing.T) {
	p := PostgresConfig{
		Host: "db.internal", Port: 5432,
		User: "user@corp", Password: "p:ss/w rd", Database: "ditto",
	}
	dsn := p.DSN()
	want := "postgresql://user%40corp:p%3Ass%2Fw%20rd@db.internal:5432/ditto"
	if dsn != want {
		t.Fatalf("DSN encoding: got %q want %q", dsn, want)
	}
}

func TestPublicBaseURLTrailingSlashStripped(t *testing.T) {
	env := minimalEnv()
	env["DITTO_INFERENCE_PUBLIC_BASE_URL"] = "https://relay.heyditto.ai/"
	cfg, err := Load(MapLookup(env))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Inference.PublicBaseURL != "https://relay.heyditto.ai" {
		t.Fatalf("trailing slash must be stripped: %q", cfg.Inference.PublicBaseURL)
	}

	env["DITTO_INFERENCE_PUBLIC_BASE_URL"] = "not-a-url"
	if _, err := Load(MapLookup(env)); err == nil {
		t.Fatal("relative public base url must fail boot")
	}
}
