package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/pprofserver"
)

const (
	chatUpstreamURL      = "https://openrouter.ai/api/v1/chat/completions"
	readerModel          = "openai/gpt-4.1"
	officialJudgeModel   = "gpt-4o-2024-08-06"
	openRouterJudgeModel = "openai/gpt-4o-2024-08-06"
	readerRevision       = "longmemeval-openrouter-gpt41-reader-v1"
	judgeRevision        = "longmemeval-official-gpt4o-openrouter-v1"
	maximumBody          = 4 << 20
	maximumResponse      = 16 << 20
	maximumAttempts      = 4
)

type profile struct {
	Name          string
	Revision      string
	AcceptedModel string
	UpstreamModel string
	ProviderOrder []string
	AllowFallback bool
	DefaultPort   int
	AllowedPaths  map[string]bool
	HTTPReferer   string
	Title         string
}

func profileFor(name string) (profile, error) {
	switch name {
	case "reader":
		return profile{
			Name: "reader", Revision: readerRevision, AcceptedModel: readerModel,
			UpstreamModel: readerModel, ProviderOrder: []string{"openai"},
			AllowFallback: false, DefaultPort: 18437,
			AllowedPaths: map[string]bool{"/v1/chat/completions": true, "/chat/completions": true},
			HTTPReferer:  "https://github.com/ditto-assistant/dittobench-api",
			Title:        "Ditto top-five LongMemEval reader",
		}, nil
	case "judge":
		return profile{
			Name: "judge", Revision: judgeRevision, AcceptedModel: officialJudgeModel,
			UpstreamModel: openRouterJudgeModel, ProviderOrder: []string{"openai"},
			AllowFallback: false, DefaultPort: 18436,
			AllowedPaths: map[string]bool{"/v1/chat/completions": true},
			HTTPReferer:  "https://github.com/xiaowu0162/LongMemEval",
			Title:        "LongMemEval official evaluator",
		}, nil
	default:
		return profile{}, errors.New("LONGMEMEVAL_PROXY_PROFILE must be reader or judge")
	}
}

func configuredReaderProfile(
	selected profile,
	acceptedModel, upstreamModel, providerOrder, revision string,
	allowFallback bool,
) (profile, error) {
	if selected.Name != "reader" {
		return profile{}, errors.New("only the reader profile is configurable")
	}
	acceptedModel = strings.TrimSpace(acceptedModel)
	upstreamModel = strings.TrimSpace(upstreamModel)
	revision = strings.TrimSpace(revision)
	if acceptedModel == "" || upstreamModel == "" || revision == "" {
		return profile{}, errors.New("reader accepted model, upstream model, and revision are required")
	}

	var providers []string
	providerOrder = strings.TrimSpace(providerOrder)
	if providerOrder != "" && providerOrder != "auto" {
		seen := make(map[string]bool)
		for _, raw := range strings.Split(providerOrder, ",") {
			provider := strings.TrimSpace(raw)
			if provider == "" || seen[provider] {
				return profile{}, errors.New("reader provider order must contain unique non-empty names")
			}
			seen[provider] = true
			providers = append(providers, provider)
		}
	}

	changed := acceptedModel != selected.AcceptedModel || upstreamModel != selected.UpstreamModel ||
		!slicesEqual(providers, selected.ProviderOrder) || allowFallback != selected.AllowFallback
	if changed && revision == selected.Revision {
		return profile{}, errors.New("a custom reader configuration requires a distinct revision")
	}
	selected.AcceptedModel = acceptedModel
	selected.UpstreamModel = upstreamModel
	selected.ProviderOrder = providers
	selected.AllowFallback = allowFallback
	selected.Revision = revision
	return selected, nil
}

