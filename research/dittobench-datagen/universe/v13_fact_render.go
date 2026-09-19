package universe

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// V13FactRenderRequest is author-side structured truth, not previously rendered
// prose. The only answer authority remains the typed-world evaluator. Values
// are represented by tokens; a renderer can rearrange language, not invent or
// substitute the literal facts. This request is private producer input.
type V13FactRenderRequest struct {
	Revision         string               `json:"revision"`
	Subject          string               `json:"subject"`
	SubjectEntity    string               `json:"subject_entity"`
	SubjectMode      string               `json:"subject_mode"`
	Facts            []V13RenderAssertion `json:"facts"`
	Query            []V13RenderQuery     `json:"query"`
	Bindings         map[string]string    `json:"bindings"`
	Required         [3][]string          `json:"record_required_tokens"`
	Allowed          [3][]string          `json:"record_allowed_tokens"`
	QuestionAllowed  []string             `json:"question_allowed_tokens"`
	QuestionTemplate string               `json:"question_template,omitempty"`
}

type V13RenderAssertion struct {
	Record               int `json:"record"`
	Entity, Field, Value string
	Meaning              string `json:"field_meaning"`
	Mode                 string `json:"mode"`
	Sequence             int    `json:"sequence"`
	Date                 string `json:"date,omitempty"`
	Kind                 string `json:"kind"`
	Unit                 string `json:"unit,omitempty"`
	Relation             string `json:"relation"`
}

type V13RenderQuery struct{ Op, Field, Meaning, Requirement string }

// These describe the evaluator's operators, not model-selected interpretations
// of prose. Each query entry is a separate answer obligation.
func v13QueryRequirement(op string) string {
	switch op {
	case "read":
		return "Explicitly ask for the VALUE of this field, not merely mention the field while asking for another value."
	case "latest":
		return "Explicitly ask for the final current VALUE of this field after its superseding update."
	case "latest_date":
		return "Ask which occurrence has the latest explicit occurrence date, regardless of record order."
	case "conflict":
		return "Ask which person each independent source names as the sole launch approver and whether those assignments agree. Compare the records, not whether the people personally agree."
	case "set_after_update":
		return "Ask for the complete current set after the stated removal and addition, retaining all unaffected initial members."
	default:
		return ""
	}
}

func v13AssertionRelation(mode string, sequence int) string {
	switch mode {
	case "history":
		if sequence == 0 {
			return "Initial state, explicitly superseded by the final update. Merely saying had or was does not establish this ordering. Record position and note timestamp do not establish state chronology."
		}
		return "Final update, explicitly superseding the initial state of this same field. No further update follows."
	case "independent":
		return "This source identifies the value person as the SOLE launch approver for the entity. The person is the assigned approver, not the speaker or maker of a claim. No source has priority."
	default:
		return mode
	}
}

type V13FactRenderPlan struct {
	Records  [3]string `json:"records"`
	Question string    `json:"question"`
}

// V13FactRenderer must independently check each bound rendering against its
// structured facts/query. Plan production alone never blesses text. Production
// implementations must pin model/provider identity, persist receipts and
// enforce a budget; the interface has no fallback to old surface rewriting.
type V13FactRenderer interface {
	Plan(context.Context, V13FactRenderRequest) (V13FactRenderPlan, error)
	Check(context.Context, V13FactRenderRequest, V13FactRenderPlan) error
}

func V13FactRenderDigest(value any) (string, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	h := sha256.Sum256(raw)
	return hex.EncodeToString(h[:]), nil
}

