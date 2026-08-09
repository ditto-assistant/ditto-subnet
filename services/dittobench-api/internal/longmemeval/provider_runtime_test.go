package longmemeval

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type recordingAuthorizer struct {
	mu         sync.Mutex
	references []SecretManagerReference
	requests   []*http.Request
	credential string
	err        error
}

func (a *recordingAuthorizer) Authorize(_ context.Context, reference SecretManagerReference, request *http.Request) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.references = append(a.references, reference)
	a.requests = append(a.requests, request)
	if a.err != nil {
		return a.err
	}
	request.Header.Set("Authorization", "Bearer "+a.credential)
	return nil
}

func (a *recordingAuthorizer) snapshot() []SecretManagerReference {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]SecretManagerReference(nil), a.references...)
}

func (a *recordingAuthorizer) requestSnapshot() []*http.Request {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]*http.Request(nil), a.requests...)
}

type runtimeUpstream struct {
	server *httptest.Server
	mu     sync.Mutex
	bodies []map[string]any
	header []http.Header
	next   func(int, map[string]any) (int, string)
	count  atomic.Int64
}

func newRuntimeUpstream(t *testing.T) *runtimeUpstream {
	t.Helper()
	upstream := &runtimeUpstream{}
	upstream.next = func(index int, body map[string]any) (int, string) {
		model, _ := body["model"].(string)
		content := "reader answer"
		if model == officialJudgeModel {
			content = "yes"
		}
		return http.StatusOK, fmt.Sprintf(
			`{"id":"receipt-%d","model":%q,"provider":"OpenAI","choices":[{"message":{"content":%q}}],"usage":{"prompt_tokens":11,"completion_tokens":3,"total_tokens":14,"cost":0.00000125}}`,
			index, model, content,
		)
	}
	upstream.server = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		index := int(upstream.count.Add(1))
		var body map[string]any
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			t.Errorf("decode upstream request: %v", err)
			writer.WriteHeader(http.StatusBadRequest)
			return
		}
		upstream.mu.Lock()
		upstream.bodies = append(upstream.bodies, body)
		upstream.header = append(upstream.header, request.Header.Clone())
		status, response := upstream.next(index, body)
		upstream.mu.Unlock()
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(status)
		_, _ = io.WriteString(writer, response)
	}))
	t.Cleanup(upstream.server.Close)
	return upstream
}

func runtimeProviderProfile(t *testing.T) Profile {
	t.Helper()
	profile := loadTestProfile(t)
	for index := range profile.Providers {
		profile.Providers[index].MaxRequests = 500
		profile.Providers[index].MaxPromptTokens = 10_000_000
		profile.Providers[index].MaxCompletionTokens = 10_000_000
		profile.Providers[index].MaxTotalTokens = 20_000_000
		profile.Providers[index].MaxCostUSDmicros = 10_000_000
	}
	return profile
}

func runtimeProviderConfig(upstream *runtimeUpstream, authorizer RequestAuthorizer) ProviderRuntimeConfig {
	return ProviderRuntimeConfig{
		Authorizer: authorizer,
		HTTPClient: upstream.server.Client(),
		Lanes: []ProviderLaneRuntimeConfig{
			{
				Lane: ReaderLane, UpstreamURL: upstream.server.URL + "/v1/chat/completions",
				RouteProvider: "openai", ReceiptProvider: "OpenAI",
				CredentialReference: SecretManagerReference{ProjectID: "ditto-prod", SecretID: "openrouter-reader", Version: "7"},
				RequestTimeout:      time.Second,
			},
			{
				Lane: JudgeLane, UpstreamURL: upstream.server.URL + "/v1/chat/completions",
				RouteProvider: "openai", ReceiptProvider: "OpenAI",
				CredentialReference: SecretManagerReference{ProjectID: "ditto-prod", SecretID: "openrouter-judge", Version: "11"},
				RequestTimeout:      time.Second,
			},
		},
	}
}

func newRuntimeProviderSession(t *testing.T, profile Profile, upstream *runtimeUpstream) (*ProviderSession, *recordingAuthorizer) {
	t.Helper()
	authorizer := &recordingAuthorizer{credential: "private-provider-credential"}
	session, err := NewProviderSession(context.Background(), profile, runtimeProviderConfig(upstream, authorizer))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session, authorizer
}

func readerRequest(t *testing.T, session *ProviderSession, body string) *httptest.ResponseRecorder {
	t.Helper()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	request.Header.Set("Authorization", "Bearer harness-controlled-credential")
	session.ReaderHandler().ServeHTTP(recorder, request)
	return recorder
}

func evidenceByLane(t *testing.T, session *ProviderSession) map[string]ProviderEvidence {
	t.Helper()
	values, err := session.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	result := make(map[string]ProviderEvidence, len(values))
	for _, value := range values {
		result[value.Lane] = value
	}
	return result
}