func slicesEqual(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

type proxy struct {
	profile          profile
	apiKey           string
	upstreamURL      string
	client           *http.Client
	requests         atomic.Uint64
	successes        atomic.Uint64
	failures         atomic.Uint64
	promptTokens     atomic.Uint64
	completionTokens atomic.Uint64
	latencyMS        atomic.Uint64
}

func newProxy(selected profile, apiKey, upstreamURL string, client *http.Client) *proxy {
	if client == nil {
		client = &http.Client{
			Timeout:       300 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}
	}
	return &proxy{profile: selected, apiKey: strings.TrimSpace(apiKey), upstreamURL: upstreamURL, client: client}
}

func rewriteRequest(selected profile, raw []byte) ([]byte, error) {
	var request map[string]any
	if json.Unmarshal(raw, &request) != nil || request == nil {
		return nil, errors.New("request must be an object")
	}
	model, _ := request["model"].(string)
	if model != selected.AcceptedModel {
		return nil, fmt.Errorf("model must be %s", selected.AcceptedModel)
	}
	if stream, _ := request["stream"].(bool); stream {
		return nil, errors.New("streaming is not supported")
	}
	request["model"] = selected.UpstreamModel
	request["stream"] = false
	provider := map[string]any{
		"allow_fallbacks": selected.AllowFallback,
		"data_collection": "deny",
	}
	if len(selected.ProviderOrder) > 0 {
		provider["only"] = selected.ProviderOrder
	}
	request["provider"] = provider
	return json.Marshal(request)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func retryable(status int) bool {
	return status == http.StatusRequestTimeout || status == http.StatusTooManyRequests || status >= 500
}

func (p *proxy) forward(ctx context.Context, body []byte) (int, []byte, error) {
	var lastStatus int
	for attempt := 1; attempt <= maximumAttempts; attempt++ {
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, p.upstreamURL, bytes.NewReader(body))
		if err != nil {
			return 0, nil, err
		}
		request.Header.Set("Authorization", "Bearer "+p.apiKey)
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("HTTP-Referer", p.profile.HTTPReferer)
		request.Header.Set("X-Title", p.profile.Title)
		started := time.Now()
		response, err := p.client.Do(request)
		p.latencyMS.Add(uint64(time.Since(started).Milliseconds()))
		if err != nil {
			if attempt < maximumAttempts && ctx.Err() == nil {
				select {
				case <-time.After(time.Duration(1<<(attempt-1)) * time.Second):
					continue
				case <-ctx.Done():
					return 0, nil, ctx.Err()
				}
			}
			return 0, nil, err
		}
		responseBody, readErr := io.ReadAll(io.LimitReader(response.Body, maximumResponse+1))
		_ = response.Body.Close()
		if readErr != nil || len(responseBody) > maximumResponse {
			return 0, nil, errors.New("OpenRouter returned an unreadable response")
		}
		lastStatus = response.StatusCode
		if retryable(response.StatusCode) && attempt < maximumAttempts {
			select {
			case <-time.After(time.Duration(1<<(attempt-1)) * time.Second):
				continue
			case <-ctx.Done():
				return 0, nil, ctx.Err()
			}
		}
		return response.StatusCode, responseBody, nil
	}
	return lastStatus, nil, errors.New("OpenRouter retry budget exhausted")
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet && r.URL.Path == "/health" {
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "ok", "profile_revision": p.profile.Revision,
			"profile": p.profile.Name, "accepted_model": p.profile.AcceptedModel,
			"upstream_model":  p.profile.UpstreamModel,
			"provider_order":  p.profile.ProviderOrder,
			"allow_fallbacks": p.profile.AllowFallback,
			"requests":        p.requests.Load(), "successes": p.successes.Load(),
			"failures": p.failures.Load(), "prompt_tokens": p.promptTokens.Load(),
			"completion_tokens": p.completionTokens.Load(), "provider_latency_ms": p.latencyMS.Load(),
		})
		return
	}
	if r.Method != http.MethodPost || !p.profile.AllowedPaths[r.URL.Path] {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	p.requests.Add(1)
	if p.apiKey == "" {
		p.failures.Add(1)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "OpenRouter proxy unavailable"})
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, maximumBody+1))
	if err != nil || len(raw) == 0 || len(raw) > maximumBody {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "invalid request size"})
		return
	}
	body, err := rewriteRequest(p.profile, raw)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	status, responseBody, err := p.forward(r.Context(), body)
	if err != nil {
		p.failures.Add(1)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "OpenRouter proxy unavailable"})
		return
	}
	if status >= 200 && status < 300 {
		p.successes.Add(1)
		var usage struct {
			Usage *struct {
				PromptTokens     int `json:"prompt_tokens"`
				CompletionTokens int `json:"completion_tokens"`
			} `json:"usage"`
		}
		if json.Unmarshal(responseBody, &usage) == nil && usage.Usage != nil &&
			usage.Usage.PromptTokens >= 0 && usage.Usage.CompletionTokens >= 0 {
			p.promptTokens.Add(uint64(usage.Usage.PromptTokens))
			p.completionTokens.Add(uint64(usage.Usage.CompletionTokens))
		}
	} else {
		p.failures.Add(1)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = w.Write(responseBody)
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envBool(name string, fallback bool) (bool, error) {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be true or false", name)
	}
	return parsed, nil
}

func main() {
	selected, err := profileFor(envOr("LONGMEMEVAL_PROXY_PROFILE", "reader"))
	if err != nil {
		log.Fatal(err)
	}
	if selected.Name == "reader" {
		allowFallback, boolErr := envBool("LONGMEMEVAL_ALLOW_FALLBACKS", selected.AllowFallback)
		if boolErr != nil {
			log.Fatal(boolErr)
		}
		selected, err = configuredReaderProfile(
			selected,
			envOr("LONGMEMEVAL_ACCEPTED_MODEL", selected.AcceptedModel),
			envOr("LONGMEMEVAL_UPSTREAM_MODEL", selected.UpstreamModel),
			envOr("LONGMEMEVAL_PROVIDER_ORDER", strings.Join(selected.ProviderOrder, ",")),
			envOr("LONGMEMEVAL_READER_REVISION", selected.Revision),
			allowFallback,
		)
		if err != nil {
			log.Fatal(err)
		}
	}
	apiKey := strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY"))
	if apiKey == "" {
		apiKey = strings.TrimSpace(os.Getenv("RELAY_API_KEY"))
	}
	if apiKey == "" {
		log.Fatal("OPENROUTER_API_KEY is required")
	}
	port, err := strconv.Atoi(envOr("LONGMEMEVAL_PROXY_PORT", strconv.Itoa(selected.DefaultPort)))
	if err != nil || port < 1 || port > 65535 {
		log.Fatal("LONGMEMEVAL_PROXY_PORT must be a valid port")
	}
	pprofserver.Start(context.Background(), "longmemeval-openrouter-proxy", port)
	handler := newProxy(selected, apiKey, chatUpstreamURL, nil)
	server := &http.Server{
		Addr: fmt.Sprintf(":%d", port), Handler: handler,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 30 * time.Second,
		WriteTimeout: 5 * time.Minute, IdleTimeout: 30 * time.Second,
		MaxHeaderBytes: 32 << 10,
	}
	log.Printf("LongMemEval OpenRouter %s proxy on :%d; profile=%s", selected.Name, port, selected.Revision)
	log.Fatal(server.ListenAndServe())
}
