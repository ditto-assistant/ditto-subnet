package codingplatform

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

type typedNilTransport struct{}

func (*typedNilTransport) RoundTrip(*http.Request) (*http.Response, error) {
	panic("typed nil transport must not be called")
}

func TestConfigFailsClosedOnAuthorityAndTransportDrift(t *testing.T) {
	fixture := newPlatformFixture(t)
	var typedNil *typedNilTransport
	tests := map[string]func(*Config){
		"typed nil transport": func(value *Config) { value.Transport = typedNil },
		"plaintext URL": func(value *Config) {
			value.Capability.ProxyURL = "http://relay.invalid" + dispatchAPIPath
		},
		"wrong path": func(value *Config) {
			value.Capability.ProxyURL = "https://relay.invalid/api/v1/inference/chat/completions"
		},
		"URL credentials": func(value *Config) {
			value.Capability.ProxyURL = "https://user:pass@relay.invalid" + dispatchAPIPath
		},
		"URL query":       func(value *Config) { value.Capability.ProxyURL += "?target=other" },
		"bearer":          func(value *Config) { value.Capability.Bearer = "short" },
		"public key":      func(value *Config) { value.Capability.BrokerPublicKey = strings.Repeat("A", 43) },
		"private key":     func(value *Config) { value.Capability.BrokerPrivateKey = ed25519.PrivateKey{1} },
		"grant policy":    func(value *Config) { value.Capability.Binding.InferenceGrantSHA256 = strings.Repeat("a", 64) },
		"generation":      func(value *Config) { value.Capability.Binding.Generation = 0 },
		"expired":         func(value *Config) { value.Capability.Binding.Deadline = fixture.now },
		"future issuance": func(value *Config) { value.Capability.Binding.IssuedAt = fixture.now.Add(time.Second) },
		"lifetime": func(value *Config) {
			value.Capability.Binding.IssuedAt = fixture.now.Add(-time.Minute)
			value.Capability.Binding.Deadline = fixture.now.Add(3 * time.Hour)
		},
		"request budget": func(value *Config) { value.Capability.Binding.RequestBudget = 0 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			config := fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
				t.Fatal("invalid config reached transport")
				return nil, nil
			}))
			mutate(&config)
			if _, err := New(config); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("err=%v", err)
			}
		})
	}
}

func TestDefaultTransportIsPrivateBoundedAndNoRedirect(t *testing.T) {
	fixture := newPlatformFixture(t)
	client, err := New(fixture.config(nil))
	if err != nil {
		t.Fatal(err)
	}
	transport, ok := client.httpClient.Transport.(*http.Transport)
	if !ok || transport.Proxy != nil || transport.DialContext == nil ||
		transport.TLSClientConfig == nil || transport.TLSClientConfig.MinVersion != tls.VersionTLS12 ||
		transport.MaxResponseHeaderBytes != 64<<10 || transport.MaxConnsPerHost != 2 {
		t.Fatalf("default transport=%#v", transport)
	}
	redirect := &http.Request{}
	if err := client.httpClient.CheckRedirect(redirect, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Fatalf("redirect err=%v", err)
	}
}

func TestRequestValidationHappensBeforeTransport(t *testing.T) {
	fixture := newPlatformFixture(t)
	var calls atomic.Int32
	client, err := New(fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls.Add(1)
		return nil, errors.New("unreachable")
	})))
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string]func(*codingrelay.UpstreamRequest){
		"sequence":         func(value *codingrelay.UpstreamRequest) { value.Sequence = 0 },
		"request sequence": func(value *codingrelay.UpstreamRequest) { value.RequestSequence = 0 },
		"attempt":          func(value *codingrelay.UpstreamRequest) { value.Attempt = 0 },
		"request ID":       func(value *codingrelay.UpstreamRequest) { value.RequestID = "not-a-uuid" },
		"locked digest":    func(value *codingrelay.UpstreamRequest) { value.LockedRequestSHA256 = strings.Repeat("a", 64) },
		"deadline":         func(value *codingrelay.UpstreamRequest) { value.Deadline = value.Deadline.Add(time.Second) },
		"model":            func(value *codingrelay.UpstreamRequest) { value.LockedRequest.Model = "other/model" },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			request := fixture.request
			mutate(&request)
			if _, err := client.Complete(t.Context(), request); !errors.Is(err, ErrInvalidRequest) {
				t.Fatalf("err=%v", err)
			}
		})
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid requests made %d transport calls", calls.Load())
	}
}