func TestProviderSessionStartsAtExactZeroAndUsesSecretReferencesOnly(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	session, authorizer := newRuntimeProviderSession(t, profile, upstream)
	values := evidenceByLane(t, session)
	for _, lane := range []string{ReaderLane, JudgeLane} {
		value := values[lane]
		policy := providerPolicy(&profile, lane)
		if value.Lane != lane || value.Provider != policy.Provider || value.Model != policy.Model ||
			value.ProfileRevision != policy.ProfileRevision || value.CostSource != AuthoritativeCostV1 ||
			value.Currency != "USD" || value.FallbackUsed || !zeroProviderCounters(value) ||
			value.ReceiptSetSHA256 != "" {
			t.Fatalf("zero %s evidence=%#v", lane, value)
		}
	}
	if got := authorizer.snapshot(); len(got) != 0 {
		t.Fatalf("authorizer invoked before provider spend: %#v", got)
	}
	raw, err := json.Marshal(runtimeProviderConfig(upstream, authorizer).Lanes)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(raw, []byte(authorizer.credential)) {
		t.Fatal("runtime configuration serialized credential material")
	}
}

func TestReaderRelayPinsFrozenIdentityAndRecordsAuthoritativeReceipt(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	session, authorizer := newRuntimeProviderSession(t, profile, upstream)
	response := readerRequest(t, session,
		`{"model":"openai/gpt-oss-20b","stream":false,"models":["attacker/fallback"],"provider":{"only":["attacker"],"allow_fallbacks":true},"messages":[{"role":"user","content":"hello"}]}`,
	)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "reader answer") {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	upstream.mu.Lock()
	body := upstream.bodies[0]
	header := upstream.header[0]
	upstream.mu.Unlock()
	if body["model"] != providerPolicy(&profile, ReaderLane).Model || body["stream"] != false {
		t.Fatalf("reader identity not pinned: %#v", body)
	}
	if body["max_tokens"] != float64(20_000) {
		t.Fatalf("reader omitted bound was not derived from the frozen policy: %#v", body["max_tokens"])
	}
	if _, exists := body["models"]; exists {
		t.Fatalf("reader retained fallback model list: %#v", body)
	}
	routing, ok := body["provider"].(map[string]any)
	if !ok || routing["allow_fallbacks"] != false || routing["data_collection"] != "deny" ||
		routing["require_parameters"] != true || !reflect.DeepEqual(routing["only"], []any{"openai"}) ||
		!reflect.DeepEqual(routing["order"], []any{"openai"}) {
		t.Fatalf("reader routing=%#v", routing)
	}
	if header.Get("Authorization") != "Bearer "+authorizer.credential ||
		header.Get("Authorization") == "Bearer harness-controlled-credential" {
		t.Fatalf("trusted authorization boundary failed: %q", header.Get("Authorization"))
	}
	refs := authorizer.snapshot()
	if len(refs) != 1 || refs[0].SecretID != "openrouter-reader" || refs[0].Version != "7" {
		t.Fatalf("credential references=%#v", refs)
	}
	authorizedRequests := authorizer.requestSnapshot()
	if len(authorizedRequests) != 1 || authorizedRequests[0].Header.Get("Authorization") != "" {
		t.Fatal("provider request retained credential-bearing authorization after transport completed")
	}
	reader := evidenceByLane(t, session)[ReaderLane]
	if reader.Requests != 1 || reader.Successes != 1 || reader.ReceiptedRequests != 1 ||
		reader.PromptTokens != 11 || reader.CompletionTokens != 3 || reader.TotalTokens != 14 ||
		reader.CostUSDmicros != 2 || !validSHA256(reader.ReceiptSetSHA256) {
		t.Fatalf("reader evidence=%#v", reader)
	}
}

