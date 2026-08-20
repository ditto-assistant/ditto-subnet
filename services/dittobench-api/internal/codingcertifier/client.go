package codingcertifier

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

const (
	maxHealthResponseBytes = 16 << 10
	maxSeedResponseBytes   = 64 << 10
	maxRunResponseBytes    = 64 << 10
)

// ErrCodingUnsupported means the harness has no coding endpoint. It is a
// backwards-compatible core-only result, not a failed coding score.
var ErrCodingUnsupported = errors.New("coding capability is unsupported")

// HarnessFailureKind separates validator transport from candidate HTTP or
// protocol behavior. The certifier combines this with authoritative event
// count before assigning a terminal domain.
type HarnessFailureKind string

const (
	HarnessFailureTransport HarnessFailureKind = "transport"
	HarnessFailureTimeout   HarnessFailureKind = "timeout"
	HarnessFailureHTTP      HarnessFailureKind = "http"
	HarnessFailureProtocol  HarnessFailureKind = "protocol"
)

// HarnessError is one bounded phase-specific harness failure.
type HarnessError struct {
	Operation  string
	Kind       HarnessFailureKind
	HTTPStatus int
	Err        error
}

func (failure *HarnessError) Error() string {
	if failure.HTTPStatus != 0 {
		return fmt.Sprintf("coding harness %s failed with HTTP %d: %v", failure.Operation, failure.HTTPStatus, failure.Err)
	}
	return fmt.Sprintf("coding harness %s %s failure: %v", failure.Operation, failure.Kind, failure.Err)
}

func (failure *HarnessError) Unwrap() error { return failure.Err }

func harnessFailure(operation string, kind HarnessFailureKind, status int, err error) error {
	return &HarnessError{Operation: operation, Kind: kind, HTTPStatus: status, Err: err}
}

// HTTPHarnessClient is a strict client for one validator-started harness. The
// caller must supply a transport authorized for that sandbox origin.
type HTTPHarnessClient struct {
	baseURL string
	client  *http.Client
}

// NewHTTPHarnessClient constructs a redirect-free client for one exact
// harness origin.
func NewHTTPHarnessClient(baseURL string, client *http.Client) (*HTTPHarnessClient, error) {
	if client == nil {
		return nil, errors.New("coding harness HTTP client is required")
	}
	parsed, err := url.Parse(baseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || (parsed.Path != "" && parsed.Path != "/") || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, errors.New("coding harness base URL is invalid")
	}
	copy := *client
	copy.Jar = nil
	copy.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &HTTPHarnessClient{baseURL: strings.TrimRight(baseURL, "/"), client: &copy}, nil
}

// Health probes the additive coding endpoint with a bounded response.
func (client *HTTPHarnessClient) Health(ctx context.Context) (HealthResponse, error) {
	var response HealthResponse
	status, err := client.do(ctx, http.MethodGet, "/coding/health", nil, maxHealthResponseBytes, &response)
	if status == http.StatusNotFound || status == http.StatusMethodNotAllowed {
		return HealthResponse{}, ErrCodingUnsupported
	}
	if err != nil {
		return HealthResponse{}, err
	}
	if err := response.validate(); err != nil {
		return HealthResponse{}, harnessFailure("health", HarnessFailureProtocol, status, err)
	}
	return response.normalized(), nil
}

// Seed installs one exact task-scoped memory bundle.
func (client *HTTPHarnessClient) Seed(
	ctx context.Context,
	request codingcontract.SeedRequest,
) (SeedResponse, error) {
	if err := request.Validate(); err != nil {
		return SeedResponse{}, err
	}
	var response SeedResponse
	_, err := client.do(ctx, http.MethodPost, "/coding/seed", request, maxSeedResponseBytes, &response)
	return response, err
}

// Run invokes one coding canary. The response is advisory; the validator-owned
// workspace remains the patch authority.
func (client *HTTPHarnessClient) Run(
	ctx context.Context,
	request codingcontract.RunRequest,
) (RunResponse, error) {
	if err := request.Validate(); err != nil {
		return RunResponse{}, err
	}
	var response RunResponse
	_, err := client.do(ctx, http.MethodPost, "/coding/run", request, maxRunResponseBytes, &response)
	if err != nil {
		return RunResponse{}, err
	}
	if err := response.validate(request); err != nil {
		return RunResponse{}, harnessFailure("run", HarnessFailureProtocol, http.StatusOK, err)
	}
	return response, nil
}

