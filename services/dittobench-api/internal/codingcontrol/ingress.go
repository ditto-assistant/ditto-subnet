// Package codingcontrol authenticates validator-signed requests before they
// reach the private coding supervisor and publication handlers.
package codingcontrol

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

const (
	EnvelopeHeader   = "X-Dittobench-Coding-Control"
	maxEnvelopeBytes = 4 << 10
	maxRequestBytes  = 8 << 20
)

var (
	ErrInvalidConfig = errors.New("coding executor control ingress configuration is invalid")
	ErrPrivate       = errors.New("coding executor control ingress is private")
	validatorHotkey  = regexp.MustCompile(`^[1-9A-HJ-NP-Za-km-z]{47,48}$`)
	operationByPath  = map[string]string{
		"/v1/coding/supervisor/prepare":         "supervisor.prepare",
		"/v1/coding/supervisor/author":          "supervisor.author",
		"/v1/coding/supervisor/grade":           "supervisor.grade",
		"/v1/coding/supervisor/abort-authoring": "supervisor.abort-authoring",
		"/v1/coding/supervisor/abort-grading":   "supervisor.abort-grading",
		"/v1/coding/supervisor/recover":         "supervisor.recover",
		"/v1/coding/publications/prepare":       "publications.prepare",
		"/v1/coding/publications/acknowledge":   "publications.acknowledge",
		"/v1/coding/publications/pending":       "publications.pending",
		"/v1/coding/publications/open":          "publications.open",
		"/v1/coding/publications/lookup":        "publications.lookup",
	}
)

type Config struct {
	Downstream      http.Handler
	ControlToken    string
	ValidatorHotkey string
	Now             func() time.Time
	VerifySignature codingcontract.ExecutorSignatureVerifier
	MaximumNonces   int
}

type Ingress struct {
	downstream http.Handler
	token      string
	hotkey     string
	verifier   *codingcontract.ExecutorControlVerifier
}

func (config Config) String() string   { return "CodingExecutorControlIngressConfig{private}" }
func (config Config) GoString() string { return config.String() }
func (config Config) MarshalJSON() ([]byte, error) {
	return nil, ErrPrivate
}

func (ingress *Ingress) String() string {
	if ingress == nil {
		return "CodingExecutorControlIngress{nil=true}"
	}
	return "CodingExecutorControlIngress{private}"
}

func (ingress *Ingress) GoString() string { return ingress.String() }
func (ingress *Ingress) MarshalJSON() ([]byte, error) {
	return nil, ErrPrivate
}

func New(config Config) (*Ingress, error) {
	if config.Downstream == nil || !validToken(config.ControlToken) ||
		!ValidValidatorHotkey(config.ValidatorHotkey) || config.Now == nil ||
		config.VerifySignature == nil || config.MaximumNonces < 1 || config.MaximumNonces > 65536 {
		return nil, ErrInvalidConfig
	}
	verifier, err := codingcontract.NewExecutorControlVerifier(
		config.Now,
		config.VerifySignature,
		config.MaximumNonces,
	)
	if err != nil {
		return nil, ErrInvalidConfig
	}
	return &Ingress{
		downstream: config.Downstream,
		token:      config.ControlToken,
		hotkey:     config.ValidatorHotkey,
		verifier:   verifier,
	}, nil
}

func ValidValidatorHotkey(value string) bool {
	return validatorHotkey.MatchString(value)
}

func (ingress *Ingress) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		setPrivateHeaders(response)
		operation, pathOK := operationByPath[request.URL.Path]
		if ingress == nil || !pathOK || request.URL.RawQuery != "" {
			writeError(response, http.StatusNotFound)
			return
		}
		if request.Method != http.MethodPost {
			response.Header().Set("Allow", http.MethodPost)
			writeError(response, http.StatusMethodNotAllowed)
			return
		}
		envelope, ok := parseEnvelope(request.Header.Values(EnvelopeHeader))
		if !ok || envelope.ValidatorHotkey != ingress.hotkey ||
			envelope.Operation != operation || envelope.Method != http.MethodPost {
			writeError(response, http.StatusUnauthorized)
			return
		}
		if request.ContentLength > maxRequestBytes {
			writeError(response, http.StatusRequestEntityTooLarge)
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, maxRequestBytes))
		if err != nil || codingcontract.ValidateJSONDocument(body, maxRequestBytes) != nil {
			writeError(response, http.StatusBadRequest)
			return
		}
		if strings.HasPrefix(operation, "supervisor.") && !supervisorIdentityMatches(body, envelope) {
			writeError(response, http.StatusUnauthorized)
			return
		}
		if err := ingress.verifier.Verify(envelope, body); err != nil {
			writeError(response, http.StatusUnauthorized)
			return
		}
		request.Body = io.NopCloser(bytes.NewReader(body))
		request.ContentLength = int64(len(body))
		request.Header.Set("Content-Length", strconv.Itoa(len(body)))
		request.Header.Set("Authorization", "Bearer "+ingress.token)
		request.Header.Del(EnvelopeHeader)
		ingress.downstream.ServeHTTP(response, request)
	})
}

func parseEnvelope(values []string) (codingcontract.ExecutorControlEnvelope, bool) {
	var zero codingcontract.ExecutorControlEnvelope
	if len(values) != 1 || len(values[0]) == 0 || len(values[0]) > base64.RawURLEncoding.EncodedLen(maxEnvelopeBytes) {
		return zero, false
	}
	raw, err := base64.RawURLEncoding.DecodeString(values[0])
	if err != nil || len(raw) == 0 || len(raw) > maxEnvelopeBytes ||
		codingcontract.ValidateJSONDocument(raw, maxEnvelopeBytes) != nil {
		return zero, false
	}
	var envelope codingcontract.ExecutorControlEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil || envelope.Validate() != nil {
		return zero, false
	}
	return envelope, true
}

func supervisorIdentityMatches(body []byte, envelope codingcontract.ExecutorControlEnvelope) bool {
	var identity struct {
		TicketID    string `json:"ticket_id"`
		CodingRunID string `json:"coding_run_id"`
	}
	return json.Unmarshal(body, &identity) == nil &&
		identity.TicketID == envelope.TicketID &&
		identity.CodingRunID == envelope.CodingRunID
}

func validToken(value string) bool {
	return len(value) >= 32 && len(value) <= 4096 && strings.TrimSpace(value) == value &&
		!strings.ContainsAny(value, "\x00\r\n")
}

func setPrivateHeaders(response http.ResponseWriter) {
	response.Header().Set("Cache-Control", "no-store")
	response.Header().Set("Content-Type", "application/json")
	response.Header().Set("X-Content-Type-Options", "nosniff")
}

func writeError(response http.ResponseWriter, status int) {
	response.WriteHeader(status)
	_, _ = response.Write([]byte(`{"error":"executor_control_rejected"}` + "\n"))
}