func TestReaderRelayRejectsUntrustedRequestVariantsBeforeSpend(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	tests := map[string]struct {
		method string
		path   string
		body   string
		status int
	}{
		"wrong method":              {http.MethodGet, "/v1/chat/completions", `{}`, http.StatusNotFound},
		"wrong path":                {http.MethodPost, "/v1/embeddings", `{}`, http.StatusNotFound},
		"query":                     {http.MethodPost, "/v1/chat/completions?target=other", `{}`, http.StatusNotFound},
		"not object":                {http.MethodPost, "/v1/chat/completions", `[]`, http.StatusBadRequest},
		"wrong model":               {http.MethodPost, "/v1/chat/completions", `{"model":"attacker/model"}`, http.StatusBadRequest},
		"missing model":             {http.MethodPost, "/v1/chat/completions", `{}`, http.StatusBadRequest},
		"stream true":               {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","stream":true}`, http.StatusBadRequest},
		"stream wrong type":         {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","stream":"false"}`, http.StatusBadRequest},
		"null completion bound":     {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","max_tokens":null}`, http.StatusBadRequest},
		"zero completion bound":     {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","max_tokens":0}`, http.StatusBadRequest},
		"fraction completion bound": {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","max_tokens":1.5}`, http.StatusBadRequest},
		"over frozen bound":         {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","max_tokens":20001}`, http.StatusBadRequest},
		"contradictory bounds":      {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","max_tokens":1,"max_completion_tokens":2}`, http.StatusBadRequest},
		"multiple completions":      {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","max_tokens":1,"n":2}`, http.StatusBadRequest},
		"trailing JSON":             {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b"}{}`, http.StatusBadRequest},
		"duplicate model":           {http.MethodPost, "/v1/chat/completions", `{"model":"openai/gpt-oss-20b","model":"attacker/model"}`, http.StatusBadRequest},
		"empty body":                {http.MethodPost, "/v1/chat/completions", ``, http.StatusRequestEntityTooLarge},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			session, authorizer := newRuntimeProviderSession(t, profile, upstream)
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(testCase.method, testCase.path, strings.NewReader(testCase.body))
			session.ReaderHandler().ServeHTTP(recorder, request)
			if recorder.Code != testCase.status || strings.Contains(recorder.Body.String(), authorizer.credential) {
				t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
			}
			if values := evidenceByLane(t, session); values[ReaderLane].Requests != 0 {
				t.Fatal("rejected request reached provider")
			}
		})
	}
}

func TestReaderRelayRejectsOversizedBodyWithoutSpend(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/chat/completions", strings.NewReader(strings.Repeat("x", maxProviderRequestBytes+1)))
	session.ReaderHandler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusRequestEntityTooLarge || evidenceByLane(t, session)[ReaderLane].Requests != 0 {
		t.Fatalf("status=%d", recorder.Code)
	}
}

func TestReaderRewriteInjectsFrozenBoundAndRequiresConsistentExplicitBound(t *testing.T) {
	profile := runtimeProviderProfile(t)
	policy := *providerPolicy(&profile, ReaderLane)
	for name, raw := range map[string]string{
		"null":          `{"model":"openai/gpt-oss-20b","max_tokens":null}`,
		"contradictory": `{"model":"openai/gpt-oss-20b","max_tokens":1,"max_completion_tokens":2}`,
		"multiple":      `{"model":"openai/gpt-oss-20b","max_tokens":1,"n":2}`,
		"over bound":    `{"model":"openai/gpt-oss-20b","max_tokens":20001}`,
	} {
		t.Run(name, func(t *testing.T) {
			if _, _, err := rewriteReaderRequest([]byte(raw), policy, "openai"); err == nil {
				t.Fatal("unbounded or ambiguous reader request accepted")
			}
		})
	}
	injected, injectedReservation, err := rewriteReaderRequest(
		[]byte(`{"model":"openai/gpt-oss-20b"}`), policy, "openai",
	)
	if err != nil || injectedReservation.CompletionTokens != 20_000 ||
		!bytes.Contains(injected, []byte(`"max_tokens":20000`)) {
		t.Fatalf("injected=%s reservation=%#v err=%v", injected, injectedReservation, err)
	}
	encoded, reservation, err := rewriteReaderRequest(
		[]byte(`{"model":"openai/gpt-oss-20b","max_tokens":7,"max_completion_tokens":7,"n":1}`),
		policy,
		"openai",
	)
	if err != nil || reservation.CompletionTokens != 7 || reservation.PromptTokens != uint64(len(encoded)) {
		t.Fatalf("reservation=%#v len=%d err=%v", reservation, len(encoded), err)
	}
}

func TestProviderSessionConstructorFailsClosed(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	authorizer := &recordingAuthorizer{credential: "private"}
	valid := runtimeProviderConfig(upstream, authorizer)
	tests := map[string]func(*Profile, *ProviderRuntimeConfig){
		"nil authorizer": func(_ *Profile, c *ProviderRuntimeConfig) { c.Authorizer = nil },
		"typed nil authorizer": func(_ *Profile, c *ProviderRuntimeConfig) {
			var authorizer *recordingAuthorizer
			c.Authorizer = authorizer
		},
		"missing config lane":    func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes = c.Lanes[:1] },
		"duplicate config lane":  func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[1].Lane = ReaderLane },
		"missing profile lane":   func(p *Profile, _ *ProviderRuntimeConfig) { p.Providers[1].Lane = "embedding" },
		"wrong profile provider": func(p *Profile, _ *ProviderRuntimeConfig) { p.Providers[0].Provider = "direct" },
		"zero derived reader completion bound": func(p *Profile, _ *ProviderRuntimeConfig) {
			providerPolicy(p, ReaderLane).MaxCompletionTokens = providerPolicy(p, ReaderLane).MaxRequests - 1
		},
		"judge model":            func(p *Profile, _ *ProviderRuntimeConfig) { providerPolicy(p, JudgeLane).Model = "openai/gpt-4o-mini" },
		"judge revision":         func(p *Profile, _ *ProviderRuntimeConfig) { providerPolicy(p, JudgeLane).ProfileRevision = "changed" },
		"judge route":            func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[1].RouteProvider = "other" },
		"judge receipt provider": func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[1].ReceiptProvider = "Other" },
		"empty route":            func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].RouteProvider = "" },
		"header route":           func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].RouteProvider = "openai\r\nX-Evil: yes" },
		"zero timeout":           func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].RequestTimeout = 0 },
		"empty secret project":   func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].CredentialReference.ProjectID = "" },
		"empty secret name":      func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].CredentialReference.SecretID = "" },
		"empty secret version":   func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].CredentialReference.Version = "" },
		"URL credential": func(_ *Profile, c *ProviderRuntimeConfig) {
			c.Lanes[0].UpstreamURL = "https://user:pass@openrouter.ai/api/v1/chat/completions"
		},
		"URL query":    func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].UpstreamURL += "?target=other" },
		"URL fragment": func(_ *Profile, c *ProviderRuntimeConfig) { c.Lanes[0].UpstreamURL += "#other" },
		"wrong URL path": func(_ *Profile, c *ProviderRuntimeConfig) {
			c.Lanes[0].UpstreamURL = "https://openrouter.ai/api/v1/embeddings"
		},
		"insecure remote": func(_ *Profile, c *ProviderRuntimeConfig) {
			c.Lanes[0].UpstreamURL = "http://example.com/v1/chat/completions"
		},
		"other HTTPS host": func(_ *Profile, c *ProviderRuntimeConfig) {
			c.Lanes[0].UpstreamURL = "https://example.com/v1/chat/completions"
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changedProfile := profile
			changedProfile.Providers = append([]ProviderPolicy(nil), profile.Providers...)
			changedConfig := valid
			changedConfig.Lanes = append([]ProviderLaneRuntimeConfig(nil), valid.Lanes...)
			mutate(&changedProfile, &changedConfig)
			session, err := NewProviderSession(context.Background(), changedProfile, changedConfig)
			if err == nil || session != nil {
				t.Fatalf("invalid runtime accepted: %#v", session)
			}
			if strings.Contains(err.Error(), authorizer.credential) {
				t.Fatal("constructor error exposed credential material")
			}
		})
	}
	if session, err := NewProviderSession(nil, profile, valid); err == nil || session != nil {
		t.Fatal("nil parent context accepted")
	}
}

