package gen

import (
	"context"
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func divergenceFactDocument(s divergenceFactSource) (universe.V13FactDocumentRequest, error) {
	r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "stated_evidence", Bindings: map[string]string{"{{actual0}}": s.Actual, "{{other0}}": s.Other}}
	args := map[string]string{"actual": "{{actual0}}", "other": "{{other0}}"}
	relation := ""
	switch s.Kind {
	case "negation":
		relation = "The narrator's dentist is NOT Dr. other. Their CURRENT dentist IS Dr. actual. Both people must appear, with explicit negation applied only to other. Do not suggest a second current dentist."
	case "supersession":
		relation = "The narrator previously banked with other. Last month they moved EVERY account to actual, which is now their bank. Explicitly distinguish superseded from current state."
	case "hypothetical_seats":
		relation = "For subject's event, approving a larger venue WOULD HAVE meant booking other seats. That proposal was REJECTED. Actual seats were in fact booked. Do not treat the hypothetical as actual or add the figures together. State both counts in seats."
		r.Bindings["{{subject0}}"] = s.Subject
		args["subject"] = "{{subject0}}"
	case "source_priority":
		relation = "Speaker claimed the front-door code was other. The narrator's OWN note states actual. Explicitly establish that the narrator's note is the authoritative source to trust, overriding the speaker's claim. This is not an unresolved equal-authority disagreement."
		r.Bindings["{{speaker0}}"] = s.Speaker
		args["speaker"] = "{{speaker0}}"
	default:
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact divergence: unsupported source")
	}
	r.Records = []universe.V13FactDocumentRecord{{MinBytes: 1, MaxBytes: 1400, Assertions: []universe.V13DocumentAssertion{{Kind: s.Kind, Relation: relation, Arguments: args}}}}
	if s.Actual == s.Other {
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact divergence: indistinguishable values")
	}
	if err := universe.ValidateV13FactDocumentRequest(r); err != nil {
		return universe.V13FactDocumentRequest{}, err
	}
	return r, nil
}

func renderDivergenceFacts(ctx context.Context, cases []StagedCase, pairs []protocol.MemoryPair, sources []divergenceFactSource, renderer universe.V13FactDocumentRenderer) ([]protocol.MemoryPair, error) {
	if len(cases) != len(pairs) || len(pairs) != len(sources) {
		return nil, fmt.Errorf("fact divergence: source cardinality mismatch")
	}
	requests := make([]universe.V13FactDocumentRequest, len(sources))
	for i, s := range sources {
		if len(cases[i].RequiredPairIDs) != 1 || cases[i].RequiredPairIDs[0] != pairs[i].PairID {
			return nil, fmt.Errorf("fact divergence: evidence identity mismatch")
		}
		r, err := divergenceFactDocument(s)
		if err != nil {
			return nil, err
		}
		requests[i] = r
	}
	out := append([]protocol.MemoryPair(nil), pairs...)
	for i, r := range requests {
		p, err := universe.RenderV13FactDocument(ctx, r, renderer)
		if err != nil {
			return nil, err
		}
		out[i].Prompt = p.Records[0]
	}
	return out, nil
}
