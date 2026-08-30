package codingplatform

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

var bearerPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{32,128}$`)

// Client is one secret-owning validator-side adapter for codingrelay.Upstream.
type Client struct {
	mu sync.Mutex

	policy     codingcontract.InferencePolicy
	binding    codingrelay.Binding
	proxyURL   string
	bearer     []byte
	privateKey ed25519.PrivateKey
	httpClient *http.Client
	now        func() time.Time
	newNonce   func() string
	lastNow    time.Time
	nonces     map[string]struct{}
	closed     bool
}

// New validates and deep-owns one Platform grant capability.
func New(config Config) (*Client, error) {
	now := time.Now().UTC()
	if config.Now != nil {
		now = config.Now().UTC()
	}
	validated, err := validateConfig(config, now)
	if err != nil {
		return nil, err
	}
	return &Client{
		policy:     validated.policy,
		binding:    validated.binding,
		proxyURL:   validated.proxyURL,
		bearer:     validated.bearer,
		privateKey: validated.privateKey,
		httpClient: validated.httpClient,
		now:        validated.now,
		newNonce:   validated.newNonce,
		lastNow:    now,
		nonces:     make(map[string]struct{}, int(validated.policy.MaxRequests+validated.policy.MaxRetries)),
	}, nil
}

type validatedConfig struct {
	policy     codingcontract.InferencePolicy
	binding    codingrelay.Binding
	proxyURL   string
	bearer     []byte
	privateKey ed25519.PrivateKey
	httpClient *http.Client
	now        func() time.Time
	newNonce   func() string
}

func validateConfig(config Config, now time.Time) (validatedConfig, error) {
	var zero validatedConfig
	policy := config.Policy
	if err := policy.Validate(); err != nil {
		return zero, ErrInvalidConfig
	}
	binding := config.Capability.Binding
	binding.IssuedAt = binding.IssuedAt.UTC()
	binding.Deadline = binding.Deadline.UTC()
	grantSHA256, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil || !validBinding(binding, policy, grantSHA256, now) {
		return zero, ErrInvalidConfig
	}
	proxyURL, err := validatedProxyURL(config.Capability.ProxyURL)
	if err != nil || !bearerPattern.MatchString(config.Capability.Bearer) {
		return zero, ErrInvalidConfig
	}
	privateKey := append(ed25519.PrivateKey(nil), config.Capability.BrokerPrivateKey...)
	if len(privateKey) != ed25519.PrivateKeySize {
		zeroBytes(privateKey)
		return zero, ErrInvalidConfig
	}
	publicKey, ok := privateKey.Public().(ed25519.PublicKey)
	if !ok || base64.RawURLEncoding.EncodeToString(publicKey) != strings.TrimSuffix(config.Capability.BrokerPublicKey, "=") {
		zeroBytes(privateKey)
		return zero, ErrInvalidConfig
	}
	transport := config.Transport
	if transport == nil {
		transport = newDefaultTransport()
	} else if nilInterface(transport) {
		zeroBytes(privateKey)
		return zero, ErrInvalidConfig
	}
	client := &http.Client{
		Transport: transport,
		Timeout:   time.Duration(policy.RequestTimeoutMilliseconds) * time.Millisecond,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	nowFunction := config.Now
	if nowFunction == nil {
		nowFunction = time.Now
	}
	nonceFunction := config.NewNonce
	if nonceFunction == nil {
		nonceFunction = uuid.NewString
	}
	return validatedConfig{
		policy: policy, binding: binding, proxyURL: proxyURL,
		bearer: []byte(config.Capability.Bearer), privateKey: privateKey,
		httpClient: client, now: nowFunction, newNonce: nonceFunction,
	}, nil
}

func newDefaultTransport() *http.Transport {
	return &http.Transport{
		Proxy:                  nil,
		DialContext:            (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:      true,
		MaxIdleConns:           2,
		MaxIdleConnsPerHost:    1,
		MaxConnsPerHost:        2,
		IdleConnTimeout:        90 * time.Second,
		TLSHandshakeTimeout:    10 * time.Second,
		ExpectContinueTimeout:  time.Second,
		MaxResponseHeaderBytes: 64 << 10,
		TLSClientConfig:        &tls.Config{MinVersion: tls.VersionTLS12},
	}
}

func validBinding(
	binding codingrelay.Binding,
	policy codingcontract.InferencePolicy,
	grantSHA256 string,
	now time.Time,
) bool {
	return validIdentifier(binding.AttemptID, 256) &&
		validSHA256(binding.AgentArtifactSHA256) &&
		validIdentifier(binding.HarnessInstanceID, 256) &&
		canonicalUUID(binding.TicketID) &&
		validIdentifier(binding.CaseID, 256) &&
		validIdentifier(binding.ProfileCapabilityID, 256) &&
		canonicalUUID(binding.GrantID) &&
		binding.Generation > 0 && binding.Generation <= 1<<31-1 &&
		binding.InferenceGrantSHA256 == grantSHA256 &&
		!binding.IssuedAt.IsZero() && !binding.Deadline.IsZero() &&
		!binding.IssuedAt.After(now) && binding.Deadline.After(now) &&
		binding.Deadline.After(binding.IssuedAt) &&
		binding.Deadline.Sub(binding.IssuedAt) <= 2*time.Hour &&
		binding.RequestBudget > 0 && binding.RequestBudget <= policy.MaxRequests &&
		binding.PromptTokenBudget > 0 && binding.PromptTokenBudget <= policy.MaxPromptTokens &&
		binding.CompletionTokenBudget > 0 && binding.CompletionTokenBudget <= policy.MaxCompletionTokens
}

func validatedProxyURL(raw string) (string, error) {
	if len(raw) == 0 || len(raw) > 2048 || !utf8.ValidString(raw) {
		return "", ErrInvalidConfig
	}
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" ||
		parsed.Path != dispatchAPIPath || parsed.EscapedPath() != dispatchAPIPath ||
		(parsed.Port() != "" && parsed.Port() != "443") {
		return "", ErrInvalidConfig
	}
	return parsed.String(), nil
}

func nilInterface(value any) bool {
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

// Complete implements codingrelay.Upstream. It performs exactly one HTTP
// dispatch; receipt-free model retries remain owned by codingrelay.
func (client *Client) Complete(
	ctx context.Context,
	request codingrelay.UpstreamRequest,
) (codingrelay.UpstreamResult, error) {
	if client == nil || ctx == nil {
		return codingrelay.UpstreamResult{}, ErrInvalidRequest
	}
	if err := ctx.Err(); err != nil {
		return codingrelay.UpstreamResult{}, err
	}
	prepared, err := client.prepare(request)
	if err != nil {
		return codingrelay.UpstreamResult{}, err
	}
	defer prepared.clear()

	callContext, cancel := context.WithDeadline(ctx, prepared.deadline)
	defer cancel()
	httpRequest, err := http.NewRequestWithContext(
		callContext,
		http.MethodPost,
		prepared.proxyURL,
		bytes.NewReader(prepared.body),
	)
	if err != nil {
		return codingrelay.UpstreamResult{}, ErrInvalidRequest
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	httpRequest.Header.Set("Accept", "application/json")
	httpRequest.Header.Set("Cache-Control", "no-store")
	httpRequest.Header.Set("Authorization", "Bearer "+string(prepared.bearer))
	httpRequest.Header.Set("X-Ditto-Grant", prepared.grantID)
	httpRequest.Header.Set("X-Ditto-Generation", strconv.FormatUint(uint64(prepared.generation), 10))
	httpRequest.Header.Set("X-Ditto-Nonce", prepared.nonce)
	httpRequest.Header.Set("X-Ditto-Requested-At", isoformatMicro(prepared.requestedAt))
	httpRequest.Header.Set("X-Ditto-Proof", prepared.proof)
	defer httpRequest.Header.Del("Authorization")

	response, err := client.httpClient.Do(httpRequest)
	if err != nil {
		return codingrelay.UpstreamResult{}, ErrTransport
	}
	defer response.Body.Close()
	maximum := dispatchResponseMaximum(client.policy)
	body, readErr := io.ReadAll(io.LimitReader(response.Body, int64(maximum)+1))
	defer zeroBytes(body)
	if readErr != nil || len(body) == 0 || len(body) > maximum {
		return codingrelay.UpstreamResult{}, ErrUnsettledResponse
	}
	if response.StatusCode != http.StatusOK || !isJSON(response.Header.Get("Content-Type")) ||
		!hasNoStore(response.Header.Values("Cache-Control")) {
		return codingrelay.UpstreamResult{}, ErrUnsettledResponse
	}
	parsed, err := parseDispatchResponse(body, client.policy)
	if err != nil {
		return codingrelay.UpstreamResult{}, ErrResponseIntegrity
	}
	defer zeroBytes(parsed.NormalizedResponse)
	defer zeroBytes(parsed.FailureResponseProjection)
	if !responseMatches(prepared, parsed, client.policy) {
		return codingrelay.UpstreamResult{}, ErrResponseIntegrity
	}
	return codingrelay.UpstreamResult{
		Settlement:                parsed.Settlement.Clone(),
		NormalizedResponse:        append([]byte(nil), parsed.NormalizedResponse...),
		FailureResponseProjection: append([]byte(nil), parsed.FailureResponseProjection...),
	}, nil
}

type preparedRequest struct {
	proxyURL    string
	body        []byte
	bearer      []byte
	grantID     string
	ticketID    string
	caseID      string
	profileID   string
	grantSHA256 string
	generation  uint32
	nonce       string
	requestedAt time.Time
	deadline    time.Time
	request     codingrelay.UpstreamRequest
	proof       string
}

func (prepared *preparedRequest) clear() {
	zeroBytes(prepared.bearer)
	zeroBytes(prepared.body)
	clearLockedRequest(&prepared.request.LockedRequest)
	prepared.request = codingrelay.UpstreamRequest{}
}

func (client *Client) prepare(request codingrelay.UpstreamRequest) (preparedRequest, error) {
	now := client.now().UTC()
	nonce := client.newNonce()
	client.mu.Lock()
	defer client.mu.Unlock()
	if client.closed {
		return preparedRequest{}, ErrCapabilityClosed
	}
	if now.Before(client.lastNow) {
		client.closeLocked()
		return preparedRequest{}, ErrClockRollback
	}
	client.lastNow = now
	if !client.binding.Deadline.After(now) {
		client.closeLocked()
		return preparedRequest{}, ErrCapabilityExpired
	}
	if err := validateUpstreamRequest(request, client.policy, client.binding); err != nil {
		return preparedRequest{}, err
	}
	if !canonicalUUID(nonce) {
		client.closeLocked()
		return preparedRequest{}, ErrInvalidConfig
	}
	if _, duplicate := client.nonces[nonce]; duplicate {
		client.closeLocked()
		return preparedRequest{}, ErrInvalidConfig
	}
	if len(client.nonces) >= int(client.policy.MaxRequests+client.policy.MaxRetries) {
		client.closeLocked()
		return preparedRequest{}, ErrInvalidConfig
	}
	client.nonces[nonce] = struct{}{}
	wire := dispatchRequest{
		Schema: dispatchRequestSchema, CodingContractVersion: codingcontract.ContractVersion,
		WeightEligible: false, TicketID: client.binding.TicketID,
		CaseID: client.binding.CaseID, ProfileCapabilityID: client.binding.ProfileCapabilityID,
		InferenceGrantSHA256: client.binding.InferenceGrantSHA256,
		GrantID:              client.binding.GrantID, Generation: client.binding.Generation,
		Sequence: request.Sequence, RequestSequence: request.RequestSequence, Attempt: request.Attempt,
		RequestID: request.RequestID, LockedRequestSHA256: request.LockedRequestSHA256,
		LockedRequest: cloneLockedRequest(request.LockedRequest),
		Deadline:      isoformatMicro(client.binding.Deadline),
	}
	body, err := json.Marshal(wire)
	clearLockedRequest(&wire.LockedRequest)
	if err != nil || uint64(len(body)) > client.policy.MaxRequestBytes+maximumEnvelopeBytes {
		zeroBytes(body)
		return preparedRequest{}, ErrInvalidRequest
	}
	bearer := append([]byte(nil), client.bearer...)
	privateKey := append(ed25519.PrivateKey(nil), client.privateKey...)
	proof := base64.RawURLEncoding.EncodeToString(ed25519.Sign(
		privateKey,
		dispatchProofMessage(client.binding.GrantID, client.binding.Generation, nonce, now, body),
	))
	zeroBytes(privateKey)
	deadline := client.binding.Deadline
	policyDeadline := now.Add(time.Duration(client.policy.RequestTimeoutMilliseconds) * time.Millisecond)
	if policyDeadline.Before(deadline) {
		deadline = policyDeadline
	}
	return preparedRequest{
		proxyURL: client.proxyURL, body: body, bearer: bearer,
		grantID: client.binding.GrantID, generation: client.binding.Generation,
		ticketID: client.binding.TicketID, caseID: client.binding.CaseID,
		profileID: client.binding.ProfileCapabilityID, grantSHA256: client.binding.InferenceGrantSHA256,
		nonce: nonce, requestedAt: now, deadline: deadline, request: cloneUpstreamRequest(request), proof: proof,
	}, nil
}

func validateUpstreamRequest(
	request codingrelay.UpstreamRequest,
	policy codingcontract.InferencePolicy,
	binding codingrelay.Binding,
) error {
	if request.Sequence == 0 || request.Sequence > policy.MaxRequests+policy.MaxRetries ||
		request.RequestSequence == 0 || request.RequestSequence > binding.RequestBudget ||
		request.Attempt == 0 || request.Attempt > policy.MaxAttemptsPerRequest ||
		!canonicalUUID(request.RequestID) || !validSHA256(request.LockedRequestSHA256) ||
		!request.Deadline.Equal(binding.Deadline) {
		return ErrInvalidRequest
	}
	locked := cloneLockedRequest(request.LockedRequest)
	defer clearLockedRequest(&locked)
	if err := locked.ValidateAgainst(policy); err != nil {
		return ErrInvalidRequest
	}
	digest, err := codingcontract.InferenceLockedRequestSHA256(policy, locked)
	if err != nil || digest != request.LockedRequestSHA256 {
		return ErrInvalidRequest
	}
	return nil
}

func dispatchProofMessage(
	grantID string,
	generation uint32,
	nonce string,
	requestedAt time.Time,
	body []byte,
) []byte {
	digest := sha256.Sum256(body)
	return []byte(fmt.Sprintf(
		"ditto-inference:v1:%s:%d:%s:%s:%s",
		grantID,
		generation,
		nonce,
		isoformatMicro(requestedAt),
		hex.EncodeToString(digest[:]),
	))
}

func isoformatMicro(value time.Time) string {
	return value.UTC().Format("2006-01-02T15:04:05.000000") + "+00:00"
}

func isJSON(value string) bool {
	mediaType, _, err := mime.ParseMediaType(value)
	return err == nil && strings.EqualFold(mediaType, "application/json")
}

func hasNoStore(values []string) bool {
	for _, value := range values {
		for _, directive := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(directive), "no-store") {
				return true
			}
		}
	}
	return false
}

func dispatchResponseMaximum(policy codingcontract.InferencePolicy) int {
	return int((policy.MaxResponseBytes+2)/3*4) + maximumEnvelopeBytes
}

func responseMatches(
	request preparedRequest,
	response dispatchResponse,
	policy codingcontract.InferencePolicy,
) bool {
	settlement := response.Settlement
	if response.Schema != dispatchResponseSchema ||
		response.CodingContractVersion != codingcontract.ContractVersion || response.WeightEligible ||
		response.Sequence != request.request.Sequence ||
		settlement.TicketID != request.ticketID ||
		settlement.CaseID != request.caseID || settlement.ProfileCapabilityID != request.profileID ||
		settlement.InferenceGrantSHA256 != request.grantSHA256 ||
		settlement.GrantID != request.grantID || settlement.Generation != request.generation ||
		settlement.RequestID != request.request.RequestID ||
		settlement.RequestSequence != request.request.RequestSequence ||
		settlement.Attempt != request.request.Attempt ||
		settlement.LockedRequestSHA256 != request.request.LockedRequestSHA256 {
		return false
	}
	if err := settlement.Validate(policy); err != nil {
		return false
	}
	return responseProjectionMatches(policy, response)
}

func responseProjectionMatches(
	policy codingcontract.InferencePolicy,
	response dispatchResponse,
) bool {
	settlement := response.Settlement
	switch settlement.Outcome {
	case codingcontract.InferenceReceiptComplete:
		if len(response.NormalizedResponse) == 0 || len(response.FailureResponseProjection) != 0 ||
			settlement.ResponseSHA256 == nil {
			return false
		}
		normalized, err := codingcontract.ParseInferenceNormalizedResponse(response.NormalizedResponse, policy)
		if err != nil {
			return false
		}
		digest, err := codingcontract.InferenceNormalizedResponseSHA256(policy, normalized)
		return err == nil && digest == *settlement.ResponseSHA256
	case codingcontract.InferenceReceiptFreeRetry:
		return len(response.NormalizedResponse) == 0 && len(response.FailureResponseProjection) == 0 &&
			settlement.ResponseSHA256 == nil
	case codingcontract.InferenceReceiptProviderFailed:
		if len(response.NormalizedResponse) != 0 {
			return false
		}
		if settlement.ResponseSHA256 == nil {
			return len(response.FailureResponseProjection) == 0
		}
		if len(response.FailureResponseProjection) == 0 {
			return false
		}
		digest, err := codingcontract.InferenceCanonicalResponseProjectionSHA256(
			policy,
			response.FailureResponseProjection,
		)
		return err == nil && digest == *settlement.ResponseSHA256
	default:
		return false
	}
}

// Close revokes this local client capability and zeroes its retained secret
// buffers. It does not revoke the durable Platform grant.
func (client *Client) Close() error {
	if client == nil {
		return nil
	}
	client.mu.Lock()
	if client.closed {
		client.mu.Unlock()
		return nil
	}
	client.closeLocked()
	transport := client.httpClient.Transport
	client.mu.Unlock()
	if closer, ok := transport.(interface{ CloseIdleConnections() }); ok {
		closer.CloseIdleConnections()
	}
	return nil
}

func (client *Client) closeLocked() {
	client.closed = true
	zeroBytes(client.bearer)
	zeroBytes(client.privateKey)
	client.binding = codingrelay.Binding{}
	client.proxyURL = ""
	clear(client.nonces)
}

func (client *Client) String() string { return "CodingPlatformClient{private}" }

func (client *Client) GoString() string { return client.String() }

func (client *Client) LogValue() slog.Value {
	return slog.StringValue("coding-platform-client")
}

func (client *Client) MarshalJSON() ([]byte, error) {
	return nil, ErrSecretSerialization
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed.String() == value && parsed != uuid.Nil
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil && strings.ToLower(value) == value
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || !utf8.ValidString(value) || len([]byte(value)) > maximum {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func cloneLockedRequest(value codingcontract.InferenceLockedRequest) codingcontract.InferenceLockedRequest {
	value.Messages = cloneRawMessages(value.Messages)
	value.Tools = cloneTools(value.Tools)
	value.Provider.Only = append([]string(nil), value.Provider.Only...)
	value.Provider.Order = append([]string(nil), value.Provider.Order...)
	return value
}

func cloneUpstreamRequest(value codingrelay.UpstreamRequest) codingrelay.UpstreamRequest {
	value.LockedRequest = cloneLockedRequest(value.LockedRequest)
	value.Deadline = value.Deadline.UTC()
	return value
}

func cloneRawMessages(values []json.RawMessage) []json.RawMessage {
	if values == nil {
		return nil
	}
	result := make([]json.RawMessage, len(values))
	for index, value := range values {
		result[index] = append(json.RawMessage(nil), value...)
	}
	return result
}

func cloneTools(values []codingcontract.InferenceTool) []codingcontract.InferenceTool {
	if values == nil {
		return nil
	}
	result := make([]codingcontract.InferenceTool, len(values))
	for index, value := range values {
		result[index] = value
		result[index].Function.Parameters = append(json.RawMessage(nil), value.Function.Parameters...)
	}
	return result
}

func clearLockedRequest(value *codingcontract.InferenceLockedRequest) {
	if value == nil {
		return
	}
	for index := range value.Messages {
		zeroBytes(value.Messages[index])
	}
	for index := range value.Tools {
		zeroBytes(value.Tools[index].Function.Parameters)
	}
	*value = codingcontract.InferenceLockedRequest{}
}

func zeroBytes(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

var _ codingrelay.Upstream = (*Client)(nil)
var _ json.Marshaler = (*Client)(nil)
var _ fmt.Stringer = (*Client)(nil)
var _ slog.LogValuer = (*Client)(nil)