func TestValidateProviderRuntimeConfigIsSideEffectFree(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	authorizer := &recordingAuthorizer{credential: "private"}
	config := runtimeProviderConfig(upstream, authorizer)
	if err := ValidateProviderRuntimeConfig(profile, config); err != nil {
		t.Fatal(err)
	}
	if upstream.count.Load() != 0 || len(authorizer.snapshot()) != 0 {
		t.Fatal("installation validation contacted provider or credential source")
	}
}

func TestProviderReceiptValidationRejectsEveryMissingOrDriftingField(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	valid := `{"id":"receipt-x","model":"openai/gpt-oss-20b","provider":"OpenAI","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.000001}}`
	tests := map[string]string{
		"missing id":            strings.Replace(valid, `"id":"receipt-x",`, "", 1),
		"missing usage":         strings.Replace(valid, `,"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.000001}`, "", 1),
		"wrong model":           strings.Replace(valid, "openai/gpt-oss-20b", "other/model", 1),
		"wrong provider":        strings.Replace(valid, `"provider":"OpenAI"`, `"provider":"Other"`, 1),
		"missing prompt":        strings.Replace(valid, `"prompt_tokens":2,`, "", 1),
		"negative prompt":       strings.Replace(valid, `"prompt_tokens":2`, `"prompt_tokens":-1`, 1),
		"fraction prompt":       strings.Replace(valid, `"prompt_tokens":2`, `"prompt_tokens":2.5`, 1),
		"exponent prompt":       strings.Replace(valid, `"prompt_tokens":2`, `"prompt_tokens":2e0`, 1),
		"missing completion":    strings.Replace(valid, `"completion_tokens":1,`, "", 1),
		"missing total":         strings.Replace(valid, `"total_tokens":3,`, "", 1),
		"wrong total":           strings.Replace(valid, `"total_tokens":3`, `"total_tokens":4`, 1),
		"missing cost":          strings.Replace(valid, `,"cost":0.000001`, "", 1),
		"negative cost":         strings.Replace(valid, `"cost":0.000001`, `"cost":-0.1`, 1),
		"string cost":           strings.Replace(valid, `"cost":0.000001`, `"cost":"0.000001"`, 1),
		"duplicate usage field": strings.Replace(valid, `"prompt_tokens":2`, `"prompt_tokens":2,"prompt_tokens":3`, 1),
		"duplicate top field":   strings.Replace(valid, `"id":"receipt-x"`, `"id":"receipt-x","id":"receipt-y"`, 1),
		"trailing JSON":         valid + `{}`,
	}
	for name, response := range tests {
		t.Run(name, func(t *testing.T) {
			upstream.next = func(_ int, _ map[string]any) (int, string) { return http.StatusOK, response }
			session, _ := newRuntimeProviderSession(t, profile, upstream)
			recorder := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`)
			if recorder.Code != http.StatusBadGateway {
				t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
			}
			if _, err := session.Snapshot(context.Background()); err == nil {
				t.Fatal("invalid provider receipt retained signable accounting")
			}
		})
	}
}

func TestProviderSessionRejectsDuplicateReceiptAndCannotRecover(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	upstream.next = func(_ int, body map[string]any) (int, string) {
		return http.StatusOK, fmt.Sprintf(`{"id":"same","model":%q,"provider":"OpenAI","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`, body["model"])
	}
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	if got := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); got.Code != http.StatusOK {
		t.Fatalf("first status=%d", got.Code)
	}
	if got := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); got.Code != http.StatusBadGateway {
		t.Fatalf("duplicate status=%d", got.Code)
	}
	if _, err := session.Snapshot(context.Background()); err == nil {
		t.Fatal("duplicate receipt did not poison accounting")
	}
	before := upstream.count.Load()
	if got := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); got.Code != http.StatusBadGateway {
		t.Fatalf("poisoned status=%d", got.Code)
	}
	if upstream.count.Load() != before {
		t.Fatal("poisoned session sent another provider request")
	}
}

