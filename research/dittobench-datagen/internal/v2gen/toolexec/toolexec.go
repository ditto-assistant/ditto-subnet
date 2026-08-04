// Package toolexec implements the validator-served MOCK tool-execution endpoint
// for observed execution. A harness that supports observed execution POSTs each
// of its non-memory catalog tool calls to this
// endpoint (RunRequest.ToolEndpoint) instead of stubbing them locally; the server
//
//  1. returns a deterministic, seed-derived MOCK result for the tool (so the
//     harness has real content to reason over: a web snippet, a page's text, a
//     job status), and
//  2. RECORDS the call as the authoritative observed tool trajectory for that
//     case, so the validator does not have to trust the harness's self-reported
//     tool_calls.
//
// Memory tools (search_memories, search_subjects, fetch_memories,
// search_memories_in_subjects) are deliberately NOT served: the harness answers
// those from its own seeded memory, and serving them would leak the answer. A
// call to a non-served tool is recorded (it IS part of the trajectory) but
// answered with an error, exactly as a real unavailable tool would be.
//
// Determinism: every result is a pure function of (masterSeed, caseID, toolName,
// args). No wall clock, no crypto-rand, no map-iteration order, so the served
// content is reproducible from the run seed and can be folded into the dataset
// hash (the reproducibility contract).
package toolexec