func v13FactRenderRequest(w v13FactWorld, s v13Schema, business bool) (V13FactRenderRequest, error) {
	r := V13FactRenderRequest{Revision: "v13-structured-fact-render-v3", Bindings: map[string]string{}, SubjectMode: "named entity"}
	if _, err := w.evaluate(); err != nil {
		return r, err
	}
	meaning := map[string]string{s.Owner: "accountable owner", s.Status: "standing status", s.Event: "dated occurrence", s.Client: "commissioning customer who pays us", s.Vendor: "supplier whom we pay", s.Action: "pending next action", s.Responsible: "person responsible for the pending next action", s.Channel: "agreed communication channel"}
	if !business {
		meaning = map[string]string{}
	}
	// Entity and field tokens are shared, value/date tokens belong to facts.
	// This makes a counterfactual one binding change, not another LLM rewrite.
	shared := map[string]string{}
	bind := func(kind, value string) string {
		key := kind + "\x00" + value
		if token := shared[key]; token != "" {
			return token
		}
		token := fmt.Sprintf("{{%s%d}}", kind, len(shared))
		shared[key] = token
		r.Bindings[token] = value
		return token
	}
	r.Subject = bind("subject", w.Entity)
	if business {
		r.Subject = bind("purpose", w.Purpose)
		r.SubjectMode = "workstream resolved by remit, not by direct alias"
	}
	r.QuestionAllowed = append(r.QuestionAllowed, r.Subject)
	fields := map[string]string{}
	for i, f := range w.Facts {
		a := V13RenderAssertion{Record: f.Record, Entity: bind("entity", f.Entity), Field: bind("field", f.Field), Value: fmt.Sprintf("{{value%d}}", i), Mode: f.Mode, Sequence: f.Order, Kind: f.Value.Kind, Unit: f.Value.Unit, Meaning: meaning[f.Field]}
		a.Relation = v13AssertionRelation(f.Mode, f.Order)
		if a.Meaning == "" {
			a.Meaning = f.Field
		}
		if f.Mode == "independent" {
			a.Meaning = "person identified by this independent source as the sole launch approver; no source supersedes another"
		}
		fields[f.Field] = a.Field
		r.Bindings[a.Value] = f.Value.Surface
		tokens := []string{a.Entity, a.Field, a.Value}
		if f.Mode == "dated" {
			a.Date = fmt.Sprintf("{{date%d}}", i)
			r.Bindings[a.Date] = f.Date.Format("2006-01-02")
			tokens = append(tokens, a.Date)
		}
		r.Required[f.Record] = append(r.Required[f.Record], a.Entity, a.Value)
		// Only opaque schema roles require literal field tokens. Ordinary
		// descriptive fields (e.g. a neutral planned milestone date) may be
		// expressed naturally and remain subject to independent checking.
		if business && meaning[f.Field] != "" && f.Mode != "independent" {
			r.Required[f.Record] = append(r.Required[f.Record], a.Field)
		}
		if a.Date != "" {
			r.Required[f.Record] = append(r.Required[f.Record], a.Date)
		}
		r.Allowed[f.Record] = append(r.Allowed[f.Record], tokens...)
		r.Facts = append(r.Facts, a)
	}
	for _, q := range w.Query {
		m := meaning[q.Field]
		if m == "" {
			m = q.Field
		}
		if q.Op == "conflict" {
			m = "exclusive launch-approver assignments; report the people named by the sources and whether the assignments agree"
		}
		requirement := v13QueryRequirement(q.Op)
		if requirement == "" {
			return r, fmt.Errorf("unsupported render query operator")
		}
		r.Query = append(r.Query, V13RenderQuery{Op: q.Op, Field: fields[q.Field], Meaning: m, Requirement: requirement})
		r.QuestionAllowed = append(r.QuestionAllowed, fields[q.Field])
	}
	// The final materialized task supplies this exact entity/remit binding
	// separately from the authored records. Make it explicit to the checker
	// rather than asking it to infer an opaque alias from an unrelated remit.
	r.SubjectEntity = bind("entity", w.Entity)
	for i := range r.Required {
		if len(r.Required[i]) == 0 {
			return r, fmt.Errorf("render request has an unsupported empty record")
		}
	}
	return r, nil
}

var v13RenderToken = regexp.MustCompile(`\{\{[a-z]+[0-9]+\}\}`)
var v13RelativeChronology = regexp.MustCompile(`(?i)\b(later|earlier|then|subsequently|afterwards?|beforehand)\b`)
var v13InitialHistory = regexp.MustCompile(`(?i)\b(initial|initially|original|originally|opening|outset)\b`)
var v13FinalHistory = regexp.MustCompile(`(?i)\b(final|latest|supersedes|superseded|superseding|replaces|replaced|replacing)\b`)