func TestProviderSessionRejectsReceiptIdentityReusedAcrossLanes(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	upstream.next = func(_ int, body map[string]any) (int, string) {
		return http.StatusOK, fmt.Sprintf(`{"id":"cross-lane-replay","model":%q,"provider":"OpenAI","choices":[{"message":{"content":"yes"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`, body["model"])
	}
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	if response := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); response.Code != http.StatusOK {
		t.Fatalf("reader status=%d", response.Code)
	}
	_, err := session.Judge().Judge(context.Background(), JudgeInput{Reference: DatasetCase{
		QuestionID: "q", QuestionType: "multi-session", Question: "Q", Answer: "A",
	}, Hypothesis: "H"})
	if err == nil {
		t.Fatal("cross-lane receipt replay was accepted")
	}
	if _, err := session.Snapshot(context.Background()); err == nil {
		t.Fatal("cross-lane receipt replay retained signable accounting")
	}
}

func TestProviderSessionCountsReceiptedHTTPFailuresWithoutCallingThemSuccess(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	upstream.next = func(index int, body map[string]any) (int, string) {
		return http.StatusTooManyRequests, fmt.Sprintf(`{"id":"failed-%d","model":%q,"provider":"OpenAI","usage":{"prompt_tokens":1,"completion_tokens":0,"total_tokens":1,"cost":0.0000001},"error":{"message":"limited"}}`, index, body["model"])
	}
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	response := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`)
	if response.Code != http.StatusTooManyRequests {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	reader := evidenceByLane(t, session)[ReaderLane]
	if reader.Requests != 1 || reader.ReceiptedRequests != 1 || reader.Successes != 0 || reader.CostUSDmicros != 1 {
		t.Fatalf("failure accounting=%#v", reader)
	}
}

func TestProviderSessionPoisonsReceiptThatViolatesReservedTokenBounds(t *testing.T) {
	profile := runtimeProviderProfile(t)
	for _, testCase := range []struct {
		name       string
		prompt     uint64
		completion uint64
		total      uint64
	}{
		{"prompt", 1_000_000, 1, 1_000_001},
		{"completion", 1, 65, 66},
		{"total", 1, 1, 3},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			upstream := newRuntimeUpstream(t)
			upstream.next = func(index int, body map[string]any) (int, string) {
				return http.StatusOK, fmt.Sprintf(`{"id":"bound-%d","model":%q,"provider":"OpenAI","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":%d,"completion_tokens":%d,"total_tokens":%d,"cost":0}}`,
					index, body["model"], testCase.prompt, testCase.completion, testCase.total)
			}
			session, _ := newRuntimeProviderSession(t, profile, upstream)
			if response := readerRequest(t, session, `{"model":"openai/gpt-oss-20b","max_tokens":64}`); response.Code != http.StatusBadGateway {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
			if _, err := session.Snapshot(context.Background()); err == nil {
				t.Fatal("provider token-bound violation retained signable accounting")
			}
		})
	}
}

func TestProviderSessionFailsClosedOnNetworkFailureAndAuthorizationFailure(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	config := runtimeProviderConfig(upstream, &recordingAuthorizer{credential: "private"})
	upstream.server.Close()
	session, err := NewProviderSession(context.Background(), profile, config)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	if got := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); got.Code != http.StatusBadGateway {
		t.Fatalf("network status=%d", got.Code)
	}
	if _, err := session.Snapshot(context.Background()); err == nil {
		t.Fatal("unknown network outcome retained signable accounting")
	}

	upstream2 := newRuntimeUpstream(t)
	credential := "do-not-expose-provider-secret"
	authorizer := &recordingAuthorizer{credential: credential, err: errors.New("failed with " + credential)}
	session2, err := NewProviderSession(context.Background(), profile, runtimeProviderConfig(upstream2, authorizer))
	if err != nil {
		t.Fatal(err)
	}
	defer session2.Close()
	response := readerRequest(t, session2, `{"model":"openai/gpt-oss-20b"}`)
	if response.Code != http.StatusBadGateway || strings.Contains(response.Body.String(), credential) {
		t.Fatalf("authorization response=%s", response.Body.String())
	}
	if evidenceByLane(t, session2)[ReaderLane].Requests != 0 || upstream2.count.Load() != 0 {
		t.Fatal("authorization failure was counted as provider spend")
	}
}

func TestProviderSessionEnforcesEveryFrozenCap(t *testing.T) {
	base := runtimeProviderProfile(t)
	tests := map[string]func(*ProviderPolicy){
		"request": func(p *ProviderPolicy) { p.MaxRequests = 1 },
		"prompt": func(p *ProviderPolicy) {
			p.MaxRequests = 1
			p.MaxPromptTokens = 80
			p.MaxTotalTokens = 80
			p.MaxCompletionTokens = 80
		},
		"completion": func(p *ProviderPolicy) {
			p.MaxRequests = 1
			p.MaxCompletionTokens = 2
		},
		"total": func(p *ProviderPolicy) {
			p.MaxRequests = 1
			p.MaxPromptTokens = 300
			p.MaxCompletionTokens = 300
			p.MaxTotalTokens = 300
		},
		"cost": func(p *ProviderPolicy) { p.MaxCostUSDmicros = 1 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			profile := base
			profile.Providers = append([]ProviderPolicy(nil), base.Providers...)
			mutate(providerPolicy(&profile, ReaderLane))
			upstream := newRuntimeUpstream(t)
			session, _ := newRuntimeProviderSession(t, profile, upstream)
			body := `{"model":"openai/gpt-oss-20b","max_tokens":64}`
			if name == "completion" {
				body = `{"model":"openai/gpt-oss-20b","max_tokens":3}`
			}
			if name == "total" {
				body = `{"model":"openai/gpt-oss-20b","max_tokens":150}`
			}
			first := readerRequest(t, session, body)
			if name == "request" {
				if first.Code != http.StatusOK {
					t.Fatalf("first request status=%d", first.Code)
				}
				if second := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); second.Code != http.StatusBadGateway {
					t.Fatalf("second request status=%d", second.Code)
				}
				if _, err := session.Snapshot(context.Background()); err != nil {
					t.Fatalf("request cap should preserve prior accounting: %v", err)
				}
			} else if name == "cost" {
				if first.Code != http.StatusBadGateway {
					t.Fatalf("over-cap response status=%d", first.Code)
				}
				if _, err := session.Snapshot(context.Background()); err == nil {
					t.Fatal("over-cap accounting remained signable")
				}
			} else {
				expectedStatus := http.StatusBadGateway
				if name == "completion" {
					expectedStatus = http.StatusBadRequest
				}
				if first.Code != expectedStatus || upstream.count.Load() != 0 {
					t.Fatalf("avoidable over-cap request spent provider budget: status=%d calls=%d", first.Code, upstream.count.Load())
				}
				if _, err := session.Snapshot(context.Background()); err != nil {
					t.Fatalf("preflight rejection poisoned prior accounting: %v", err)
				}
			}
		})
	}
}

func TestOfficialJudgePreservesPinnedPromptAndRouting(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	upstream.next = func(index int, body map[string]any) (int, string) {
		return http.StatusOK, fmt.Sprintf(`{"id":"judge-%d","model":"%s","provider":"OpenAI","choices":[{"message":{"content":"YES"}}],"usage":{"prompt_tokens":20,"completion_tokens":1,"total_tokens":21,"cost":0.000002}}`, index, officialJudgeModel)
	}
	session, authorizer := newRuntimeProviderSession(t, profile, upstream)
	input := JudgeInput{Reference: DatasetCase{
		QuestionID: "q-1", QuestionType: "knowledge-update", Question: "What is current?", Answer: "new",
	}, Hypothesis: "It changed from old to new."}
	correct, err := session.Judge().Judge(context.Background(), input)
	if err != nil || !correct {
		t.Fatalf("correct=%v err=%v", correct, err)
	}
	upstream.mu.Lock()
	body := upstream.bodies[0]
	header := upstream.header[0]
	upstream.mu.Unlock()
	if body["model"] != officialJudgeModel || body["temperature"] != float64(0) ||
		body["max_tokens"] != float64(officialJudgeMaxTokens) || body["n"] != float64(1) || body["stream"] != false {
		t.Fatalf("official judge request=%#v", body)
	}
	routing := body["provider"].(map[string]any)
	if routing["allow_fallbacks"] != false || !reflect.DeepEqual(routing["only"], []any{"openai"}) ||
		!reflect.DeepEqual(routing["order"], []any{"openai"}) {
		t.Fatalf("official judge routing=%#v", routing)
	}
	messages := body["messages"].([]any)
	message := messages[0].(map[string]any)
	wantPrompt := "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: What is current?\n\nCorrect Answer: new\n\nModel Response: It changed from old to new.\n\nIs the model response correct? Answer yes or no only."
	if message["role"] != "user" || message["content"] != wantPrompt {
		t.Fatalf("official prompt=%q", message["content"])
	}
	refs := authorizer.snapshot()
	if len(refs) != 1 || refs[0].SecretID != "openrouter-judge" || header.Get("Authorization") != "Bearer "+authorizer.credential {
		t.Fatalf("judge authorization refs=%#v header=%q", refs, header.Get("Authorization"))
	}
	judge := evidenceByLane(t, session)[JudgeLane]
	if judge.Requests != 1 || judge.Successes != 1 || judge.ReceiptedRequests != 1 || judge.PromptTokens != 20 ||
		judge.CompletionTokens != 1 || judge.TotalTokens != 21 || judge.CostUSDmicros != 2 {
		t.Fatalf("judge evidence=%#v", judge)
	}
}

func TestOfficialJudgePromptMatchesEveryPinnedBranch(t *testing.T) {
	ordinaryPrefix := "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no."
	tests := map[string]struct {
		id       string
		task     string
		contains []string
		excludes []string
	}{
		"single user":      {id: "q1", task: "single-session-user", contains: []string{ordinaryPrefix, "Correct Answer: A"}},
		"single assistant": {id: "q2", task: "single-session-assistant", contains: []string{ordinaryPrefix, "Correct Answer: A"}},
		"multi session":    {id: "q3", task: "multi-session", contains: []string{ordinaryPrefix, "Correct Answer: A"}},
		"temporal":         {id: "q4", task: "temporal-reasoning", contains: []string{ordinaryPrefix, "do not penalize off-by-one errors", "predicting 19 days when the answer is 18"}},
		"knowledge update": {id: "q5", task: "knowledge-update", contains: []string{"contains some previous information along with an updated answer", "Correct Answer: A"}},
		"preference":       {id: "q6", task: "single-session-preference", contains: []string{"rubric for desired personalized response", "Rubric: A"}, excludes: []string{"Correct Answer: A"}},
		"abstention":       {id: "q7_abs", task: "future-unknown-type", contains: []string{"unanswerable question", "Explanation: A", "Does the model correctly identify"}, excludes: []string{"Correct Answer: A"}},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			prompt, err := officialJudgePrompt(JudgeInput{Reference: DatasetCase{
				QuestionID: testCase.id, QuestionType: testCase.task, Question: "Q", Answer: "A",
			}, Hypothesis: "H"})
			if err != nil {
				t.Fatal(err)
			}
			for _, expected := range append(testCase.contains, "Question: Q", "Model Response: H") {
				if !strings.Contains(prompt, expected) {
					t.Fatalf("prompt lacks %q: %s", expected, prompt)
				}
			}
			for _, forbidden := range testCase.excludes {
				if strings.Contains(prompt, forbidden) {
					t.Fatalf("prompt contains %q: %s", forbidden, prompt)
				}
			}
		})
	}
}

func TestOfficialJudgeRejectsUnsupportedAndMalformedInputsBeforeSpend(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	inputs := []JudgeInput{
		{},
		{Reference: DatasetCase{QuestionID: "q", QuestionType: "unknown", Question: "Q", Answer: "A"}, Hypothesis: "H"},
		{Reference: DatasetCase{QuestionID: "q", QuestionType: "multi-session", Answer: "A"}, Hypothesis: "H"},
	}
	for _, input := range inputs {
		if _, err := session.Judge().Judge(context.Background(), input); err == nil {
			t.Fatalf("invalid judge input accepted: %#v", input)
		}
	}
	if upstream.count.Load() != 0 || evidenceByLane(t, session)[JudgeLane].Requests != 0 {
		t.Fatal("invalid judge input reached provider")
	}
}

func TestOfficialJudgeRequiresUnambiguousNormalizedLabel(t *testing.T) {
	profile := runtimeProviderProfile(t)
	for _, testCase := range []struct {
		response  string
		correct   bool
		wantError bool
	}{{"no", false, false}, {" YES\n", true, false}, {"No.", false, true}, {"yesterday", false, true}, {"yes or no", false, true}} {
		t.Run(testCase.response, func(t *testing.T) {
			upstream := newRuntimeUpstream(t)
			upstream.next = func(index int, _ map[string]any) (int, string) {
				return http.StatusOK, fmt.Sprintf(`{"id":"judge-%d","model":"%s","provider":"OpenAI","choices":[{"message":{"content":%q}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`, index, officialJudgeModel, testCase.response)
			}
			session, _ := newRuntimeProviderSession(t, profile, upstream)
			got, err := session.Judge().Judge(context.Background(), JudgeInput{Reference: DatasetCase{
				QuestionID: "q", QuestionType: "multi-session", Question: "Q", Answer: "A",
			}, Hypothesis: "H"})
			if (err != nil) != testCase.wantError || (!testCase.wantError && got != testCase.correct) {
				t.Fatalf("correct=%v want=%v err=%v wantError=%v", got, testCase.correct, err, testCase.wantError)
			}
		})
	}
}

func TestOfficialJudgeRejectsMalformedProviderResponses(t *testing.T) {
	profile := runtimeProviderProfile(t)
	tests := map[string]struct {
		status int
		body   string
	}{
		"HTTP failure":  {http.StatusServiceUnavailable, `{"id":"j1","model":"openai/gpt-4o-2024-08-06","provider":"OpenAI","usage":{"prompt_tokens":1,"completion_tokens":0,"total_tokens":1,"cost":0}}`},
		"no choices":    {http.StatusOK, `{"id":"j1","model":"openai/gpt-4o-2024-08-06","provider":"OpenAI","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`},
		"two choices":   {http.StatusOK, `{"id":"j1","model":"openai/gpt-4o-2024-08-06","provider":"OpenAI","choices":[{"message":{"content":"yes"}},{"message":{"content":"no"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`},
		"empty content": {http.StatusOK, `{"id":"j1","model":"openai/gpt-4o-2024-08-06","provider":"OpenAI","choices":[{"message":{"content":" "}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			upstream := newRuntimeUpstream(t)
			upstream.next = func(_ int, _ map[string]any) (int, string) { return testCase.status, testCase.body }
			session, _ := newRuntimeProviderSession(t, profile, upstream)
			_, err := session.Judge().Judge(context.Background(), JudgeInput{Reference: DatasetCase{
				QuestionID: "q", QuestionType: "multi-session", Question: "Q", Answer: "A",
			}, Hypothesis: "H"})
			if err == nil {
				t.Fatal("malformed official judge response accepted")
			}
		})
	}
}

func TestProviderSessionCancellationDeadlineAndClosePreventUnboundedSpend(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	entered := make(chan struct{}, 3)
	upstream.next = func(_ int, body map[string]any) (int, string) {
		entered <- struct{}{}
		time.Sleep(250 * time.Millisecond)
		return http.StatusOK, fmt.Sprintf(`{"id":"late","model":%q,"provider":"OpenAI","choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":0}}`, body["model"])
	}
	config := runtimeProviderConfig(upstream, &recordingAuthorizer{credential: "private"})
	config.Lanes[0].RequestTimeout = 20 * time.Millisecond
	session, err := NewProviderSession(context.Background(), profile, config)
	if err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	response := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`)
	if response.Code != http.StatusBadGateway || time.Since(started) > 200*time.Millisecond {
		t.Fatalf("deadline status=%d elapsed=%s", response.Code, time.Since(started))
	}
	if _, err := session.Snapshot(context.Background()); err == nil {
		t.Fatal("timed-out provider outcome retained signable receipts")
	}
	if err := session.Close(); err != nil || session.Close() != nil {
		t.Fatal("close is not idempotent")
	}
	before := upstream.count.Load()
	if got := readerRequest(t, session, `{"model":"openai/gpt-oss-20b"}`); got.Code != http.StatusBadGateway {
		t.Fatalf("closed session status=%d", got.Code)
	}
	if upstream.count.Load() != before {
		t.Fatal("closed session issued provider request")
	}
}

func TestProviderSessionConcurrentReceiptsAreCompleteAndDigestStable(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	const count = 64
	var wait sync.WaitGroup
	errorsSeen := make(chan string, count)
	for index := 0; index < count; index++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			response := readerRequest(t, session, `{"model":"openai/gpt-oss-20b","messages":[]}`)
			if response.Code != http.StatusOK {
				errorsSeen <- response.Body.String()
			}
		}()
	}
	wait.Wait()
	close(errorsSeen)
	for message := range errorsSeen {
		t.Fatalf("concurrent response failed: %s", message)
	}
	first := evidenceByLane(t, session)[ReaderLane]
	second := evidenceByLane(t, session)[ReaderLane]
	if first != second || first.Requests != count || first.Successes != count ||
		first.ReceiptedRequests != count || first.PromptTokens != count*11 ||
		first.CompletionTokens != count*3 || first.TotalTokens != count*14 ||
		first.CostUSDmicros != 80 || !validSHA256(first.ReceiptSetSHA256) {
		t.Fatalf("concurrent evidence first=%#v second=%#v", first, second)
	}
}

