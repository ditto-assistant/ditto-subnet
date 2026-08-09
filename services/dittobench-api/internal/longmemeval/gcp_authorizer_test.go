package longmemeval

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"hash/crc32"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testMetadataToken      = "metadata-access-token-private"
	testProviderCredential = "provider-api-key-private"
	testSecretResource     = "projects/ditto-prod/secrets/openrouter-reader/versions/7"
)

type gcpAuthorizerFixture struct {
	server        *httptest.Server
	metadataCalls atomic.Int64
	secretCalls   atomic.Int64
	metadata      func(http.ResponseWriter, *http.Request)
	secret        func(http.ResponseWriter, *http.Request)
}

func newGCPAuthorizerFixture(t *testing.T) *gcpAuthorizerFixture {
	t.Helper()
	fixture := &gcpAuthorizerFixture{}
	fixture.metadata = func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Metadata-Flavor") != "Google" || request.Header.Get("Authorization") != "" {
			t.Errorf("metadata headers=%#v", request.Header)
		}
		writer.Header().Set("Metadata-Flavor", "Google")
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{"access_token":"`+testMetadataToken+`","expires_in":3599,"token_type":"Bearer"}`)
	}
	fixture.secret = func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer "+testMetadataToken || request.Header.Get("Metadata-Flavor") != "" {
			t.Errorf("Secret Manager headers=%#v", request.Header)
		}
		writeSecretManagerPayload(writer, testSecretResource, []byte(testProviderCredential))
	}
	fixture.server = httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/computeMetadata/v1/instance/service-accounts/default/token":
			fixture.metadataCalls.Add(1)
			fixture.metadata(writer, request)
		case "/v1/" + testSecretResource + ":access":
			fixture.secretCalls.Add(1)
			fixture.secret(writer, request)
		default:
			writer.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(fixture.server.Close)
	return fixture
}

func writeSecretManagerPayload(writer http.ResponseWriter, name string, value []byte) {
	checksum := crc32.Checksum(value, crc32.MakeTable(crc32.Castagnoli))
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{
		"name": name,
		"payload": map[string]string{
			"data": base64.StdEncoding.EncodeToString(value), "dataCrc32c": fmt.Sprint(checksum),
		},
	})
}

func newTestGCPAuthorizer(t *testing.T, fixture *gcpAuthorizerFixture, maximum int64) *GCPSecretManagerAuthorizer {
	t.Helper()
	authorizer, err := NewGCPSecretManagerAuthorizer(GCPSecretManagerAuthorizerConfig{
		MetadataTokenURL:     fixture.server.URL + "/computeMetadata/v1/instance/service-accounts/default/token",
		SecretManagerBaseURL: fixture.server.URL,
		HTTPClient:           fixture.server.Client(), MaxSecretBytes: maximum,
	})
	if err != nil {
		t.Fatal(err)
	}
	return authorizer
}

func testSecretReference() SecretManagerReference {
	return SecretManagerReference{ProjectID: "ditto-prod", SecretID: "openrouter-reader", Version: "7"}
}

func TestGCPAuthorizerResolvesExactImmutableReferenceInRequestMemory(t *testing.T) {
	fixture := newGCPAuthorizerFixture(t)
	authorizer := newTestGCPAuthorizer(t, fixture, 0)
	for index := 0; index < 2; index++ {
		request, err := http.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Authorization", "Bearer attacker-controlled")
		if err := authorizer.Authorize(context.Background(), testSecretReference(), request); err != nil {
			t.Fatal(err)
		}
		if request.Header.Get("Authorization") != "Bearer "+testProviderCredential {
			t.Fatal("provider authorization did not receive resolved fake credential")
		}
	}
	// Deliberately resolve each time: provider credential bytes live only for
	// the outbound request and are not cached in the session or authorizer.
	if fixture.metadataCalls.Load() != 2 || fixture.secretCalls.Load() != 2 {
		t.Fatalf("metadata calls=%d secret calls=%d", fixture.metadataCalls.Load(), fixture.secretCalls.Load())
	}
}

func TestGCPAuthorizerAcceptsCanonicalProjectNumberResponseForTextProjectID(t *testing.T) {
	fixture := newGCPAuthorizerFixture(t)
	fixture.secret = func(writer http.ResponseWriter, _ *http.Request) {
		writeSecretManagerPayload(writer, "projects/123456789012/secrets/openrouter-reader/versions/7", []byte(testProviderCredential))
	}
	authorizer := newTestGCPAuthorizer(t, fixture, 0)
	request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
	if err := authorizer.Authorize(context.Background(), testSecretReference(), request); err != nil {
		t.Fatal(err)
	}
	if request.Header.Get("Authorization") != "Bearer "+testProviderCredential {
		t.Fatal("provider authorization did not receive canonical-project fake credential")
	}
}

func TestSecretVersionResponseNameValidationFailsClosed(t *testing.T) {
	reference := testSecretReference()
	tests := map[string]struct {
		name      string
		reference SecretManagerReference
		want      bool
	}{
		"exact project ID":         {testSecretResource, reference, true},
		"canonical project number": {"projects/123456789012/secrets/openrouter-reader/versions/7", reference, true},
		"wrong textual project":    {"projects/other-prod/secrets/openrouter-reader/versions/7", reference, false},
		"wrong secret":             {"projects/ditto-prod/secrets/openrouter-judge/versions/7", reference, false},
		"wrong version":            {"projects/ditto-prod/secrets/openrouter-reader/versions/8", reference, false},
		"wrong shape":              {"projects/ditto-prod/secrets/openrouter-reader", reference, false},
		"numeric input exact": {"projects/123456789012/secrets/openrouter-reader/versions/7",
			SecretManagerReference{ProjectID: "123456789012", SecretID: "openrouter-reader", Version: "7"}, true},
		"numeric input mismatch": {"projects/999999999999/secrets/openrouter-reader/versions/7",
			SecretManagerReference{ProjectID: "123456789012", SecretID: "openrouter-reader", Version: "7"}, false},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			if got := matchesSecretVersionName(testCase.name, testCase.reference); got != testCase.want {
				t.Fatalf("matches=%v want=%v", got, testCase.want)
			}
		})
	}
}

func TestGCPAuthorizerConfigurationHasStrictTrustedOrigins(t *testing.T) {
	validMetadata := defaultMetadataTokenURL
	validSecret := defaultSecretManagerURL
	tests := map[string]GCPSecretManagerAuthorizerConfig{
		"metadata HTTPS":      {MetadataTokenURL: "https://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"},
		"metadata other host": {MetadataTokenURL: "http://example.com/computeMetadata/v1/instance/service-accounts/default/token"},
		"metadata wrong path": {MetadataTokenURL: "http://metadata.google.internal/computeMetadata/v1/project/project-id"},
		"metadata query":      {MetadataTokenURL: validMetadata + "?recursive=true"},
		"metadata credential": {MetadataTokenURL: "http://user:pass@metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"},
		"secret HTTP":         {MetadataTokenURL: validMetadata, SecretManagerBaseURL: "http://secretmanager.googleapis.com"},
		"secret other host":   {MetadataTokenURL: validMetadata, SecretManagerBaseURL: "https://example.com"},
		"secret path":         {MetadataTokenURL: validMetadata, SecretManagerBaseURL: validSecret + "/v1"},
		"secret query":        {MetadataTokenURL: validMetadata, SecretManagerBaseURL: validSecret + "?target=other"},
		"secret credential":   {MetadataTokenURL: validMetadata, SecretManagerBaseURL: "https://user:pass@secretmanager.googleapis.com"},
		"negative size":       {MetadataTokenURL: validMetadata, SecretManagerBaseURL: validSecret, MaxSecretBytes: -1},
		"excessive size":      {MetadataTokenURL: validMetadata, SecretManagerBaseURL: validSecret, MaxSecretBytes: (1 << 20) + 1},
	}
	for name, config := range tests {
		t.Run(name, func(t *testing.T) {
			if authorizer, err := NewGCPSecretManagerAuthorizer(config); err == nil || authorizer != nil {
				t.Fatalf("untrusted config accepted: %#v", authorizer)
			}
		})
	}
	if authorizer, err := NewGCPSecretManagerAuthorizer(GCPSecretManagerAuthorizerConfig{}); err != nil ||
		authorizer.metadataTokenURL != defaultMetadataTokenURL || authorizer.secretManagerBaseURL != defaultSecretManagerURL ||
		authorizer.maxSecretBytes != defaultMaxSecretBytes {
		t.Fatalf("default authorizer=%#v err=%v", authorizer, err)
	}
}

func TestGCPAuthorizerRejectsMutableOrMalformedReferencesBeforeNetwork(t *testing.T) {
	fixture := newGCPAuthorizerFixture(t)
	authorizer := newTestGCPAuthorizer(t, fixture, 0)
	tests := map[string]func(*SecretManagerReference){
		"empty project":  func(r *SecretManagerReference) { r.ProjectID = "" },
		"project slash":  func(r *SecretManagerReference) { r.ProjectID = "a/b" },
		"empty secret":   func(r *SecretManagerReference) { r.SecretID = "" },
		"secret slash":   func(r *SecretManagerReference) { r.SecretID = "a/b" },
		"latest":         func(r *SecretManagerReference) { r.Version = "latest" },
		"version alias":  func(r *SecretManagerReference) { r.Version = "prod" },
		"zero version":   func(r *SecretManagerReference) { r.Version = "0" },
		"empty version":  func(r *SecretManagerReference) { r.Version = "" },
		"header newline": func(r *SecretManagerReference) { r.SecretID = "secret\r\nX-Evil" },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			reference := testSecretReference()
			mutate(&reference)
			request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
			if err := authorizer.Authorize(context.Background(), reference, request); err == nil {
				t.Fatal("invalid Secret Manager reference accepted")
			}
			if request.Header.Get("Authorization") != "" {
				t.Fatal("invalid reference modified provider request")
			}
		})
	}
	if fixture.metadataCalls.Load() != 0 || fixture.secretCalls.Load() != 0 {
		t.Fatal("invalid reference reached GCP")
	}
}

func TestValidateSecretManagerReferenceIsMaskedAndSideEffectFree(t *testing.T) {
	if err := ValidateSecretManagerReference(testSecretReference()); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*SecretManagerReference){
		"project": func(reference *SecretManagerReference) { reference.ProjectID = "private/project" },
		"secret":  func(reference *SecretManagerReference) { reference.SecretID = "private/secret" },
		"version": func(reference *SecretManagerReference) { reference.Version = "latest-private" },
	} {
		t.Run(name, func(t *testing.T) {
			reference := testSecretReference()
			mutate(&reference)
			err := ValidateSecretManagerReference(reference)
			if err == nil || err.Error() != "Secret Manager reference is invalid" {
				t.Fatal("invalid Secret Manager reference did not return the masked validation error")
			}
			for _, sensitive := range []string{reference.ProjectID, reference.SecretID, reference.Version} {
				if sensitive != "" && strings.Contains(err.Error(), sensitive) {
					t.Fatal("Secret Manager reference validation error exposed a reference component")
				}
			}
		})
	}
}

func TestGCPAuthorizerRejectsMetadataFailuresWithoutEchoingBodies(t *testing.T) {
	tests := map[string]func(http.ResponseWriter, *http.Request){
		"HTTP status": func(writer http.ResponseWriter, _ *http.Request) {
			writer.WriteHeader(http.StatusForbidden)
			_, _ = io.WriteString(writer, testProviderCredential)
		},
		"missing flavor": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(writer, `{"access_token":"token","expires_in":1,"token_type":"Bearer"}`)
		},
		"missing token": func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Metadata-Flavor", "Google")
			_, _ = io.WriteString(writer, `{"access_token":"","expires_in":1,"token_type":"Bearer"}`)
		},
		"expired": func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Metadata-Flavor", "Google")
			_, _ = io.WriteString(writer, `{"access_token":"token","expires_in":0,"token_type":"Bearer"}`)
		},
		"wrong type": func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Metadata-Flavor", "Google")
			_, _ = io.WriteString(writer, `{"access_token":"token","expires_in":1,"token_type":"Basic"}`)
		},
		"unknown field": func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Metadata-Flavor", "Google")
			_, _ = io.WriteString(writer, `{"access_token":"token","expires_in":1,"token_type":"Bearer","credential":"`+testProviderCredential+`"}`)
		},
		"trailing": func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Metadata-Flavor", "Google")
			_, _ = io.WriteString(writer, `{"access_token":"token","expires_in":1,"token_type":"Bearer"}{}`)
		},
		"oversized": func(writer http.ResponseWriter, _ *http.Request) {
			writer.Header().Set("Metadata-Flavor", "Google")
			_, _ = io.WriteString(writer, strings.Repeat("x", maxMetadataResponse+1))
		},
	}
	for name, handler := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newGCPAuthorizerFixture(t)
			fixture.metadata = handler
			authorizer := newTestGCPAuthorizer(t, fixture, 0)
			request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
			err := authorizer.Authorize(context.Background(), testSecretReference(), request)
			if err == nil {
				t.Fatal("invalid metadata response was accepted")
			}
			if strings.Contains(err.Error(), testProviderCredential) {
				t.Fatal("metadata error exposed fake credential material")
			}
			if request.Header.Get("Authorization") != "" {
				t.Fatal("metadata failure modified provider authorization")
			}
			if fixture.secretCalls.Load() != 0 {
				t.Fatal("invalid metadata token reached Secret Manager")
			}
		})
	}
}

func TestGCPAuthorizerRejectsSecretManagerFailuresWithoutEchoingBodies(t *testing.T) {
	tests := map[string]func(http.ResponseWriter, *http.Request){
		"HTTP status": func(writer http.ResponseWriter, _ *http.Request) {
			writer.WriteHeader(http.StatusForbidden)
			_, _ = io.WriteString(writer, testProviderCredential)
		},
		"wrong name": func(writer http.ResponseWriter, _ *http.Request) {
			writeSecretManagerPayload(writer, "projects/other/secrets/other/versions/1", []byte(testProviderCredential))
		},
		"missing payload": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(writer, `{"name":"`+testSecretResource+`"}`)
		},
		"missing data": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(writer, `{"name":"`+testSecretResource+`","payload":{"data":"","dataCrc32c":"0"}}`)
		},
		"bad base64": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(writer, `{"name":"`+testSecretResource+`","payload":{"data":"%%%","dataCrc32c":"0"}}`)
		},
		"bad CRC": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(writer, `{"name":"`+testSecretResource+`","payload":{"data":"`+base64.StdEncoding.EncodeToString([]byte(testProviderCredential))+`","dataCrc32c":"0"}}`)
		},
		"non-decimal CRC": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(writer, `{"name":"`+testSecretResource+`","payload":{"data":"`+base64.StdEncoding.EncodeToString([]byte(testProviderCredential))+`","dataCrc32c":"bad"}}`)
		},
		"newline credential": func(writer http.ResponseWriter, _ *http.Request) {
			writeSecretManagerPayload(writer, testSecretResource, []byte("key\nother"))
		},
		"unknown field": func(writer http.ResponseWriter, _ *http.Request) {
			checksum := crc32.Checksum([]byte(testProviderCredential), crc32.MakeTable(crc32.Castagnoli))
			_, _ = io.WriteString(writer, fmt.Sprintf(`{"name":%q,"payload":{"data":%q,"dataCrc32c":%q},"secret":%q}`,
				testSecretResource, base64.StdEncoding.EncodeToString([]byte(testProviderCredential)), fmt.Sprint(checksum), testProviderCredential))
		},
		"trailing": func(writer http.ResponseWriter, _ *http.Request) {
			writeSecretManagerPayload(writer, testSecretResource, []byte(testProviderCredential))
			_, _ = io.WriteString(writer, `{}`)
		},
	}
	for name, handler := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newGCPAuthorizerFixture(t)
			fixture.secret = handler
			authorizer := newTestGCPAuthorizer(t, fixture, 0)
			request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
			err := authorizer.Authorize(context.Background(), testSecretReference(), request)
			if err == nil {
				t.Fatal("invalid Secret Manager response was accepted")
			}
			if strings.Contains(err.Error(), testProviderCredential) {
				t.Fatal("Secret Manager error exposed fake credential material")
			}
			if request.Header.Get("Authorization") != "" {
				t.Fatal("Secret Manager failure modified provider authorization")
			}
		})
	}
}

func TestGCPAuthorizerEnforcesDecodedSecretSize(t *testing.T) {
	fixture := newGCPAuthorizerFixture(t)
	fixture.secret = func(writer http.ResponseWriter, _ *http.Request) {
		writeSecretManagerPayload(writer, testSecretResource, []byte("123456789"))
	}
	authorizer := newTestGCPAuthorizer(t, fixture, 8)
	request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
	if err := authorizer.Authorize(context.Background(), testSecretReference(), request); err == nil ||
		request.Header.Get("Authorization") != "" {
		t.Fatal("oversized decoded credential accepted")
	}
}

func TestGCPAuthorizerNeverFollowsMetadataOrSecretRedirects(t *testing.T) {
	redirectTargetCalls := atomic.Int64{}
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		redirectTargetCalls.Add(1)
	}))
	defer target.Close()
	for _, stage := range []string{"metadata", "secret"} {
		t.Run(stage, func(t *testing.T) {
			fixture := newGCPAuthorizerFixture(t)
			redirect := func(writer http.ResponseWriter, _ *http.Request) {
				writer.Header().Set("Location", target.URL)
				writer.WriteHeader(http.StatusFound)
			}
			if stage == "metadata" {
				fixture.metadata = redirect
			} else {
				fixture.secret = redirect
			}
			authorizer := newTestGCPAuthorizer(t, fixture, 0)
			request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
			if err := authorizer.Authorize(context.Background(), testSecretReference(), request); err == nil {
				t.Fatal("redirected credential request accepted")
			}
		})
	}
	if redirectTargetCalls.Load() != 0 {
		t.Fatal("credential-bearing redirect was followed")
	}
}

func TestGCPAuthorizerHonorsCancellationAtBothGCPCalls(t *testing.T) {
	for _, stage := range []string{"metadata", "secret"} {
		t.Run(stage, func(t *testing.T) {
			fixture := newGCPAuthorizerFixture(t)
			block := func(_ http.ResponseWriter, request *http.Request) { <-request.Context().Done() }
			if stage == "metadata" {
				fixture.metadata = block
			} else {
				fixture.secret = block
			}
			authorizer := newTestGCPAuthorizer(t, fixture, 0)
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
			defer cancel()
			request := httptest.NewRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", nil)
			started := time.Now()
			if err := authorizer.Authorize(ctx, testSecretReference(), request); err == nil ||
				time.Since(started) > time.Second || request.Header.Get("Authorization") != "" {
				t.Fatalf("cancellation err=%v elapsed=%s", err, time.Since(started))
			}
		})
	}
}

func TestProviderRuntimeConfigNeverSerializesServerOwnedSecretReference(t *testing.T) {
	config := ProviderLaneRuntimeConfig{
		Lane: ReaderLane, UpstreamURL: "https://openrouter.ai/api/v1/chat/completions",
		RouteProvider: "openai", ReceiptProvider: "OpenAI", RequestTimeout: time.Second,
		CredentialReference: testSecretReference(),
	}
	raw, err := json.Marshal(config)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"ProjectID", "project_id", "ditto-prod", "openrouter-reader", "Version"} {
		if strings.Contains(string(raw), forbidden) {
			t.Fatalf("serialized runtime config exposed server-owned reference %q: %s", forbidden, raw)
		}
	}
}

func TestSecretBytesResolverReturnsFreshCallerOwnedBytesAndRequiresZeroing(t *testing.T) {
	fixture := newGCPAuthorizerFixture(t)
	resolver := newTestGCPAuthorizer(t, fixture, 0)
	first, err := resolver.Resolve(context.Background(), testSecretReference())
	if err != nil {
		t.Fatal(err)
	}
	second, err := resolver.Resolve(context.Background(), testSecretReference())
	if err != nil {
		ZeroSecretBytes(first)
		t.Fatal(err)
	}
	if string(first) != testProviderCredential || string(second) != testProviderCredential {
		ZeroSecretBytes(first)
		ZeroSecretBytes(second)
		t.Fatal("resolver returned unexpected fake key bytes")
	}
	first[0] ^= 0xff
	if string(second) != testProviderCredential {
		ZeroSecretBytes(first)
		ZeroSecretBytes(second)
		t.Fatal("resolver calls aliased caller-owned key buffers")
	}
	ZeroSecretBytes(first)
	ZeroSecretBytes(second)
	for _, value := range append(first, second...) {
		if value != 0 {
			t.Fatal("ZeroSecretBytes did not overwrite caller-owned key buffer")
		}
	}
	if fixture.metadataCalls.Load() != 2 || fixture.secretCalls.Load() != 2 {
		t.Fatal("resolver unexpectedly cached provider key material")
	}
}

func TestSecretBytesResolverMasksReferenceTransportIdentityAndCRCFailures(t *testing.T) {
	const masked = "Secret Manager value is unavailable"
	tests := map[string]func(*gcpAuthorizerFixture, *SecretManagerReference, *context.Context){
		"mutable reference": func(_ *gcpAuthorizerFixture, reference *SecretManagerReference, _ *context.Context) {
			reference.Version = "latest-sensitive-alias"
		},
		"metadata body": func(fixture *gcpAuthorizerFixture, _ *SecretManagerReference, _ *context.Context) {
			fixture.metadata = func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(http.StatusForbidden)
				_, _ = io.WriteString(writer, testProviderCredential)
			}
		},
		"resource identity": func(fixture *gcpAuthorizerFixture, _ *SecretManagerReference, _ *context.Context) {
			fixture.secret = func(writer http.ResponseWriter, _ *http.Request) {
				writeSecretManagerPayload(writer, "projects/other-prod/secrets/other/versions/9", []byte(testProviderCredential))
			}
		},
		"CRC": func(fixture *gcpAuthorizerFixture, _ *SecretManagerReference, _ *context.Context) {
			fixture.secret = func(writer http.ResponseWriter, _ *http.Request) {
				_, _ = io.WriteString(writer, `{"name":"`+testSecretResource+`","payload":{"data":"`+
					base64.StdEncoding.EncodeToString([]byte(testProviderCredential))+`","dataCrc32c":"0"}}`)
			}
		},
		"nil context": func(_ *gcpAuthorizerFixture, _ *SecretManagerReference, ctx *context.Context) {
			*ctx = nil
		},
	}
	for name, configure := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newGCPAuthorizerFixture(t)
			resolver := newTestGCPAuthorizer(t, fixture, 0)
			reference := testSecretReference()
			var ctx context.Context = context.Background()
			configure(fixture, &reference, &ctx)
			value, err := resolver.Resolve(ctx, reference)
			ZeroSecretBytes(value)
			if err == nil || value != nil {
				t.Fatal("failed secret resolution returned key bytes")
			}
			if err.Error() != masked || strings.Contains(err.Error(), testProviderCredential) ||
				strings.Contains(err.Error(), reference.ProjectID) || strings.Contains(err.Error(), reference.SecretID) ||
				strings.Contains(err.Error(), reference.Version) {
				t.Fatal("secret resolver error was not fully masked")
			}
		})
	}
}

func TestSecretBytesResolverCancellationIsMaskedAndBounded(t *testing.T) {
	fixture := newGCPAuthorizerFixture(t)
	fixture.secret = func(_ http.ResponseWriter, request *http.Request) { <-request.Context().Done() }
	resolver := newTestGCPAuthorizer(t, fixture, 0)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	started := time.Now()
	value, err := resolver.Resolve(ctx, testSecretReference())
	ZeroSecretBytes(value)
	if err == nil || value != nil || err.Error() != "Secret Manager value is unavailable" ||
		time.Since(started) > time.Second {
		t.Fatal("canceled secret resolution did not fail closed within its deadline")
	}
}
