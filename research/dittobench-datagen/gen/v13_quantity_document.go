package gen

import (
	"context"
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func quantityFactDocument(fc FamilyV2Case) (universe.V13FactDocumentRequest, error) {
	relation := ""
	switch fc.Convention {
	case ConventionAdds:
		relation = "On THIS account, the named entry increases the opening figure by its magnitude. This explicitly stated convention overrides the label's usual meaning."
	case ConventionReduces:
		relation = "On THIS account, the named entry decreases the opening figure by its magnitude. This explicitly stated convention overrides the label's usual meaning."
	case ConventionNone:
		relation = "On THIS account, the named entry is tracked as a memo only and has NO effect on the opening figure, despite its recorded magnitude. This explicitly stated convention overrides the label's usual meaning."
	default:
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact quantity: unknown convention")
	}
	settled := "Already paid against the invoice; this amount is subtracted from what remains owed."
	if !fc.Unit.Monetary {
		switch fc.Unit.Name {
		case "nights":
			settled = "Nights already used from the booking; subtract them from the remaining stay."
		case "seats":
			settled = "Seats already assigned from the reservation; subtract them from the still available seats."
		case "hours":
			settled = "Hours already billed or used against the retainer; subtract them from the remaining hours."
		case "licences":
			settled = "Licences already assigned to users from the pool; subtract them from the remaining licences."
		case "percentage points":
			settled = "Percentage points already consumed by the early-payment clause; subtract them from the remaining discount. These are absolute points, not a relative percentage."
		default:
			return universe.V13FactDocumentRequest{}, fmt.Errorf("fact quantity: unknown unit")
		}
	}
	r := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "record_quantity", Bindings: map[string]string{
		"{{subject0}}": fc.Subject, "{{unit0}}": fc.Unit.Name, "{{cue0}}": fc.Cue.Phrase,
		"{{opening0}}": familyV2Amount(fc.Unit, fc.Opening), "{{magnitude0}}": familyV2Amount(fc.Unit, fc.Magnitude), "{{settled0}}": familyV2Amount(fc.Unit, fc.Settled),
	}, Records: []universe.V13FactDocumentRecord{{MinBytes: 1, MaxBytes: 1800, Assertions: []universe.V13DocumentAssertion{
		{Kind: "opening_quantity", Relation: "Original figure for this subject in the explicit unit. State the operands only, never calculate the standing figure or answer.", Arguments: map[string]string{"subject": "{{subject0}}", "unit": "{{unit0}}", "opening": "{{opening0}}"}},
		{Kind: "stated_convention", Relation: relation, Arguments: map[string]string{"subject": "{{subject0}}", "entry_label": "{{cue0}}", "magnitude": "{{magnitude0}}"}},
		{Kind: "settled_quantity", Relation: settled + " State the amount only; do not calculate any intermediate or final total.", Arguments: map[string]string{"subject": "{{subject0}}", "settled": "{{settled0}}"}},
	}}}}
	query := "Ask for the current remaining quantity for subject after the recorded entry and already settled amount, expressed in unit. Do not supply operands, a formula, the convention's direction, or the result; these must be recovered from the stored record."
	if fc.Unit.Monetary {
		query = "Ask what is currently owed on subject's account, expressed in unit. Do not supply operands, a formula, the convention's direction, or the result; these must be recovered from the stored record."
	}
	args := map[string]string{"subject": "{{subject0}}", "unit": "{{unit0}}"}
	switch fc.Shape {
	case familyV2Direction:
		query += " Also explicitly ask whether the entry labeled cue increased, decreased or left unchanged the opening figure under this account's convention. Ask for BOTH this entry's direction and the final remaining quantity; not the net direction after settlement. Do not suggest which direction is correct."
		args["cue"] = "{{cue0}}"
	case familyV2Plain:
	default:
		return universe.V13FactDocumentRequest{}, fmt.Errorf("fact quantity: unknown question shape")
	}
	r.Records = append(r.Records, universe.V13FactDocumentRecord{Role: "request", MinBytes: 1, MaxBytes: 1400, Assertions: []universe.V13DocumentAssertion{{Kind: "quantity_query_" + fc.Shape, Relation: query, Arguments: args}}})
	if err := universe.ValidateV13FactDocumentRequest(r); err != nil {
		return universe.V13FactDocumentRequest{}, err
	}
	return r, nil
}

func renderQuantityFacts(ctx context.Context, families []FamilyV2Case, renderer universe.V13FactDocumentRenderer) ([]FamilyV2Case, error) {
	requests := make([]universe.V13FactDocumentRequest, len(families))
	for i, fc := range families {
		if len(fc.Pairs) != 1 || len(fc.Staged.RequiredPairIDs) != 1 || fc.Pairs[0].PairID != fc.Staged.RequiredPairIDs[0] {
			return nil, fmt.Errorf("fact quantity: inconsistent evidence identity")
		}
		r, err := quantityFactDocument(fc)
		if err != nil {
			return nil, err
		}
		requests[i] = r
	}
	out := append([]FamilyV2Case(nil), families...)
	for i, r := range requests {
		plan, err := universe.RenderV13FactDocument(ctx, r, renderer)
		if err != nil {
			return nil, err
		}
		out[i].Pairs = append([]protocol.MemoryPair(nil), families[i].Pairs...)
		out[i].Pairs[0].Prompt = plan.Records[0]
		out[i].Staged.Case.Question = plan.Records[1]
	}
	return out, nil
}
