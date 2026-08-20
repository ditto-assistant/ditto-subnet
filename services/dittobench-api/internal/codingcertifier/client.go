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
		return HealthResponse{}, err
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
		return RunResponse{}, err
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
		return 0, errors.New("coding harness request context is required")
	}
	var body io.Reader
	if requestBody != nil {
		encoded, err := json.Marshal(requestBody)
		if err != nil {
			return 0, errors.New("encode coding harness request")
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, client.baseURL+path, body)
	if err != nil {
		return 0, errors.New("build coding harness request")
	}
	request.Header.Set("Accept", "application/json")
	if requestBody != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.client.Do(request)
	if err != nil {
		return 0, fmt.Errorf("coding harness request failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))
		return response.StatusCode, fmt.Errorf("coding harness returned HTTP %d", response.StatusCode)
	}
	mediaType, _, mediaErr := mime.ParseMediaType(response.Header.Get("Content-Type"))
	if mediaErr != nil || strings.ToLower(mediaType) != "application/json" {
		return response.StatusCode, errors.New("coding harness response is not JSON")
	}
	limited := io.LimitReader(response.Body, maximum+1)
	raw, err := io.ReadAll(limited)
	if err != nil || int64(len(raw)) > maximum {
		return response.StatusCode, errors.New("coding harness response exceeds its bound")
	}
	if err := validateJSONEnvelope(raw); err != nil {
		return response.StatusCode, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(responseBody); err != nil {
		return response.StatusCode, errors.New("coding harness response JSON is invalid")
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return response.StatusCode, errors.New("coding harness response contains trailing content")
	}
	return response.StatusCode, nil
}

func validateJSONEnvelope(body []byte) error {
	if err := codingcontract.ValidateRawJSONUnicode(body); err != nil {
		return errors.New("coding harness response JSON contains invalid Unicode")
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := scanJSONValue(decoder, 0); err != nil {
		return err
	}
	if token, err := decoder.Token(); !errors.Is(err, io.EOF) || token != nil {
		return errors.New("coding harness response contains trailing content")
	}
	return nil
}

func scanJSONValue(decoder *json.Decoder, depth int) error {
	if depth > 32 {
		return errors.New("coding harness response exceeds JSON depth")
	}
	token, err := decoder.Token()
	if err != nil {
		return errors.New("coding harness response JSON is invalid")
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
				return errors.New("coding harness response object key is invalid")
			}
			if _, duplicate := seen[key]; duplicate {
				return errors.New("coding harness response contains a duplicate field")
			}
			seen[key] = struct{}{}
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return errors.New("coding harness response object is incomplete")
		}
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder, depth+1); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errors.New("coding harness response array is incomplete")
		}
	default:
		return errors.New("coding harness response JSON delimiter is invalid")
	}
	return nil
}