func (client *HTTPHarnessClient) do(
	ctx context.Context,
	method string,
	path string,
	requestBody any,
	maximum int64,
	responseBody any,
) (int, error) {
	if ctx == nil {
		return 0, harnessFailure(path, HarnessFailureProtocol, 0, errors.New("request context is required"))
	}
	var body io.Reader
	if requestBody != nil {
		encoded, err := json.Marshal(requestBody)
		if err != nil {
			return 0, harnessFailure(path, HarnessFailureProtocol, 0, errors.New("encode request"))
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, client.baseURL+path, body)
	if err != nil {
		return 0, harnessFailure(path, HarnessFailureProtocol, 0, errors.New("build request"))
	}
	request.Header.Set("Accept", "application/json")
	if requestBody != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.client.Do(request)
	if err != nil {
		kind := HarnessFailureTransport
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			kind = HarnessFailureTimeout
		}
		return 0, harnessFailure(strings.TrimPrefix(path, "/coding/"), kind, 0, err)
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))
		return response.StatusCode, harnessFailure(
			strings.TrimPrefix(path, "/coding/"), HarnessFailureHTTP, response.StatusCode, errors.New("non-success response"),
		)
	}
	mediaType, _, mediaErr := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if mediaErr != nil || strings.ToLower(mediaType) != "application/json" {
		return response.StatusCode, harnessFailure(
			strings.TrimPrefix(path, "/coding/"), HarnessFailureProtocol, response.StatusCode, errors.New("response is not JSON"),
		)
	}
	limited := io.LimitReader(response.Body, maximum+1)
	raw, err := io.ReadAll(limited)
	if err != nil || int64(len(raw)) > maximum {
		return response.StatusCode, harnessFailure(
			strings.TrimPrefix(path, "/coding/"), HarnessFailureProtocol, response.StatusCode, errors.New("response exceeds bound"),
		)
	}
	if err := validateJSONEnvelope(raw); err != nil {
		return response.StatusCode, harnessFailure(
			strings.TrimPrefix(path, "/coding/"), HarnessFailureProtocol, response.StatusCode, err,
		)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(responseBody); err != nil {
		return response.StatusCode, harnessFailure(
			strings.TrimPrefix(path, "/coding/"), HarnessFailureProtocol, response.StatusCode, errors.New("response JSON is invalid"),
		)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return response.StatusCode, harnessFailure(
			strings.TrimPrefix(path, "/coding/"), HarnessFailureProtocol, response.StatusCode, errors.New("response contains trailing content"),
		)
	}
	return response.StatusCode, nil
}

func validateJSONEnvelope(body []byte) error {
	if err := codingcontract.ValidateRawJSONUnicode(body); err != nil {
		return errors.New("JSON contains invalid Unicode")
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := scanJSONValue(decoder, 0); err != nil {
		return err
	}
	if token, err := decoder.Token(); !errors.Is(err, io.EOF) || token != nil {
		return errors.New("JSON contains trailing content")
	}
	return nil
}

func scanJSONValue(decoder *json.Decoder, depth int) error {
	if depth > 32 {
		return errors.New("JSON exceeds the depth limit")
	}
	token, err := decoder.Token()
	if err != nil {
		return errors.New("JSON is invalid")
	}
	delimiter, structured := token.(json.Delim)
	if !structured {
		return nil
	}
	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			key, ok := keyToken.(string)
			if err != nil || !ok {
				return errors.New("JSON object key is invalid")
			}
			if _, duplicate := seen[key]; duplicate {
				return errors.New("JSON contains a duplicate field")
			}
			seen[key] = struct{}{}
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return errors.New("JSON object is incomplete")
		}
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errors.New("JSON array is incomplete")
		}
	default:
		return errors.New("JSON delimiter is invalid")
	}
	return nil
}
