package longmemeval

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

const (
	NativeMemoryCondition     = "longmemeval-s-cleaned-native-memory-tools-v2"
	minimumProjectionKeyBytes = 32
	maxHarnessResponseBytes   = 4 << 20
)

const nativeMemorySystemPrompt = "Answer the question using the user's stored conversation memory. " +
	"The current date is %s. Use the available native memory tools as needed to search, scope, and " +
	"fetch the relevant stored conversations before answering. Give a concise, direct answer. If the " +
	"stored conversations do not contain enough information, explicitly say that the question cannot " +
	"be answered from memory. Do not use external knowledge."

var longMemTimestamp = regexp.MustCompile(
	`^(\d{4})/(\d{2})/(\d{2}) \([A-Za-z]{3}\) (\d{2}):(\d{2})$`,
)

// Harness is the language-neutral /seed + /run boundary. Implementations must
// not return provider accounting reported by the submitted harness; trusted
// accounting comes from ProviderMeter instead.
type Harness interface {
	Seed(context.Context, protocol.SeedRequest) (protocol.SeedResponse, error)
	Run(context.Context, protocol.RunRequest) (protocol.RunResponse, error)
}

// ProjectedCase contains the complete harness-visible form for one selected
// row. QuestionID and reference answer are deliberately absent.
type ProjectedCase struct {
	SeedRequests []protocol.SeedRequest
	RunRequest   protocol.RunRequest
	questionID   string
}

// ProjectSelectedCases applies a caller-held cryptographic projection key and
// independently permutes the case execution order. Equal aliases mean only
// equal semantic grouping within this bundle; no source labels are encoded.
func ProjectSelectedCases(dataset LoadedDataset, key []byte, seedBatchPairs int) ([]ProjectedCase, error) {
	if len(key) < minimumProjectionKeyBytes {
		return nil, fmt.Errorf("LongMemEval projection key must contain at least %d bytes", minimumProjectionKeyBytes)
	}
	if seedBatchPairs < 1 {
		return nil, errors.New("LongMemEval seed batch size must be positive")
	}
	if dataset.Selection.CaseSetDigest != caseSetDigest(dataset.Selection) ||
		dataset.Selection.DatasetSHA256 != dataset.SHA256 {
		return nil, errors.New("LongMemEval loaded dataset identity is inconsistent")
	}

	projected := make([]projectedOrder, 0, len(dataset.Selection.Cases))
	for _, selected := range dataset.Selection.Cases {
		entry, ok := dataset.selected[selected.QuestionID]
		if !ok {
			return nil, errors.New("LongMemEval selected row is unavailable")
		}
		item, err := projectCase(dataset.Selection, entry, key, seedBatchPairs)
		if err != nil {
			return nil, err
		}
		projected = append(projected, projectedOrder{
			value: item,
			rank:  keyedDigest(key, "case-order", dataset.Selection.CaseSetDigest, entry.QuestionID),
		})
	}
	sort.Slice(projected, func(i, j int) bool {
		return bytes.Compare(projected[i].rank[:], projected[j].rank[:]) < 0
	})
	result := make([]ProjectedCase, len(projected))
	for index := range projected {
		result[index] = projected[index].value
	}
	return result, nil
}

type projectedOrder struct {
	value ProjectedCase
	rank  [sha256.Size]byte
}

func projectCase(selection Selection, entry DatasetCase, key []byte, seedBatchPairs int) (ProjectedCase, error) {
	if err := validateDatasetCase(entry); err != nil {
		return ProjectedCase{}, err
	}
	userID := opaqueUUID(key, "user", selection.CaseSetDigest, entry.QuestionID)
	caseID := opaqueUUID(key, "case", selection.CaseSetDigest, entry.QuestionID)
	pairs, err := projectPairs(entry, key, selection.CaseSetDigest)
	if err != nil {
		return ProjectedCase{}, err
	}
	seeds := make([]protocol.SeedRequest, 0, max(1, (len(pairs)+seedBatchPairs-1)/seedBatchPairs))
	if len(pairs) == 0 {
		seeds = append(seeds, protocol.SeedRequest{
			UserID:   userID,
			Pairs:    []protocol.MemoryPair{},
			Subjects: []protocol.Subject{},
			Links:    []protocol.SubjectLink{},
		})
	} else {
		for start := 0; start < len(pairs); start += seedBatchPairs {
			end := min(start+seedBatchPairs, len(pairs))
			seeds = append(seeds, protocol.SeedRequest{
				UserID:   userID,
				Pairs:    append([]protocol.MemoryPair(nil), pairs[start:end]...),
				Subjects: []protocol.Subject{},
				Links:    []protocol.SubjectLink{},
			})
		}
	}
	return ProjectedCase{
		SeedRequests: seeds,
		RunRequest: protocol.RunRequest{
			CaseID:       caseID,
			SystemPrompt: fmt.Sprintf(nativeMemorySystemPrompt, entry.QuestionDate),
			UserInput:    entry.Question,
			Tools:        NativeMemoryTools(),
			BenchVersion: 9,
			UserID:       userID,
		},
		questionID: entry.QuestionID,
	}, nil
}

