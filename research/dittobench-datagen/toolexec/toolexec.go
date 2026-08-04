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

// NeedleForV8World binds repeated subjects to one seed-local value and a
// noun-compatible unit. It is used only by V8 world categories; frozen older
// benchmark vectors continue through NeedleFor unchanged.
func NeedleForV8World(masterSeed int64, caseID string) Needle {
	r := rand.New(rand.NewSource(caseSeed(masterSeed, caseID)))
	name := coinedNames[r.Intn(len(coinedNames))]
	noun := coinedNouns[r.Intn(len(coinedNouns))]
	subject := fmt.Sprintf("the %s %s", name, noun)
	vr := rand.New(rand.NewSource(int64(fnv1a(fmt.Sprintf("%d|%s", masterSeed, subject))) & ((1 << 63) - 1)))
	units := unitsForNoun[noun]
	return Needle{Subject: subject, Value: commaNumber(100 + vr.Intn(99900)), Unit: units[vr.Intn(len(units))]}
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
	bearer    string // the ONE expected content tool that serves the needle (see needleBearer)
	decoy     string // a seed-derived number != needle.Value, served by non-bearer content tools
	jobID     string // the stable job id execute_agent_job serves for this case
	dependent bool   // a dependent-arg chain: get_agent_job_status gates the needle on jobID
	recovery  bool   // an error-recovery case: the first content-tool call returns a transient error
	linkDep   bool   // a dependent link chain: read_links gates the needle on pageURL
	pageURL   string // the stable URL search_web serves for a link chain
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

// linkChainMarker tags a dependent-arg LINK chain (bench_version 7):
// search_web serves a STABLE page URL for the case, and read_links returns the
// answer needle ONLY when called with that URL. Like the job chain, the
// trajectory cannot be faked: the harness must read the search result and
// thread the served URL into the read call. Markers compose — a category name
// may select several gates at once (e.g. "job_chain_recovery_result_usage").
const linkChainMarker = "link_chain"

// IsLinkChain reports whether a category is a dependent-arg link chain.
func IsLinkChain(category string) bool { return strings.Contains(category, linkChainMarker) }

// BuildFixture derives the deterministic mock environment for one tool case.
func BuildFixture(masterSeed int64, c protocol.ToolCase) Fixture {
	f := Fixture{seed: caseSeed(masterSeed, c.ID)}
	f.jobID = jobIDForSeed(f.seed)
	f.dependent = IsJobChain(c.Category)
	f.recovery = IsErrorRecovery(c.Category)
	if IsLinkChain(c.Category) {
		f.linkDep = true
		f.pageURL = pageURLForSeed(f.seed)
	}
	if caseCarriesNeedle(c) {
		f.needle = NeedleFor(masterSeed, c.ID)
		if strings.HasPrefix(c.Category, "world_") {
			f.needle = NeedleForV8World(masterSeed, c.ID)
		}
		f.has = true
		f.bearer = needleBearer(c)
		f.decoy = decoyValue(f.seed, f.needle.Value)
	}
	return f
}

// needleBearer returns the ONE tool that serves the answer needle for a case:
// the LAST content tool in the expected trajectory, i.e. the tool whose result a
// correct answer actually USES. For a single-tool web case that is search_web;
// for a search-then-read case it is read_links (so a harness must genuinely read
// the page, not fish the number out of the search snippet); for a job chain it
// is get_agent_job_status. Serving the needle only from the bearer (Result)
// closes the AV-1 hole where search_web handed out the needle regardless of what
// the case actually expected, and where any content tool could fish it.
func needleBearer(c protocol.ToolCase) string {
	bearer := ""
	for _, t := range c.ExpectedTools {
		if contentTools[t.Name] {
			bearer = t.Name
		}
	}
	return bearer
}

// decoyValue derives a plausible wrong number (formatted like the needle but
// never equal to it) that non-bearer content tools serve. A harness that greps a
// number out of the wrong tool's result gets this instead of the answer, so the
// "call the easiest content tool and echo any number" strategy fails. Pure
// function of (seed, needle value).
func decoyValue(seed int64, needleValue string) string {
	r := rand.New(rand.NewSource(seed ^ int64(fnv1a("decoy|"+needleValue))))
	for i := 0; i < 8; i++ {
		v := commaNumber(100 + r.Intn(99900))
		if v != needleValue {
			return v
		}
	}
	return commaNumber(1) // unreachable in practice
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
	return f.servedNeedleSentence()
}

// DecoyValue is the plausible wrong number non-bearer content tools serve for
// this case (empty when the fixture carries no needle). It is validator-internal
// grading material: the scorer treats it as a result-usage distractor, so a
// harness that echoes a number fished from the wrong tool is penalized rather
// than merely uncredited.
func (f Fixture) DecoyValue() string {
	if !f.has {
		return ""
	}
	return f.decoy
}

// Bearer is the tool that serves the answer needle for this case (empty when the
// fixture carries no needle). Exported for tests and scorer diagnostics.
func (f Fixture) Bearer() string { return f.bearer }

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
	// A content tool serves the answer needle ONLY when it is this case's needle
	// bearer; every other content tool serves a plausible decoy (a wrong number),
	// so a harness cannot fish the answer from the easiest/wrong tool.
	serveNeedle := f.has && name == f.bearer
	switch name {
	case "search_web":
		src := webSources[r.Intn(len(webSources))]
		body := f.decoySentence(r)
		if serveNeedle {
			body = f.needleSentence()
		}
		url := coinedURL(r)
		if f.linkDep {
			// Dependent link chain: the served URL is the case's STABLE pageURL
			// (args-independent), so the harness has a fixed value to thread into
			// read_links. The rng draw above still happens, keeping every other
			// case's served bytes identical.
			url = f.pageURL
		}
		return fmt.Sprintf("Top result from %s: %s More at %s.", src, body, url), true
	case "read_links":
		if f.linkDep && !calledWithURL(args, f.pageURL) {
			// Dependent link chain: the needle exists only behind the URL that
			// search_web served. A harness that did not read+forward it gets a
			// not-found and cannot produce the answer value.
			return "Could not open that link: page not found. Follow the URL given in the search result.", true
		}
		body := f.decoySentence(r)
		if serveNeedle {
			body = f.needleSentence()
		}
		return fmt.Sprintf("%s\n\n%s", coinedTitle(r), body), true
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
		}
		body := f.decoySentence(r)
		if serveNeedle {
			body = f.needleSentence()
		}
		return fmt.Sprintf("Job %s: completed. Result: %s", f.jobID, body), true
	case "list_agent_jobs":
		return fmt.Sprintf("Recent jobs: %s (completed), %s (running).", jobID(r), jobID(r)), true
	case "execute_agent_workflow":
		body := f.decoySentence(r)
		if serveNeedle {
			body = f.needleSentence()
		}
		return fmt.Sprintf("Workflow run %s finished. %s", jobID(r), body), true
	case "create_image", "edit_image":
		return fmt.Sprintf("Image generated: %s", coinedURL(r)), true
	case "artifacts":
		return fmt.Sprintf("Artifact created: %s", coinedURL(r)), true
	case "file_feedback_for_team":
		return fmt.Sprintf("Thanks — your feedback was filed (ticket %s).", jobID(r)), true
	case "list_workflows":
		return "Saved workflows: weekly standup digest; invoice review; launch checklist.", true
	case "discover_capabilities":
		return "Appearance options: accent colors teal, indigo, amber, emerald, crimson, violet, cobalt, coral; fonts Atkinson Hyperlegible, Inter, and system; light and dark modes.", true
	case "search_tools":
		return "Matching tools: run_code for calculations; artifacts for file conversion; search_web for live public information.", true
	case "set_theme", "set_main_model", "set_reasoning_effort", "set_chat_tool_preferences":
		return "Setting updated.", true
	default:
		return "Done.", true
	}
}