func TestHTTPFailuresAreGenericUnsettledOutcomes(t *testing.T) {
	fixture := newPlatformFixture(t)
	secret := "private-provider-error-and-task-text"
	tests := map[string]func() (*http.Response, error){
		"transport": func() (*http.Response, error) { return nil, errors.New(secret) },
		"status": func() (*http.Response, error) {
			return response(http.StatusBadGateway, []byte(`{"error":"`+secret+`"}`)), nil
		},
		"content type": func() (*http.Response, error) {
			value := response(http.StatusOK, fixture.responseBody(t, fixture.settlement, fixture.normalized, nil))
			value.Header.Set("Content-Type", "text/plain")
			return value, nil
		},
		"cacheable": func() (*http.Response, error) {
			value := response(http.StatusOK, fixture.responseBody(t, fixture.settlement, fixture.normalized, nil))
			value.Header.Del("Cache-Control")
			return value, nil
		},
		"oversize": func() (*http.Response, error) {
			return response(http.StatusOK, bytes.Repeat([]byte{'x'}, dispatchResponseMaximum(fixture.policy)+1)), nil
		},
		"redirect": func() (*http.Response, error) {
			value := response(http.StatusFound, []byte(`{"error":"redirect refused"}`))
			value.Header.Set("Location", "https://attacker.invalid/collect")
			return value, nil
		},
	}
	for name, outcome := range tests {
		t.Run(name, func(t *testing.T) {
			client, err := New(fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
				return outcome()
			})))
			if err != nil {
				t.Fatal(err)
			}
			_, err = client.Complete(t.Context(), fixture.request)
			if err == nil || strings.Contains(err.Error(), secret) ||
				(!errors.Is(err, ErrTransport) && !errors.Is(err, ErrUnsettledResponse)) {
				t.Fatalf("err=%v", err)
			}
		})
	}
}

func TestResponseParserRejectsDuplicateMissingAndTrailingAuthority(t *testing.T) {
	fixture := newPlatformFixture(t)
	valid := fixture.responseBody(t, fixture.settlement, fixture.normalized, nil)
	var missing map[string]any
	if err := json.Unmarshal(valid, &missing); err != nil {
		t.Fatal(err)
	}
	delete(missing, "weight_eligible")
	missingBody, err := json.Marshal(missing)
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string][]byte{
		"duplicate":     bytes.Replace(valid, []byte(`"schema":`), []byte(`"schema":"wrong","schema":`), 1),
		"missing":       missingBody,
		"trailing":      append(append([]byte(nil), valid...), []byte(` {}`)...),
		"invalid UTF-8": append(append([]byte(nil), valid...), 0xff),
	}
	for name, body := range tests {
		t.Run(name, func(t *testing.T) {
			client, err := New(fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
				return response(http.StatusOK, body), nil
			})))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrResponseIntegrity) {
				t.Fatalf("err=%v", err)
			}
		})
	}
}

func TestCancellationExpiryRollbackNonceReuseAndCloseFailClosed(t *testing.T) {
	fixture := newPlatformFixture(t)
	var calls atomic.Int32
	transport := roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls.Add(1)
		return response(http.StatusOK, fixture.responseBody(t, fixture.settlement, fixture.normalized, nil)), nil
	})

	t.Run("canceled", func(t *testing.T) {
		client, err := New(fixture.config(transport))
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithCancel(t.Context())
		cancel()
		if _, err := client.Complete(ctx, fixture.request); !errors.Is(err, context.Canceled) {
			t.Fatalf("err=%v", err)
		}
	})

	t.Run("expired", func(t *testing.T) {
		config := fixture.config(transport)
		current := fixture.now
		config.Now = func() time.Time { return current }
		client, err := New(config)
		if err != nil {
			t.Fatal(err)
		}
		current = fixture.binding.Deadline
		if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrCapabilityExpired) {
			t.Fatalf("err=%v", err)
		}
	})

	t.Run("rollback", func(t *testing.T) {
		config := fixture.config(transport)
		current := fixture.now
		config.Now = func() time.Time { return current }
		client, err := New(config)
		if err != nil {
			t.Fatal(err)
		}
		current = current.Add(-time.Second)
		if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrClockRollback) {
			t.Fatalf("err=%v", err)
		}
		if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrCapabilityClosed) {
			t.Fatalf("closed err=%v", err)
		}
	})

	t.Run("nonce reuse", func(t *testing.T) {
		client, err := New(fixture.config(transport))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.Complete(t.Context(), fixture.request); err != nil {
			t.Fatal(err)
		}
		if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrInvalidConfig) {
			t.Fatalf("err=%v", err)
		}
		if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrCapabilityClosed) {
			t.Fatalf("closed err=%v", err)
		}
	})

	t.Run("close", func(t *testing.T) {
		client, err := New(fixture.config(transport))
		if err != nil {
			t.Fatal(err)
		}
		if err := client.Close(); err != nil {
			t.Fatal(err)
		}
		if err := client.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrCapabilityClosed) {
			t.Fatalf("err=%v", err)
		}
		if len(client.bearer) == 0 || !allZero(client.bearer) || len(client.privateKey) == 0 || !allZero(client.privateKey) {
			t.Fatal("client retained nonzero grant secret after close")
		}
	})
}