type logicalPair struct {
	pairID string
	pair   protocol.MemoryPair
}

func projectPairs(entry DatasetCase, key []byte, caseSetDigest string) ([]protocol.MemoryPair, error) {
	logical := make([]logicalPair, 0)
	for sessionIndex, sessionID := range entry.HaystackSessionIDs {
		timestamp, err := normalizeTimestamp(entry.HaystackDates[sessionIndex])
		if err != nil {
			return nil, fmt.Errorf("selected LongMemEval history has invalid timestamp: %w", err)
		}
		sessionAlias := opaqueUUID(key, "session", caseSetDigest, entry.QuestionID, sessionID)
		turns := entry.HaystackSessions[sessionIndex]
		var pending *pendingUser
		appendPair := func(first, last int, prompt, response string) {
			if strings.TrimSpace(prompt) == "" && strings.TrimSpace(response) == "" {
				return
			}
			identity := fmt.Sprintf("%s\x1f%s\x1f%d\x1f%d", entry.QuestionID, sessionID, first, last)
			logical = append(logical, logicalPair{
				pairID: identity,
				pair: protocol.MemoryPair{
					PairID:    opaqueUUID(key, "pair", caseSetDigest, identity),
					SessionID: sessionAlias,
					Timestamp: timestamp,
					Prompt:    prompt,
					Response:  response,
				},
			})
		}
		for turnIndex, turn := range turns {
			if turn.Role != "user" && turn.Role != "assistant" {
				return nil, fmt.Errorf("selected LongMemEval history has unsupported role %q", turn.Role)
			}
			if turn.Role == "user" {
				if pending != nil {
					appendPair(pending.index, pending.index, pending.content, "")
				}
				pending = &pendingUser{index: turnIndex, content: turn.Content}
				continue
			}
			if pending == nil {
				appendPair(turnIndex, turnIndex, "", turn.Content)
			} else {
				appendPair(pending.index, turnIndex, pending.content, turn.Content)
				pending = nil
			}
		}
		if pending != nil {
			appendPair(pending.index, pending.index, pending.content, "")
		}
	}

	// Match the imported adapter's upsert contract: repeated logical pair IDs
	// resolve to their final occurrence while the surviving records retain
	// chronological dataset order.
	seen := make(map[string]struct{}, len(logical))
	reversed := make([]protocol.MemoryPair, 0, len(logical))
	for index := len(logical) - 1; index >= 0; index-- {
		if _, exists := seen[logical[index].pairID]; exists {
			continue
		}
		seen[logical[index].pairID] = struct{}{}
		reversed = append(reversed, logical[index].pair)
	}
	pairs := make([]protocol.MemoryPair, len(reversed))
	for index := range reversed {
		pairs[len(reversed)-1-index] = reversed[index]
	}
	return pairs, nil
}

type pendingUser struct {
	index   int
	content string
}

func normalizeTimestamp(value string) (string, error) {
	parts := longMemTimestamp.FindStringSubmatch(value)
	if parts == nil {
		return "", fmt.Errorf("unsupported LongMemEval timestamp %q", value)
	}
	return fmt.Sprintf("%s-%s-%sT%s:%s:00Z", parts[1], parts[2], parts[3], parts[4], parts[5]), nil
}

func keyedDigest(key []byte, domain string, parts ...string) [sha256.Size]byte {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte("ditto-v9-longmemeval-projection-v1"))
	mac.Write([]byte{0})
	mac.Write([]byte(domain))
	for _, part := range parts {
		mac.Write([]byte{0})
		mac.Write([]byte(part))
	}
	var result [sha256.Size]byte
	copy(result[:], mac.Sum(nil))
	return result
}

func opaqueUUID(key []byte, domain string, parts ...string) string {
	digest := keyedDigest(key, domain, parts...)
	value := append([]byte(nil), digest[:16]...)
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	return uuid.Must(uuid.FromBytes(value)).String()
}