func TestReceiptSetDigestIsOrderIndependentAndCommitsEveryField(t *testing.T) {
	receipts := map[string]providerReceipt{
		"b": {ID: "b", Model: "m", ReceiptProvider: "p", PromptTokens: 2, CompletionTokens: 1, TotalTokens: 3, CostUSD: "1/1000", HTTPStatus: 200},
		"a": {ID: "a", Model: "m", ReceiptProvider: "p", PromptTokens: 1, CompletionTokens: 1, TotalTokens: 2, CostUSD: "0", HTTPStatus: 200},
	}
	first := receiptSetDigest(ReaderLane, receipts)
	keys := []string{"a", "b"}
	sort.Sort(sort.Reverse(sort.StringSlice(keys)))
	reordered := make(map[string]providerReceipt)
	for _, key := range keys {
		reordered[key] = receipts[key]
	}
	if second := receiptSetDigest(ReaderLane, reordered); first != second {
		t.Fatal("map iteration order changed receipt digest")
	}
	for name, mutate := range map[string]func(*providerReceipt){
		"id":         func(r *providerReceipt) { r.ID = "other" },
		"model":      func(r *providerReceipt) { r.Model = "other" },
		"provider":   func(r *providerReceipt) { r.ReceiptProvider = "other" },
		"prompt":     func(r *providerReceipt) { r.PromptTokens++ },
		"completion": func(r *providerReceipt) { r.CompletionTokens++ },
		"total":      func(r *providerReceipt) { r.TotalTokens++ },
		"cost":       func(r *providerReceipt) { r.CostUSD = "1/2" },
		"status":     func(r *providerReceipt) { r.HTTPStatus++ },
	} {
		t.Run(name, func(t *testing.T) {
			changed := map[string]providerReceipt{"a": receipts["a"], "b": receipts["b"]}
			value := changed["a"]
			mutate(&value)
			changed["a"] = value
			if receiptSetDigest(ReaderLane, changed) == first {
				t.Fatal("receipt mutation did not change digest")
			}
		})
	}
	if receiptSetDigest(JudgeLane, receipts) == first {
		t.Fatal("receipt digest did not bind lane")
	}
}

