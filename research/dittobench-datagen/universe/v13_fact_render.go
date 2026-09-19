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
	Revision        string               `json:"revision"`
	Subject         string               `json:"subject"`
	SubjectMode     string               `json:"subject_mode"`
	Facts           []V13RenderAssertion `json:"facts"`
	Query           []V13RenderQuery     `json:"query"`
	Bindings        map[string]string    `json:"bindings"`
	Required        [3][]string          `json:"record_required_tokens"`
	Allowed         [3][]string          `json:"record_allowed_tokens"`
	QuestionAllowed []string             `json:"question_allowed_tokens"`
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
}

type V13RenderQuery struct{ Op, Field, Meaning string }

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
	r := V13FactRenderRequest{Revision: "v13-structured-fact-render-v1", Bindings: map[string]string{}, SubjectMode: "named entity"}
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
		if a.Meaning == "" {
			a.Meaning = f.Field
		}
		if f.Mode == "independent" {
			a.Meaning = "independent launch-approval claim; no source supersedes another"
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
		if business && f.Mode != "independent" {
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
			m = "launch-approval claims; report the named people and agreement/disagreement"
		}
		r.Query = append(r.Query, V13RenderQuery{q.Op, fields[q.Field], m})
		r.QuestionAllowed = append(r.QuestionAllowed, fields[q.Field])
	}
	for i := range r.Required {
		if len(r.Required[i]) == 0 {
			return r, fmt.Errorf("render request has an unsupported empty record")
		}
	}
	return r, nil
}

var v13RenderToken = regexp.MustCompile(`\{\{[a-z]+[0-9]+\}\}`)

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
				return "", fmt.Errorf("render omitted a required fact binding")
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
		v, err := bind(text, r.Allowed[i], r.Required[i])
		if err != nil {
			return bound, err
		}
		bound.Records[i] = v
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
