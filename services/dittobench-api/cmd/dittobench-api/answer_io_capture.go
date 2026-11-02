package main

// Bench v12 answer-provenance CAPTURE: the broker-side recording of one scored
// case's clean-pass model I/O as bounded, normalized VALUE-TOKEN HASHES.
//
// The scorer-side answer-stuffing detection this capture used to feed
// (per-case provenance verdicts, the v12 Class-D gate integration) required
// exclusive per-case inference windows. Concurrent /run overlaps cases, so that
// integration was removed; what remains here is the capture vocabulary the
// inference broker still uses on its success path (inference_broker.go
// registerAnswerIOLocked / recordAnswerIOLocked / caseModelIO), kept intact so a
// restored per-case gate can consume it again without re-deriving the
// normalization.
//
// The capture stores ONLY value-token hashes -- never raw prose, never the
// token text, and never the answer key, which lives with the scorer and was
// never in the broker. A body that is unparseable or exceeds a capture bound
// marks the log truncated rather than storing more.

import (
	"encoding/json"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
)

// answerIO capture bounds. Size is NOT a signal: a legitimate deep-RAG agent is
// expected to use the model's full context window, so the per-side ceiling is set
// so high that a real context-window-sized prompt can never reach it (see
// answerIOMaxTokensPerSide). The truncation-marks-the-capture path is only a
// last-resort safety net bounding a truly malformed/malicious body far beyond any
// real context window. The token rule itself (bounds, hash, canonical number,
// tokenizer) lives in the shared scoregates package since Bench v13 so the relay
// capture and the scorer-side claim gates cannot drift; the v12 names below are
// aliases onto it and the captured bytes are unchanged.
const (
	answerIOMaxCalls = 64
	// answerIOMaxTokensPerSide is the maximum distinct normalized value-token
	// HASHES captured per side (input / completion) of a single model call. It is
	// set to twice gpt-oss-20b's full 131072-token context window: a prompt cannot
	// contain more DISTINCT value tokens than it has tokens, and it cannot have more
	// tokens than the context window, so a legitimate single-context prompt (however
	// large, however deep its RAG) can NEVER trip this bound. It exists solely to
	// bound the in-memory hash set against a pathological body that claims more
	// distinct value tokens than the model could ever accept. At 8 bytes per uint64 key the ceiling
	// is ~2 MB of raw keys per side, and the set only ever grows to the ACTUAL
	// distinct-value-token count of the prompt, so normal full-context runs cost a
	// few MB total (see the memory note below), not the ceiling.
	answerIOMaxTokensPerSide = scoregates.MaxValueTokensPerSide
	answerIOMaxTokenLen      = scoregates.MaxValueTokenLen
	// answerIOMinStringTokenLen bounds which non-numeric tokens are kept. Numbers
	// of any length are always kept (the v12 program families are money/number
	// answers); short common words are dropped so a value never matches on a bare
	// stopword.
	answerIOMinStringTokenLen = scoregates.MinStringTokenLen
)

// hashToken maps a normalized value token to a 64-bit FNV-1a hash. The capture
// stores ONLY these hashes (never the token text), so the recorded I/O can neither
// reproduce the prompt nor leak the answer key, and a full-context prompt costs a
// flat 8 bytes per distinct value token. FNV-1a is seed-free, so a broker's
// capture-time hashes and any later post-run candidate hashes agree without
// sharing any state.
func hashToken(tok string) uint64 { return scoregates.HashToken(tok) }

// caseModelCall is one bounded, normalized clean-pass model call: the set of
// candidate answer-value token HASHES the harness placed in the model INPUT and
// the set the model returned in its COMPLETION. Only value-token hashes are kept
// (never raw prose, never the token text), so the record can neither reproduce the
// prompt nor leak the answer key.
type caseModelCall struct {
	inputTokens      map[uint64]struct{}
	completionTokens map[uint64]struct{}
}

// caseModelIOLog is the ordered clean-pass model I/O the broker recorded for one
// case, plus a truncation bit. The bit is set only when a body was unparseable or
// the per-side capture ceiling (far above the model's full context window) was
// exceeded -- an event that a legitimate full-context prompt can never cause. It
// marks the capture as unsettled for any consumer that reads the log.
type caseModelIOLog struct {
	calls     []caseModelCall
	truncated bool
}

// canonicalNumber normalizes a numeric token to a stable canonical string:
// strips '$' and grouping commas, drops an insignificant fractional part and its
// trailing zeros, drops leading zeros, and collapses "-0" to "0". Returns ""
// when the token is not a number after stripping.
func canonicalNumber(raw string) string { return scoregates.CanonicalNumber(raw) }

// valueTokenSet extracts the bounded set of candidate answer-value token HASHES
// from a blob of message/completion text: every canonical number, plus lowercase
// alphanumeric tokens of at least answerIOMinStringTokenLen, each stored as its
// hashToken. It returns whether the per-side ceiling was exceeded so the caller
// can mark the log truncated -- an event a real context-window-sized prompt can
// never cause. It never stores raw prose or token text -- only value-token hashes
// -- so it cannot reproduce the prompt or leak the answer key.
func valueTokenSet(text string) (map[uint64]struct{}, bool) {
	tokens, truncated := scoregates.ValueTokenHashes(text)
	return map[uint64]struct{}(tokens), truncated
}

func parseChatInputText(body []byte) (string, bool) {
	var req struct {
		Messages []struct {
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(body, &req) != nil {
		return "", false
	}
	var b strings.Builder
	for _, msg := range req.Messages {
		appendRawContent(&b, msg.Content)
	}
	return b.String(), true
}

// parseChatCompletionText extracts the concatenated choice message contents from
// a chat completion body. Only message content is read (never usage token counts
// or the model field). ok=false means unparseable.
func parseChatCompletionText(body []byte) (string, bool) {
	var resp struct {
		Choices []struct {
			Message struct {
				Content json.RawMessage `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if json.Unmarshal(body, &resp) != nil {
		return "", false
	}
	var b strings.Builder
	for _, choice := range resp.Choices {
		appendRawContent(&b, choice.Message.Content)
	}
	return b.String(), true
}

// appendRawContent appends an OpenAI message content field, which is either a
// string or an array of typed parts, as plain text.
func appendRawContent(b *strings.Builder, raw json.RawMessage) {
	if len(raw) == 0 {
		return
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		b.WriteByte(' ')
		b.WriteString(s)
		return
	}
	var parts []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) == nil {
		for _, part := range parts {
			b.WriteByte(' ')
			b.WriteString(part.Text)
		}
	}
}