import (
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"sort"
	"strings"
	"sync"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// memoryTools are the catalog tools the mock endpoint does NOT execute: the
// harness answers memory questions from its own seeded store, so serving them
// would hand over the answer. Kept as a set for O(1) Serves() checks.
var memoryTools = map[string]bool{
	"search_memories":             true,
	"search_subjects":             true,
	"fetch_memories":              true,
	"search_memories_in_subjects": true,
}

// Serves reports whether the mock endpoint executes the named catalog tool (i.e.
// it is an external-world tool, not a memory tool). A case whose every expected
// tool Serves() is "observable": its trajectory can be scored from the
// validator's own observations rather than the harness's self-report.
func Serves(name string) bool { return name != "" && !memoryTools[name] }

// Observable reports whether a tool case's expected trajectory is entirely made
// of served (non-memory) tools, so the validator expects to observe every call
// via the endpoint. No-expected-tool cases (chit-chat, abstention) are not
// observable: there is nothing to observe.
func Observable(c protocol.ToolCase) bool {
	if len(c.ExpectedTools) == 0 {
		return false
	}
	for _, t := range c.ExpectedTools {
		if !Serves(t.Name) {
			return false
		}
	}
	return true
}

// Needle is the coined, fabricated fact a content tool embeds in its result. Its
// Subject is what a result-usage prompt asks about ("the Veltrix index") and its
// Value is the distinctive number a correct answer must echo ("3,418"), a value
// that CANNOT be known without executing the tool (it is invented per seed), so a
// result-usage case cannot be answered by self-report or base-model knowledge.
type Needle struct {
	Subject string // "the <Name> <noun>"
	Value   string // comma-formatted number, e.g. "3,418"
	Unit    string // "points", "acre-feet", …
}

// Sentence renders the needle as the fact a content tool returns.
func (n Needle) Sentence() string {
	return fmt.Sprintf("%s reached %s %s", n.Subject, n.Value, n.Unit)
}

// NeedleFor derives the coined fact for a case, a pure function of (masterSeed,
// caseID). Both the fixture (which serves it) and the prompt generator (which
// asks about its Subject) call this, so the question and the served answer are
// always coherent without threading any state between them.
func NeedleFor(masterSeed int64, caseID string) Needle {
	return synthNeedle(rand.New(rand.NewSource(caseSeed(masterSeed, caseID))))
}

// Fixture is the per-case mock tool environment. It is a pure function of the
// master seed and the case, so the same run seed always serves the same content.
// The needle is present only for cases whose expected tools return answer-bearing
// content (web/read/job); cases whose served tools return a bare confirmation
// (settings, feedback, image) carry none.
type Fixture struct {
	seed      int64
	needle    Needle
	has       bool
	jobID     string // the stable job id execute_agent_job serves for this case
	dependent bool   // a dependent-arg chain: get_agent_job_status gates the needle on jobID
	recovery  bool   // an error-recovery case: the first content-tool call returns a transient error
}

// jobChainMarker tags a dependent-arg result-usage category: execute_agent_job
// serves a job id, and get_agent_job_status returns the answer needle ONLY when
// called with that id. The trajectory cannot be faked: the harness must
// read the first call's result and thread its value into the second call.
const jobChainMarker = "job_chain"

// IsJobChain reports whether a category is a dependent-arg job chain (the id
// served by call 1 must be passed to call 2).
func IsJobChain(category string) bool { return strings.Contains(category, jobChainMarker) }

// recoveryMarker tags an error-recovery result-usage category: the first call to
// the served content tool returns a transient error, and the answer needle is
// delivered only on a retry, so a harness must recover from the error to score.
const recoveryMarker = "recovery"

// IsErrorRecovery reports whether a category is an error-recovery case.
func IsErrorRecovery(category string) bool { return strings.Contains(category, recoveryMarker) }

// BuildFixture derives the deterministic mock environment for one tool case.
func BuildFixture(masterSeed int64, c protocol.ToolCase) Fixture {
	f := Fixture{seed: caseSeed(masterSeed, c.ID)}
	f.jobID = jobIDForSeed(f.seed)
	f.dependent = IsJobChain(c.Category)
	f.recovery = IsErrorRecovery(c.Category)
	if caseCarriesNeedle(c) {
		f.needle = NeedleFor(masterSeed, c.ID)
		f.has = true
	}
	return f
}

// Subject is the needle's subject ("the Veltrix index"), or "" when the fixture
// carries no needle. NeedleValue is the distinctive number a result-usage answer
// must contain ("3,418"). NeedleText is the full served sentence (for the
// hashable dataset artifact). All three return "" when there is no needle.
func (f Fixture) Subject() string {
	if !f.has {
		return ""
	}
	return f.needle.Subject
}

func (f Fixture) NeedleValue() string {
	if !f.has {
		return ""
	}
	return f.needle.Value
}

func (f Fixture) NeedleText() string {
	if !f.has {
		return ""
	}
	return f.needle.Sentence()
}

// caseCarriesNeedle reports whether any of a case's expected tools returns
// answer-bearing content (a web result, a page, a job status/result): the tools
// whose output a correct answer is expected to USE.
func caseCarriesNeedle(c protocol.ToolCase) bool {
	for _, t := range c.ExpectedTools {
		if contentTools[t.Name] {
			return true
		}
	}
	return false
}

// contentTools are the served tools whose result carries a fact the user's
// answer should incorporate (result-usage). Other served tools (settings,
// feedback, image/artifact creation) return only a confirmation.
var contentTools = map[string]bool{
	"search_web":             true,
	"read_links":             true,
	"get_agent_job_status":   true,
	"execute_agent_workflow": true,
	"list_agent_jobs":        true,
}

// Result returns the mock output for a tool call against this fixture. It is
// deterministic in (fixture seed, name, args). A non-served tool yields ("",
// false) so the caller returns an error to the harness.
func (f Fixture) Result(name string, args json.RawMessage) (string, bool) {
	if !Serves(name) {
		return "", false
	}
	// A per-call RNG folds the tool name + args in so distinct calls in a
	// trajectory (e.g. two read_links on different URLs) get distinct-but-stable
	// text, without any shared mutable state.
	r := rand.New(rand.NewSource(f.seed ^ int64(fnv1a(name+"|"+argsKey(args)))))
	switch name {
	case "search_web":
		src := webSources[r.Intn(len(webSources))]
		return fmt.Sprintf("Top result from %s: %s More at %s.", src, f.needleSentence(), coinedURL(r)), true
	case "read_links":
		return fmt.Sprintf("%s\n\n%s", coinedTitle(r), f.needleSentence()), true
	case "execute_agent_job":
		// Serve the case's STABLE job id (not an args-derived one) so a dependent
		// chain has a fixed value to thread into get_agent_job_status.
		return fmt.Sprintf("Dispatched background job %s.", f.jobID), true
	case "get_agent_job_status":
		if f.dependent {
			// Dependent-arg chain: reveal the needle ONLY when queried with the id
			// execute_agent_job served. A harness that did not read+forward that id
			// gets a not-found and cannot produce the answer value.
			if calledJobID(args) != f.jobID {
				return fmt.Sprintf("Job %s: not found.", calledJobID(args)), true
			}
			return fmt.Sprintf("Job %s: completed. Result: %s", f.jobID, f.needleSentence()), true
		}
		return fmt.Sprintf("Job %s: completed. Result: %s", f.jobID, f.needleSentence()), true
	case "list_agent_jobs":
		return fmt.Sprintf("Recent jobs: %s (completed), %s (running).", jobID(r), jobID(r)), true
	case "execute_agent_workflow":
		return fmt.Sprintf("Workflow run %s finished. %s", jobID(r), f.needleSentence()), true
	case "create_image", "edit_image":
		return fmt.Sprintf("Image generated: %s", coinedURL(r)), true
	case "artifacts":
		return fmt.Sprintf("Artifact created: %s", coinedURL(r)), true
	case "file_feedback_for_team":
		return fmt.Sprintf("Thanks — your feedback was filed (ticket %s).", jobID(r)), true
	case "set_theme", "set_main_model", "set_reasoning_effort", "set_chat_tool_preferences":
		return "Setting updated.", true
	default:
		return "Done.", true
	}
}

// needleSentence renders the fixture's coined fact as a sentence, or a neutral
// filler when the fixture carries no needle (a served tool called on a case that
// did not expect a content tool).
func (f Fixture) needleSentence() string {
	if !f.has {
		return "No notable details were found."
	}
	return f.needle.Sentence() + "."
}

// --- content pools (fabricated so results can't be answered from base-model
// knowledge, an anti-memorization property like the persona pools) ---

var (
	coinedNames = []string{
		"Veltrix", "Kelvane", "Osric", "Marndor", "Quenby", "Zephyra",
		"Orlin", "Thal", "Brayen", "Cindral", "Duvos", "Ferrow",
	}
	coinedNouns = []string{
		"index", "reservoir", "initiative", "alloy", "protocol", "corridor",
		"ledger", "array", "expedition", "foundry", "syndicate", "lattice",
	}
	coinedUnits = []string{
		"points", "acre-feet", "units", "basis points", "kilotonnes",
		"megawatts", "parsecs", "hectares",
	}
	webSources = []string{
		"the Ansible Gazette", "Torva Daily", "the Merridian Review",
		"Halcyon Times", "the Verge of Nowhere", "Solaris Wire",
	}
)

// synthNeedle coins a fabricated fact "the <Name> <noun> reached <number>
// <unit>": a distinctive, unguessable value a correct result-usage answer must
// echo (it exists only in the served content).
func synthNeedle(r *rand.Rand) Needle {
	name := coinedNames[r.Intn(len(coinedNames))]
	noun := coinedNouns[r.Intn(len(coinedNouns))]
	num := 100 + r.Intn(99900) // 100..99999
	unit := coinedUnits[r.Intn(len(coinedUnits))]
	return Needle{Subject: fmt.Sprintf("the %s %s", name, noun), Value: commaNumber(num), Unit: unit}
}

func coinedURL(r *rand.Rand) string {
	host := strings.ToLower(coinedNames[r.Intn(len(coinedNames))])
	slug := strings.ToLower(coinedNouns[r.Intn(len(coinedNouns))])
	return fmt.Sprintf("https://%s.example/%s-%d", host, slug, 1000+r.Intn(9000))
}

func coinedTitle(r *rand.Rand) string {
	return fmt.Sprintf("The %s %s: an overview", coinedNames[r.Intn(len(coinedNames))], coinedNouns[r.Intn(len(coinedNouns))])
}

func jobID(r *rand.Rand) string { return fmt.Sprintf("job-%05d", r.Intn(100000)) }

// jobIDForSeed derives the stable job id a case's execute_agent_job serves, from
// the case seed alone (independent of the harness's call args), so both the
// served result and a dependent get_agent_job_status gate agree.
func jobIDForSeed(caseSeed int64) string {
	return fmt.Sprintf("job-%05d", uint64(caseSeed)%100000)
}

// calledJobID extracts the job_id argument from a get_agent_job_status call
// (empty when absent), normalized (trimmed) for the dependent-chain comparison.
func calledJobID(raw json.RawMessage) string {
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return ""
	}
	if v, ok := m["job_id"].(string); ok {
		return strings.TrimSpace(v)
	}
	return ""
}