// needleSentence renders the fixture's coined fact as a sentence, or a neutral
// filler when the fixture carries no needle. It is a pure function of the fixture
// (no per-call randomness), so the served needle text is stable per case and
// equals NeedleText() for the auditable artifact.
func (f Fixture) needleSentence() string {
	if !f.has {
		return "No notable details were found."
	}
	return f.servedNeedleSentence() + "."
}

// needleTemplates render the coined fact several ways (%[1]s=subject,
// %[2]s=value, %[3]s=unit), so the served needle is not a single fixed
// "<subject> reached <value> <unit>" string a public regex can anchor on. A
// harness must locate the value by MEANING (the subject it was asked about), not
// by a constant surface pattern.
var needleTemplates = []string{
	"%[1]s reached %[2]s %[3]s",
	"%[1]s currently stands at %[2]s %[3]s",
	"%[1]s was last measured at %[2]s %[3]s",
	"the latest figure for %[1]s is %[2]s %[3]s",
	"%[1]s climbed to %[2]s %[3]s in the newest reading",
	"analysts put %[1]s at %[2]s %[3]s",
}

// needleConnectives join a needle clause and a decoy clause into one served
// sentence (%[1]s=first clause, %[2]s=second clause). The set is deliberately
// NOT a single fixed "(separately," marker: both the connective AND the clause
// order are varied per seed (joinClauses), so a parser cannot bank the needle by
// taking "the first number" or "the number before a fixed marker" — the decoy
// clause leads on a per-seed fraction of cases and the marker rotates. The SAME
// family builds the non-bearer decoy sentence, so the bearer is not identifiable
// by the presence of a connective/parenthetical alone. Each clause still names
// its subject next to its value, so a reading harness that keys on the ASKED
// subject answers correctly without knowing the seed.
var needleConnectives = []string{
	"%[1]s; separately, %[2]s",
	"%[1]s, while %[2]s",
	"%[1]s; meanwhile, %[2]s",
	"%[1]s, whereas %[2]s",
	"%[1]s; in unrelated reporting, %[2]s",
	"%[1]s, and in a separate note, %[2]s",
}