func TestCostMicrosUsesExactRationalsAndConservativeCeiling(t *testing.T) {
	tests := map[string]uint64{
		"0": 0, "1/1000000": 1, "1/2000000": 1, "5/4000000": 2,
		"123456789/1000000": 123456789,
	}
	for value, want := range tests {
		cost, ok := new(big.Rat).SetString(value)
		if !ok {
			t.Fatal(value)
		}
		got, ok := costMicrosCeil(cost)
		if !ok || got != want {
			t.Fatalf("cost %s micros=%d,%v want=%d", value, got, ok, want)
		}
	}
	if _, ok := costMicrosCeil(big.NewRat(-1, 1)); ok {
		t.Fatal("negative cost represented")
	}
}

func TestSnapshotHonorsCanceledCallerAndReturnsIndependentValues(t *testing.T) {
	profile := runtimeProviderProfile(t)
	upstream := newRuntimeUpstream(t)
	session, _ := newRuntimeProviderSession(t, profile, upstream)
	if _, err := session.Snapshot(nil); err == nil {
		t.Fatal("nil snapshot context accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := session.Snapshot(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("snapshot error=%v", err)
	}
	first, err := session.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	first[0].Provider = "mutated"
	second, err := session.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if second[0].Provider == "mutated" || second[0].Lane != JudgeLane || second[1].Lane != ReaderLane {
		t.Fatalf("snapshot alias/order=%#v", second)
	}
}
