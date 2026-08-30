// Package codinggrantrevoke implements the narrow validator-to-Platform grant
// revocation capability. It owns no inference dispatch credential or provider
// route and zeros its bearer after the exact generation is durably revoked.
package codinggrantrevoke

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/google/uuid"
)

const (
	revokePath           = "/api/v1/validator/coding-shadow/inference-revoke-capability"
	maximumEnvelopeBytes = 64 << 10
)

var (
	ErrInvalid    = errors.New("coding grant revocation authority is invalid")
	ErrTransport  = errors.New("coding grant revocation transport failed")
	ErrResponse   = errors.New("coding grant revocation response is invalid")
	ErrSecret     = errors.New("coding grant revocation private state cannot be serialized")
	bearerPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{32,128}$`)
)

type Config struct {
	Capability codingplatform.RevocationCapability
	Binding    codingrelay.Binding
	Transport  http.RoundTripper
	Now        func() time.Time
	Timeout    time.Duration
}

type Revoker struct {
	mu sync.Mutex

	capability codingplatform.RevocationCapability
	binding    codingrelay.Binding
	bearer     []byte
	client     *http.Client
	now        func() time.Time
	committed  bool
	closed     bool
}

type capabilityRevokeRequest struct {
	GrantID    string `json:"grant_id"`
	TicketID   string `json:"ticket_id"`
	Generation uint32 `json:"generation"`
}

type capabilityRevokeResponse struct {
	Schema                string    `json:"schema"`
	CodingContractVersion int       `json:"coding_contract_version"`
	WeightEligible        bool      `json:"weight_eligible"`
	GrantID               string    `json:"grant_id"`
	TicketID              string    `json:"ticket_id"`
	Status                string    `json:"status"`
	Generation            uint32    `json:"generation"`
	RevokedAt             time.Time `json:"revoked_at"`
	Idempotent            *bool     `json:"idempotent"`
}

func New(config Config) (*Revoker, error) {
	now := time.Now().UTC()
	if config.Now != nil {
		now = config.Now().UTC()
	}
	binding := config.Binding
	binding.IssuedAt = binding.IssuedAt.UTC()
	binding.Deadline = binding.Deadline.UTC()
	capability := config.Capability
	capability.Deadline = capability.Deadline.UTC()
	if !validCapability(capability, binding, now) {
		return nil, ErrInvalid
	}
	bearer := []byte(capability.Bearer)
	capability.Bearer = ""
	transport := config.Transport
	if transport == nil {
		transport = defaultTransport()
	} else if nilLike(transport) {
		return nil, ErrInvalid
	}
	timeout := config.Timeout
	if timeout == 0 {
		timeout = 30 * time.Second
	}
	if timeout < time.Second || timeout > 2*time.Minute {
		return nil, ErrInvalid
	}
	nowFunction := config.Now
	if nowFunction == nil {
		nowFunction = time.Now
	}
	return &Revoker{
		capability: capability, binding: binding, bearer: bearer,
		client: &http.Client{Transport: transport, Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
		now: nowFunction,
	}, nil
}

func validCapability(capability codingplatform.RevocationCapability, binding codingrelay.Binding, now time.Time) bool {
	return validBinding(binding, now) && canonicalUUID(capability.GrantID) && canonicalUUID(capability.TicketID) &&
		capability.GrantID == binding.GrantID && capability.TicketID == binding.TicketID &&
		capability.Generation == binding.Generation && capability.Generation > 0 &&
		capability.InferenceGrantSHA256 == binding.InferenceGrantSHA256 && lowerSHA256(capability.InferenceGrantSHA256) &&
		capability.Deadline.Equal(binding.Deadline) && capability.Deadline.After(now) &&
		!capability.Deadline.After(now.Add(2*time.Hour)) && bearerPattern.MatchString(capability.Bearer) &&
		validRevokeURL(capability.URL)
}

func validBinding(binding codingrelay.Binding, now time.Time) bool {
	return validIdentifier(binding.AttemptID, 256) && lowerSHA256(binding.AgentArtifactSHA256) &&
		validIdentifier(binding.HarnessInstanceID, 256) && canonicalUUID(binding.TicketID) &&
		validIdentifier(binding.CaseID, 256) && validIdentifier(binding.ProfileCapabilityID, 256) &&
		canonicalUUID(binding.GrantID) && binding.Generation > 0 && lowerSHA256(binding.InferenceGrantSHA256) &&
		!binding.IssuedAt.IsZero() && !binding.IssuedAt.After(now) && binding.Deadline.After(now) &&
		binding.Deadline.After(binding.IssuedAt) && !binding.Deadline.After(now.Add(2*time.Hour)) &&
		binding.RequestBudget > 0 && binding.PromptTokenBudget > 0 && binding.CompletionTokenBudget > 0
}

func validRevokeURL(value string) bool {
	if value == "" || len(value) > 2048 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	return err == nil && parsed.Scheme == "https" && parsed.Hostname() != "" && parsed.User == nil &&
		parsed.RawQuery == "" && parsed.Fragment == "" && (parsed.Port() == "" || parsed.Port() == "443") &&
		strings.HasSuffix(parsed.Path, revokePath)
}

func defaultTransport() *http.Transport {
	return &http.Transport{
		Proxy:             nil,
		DialContext:       (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2: true, MaxIdleConns: 2, MaxIdleConnsPerHost: 1, MaxConnsPerHost: 1,
		IdleConnTimeout: 30 * time.Second, TLSHandshakeTimeout: 10 * time.Second,
		ExpectContinueTimeout: time.Second, MaxResponseHeaderBytes: 32 << 10,
		TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12},
	}
}

func (revoker *Revoker) Revoke(ctx context.Context, expected codinggateway.GrantRevocation) error {
	if revoker == nil || ctx == nil {
		return ErrInvalid
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	revoker.mu.Lock()
	if revoker.closed || !revocationsMatch(expected, revoker.binding) {
		revoker.mu.Unlock()
		return ErrInvalid
	}
	if revoker.committed {
		revoker.mu.Unlock()
		return nil
	}
	capability := revoker.capability
	binding := revoker.binding
	bearer := append([]byte(nil), revoker.bearer...)
	client := revoker.client
	clock := revoker.now
	revoker.mu.Unlock()
	defer zero(bearer)

	body, err := json.Marshal(capabilityRevokeRequest{
		GrantID: capability.GrantID, TicketID: capability.TicketID,
		Generation: capability.Generation,
	})
	if err != nil {
		return ErrInvalid
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, capability.URL, bytes.NewReader(body))
	if err != nil {
		return ErrInvalid
	}
	request.Header.Set("Authorization", "Bearer "+string(bearer))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Cache-Control", "no-store")
	response, err := client.Do(request)
	if err != nil {
		return ErrTransport
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || !strings.Contains(strings.ToLower(response.Header.Get("Cache-Control")), "no-store") {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))
		return ErrTransport
	}
	mediaType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if err != nil || !strings.EqualFold(mediaType, "application/json") {
		return ErrResponse
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, maximumEnvelopeBytes+1))
	if err != nil || len(raw) == 0 || len(raw) > maximumEnvelopeBytes ||
		codingcontract.ValidateJSONDocument(raw, maximumEnvelopeBytes) != nil {
		return ErrResponse
	}
	var shape map[string]json.RawMessage
	if json.Unmarshal(raw, &shape) != nil {
		return ErrResponse
	}
	for _, field := range []string{
		"schema", "coding_contract_version", "weight_eligible", "grant_id", "ticket_id",
		"status", "generation", "revoked_at", "idempotent",
	} {
		if _, ok := shape[field]; !ok {
			return ErrResponse
		}
	}
	var result capabilityRevokeResponse
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if decoder.Decode(&result) != nil {
		return ErrResponse
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return ErrResponse
	}
	now := clock().UTC()
	if result.Schema != "dittobench-coding-inference-revocation-v1" ||
		result.CodingContractVersion != codingcontract.ContractVersion || result.WeightEligible ||
		result.GrantID != capability.GrantID || result.TicketID != capability.TicketID ||
		result.Generation != capability.Generation || result.Status != "revoked" ||
		result.RevokedAt.Before(binding.IssuedAt) || result.RevokedAt.After(now.Add(5*time.Minute)) ||
		result.Idempotent == nil {
		return ErrResponse
	}
	revoker.mu.Lock()
	defer revoker.mu.Unlock()
	if revoker.committed {
		return nil
	}
	revoker.committed = true
	zero(revoker.bearer)
	revoker.bearer = nil
	revoker.client.CloseIdleConnections()
	return nil
}

func revocationsMatch(expected codinggateway.GrantRevocation, binding codingrelay.Binding) bool {
	return expected.TicketID == binding.TicketID && expected.CaseID == binding.CaseID &&
		expected.ProfileCapabilityID == binding.ProfileCapabilityID && expected.GrantID == binding.GrantID &&
		expected.Generation == binding.Generation && expected.InferenceGrantSHA256 == binding.InferenceGrantSHA256 &&
		expected.Deadline.Equal(binding.Deadline)
}

func (revoker *Revoker) Close() error {
	if revoker == nil {
		return nil
	}
	revoker.mu.Lock()
	defer revoker.mu.Unlock()
	if revoker.closed {
		return nil
	}
	zero(revoker.bearer)
	revoker.bearer = nil
	revoker.client.CloseIdleConnections()
	revoker.closed = true
	return nil
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func lowerSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func zero(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

func (revoker *Revoker) String() string       { return "CodingGrantRevoker{private}" }
func (revoker *Revoker) GoString() string     { return revoker.String() }
func (revoker *Revoker) LogValue() slog.Value { return slog.StringValue("coding-grant-revoker") }
func (*Revoker) MarshalJSON() ([]byte, error) { return nil, ErrSecret }

var _ codinggateway.GrantRevoker = (*Revoker)(nil)
var _ json.Marshaler = (*Revoker)(nil)