// clauseFor renders one "<subject> <verb-phrase> <value> <unit>" clause with a
// per-(seed,salt) template from needleTemplates. Distinct salts give the two
// clauses of a sentence independent templates while staying deterministic.
// Reduce modulo in uint64 space so the selection is identical on every GOARCH (a
// prior int-truncated modulo picked a different template on 32-bit builds).
func (f Fixture) clauseFor(salt, subject, value string) string {
	ti := int((uint64(fnv1a(salt)) ^ uint64(f.seed)) % uint64(len(needleTemplates)))
	return fmt.Sprintf(needleTemplates[ti], subject, value, f.needle.Unit)
}

// joinClauses links a needle clause and a decoy clause with a per-seed connective
// in a per-seed order. Neither is fixed: the connective is chosen from
// needleConnectives and, on ~half of seeds, the decoy clause LEADS. So "the first
// number" and "the number before a fixed marker" each yield the decoy on a
// meaningful fraction of seeds, defeating a pure position/marker grep. The needle
// clause always names the asked subject next to its value, preserving readability.
func (f Fixture) joinClauses(needleClause, decoyClause string) string {
	h := uint64(fnv1a("needle-join")) ^ uint64(f.seed)
	conn := needleConnectives[int(h%uint64(len(needleConnectives)))]
	first, second := needleClause, decoyClause
	if (h/uint64(len(needleConnectives)))&1 == 1 {
		first, second = decoyClause, needleClause
	}
	return fmt.Sprintf(conn, first, second)
}

// servedNeedleSentence renders the needle clause plus an inline DECOY clause,
// joined by a per-seed connective in a per-seed order (see joinClauses). Only a
// harness that reads WHICH figure belongs to the asked subject answers correctly;
// a "grab the first/any/pre-marker number" parser picks the decoy on a meaningful
// fraction of seeds.
func (f Fixture) servedNeedleSentence() string {
	if !f.has {
		return ""
	}
	needleClause := f.clauseFor("needle-tmpl", f.needle.Subject, f.needle.Value)
	decoyClause := f.clauseFor("needle-decoy-tmpl", f.decoySubject(), f.decoy)
	return f.joinClauses(needleClause, decoyClause)
}