// BindV13FactRenderPlan is structural validation ONLY. Independent Check is
// mandatory before any returned text can be used for a generated case.
func BindV13FactRenderPlan(r V13FactRenderRequest, p V13FactRenderPlan) (V13FactRenderPlan, error) {
	var bound V13FactRenderPlan
	bind := func(text string, allowed, required []string) (string, error) {
		if strings.TrimSpace(text) == "" || len(text) > 16000 {
			return "", fmt.Errorf("invalid render text size")
		}
		permit := map[string]bool{}
		for _, token := range allowed {
			permit[token] = true
		}
		seen := map[string]int{}
		for _, token := range v13RenderToken.FindAllString(text, -1) {
			if !permit[token] || r.Bindings[token] == "" {
				return "", fmt.Errorf("render used an unbound or disallowed token")
			}
			seen[token]++
		}
		for _, token := range required {
			if seen[token] == 0 {
				return "", fmt.Errorf("render omitted required token %s", token)
			}
		}
		for _, n := range seen {
			if n > 16 {
				return "", fmt.Errorf("excessive render token repetition")
			}
		}
		plain := v13RenderToken.ReplaceAllString(text, "")
		if strings.Contains(plain, "{{") || strings.Contains(plain, "}}") {
			return "", fmt.Errorf("malformed render token")
		}
		return v13RenderToken.ReplaceAllStringFunc(text, func(token string) string { return r.Bindings[token] }), nil
	}
	for i, text := range p.Records {
		for _, fact := range r.Facts {
			if fact.Record == i && fact.Mode == "dated" && v13RelativeChronology.MatchString(text) {
				return bound, fmt.Errorf("relative chronology forbidden in dated record; use the explicit date token")
			}
			if fact.Record == i && fact.Mode == "history" {
				if fact.Sequence == 0 && !v13InitialHistory.MatchString(text) {
					return bound, fmt.Errorf("history record must explicitly identify initial/original state")
				}
				if fact.Sequence == 1 && !v13FinalHistory.MatchString(text) {
					return bound, fmt.Errorf("history record must explicitly identify final superseding update")
				}
			}
		}
		v, err := bind(text, r.Allowed[i], r.Required[i])
		if err != nil {
			return bound, err
		}
		bound.Records[i] = v
	}
	if r.QuestionTemplate != "" && p.Question != r.QuestionTemplate {
		return bound, fmt.Errorf("question must exactly preserve the compiled query template")
	}
	q, err := bind(p.Question, r.QuestionAllowed, []string{r.Subject})
	if err != nil {
		return bound, err
	}
	bound.Question = q
	return bound, nil
}

func applyV13FactRender(ctx context.Context, w v13FactWorld, s v13Schema, business bool, m v13Member, renderer V13FactRenderer, cached *V13FactRenderPlan) (v13Member, *V13FactRenderPlan, error) {
	if renderer == nil {
		return m, nil, fmt.Errorf("fact renderer and independent validator required")
	}
	req, err := v13FactRenderRequest(w, s, business)
	if err != nil {
		return m, nil, err
	}
	// The typed evaluator's query compiler, not the prose author, owns which
	// values are requested. Its presentation-seeded wording still varies.
	// Only role/subject tokens are exposed to the author, never answer values.
	req.QuestionTemplate = strings.ReplaceAll(m.Question, req.Bindings[req.Subject], req.Subject)
	for _, q := range req.Query {
		req.QuestionTemplate = strings.ReplaceAll(req.QuestionTemplate, req.Bindings[q.Field], q.Field)
	}
	plan := cached
	if plan == nil {
		// Do not let an implementation mutate the authoritative request via maps.
		raw, _ := json.Marshal(req)
		var copy V13FactRenderRequest
		_ = json.Unmarshal(raw, &copy)
		created, err := renderer.Plan(ctx, copy)
		if err != nil {
			return m, nil, err
		}
		plan = &created
	}
	bound, err := BindV13FactRenderPlan(req, *plan)
	if err != nil {
		return m, nil, err
	}
	// Check receives fresh copies; mutations cannot change the accepted output.
	raw, _ := json.Marshal(req)
	var checkRequest V13FactRenderRequest
	_ = json.Unmarshal(raw, &checkRequest)
	if err := renderer.Check(ctx, checkRequest, bound); err != nil {
		return m, nil, err
	}
	m.Records, m.Question = bound.Records, bound.Question
	return m, plan, nil
}
