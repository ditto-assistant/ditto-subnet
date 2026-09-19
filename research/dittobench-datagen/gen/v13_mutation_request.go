package gen

import (
	"context"
	"fmt"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func mutationFactRequest(s protocol.ToolMutationSource) (universe.V13FactDocumentRequest, error) {
	values := map[string]string{}
	var relation string
	switch s.Kind {
	case "handoff_correction":
		values = map[string]string{"project": s.ProjectAlias, "client": s.Client, "day": s.Day}
		relation = "Request that the existing mutable handoff scratchpad for project at client be corrected: the handoff is now day. Preserve the canonical project history; change the scratchpad rather than adding to that history. Do not claim the change has already been performed."
		if s.Reviewer != "" {
			values["reviewer"] = s.Reviewer
			relation += " Also request that the same scratchpad's reviewer be changed to reviewer. Both changes are required."
		}
	case "contact_receipt_deletion":
		values = map[string]string{"nickname": s.Nickname, "relationship": s.Relation, "employer": s.Employer, "event": s.EventContext}
		relation = "Request deletion ONLY of the temporary completed email-reconciliation receipt after event for nickname, the narrator's relationship at employer. Preserve the person's canonical contact details and contact history. Do not request deletion of those facts or claim deletion already happened."
	default:
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact mutation: unsupported source")
	}
	r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "mutation_request", Bindings: map[string]string{}}
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	args := map[string]string{}
	for i, k := range keys {
		token := fmt.Sprintf("{{value%d}}", i)
		r.Bindings[token], args[k] = values[k], token
	}
	r.Records = []universe.V13FactDocumentRecord{{Role: "request", MinBytes: 1, MaxBytes: 1800, Assertions: []universe.V13DocumentAssertion{{Kind: s.Kind, Relation: relation, Arguments: args}}}}
	return r, universe.ValidateV13FactDocumentRequest(r)
}

func renderMutationRequests(ctx context.Context, tools []protocol.ToolCase, renderer universe.V13FactDocumentRenderer) ([]protocol.ToolCase, error) {
	requests := map[int]universe.V13FactDocumentRequest{}
	for i, tc := range tools {
		if tc.MutationSource == nil {
			continue
		}
		r, err := mutationFactRequest(*tc.MutationSource)
		if err != nil {
			return nil, err
		}
		requests[i] = r
	}
	out := append([]protocol.ToolCase(nil), tools...)
	for i := range out {
		r, ok := requests[i]
		if !ok {
			continue
		}
		p, err := universe.RenderV13FactDocument(ctx, r, renderer)
		if err != nil {
			return nil, err
		}
		out[i].Prompt = p.Records[0]
	}
	return out, nil
}
