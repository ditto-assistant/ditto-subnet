package universe

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

const V13FactDocumentRevision = "v13-fact-document-v1"

// A document is compiled from typed source state, never from previously
// rendered prose. Record boundaries preserve joins, evidence placement and
// staged availability. Arguments contain binding tokens, not literal values.
type V13DocumentAssertion struct {
	Kind      string            `json:"kind"`
	Relation  string            `json:"relation"`
	Arguments map[string]string `json:"arguments"`
}
type V13FactDocumentRecord struct {
	Assertions    []V13DocumentAssertion `json:"assertions"`
	MinBytes      int                    `json:"min_bytes"`
	MaxBytes      int                    `json:"max_bytes"`
	InteriorFacts bool                   `json:"interior_facts,omitempty"`
}
type V13FactDocumentRequest struct {
	Revision string                  `json:"revision"`
	Domain   string                  `json:"domain"`
	Records  []V13FactDocumentRecord `json:"records"`
	Bindings map[string]string       `json:"bindings,omitempty"`
}
type V13FactDocumentPlan struct {
	Records []string `json:"records"`
}

type V13FactDocumentRenderer interface {
	PlanDocument(context.Context, V13FactDocumentRequest) (V13FactDocumentPlan, error)
	CheckDocument(context.Context, V13FactDocumentRequest, V13FactDocumentPlan) error
}

func cloneV13DocumentRequest(r V13FactDocumentRequest) V13FactDocumentRequest {
	b, _ := json.Marshal(r)
	var copy V13FactDocumentRequest
	_ = json.Unmarshal(b, &copy)
	return copy
}
func cloneV13DocumentPlan(p V13FactDocumentPlan) V13FactDocumentPlan {
	return V13FactDocumentPlan{Records: append([]string(nil), p.Records...)}
}

// ValidateV13FactDocumentRequest rejects malformed source contracts before any
// paid renderer call. Unused bindings are rejected to keep checker authority
// limited to the facts actually present in the document.
func ValidateV13FactDocumentRequest(r V13FactDocumentRequest) error {
	invalid := func() error { return fmt.Errorf("fact document: invalid source contract") }
	if r.Revision != V13FactDocumentRevision || strings.TrimSpace(r.Domain) == "" || len(r.Domain) > 64 || len(r.Records) == 0 || len(r.Records) > 16 {
		return invalid()
	}
	used := map[string]bool{}
	for _, record := range r.Records {
		if len(record.Assertions) == 0 || record.MinBytes < 1 || record.MaxBytes < record.MinBytes || record.MaxBytes > 16000 {
			return invalid()
		}
		for _, a := range record.Assertions {
			if strings.TrimSpace(a.Kind) == "" || strings.TrimSpace(a.Relation) == "" || len(a.Arguments) == 0 {
				return invalid()
			}
			for role, token := range a.Arguments {
				value := r.Bindings[token]
				if strings.TrimSpace(role) == "" || token == "" || v13RenderToken.FindString(token) != token || strings.TrimSpace(value) == "" || strings.Contains(value, "{{") || strings.Contains(value, "}}") {
					return invalid()
				}
				used[token] = true
			}
		}
	}
	if len(used) != len(r.Bindings) {
		return invalid()
	}
	return nil
}

// BindV13FactDocument enforces structural fidelity only. Every returned record
// must still pass an independent semantic check against all its assertions.
func BindV13FactDocument(r V13FactDocumentRequest, p V13FactDocumentPlan) (V13FactDocumentPlan, error) {
	fail := func() (V13FactDocumentPlan, error) {
		return V13FactDocumentPlan{}, fmt.Errorf("fact document: invalid record contract")
	}
	if ValidateV13FactDocumentRequest(r) != nil || len(r.Records) != len(p.Records) {
		return fail()
	}
	out := V13FactDocumentPlan{Records: make([]string, len(p.Records))}
	for i, record := range r.Records {
		if len(record.Assertions) == 0 || record.MinBytes < 1 || record.MaxBytes < record.MinBytes || record.MaxBytes > 16000 {
			return fail()
		}
		required := map[string]bool{}
		for _, a := range record.Assertions {
			if a.Kind == "" || a.Relation == "" || len(a.Arguments) == 0 {
				return fail()
			}
			for role, token := range a.Arguments {
				if role == "" || v13RenderToken.FindString(token) != token || r.Bindings[token] == "" {
					return fail()
				}
				required[token] = true
			}
		}
		text := p.Records[i]
		if len(text) > 32000 || strings.TrimSpace(text) == "" {
			return fail()
		}
		seen := map[string]int{}
		for _, token := range v13RenderToken.FindAllString(text, -1) {
			if !required[token] {
				return fail()
			}
			seen[token]++
			if seen[token] > 32 {
				return fail()
			}
		}
		for token := range required {
			if seen[token] == 0 {
				return fail()
			}
		}
		plain := v13RenderToken.ReplaceAllString(text, "")
		if strings.Contains(plain, "{{") || strings.Contains(plain, "}}") {
			return fail()
		}
		out.Records[i] = v13RenderToken.ReplaceAllStringFunc(text, func(token string) string { return r.Bindings[token] })
		if len(out.Records[i]) < record.MinBytes || len(out.Records[i]) > record.MaxBytes {
			return fail()
		}
		if record.InteriorFacts {
			for token := range required {
				position := float64(strings.Index(out.Records[i], r.Bindings[token])) / float64(len(out.Records[i]))
				if position < 0.15 || position > 0.85 {
					return fail()
				}
			}
		}
	}
	return out, nil
}

// RenderV13FactDocument does not expose partially checked output. The producer
// owns facts and bindings; callbacks receive copies and cannot mutate them.
func RenderV13FactDocument(ctx context.Context, r V13FactDocumentRequest, renderer V13FactDocumentRenderer) (V13FactDocumentPlan, error) {
	if renderer == nil {
		return V13FactDocumentPlan{}, fmt.Errorf("fact document: independent renderer required")
	}
	if err := ctx.Err(); err != nil {
		return V13FactDocumentPlan{}, err
	}
	if err := ValidateV13FactDocumentRequest(r); err != nil {
		return V13FactDocumentPlan{}, err
	}
	p, err := renderer.PlanDocument(ctx, cloneV13DocumentRequest(r))
	if err != nil {
		return V13FactDocumentPlan{}, err
	}
	bound, err := BindV13FactDocument(r, p)
	if err != nil {
		return V13FactDocumentPlan{}, err
	}
	if err := ctx.Err(); err != nil {
		return V13FactDocumentPlan{}, err
	}
	if err := renderer.CheckDocument(ctx, cloneV13DocumentRequest(r), cloneV13DocumentPlan(bound)); err != nil {
		return V13FactDocumentPlan{}, err
	}
	return bound, nil
}