// NativeMemoryTools returns a fresh copy of the exact four-tool catalog used
// by the merged native-memory-tools-v2 Python adapter.
func NativeMemoryTools() []protocol.ToolDefinition {
	return []protocol.ToolDefinition{
		{
			Name:        "search_memories",
			Description: "Search past conversations and return compact memory summaries. Use fetch_memories for selected IDs that need full text. For named entities/topics, prefer search_subjects -> search_memories_in_subjects.",
			Parameters:  json.RawMessage(`{"type":"object","properties":{"queries":{"type":"array","items":{"type":"string"},"description":"Memory search queries"}},"required":["queries"]}`),
		},
		{
			Name:        "search_subjects",
			Description: "Search the user's subject graph and return subject objects with id, name, description, and similarity.",
			Parameters:  json.RawMessage(`{"type":"object","properties":{"queries":{"type":"array","items":{"type":"string"},"description":"Subject search queries"}},"required":["queries"]}`),
		},
		{
			Name:        "fetch_memories",
			Description: "Fetch full conversation text for selected memory pair IDs.",
			Parameters:  json.RawMessage(`{"type":"object","properties":{"pairIds":{"type":"array","items":{"type":"string"},"description":"Memory pair IDs to fetch"},"stripImages":{"type":"boolean","description":"Exclude images, default true"}},"required":["pairIds"]}`),
		},
		{
			Name:        "search_memories_in_subjects",
			Description: "Semantic memory search inside one subject. Use focused queries; fetch selected IDs for full text.",
			Parameters:  json.RawMessage(`{"type":"object","properties":{"subject_id":{"type":"string","description":"Subject ID from search_subjects or the prompt"},"queries":{"type":"array","items":{"type":"string"},"description":"Focused queries within the subject"}},"required":["subject_id","queries"]}`),
		},
	}
}

// HTTPHarness is the production-compatible Go replacement for the imported
// Python HTTP adapter. It sends no authorization header or environment data.
type HTTPHarness struct {
	baseURL string
	client  *http.Client
}

func NewHTTPHarness(baseURL string, client *http.Client) (*HTTPHarness, error) {
	parsed, err := url.Parse(baseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, errors.New("invalid LongMemEval harness base URL")
	}
	if client == nil {
		return nil, errors.New("LongMemEval harness HTTP client is nil")
	}
	return &HTTPHarness{baseURL: strings.TrimRight(baseURL, "/"), client: client}, nil
}

func (h *HTTPHarness) Seed(ctx context.Context, request protocol.SeedRequest) (protocol.SeedResponse, error) {
	type seedWire struct {
		UserID   string                 `json:"user_id"`
		Pairs    []protocol.MemoryPair  `json:"pairs"`
		Subjects []protocol.Subject     `json:"subjects"`
		Links    []protocol.SubjectLink `json:"links"`
	}
	body := seedWire{UserID: request.UserID, Pairs: request.Pairs, Subjects: request.Subjects, Links: request.Links}
	if body.Pairs == nil {
		body.Pairs = []protocol.MemoryPair{}
	}
	if body.Subjects == nil {
		body.Subjects = []protocol.Subject{}
	}
	if body.Links == nil {
		body.Links = []protocol.SubjectLink{}
	}
	var response protocol.SeedResponse
	if err := h.post(ctx, "/seed", body, &response); err != nil {
		return protocol.SeedResponse{}, err
	}
	if response.Pairs != len(request.Pairs) || response.Subjects != len(request.Subjects) || response.Links != len(request.Links) {
		return protocol.SeedResponse{}, errors.New("LongMemEval harness acknowledged incomplete seed payload")
	}
	return response, nil
}

func (h *HTTPHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	if request.Tools == nil {
		request.Tools = []protocol.ToolDefinition{}
	}
	type runWireResponse struct {
		FinalText    *string                     `json:"final_text"`
		ToolCalls    []protocol.ObservedToolCall `json:"tool_calls"`
		PromptTokens int64                       `json:"prompt_tokens"`
		OutputTokens int64                       `json:"output_tokens"`
		LatencyMs    int64                       `json:"latency_ms"`
		Answer       string                      `json:"answer,omitempty"`
		Abstain      bool                        `json:"abstain,omitempty"`
		Confidence   *float64                    `json:"confidence,omitempty"`
	}
	var response runWireResponse
	if err := h.post(ctx, "/run", request, &response); err != nil {
		return protocol.RunResponse{}, err
	}
	if response.FinalText == nil {
		return protocol.RunResponse{}, errors.New("LongMemEval harness response lacks final_text")
	}
	return protocol.RunResponse{
		FinalText:    *response.FinalText,
		ToolCalls:    response.ToolCalls,
		PromptTokens: response.PromptTokens,
		OutputTokens: response.OutputTokens,
		LatencyMs:    response.LatencyMs,
		Answer:       response.Answer,
		Abstain:      response.Abstain,
		Confidence:   response.Confidence,
	}, nil
}

func (h *HTTPHarness) post(ctx context.Context, path string, input, output any) error {
	body, err := json.Marshal(input)
	if err != nil {
		return fmt.Errorf("encode LongMemEval harness request: %w", err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, h.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build LongMemEval harness request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header["User-Agent"] = []string{}
	response, err := h.client.Do(request)
	if err != nil {
		return fmt.Errorf("LongMemEval harness request failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, maxHarnessResponseBytes))
		return fmt.Errorf("LongMemEval harness returned HTTP %d", response.StatusCode)
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, maxHarnessResponseBytes))
	if err := decoder.Decode(output); err != nil {
		return fmt.Errorf("decode LongMemEval harness response: %w", err)
	}
	return nil
}