// commaNumber renders n with thousands separators (deterministic, no locale).
func commaNumber(n int) string {
	s := fmt.Sprintf("%d", n)
	if len(s) <= 3 {
		return s
	}
	var b strings.Builder
	pre := len(s) % 3
	if pre > 0 {
		b.WriteString(s[:pre])
		if len(s) > pre {
			b.WriteByte(',')
		}
	}
	for i := pre; i < len(s); i += 3 {
		b.WriteString(s[i : i+3])
		if i+3 < len(s) {
			b.WriteByte(',')
		}
	}
	return b.String()
}

// caseSeed derives a per-case seed from the master seed and case id. It is pure
// and order-independent, so every case's mock environment is fixed by the run seed.
func caseSeed(master int64, caseID string) int64 {
	return master ^ int64(fnv1a(caseID))
}

// argsKey normalizes tool-call args to a stable string so identical args produce
// identical mock output regardless of key order in the JSON.
func argsKey(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return string(raw) // opaque but stable
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "%s=%v;", k, m[k])
	}
	return b.String()
}

// fnv1a is a small deterministic string hash (FNV-1a) for stable seed mixing.
func fnv1a(s string) uint32 {
	var h uint32 = 2166136261
	for i := 0; i < len(s); i++ {
		h ^= uint32(s[i])
		h *= 16777619
	}
	return h
}

