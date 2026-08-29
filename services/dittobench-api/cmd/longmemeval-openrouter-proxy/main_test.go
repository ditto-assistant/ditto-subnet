package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestProfilesRewriteOnlyFrozenModels(t *testing.T) {
	tests := []struct {
		name, inputModel, upstreamModel string
	}{
		{"reader", readerModel, readerModel},
		{"judge", officialJudgeModel, openRouterJudgeModel},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			selected, err := profileFor(test.name)
			if err != nil {
				t.Fatal(err)
			}
			body, err := rewriteRequest(selected, []byte(`{"model":"`+test.inputModel+`","messages":[],"stream":false}`))
			if err != nil {
				t.Fatal(err)
			}
			var request map[string]any
			if json.Unmarshal(body, &request) != nil || request["model"] != test.upstreamModel || request["stream"] != false {
				t.Fatalf("request=%#v", request)
			}
			provider, _ := request["provider"].(map[string]any)
			if provider["allow_fallbacks"] != false || provider["data_collection"] != "deny" {
				t.Fatalf("provider=%#v", provider)
			}
			if only, _ := provider["only"].([]any); len(only) != 1 || only[0] != "openai" {
				t.Fatalf("provider order=%#v", provider["only"])
			}
			if _, err := rewriteRequest(selected, []byte(`{"model":"other","messages":[]}`)); err == nil {
				t.Fatal("unreviewed model accepted")
			}
			if _, err := rewriteRequest(selected, []byte(`{"model":"`+test.inputModel+`","stream":true}`)); err == nil {
				t.Fatal("streaming accepted")
			}
		})
	}
}

func TestReaderProfileCanMapV8HarnessModelToAnyOpenRouterModel(t *testing.T) {
	base, err := profileFor("reader")
	if err != nil {
		t.Fatal(err)
	}
	selected, err := configuredReaderProfile(
		base,
		"openai/gpt-oss-20b",
		"anthropic/claude-sonnet-4",
		"auto",
		"longmemeval-v8-claude-sonnet-4-v1",
		false,
	)
	if err != nil {
		t.Fatal(err)
	}
	body, err := rewriteRequest(selected, []byte(`{
		"model":"openai/gpt-oss-20b","messages":[],"stream":false
	}`))
	if err != nil {
		t.Fatal(err)
	}
	var request map[string]any
	if json.Unmarshal(body, &request) != nil {
		t.Fatal("rewritten request is not JSON")
	}
	if request["model"] != "anthropic/claude-sonnet-4" {
		t.Fatalf("upstream model=%#v", request["model"])
	}
	provider, _ := request["provider"].(map[string]any)
	if _, constrained := provider["only"]; constrained {
		t.Fatalf("automatic routing unexpectedly constrained: %#v", provider)
	}
	if provider["allow_fallbacks"] != false || provider["data_collection"] != "deny" {
		t.Fatalf("provider=%#v", provider)
	}
}

func TestCustomReaderProfileRequiresDistinctRevision(t *testing.T) {
	base, _ := profileFor("reader")
	if _, err := configuredReaderProfile(
		base,
		"openai/gpt-oss-20b",
		"anthropic/claude-sonnet-4",
		"auto",
		base.Revision,
		true,
	); err == nil {
		t.Fatal("custom reader configuration reused the historical revision")
	}
}

func TestProxyForwardsPinnedRequestAndUsage(t *testing.T) {
	selected, _ := profileFor("reader")
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret" {
			t.Fatal("missing upstream authorization")
		}
		var request map[string]any
		if json.NewDecoder(r.Body).Decode(&request) != nil || request["model"] != readerModel {
			t.Fatalf("request=%#v", request)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"choices": []map[string]any{{"message": map[string]string{"role": "assistant", "content": "ok"}}},
			"usage":   map[string]int{"prompt_tokens": 12, "completion_tokens": 3},
		})
	}))
	defer upstream.Close()
	handler := newProxy(selected, "secret", upstream.URL, upstream.Client())
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(
		`{"model":"openai/gpt-4.1","messages":[]}`,
	))
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK || handler.promptTokens.Load() != 12 || handler.completionTokens.Load() != 3 {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}