func TestClientDeepOwnsGrantSecrets(t *testing.T) {
	fixture := newPlatformFixture(t)
	config := fixture.config(roundTripFunc(func(request *http.Request) (*http.Response, error) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Fatal(err)
		}
		proof, err := base64.RawURLEncoding.DecodeString(request.Header.Get("X-Ditto-Proof"))
		if err != nil || request.Header.Get("Authorization") != "Bearer "+fixture.capability.Bearer ||
			!ed25519.Verify(
				fixture.privateKey.Public().(ed25519.PublicKey),
				dispatchProofMessage(
					fixture.binding.GrantID,
					fixture.binding.Generation,
					request.Header.Get("X-Ditto-Nonce"),
					fixture.now,
					body,
				),
				proof,
			) {
			t.Fatal("client did not deep-own grant secrets")
		}
		return response(http.StatusOK, fixture.responseBody(t, fixture.settlement, fixture.normalized, nil)), nil
	}))
	client, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	zeroBytes(config.Capability.BrokerPrivateKey)
	config.Capability.Bearer = strings.Repeat("x", len(config.Capability.Bearer))
	config.Capability.Binding.CaseID = "mutated-case"
	if _, err := client.Complete(t.Context(), fixture.request); err != nil {
		t.Fatalf("client aliased caller-owned capability: %v", err)
	}
}

func TestClientAcceptsCaseInsensitiveJSONContentType(t *testing.T) {
	fixture := newPlatformFixture(t)
	transport := roundTripFunc(func(*http.Request) (*http.Response, error) {
		result := response(http.StatusOK, fixture.responseBody(t, fixture.settlement, fixture.normalized, nil))
		result.Header.Set("Content-Type", "Application/JSON")
		return result, nil
	})
	client, err := New(fixture.config(transport))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Complete(t.Context(), fixture.request); err != nil {
		t.Fatalf("err=%v", err)
	}
}

func TestNowCallbackDoesNotDeadlockClose(t *testing.T) {
	fixture := newPlatformFixture(t)
	client, err := New(fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return response(http.StatusOK, fixture.responseBody(t, fixture.settlement, fixture.normalized, nil)), nil
	})))
	if err != nil {
		t.Fatal(err)
	}
	closed := make(chan struct{})
	client.now = func() time.Time {
		go func() {
			_ = client.Close()
			close(closed)
		}()
		return fixture.now
	}
	_, _ = client.Complete(t.Context(), fixture.request)
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("Close blocked behind Now while prepare held the mutex")
	}
}

func TestSecretsCannotBeFormattedSerializedOrLogged(t *testing.T) {
	fixture := newPlatformFixture(t)
	client, err := New(fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, errors.New("unused")
	})))
	if err != nil {
		t.Fatal(err)
	}
	secret := fixture.capability.Bearer
	for _, value := range []any{fixture.capability, fixture.config(nil), client} {
		for _, rendered := range []string{fmt.Sprint(value), fmt.Sprintf("%+v", value), fmt.Sprintf("%#v", value)} {
			if strings.Contains(rendered, secret) || strings.Contains(rendered, base64Key(fixture.privateKey)) {
				t.Fatalf("formatted secret: %q", rendered)
			}
		}
		if _, err := json.Marshal(value); !errors.Is(err, ErrSecretSerialization) {
			t.Fatalf("marshal err=%v", err)
		}
		var output bytes.Buffer
		logger := slog.New(slog.NewJSONHandler(&output, nil))
		logger.Info("value", "value", value)
		if strings.Contains(output.String(), secret) || strings.Contains(output.String(), base64Key(fixture.privateKey)) {
			t.Fatalf("logged secret: %s", output.String())
		}
	}
}

func allZero(value []byte) bool {
	for _, item := range value {
		if item != 0 {
			return false
		}
	}
	return true
}

func base64Key(value []byte) string {
	return base64.RawURLEncoding.EncodeToString(value)
}