// --- HTTP server: serves results and records observed calls ---

// caseState holds one case's fixture and the trajectory the harness executed
// against it. Guarded by mu because a harness may issue tool calls concurrently.
type caseState struct {
	mu       sync.Mutex
	fixture  Fixture
	observed []protocol.ObservedToolCall
}

// Server is the validator-served mock tool endpoint for one run. Register every
// tool case's fixture, mount it at a host-reachable URL, hand that URL to the
// harness via RunRequest.ToolEndpoint, then read back Observed(caseID) after the
// case to score the authoritative trajectory.
type Server struct {
	mu    sync.RWMutex
	cases map[string]*caseState
}

// NewServer returns an empty server (register cases before serving).
func NewServer() *Server { return &Server{cases: map[string]*caseState{}} }

// Register installs a case's fixture. Safe to call before serving.
func (s *Server) Register(caseID string, f Fixture) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cases[caseID] = &caseState{fixture: f}
}

// Observed returns the tool calls the harness executed for a case, in the order
// received (nil if none / unknown case). The returned slice is a copy.
func (s *Server) Observed(caseID string) []protocol.ObservedToolCall {
	s.mu.RLock()
	cs := s.cases[caseID]
	s.mu.RUnlock()
	if cs == nil {
		return nil
	}
	cs.mu.Lock()
	defer cs.mu.Unlock()
	if len(cs.observed) == 0 {
		return nil
	}
	out := make([]protocol.ObservedToolCall, len(cs.observed))
	copy(out, cs.observed)
	return out
}

// maxToolBody caps a tool-exec request body (args are tiny).
const maxToolBody = 64 << 10

// ServeHTTP implements the tool endpoint: POST <base>/tool with a
// ToolExecRequest, returns a ToolExecResponse. Records the call (any tool) and
// returns a mock result (served tools) or an error (memory / unknown tools).
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, protocol.ToolExecResponse{Error: "POST required"})
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxToolBody))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, protocol.ToolExecResponse{Error: "unreadable body"})
		return
	}
	var req protocol.ToolExecRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeJSON(w, http.StatusBadRequest, protocol.ToolExecResponse{Error: "invalid JSON"})
		return
	}

	s.mu.RLock()
	cs := s.cases[req.CaseID]
	s.mu.RUnlock()
	if cs == nil {
		// Unknown case: still answer (so the harness isn't wedged) but there is
		// nothing to record. This should not happen for a registered run.
		writeJSON(w, http.StatusNotFound, protocol.ToolExecResponse{Error: "unknown case_id"})
		return
	}

	cs.mu.Lock()
	cs.observed = append(cs.observed, protocol.ObservedToolCall{Name: req.Name, Args: req.Args, Hop: req.Hop})
	fixture := cs.fixture
	// Prior calls to this same tool (excludes the one just appended), for the
	// error-recovery gate: the first content-tool call flakes, a retry succeeds.
	priorSameTool := 0
	for i := 0; i < len(cs.observed)-1; i++ {
		if cs.observed[i].Name == req.Name {
			priorSameTool++
		}
	}
	cs.mu.Unlock()

	// Error-recovery: the FIRST call to a content tool returns a transient error
	// (recorded, so the trajectory shows the attempt). The needle is served only
	// on the retry, so a harness that does not recover cannot answer.
	if fixture.recovery && contentTools[req.Name] && priorSameTool == 0 {
		writeJSON(w, http.StatusOK, protocol.ToolExecResponse{Error: "transient upstream error (503); retry"})
		return
	}

	result, ok := fixture.Result(req.Name, req.Args)
	if !ok {
		// A memory or unknown tool: the harness should not route it here. Record it
		// (done above: it is part of the observed trajectory) but return an error.
		writeJSON(w, http.StatusOK, protocol.ToolExecResponse{Error: "tool not available via this endpoint: " + req.Name})
		return
	}
	writeJSON(w, http.StatusOK, protocol.ToolExecResponse{Result: result})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