// decoySentence renders a plausible result that carries the DECOY number but NOT
// the answer needle: what a non-bearer content tool serves. It is built from the
// SAME two-clause + connective family as the bearer's sentence (a scored-decoy
// clause plus a second unrelated decoy clause), so the bearer cannot be told apart
// by the presence of a connective/parenthetical or by clause count alone. The
// scored DecoyValue is always present, so a harness that fishes a number from the
// wrong tool still surfaces the penalized decoy.
func (f Fixture) decoySentence(r *rand.Rand) string {
	if !f.has {
		return "No notable details were found."
	}
	primary := fmt.Sprintf(needleTemplates[r.Intn(len(needleTemplates))], f.decoySubject(), f.decoy, f.needle.Unit)
	sSubj, sVal := f.secondDecoy(r)
	secondary := fmt.Sprintf(needleTemplates[r.Intn(len(needleTemplates))], sSubj, sVal, f.needle.Unit)
	first, second := primary, secondary
	if r.Intn(2) == 1 {
		first, second = secondary, primary
	}
	conn := needleConnectives[r.Intn(len(needleConnectives))]
	return fmt.Sprintf(conn, first, second) + "."
}

// secondDecoy draws a second coined subject and wrong value for the decoy
// sentence's secondary clause, distinct from the needle and the primary decoy so
// the non-bearer sentence is a genuine two-clause statement. Pure in r (which the
// caller derives from the fixture seed), so the served text stays deterministic.
func (f Fixture) secondDecoy(r *rand.Rand) (string, string) {
	primary := f.decoySubject()
	subj := primary
	for i := 0; i < 8; i++ {
		name := coinedNames[r.Intn(len(coinedNames))]
		noun := coinedNouns[r.Intn(len(coinedNouns))]
		subj = "the " + name + " " + noun
		if subj != f.needle.Subject && subj != primary {
			break
		}
	}
	val := f.decoy
	for i := 0; i < 8; i++ {
		val = commaNumber(100 + r.Intn(99900))
		if val != f.needle.Value && val != f.decoy {
			break
		}
	}
	return subj, val
}

// decoySubject derives a coined subject distinct from the needle's, for the decoy
// clause/result. Deterministic in the fixture seed.
func (f Fixture) decoySubject() string {
	r := rand.New(rand.NewSource(f.seed ^ int64(fnv1a("decoy-subj"))))
	name := coinedNames[r.Intn(len(coinedNames))]
	nounIdx := r.Intn(len(coinedNouns))
	subj := fmt.Sprintf("the %s %s", name, coinedNouns[nounIdx])
	if subj == f.needle.Subject { // avoid colliding with the real subject
		// Step to a GUARANTEED different noun (the previous retry drew a fresh
		// random index that could land on the original noun again, so ~1 in 1700
		// fixtures still collided and served two clauses with the same subject).
		nounIdx = (nounIdx + 1) % len(coinedNouns)
		subj = fmt.Sprintf("the %s %s", name, coinedNouns[nounIdx])
	}
	return subj
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
	unitsForNoun = map[string][]string{
		"index": {"points", "basis points"}, "ledger": {"points", "basis points"},
		"syndicate": {"points", "basis points"}, "reservoir": {"acre-feet", "hectares"},
		"lattice": {"acre-feet", "hectares"}, "alloy": {"kilotonnes"},
		"foundry": {"kilotonnes"}, "corridor": {"megawatts"},
		"array": {"megawatts"}, "protocol": {"megawatts"},
		"initiative": {"units"}, "expedition": {"units"},
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
	unit := []string{"points", "acre-feet", "units", "basis points", "kilotonnes", "megawatts", "parsecs", "hectares"}[r.Intn(8)]
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

// pageURLForSeed derives the stable page URL a link-chain case's search_web
// serves, from the case seed alone, so the served result and the read_links
// gate agree.
func pageURLForSeed(caseSeed int64) string {
	r := rand.New(rand.NewSource(caseSeed ^ int64(fnv1a("link-chain-url"))))
	return coinedURL(r)
}

// calledWithURL reports whether a tool call's args reference the given URL.
// The check is a raw containment over the args JSON (tolerating either a
// string or an array "urls" value, and JSON \/-escaped slashes), so any honest
// arg shape that carries the served URL passes the gate.
func calledWithURL(raw json.RawMessage, url string) bool {
	if len(raw) == 0 || url == "" {
		return false
	}
	s := strings.ReplaceAll(string(raw), `\/`, "/")
	return strings.Contains(s, url)
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
