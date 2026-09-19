package universe

import (
	"context"
	"fmt"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// RenderV13FactStories replaces only story prompts, after every document has
// passed both structural and semantic checks. IDs, sessions, chronology,
// staged-wave assignment and hidden grading state remain owned by the world.
func (w *World) RenderV13FactStories(ctx context.Context, renderer V13FactDocumentRenderer) error {
	if w == nil || w.BenchVersion != protocol.BenchVersionV13 || renderer == nil {
		return fmt.Errorf("fact story: V13 world and renderer required")
	}
	type document struct {
		request V13FactDocumentRequest
		ids     []string
	}
	documents := make([]document, len(w.StoryArcs))
	indices := map[string]int{}
	for i, pair := range w.Pairs {
		if _, exists := indices[pair.PairID]; exists {
			return fmt.Errorf("fact story: duplicate pair identity")
		}
		indices[pair.PairID] = i
	}
	seen := map[string]bool{}
	for i := range documents {
		r, ids, err := w.V13StoryDocument(i)
		if err != nil {
			return err
		}
		for _, id := range ids {
			if _, ok := indices[id]; !ok || seen[id] {
				return fmt.Errorf("fact story: missing or repeated pair identity")
			}
			seen[id] = true
		}
		documents[i] = document{r, ids}
	}
	pairs := append([]protocol.MemoryPair(nil), w.Pairs...)
	for _, doc := range documents {
		bound, err := RenderV13FactDocument(ctx, doc.request, renderer)
		if err != nil {
			return err
		}
		for i, id := range doc.ids {
			pairs[indices[id]].Prompt = bound.Records[i]
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	w.Pairs = pairs
	return nil
}

// V13StoryDocument compiles the typed timeline, not Story sections or event
// prose. Ordered IDs map records back to their existing sessions and waves.
func (w World) V13StoryDocument(arcIndex int) (V13FactDocumentRequest, []string, error) {
	fail := func() (V13FactDocumentRequest, []string, error) {
		return V13FactDocumentRequest{}, nil, fmt.Errorf("fact story: invalid source")
	}
	if arcIndex < 0 || arcIndex >= len(w.StoryArcs) {
		return fail()
	}
	arc := w.StoryArcs[arcIndex]
	v := arc.V2
	if v == nil || arc.PersonIndex < 0 || arc.PersonIndex >= len(w.People) {
		return fail()
	}
	r := V13FactDocumentRequest{Revision: V13FactDocumentRevision, Domain: "story", Bindings: map[string]string{}}
	ids := append([]string(nil), v.PairIDs...)
	ids = append(ids, v.DecoyPairID)
	for range ids {
		r.Records = append(r.Records, V13FactDocumentRecord{MinBytes: 1800, MaxBytes: 4600, InteriorFacts: true})
	}
	tokens := map[string]string{}
	bind := func(value string) string {
		if token, ok := tokens[value]; ok {
			return token
		}
		token := fmt.Sprintf("{{value%d}}", len(tokens))
		tokens[value] = token
		r.Bindings[token] = value
		return token
	}
	add := func(record int, kind, relation string, args map[string]string) bool {
		if record < 0 || record >= len(r.Records) {
			return false
		}
		roles := make([]string, 0, len(args))
		for role := range args {
			roles = append(roles, role)
		}
		sort.Strings(roles)
		bound := map[string]string{}
		for _, role := range roles {
			if args[role] == "" {
				return false
			}
			bound[role] = bind(args[role])
		}
		r.Records[record].Assertions = append(r.Records[record].Assertions, V13DocumentAssertion{Kind: kind, Relation: relation, Arguments: bound})
		return true
	}
	for _, e := range v.Events {
		if e.Memory < 0 || e.Memory >= len(v.PairIDs) {
			return fail()
		}
		reference := v.JoinKey2
		if e.Position == 0 {
			reference = v.JoinKey1
			if !add(e.Memory, "anchor", "The person's informal subject is identified by this first reference; this binding appears only in this record.", map[string]string{"person": w.People[arc.PersonIndex].Nickname, "subject": v.SubjectAlias, "reference": v.JoinKey1}) {
				return fail()
			}
		} else if e.Position == 1 {
			if !add(e.Memory, "reference_bridge", "Both references identify the same thread. Do not introduce the original person or informal subject.", map[string]string{"first_reference": v.JoinKey1, "second_reference": v.JoinKey2}) {
				return fail()
			}
		}
		args := map[string]string{"reference": reference}
		take := func(keys ...string) {
			for _, key := range keys {
				args[key] = e.Slots[key]
			}
		}
		relation := ""
		switch e.Kind {
		case EventKickoff, EventMove, EventMedicalCourse, EventSchoolLogistics, EventWedding, EventRenovation, EventBillDispute, EventTripReplan, EventSubscriptionCancelled:
			relation = "Initial owner of the thread, before any later reassignment. The event kind defines its subject activity."
			take("who")
		case EventQuote, EventBooking:
			relation = "Initial provider, before any provider replacement. A quote or booking is not a completed outcome."
			take("from", "noun")
		case EventApprovalCapped:
			relation = "Approved spending cap and amount already spent; these are operands, not remaining balance."
			take("cap", "spent")
		case EventContactRouteChanged:
			relation = "Completed contact route replacement from the former address to the new address."
			take("from", "to")
		case EventVendorSwapped, EventProviderSwapped:
			relation = "Completed provider replacement: from is the original provider, to is the replacement provider."
			take("from", "to", "noun")
		case EventIncident:
			relation = "An intermediate setback, not the final outcome."
			take("status")
		case EventCorrection:
			relation = "A completed correction supersedes the earlier mistaken record."
			take("status")
		case EventHandoffAssigned:
			relation = "Completed ownership reassignment from prev to who. The assigned next action and channel replace any earlier next action."
			take("prev", "who", "what", "channel")
		case EventFollowUp:
			relation = "Latest assigned next action replaces earlier next actions, but does not itself reassign thread ownership. Action is pending, not completed."
			take("who", "what", "channel")
		case EventOutcome:
			relation = "Final recorded outcome supersedes intermediate statuses."
			if v.Disagree {
				relation = "One of two independent final outcome records of equal authority. Neither date nor record order resolves their disagreement."
			}
			take("status")
		case EventOutcomeDisputed:
			relation = "Independent conflicting final outcome from source, equal in authority to the other final outcome. It is not a superseding correction; dates cannot resolve this disagreement."
			take("status", "source")
		default:
			return fail()
		}
		// Within the main timeline, explicit event positions preserve repeated
		// handoffs/routes without using storage order as a chronology oracle.
		if e.Kind != EventOutcomeDisputed {
			relation = fmt.Sprintf("Timeline event %d. %s", e.Position+1, relation)
		}
		if !add(e.Memory, string(e.Kind), relation, args) {
			return fail()
		}
		if q := e.QuantityEffect; q != nil {
			qa := map[string]string{"reference": reference, "operand": q.Operand, "unit": q.Kind}
			qr := ""
			switch q.Op {
			case "initial":
				qr = "Original quoted or booked quantity."
			case "delay":
				qr = "Schedule delay by the stated duration; not a change to the quoted scope quantity."
			case "add":
				qr = "New scope adds operand2 to original operand. State the operation, never calculate the resulting total."
				qa["operand2"] = q.Operand2
			case "subtract":
				qr = "Subtract operand2 from operand. For money, operand is the approved cap and operand2 is already spent. State operands and operation, never the remaining total."
				qa["operand2"] = q.Operand2
			case "replace":
				qr = "Corrected quantity operand replaces mistaken operand2, not an arithmetic increment."
				qa["operand2"] = q.Operand2
			default:
				return fail()
			}
			if !add(e.Memory, "quantity_"+q.Op, qr, qa) {
				return fail()
			}
		} else if e.Slots["qtyphrase"] != "" || e.Slots["qtybase"] != "" {
			return fail()
		}
		if e.Kind == EventOutcome && v.Lesson != nil {
			if !add(e.Memory, "lesson", "Explicit lesson learned from this thread.", map[string]string{"reference": reference, "lesson": v.Lesson.canonical}) {
				return fail()
			}
		}
	}
	var decoy *StoryDecoySource
	for _, story := range w.Stories {
		if story.PairID == v.DecoyPairID {
			if decoy != nil {
				return fail()
			}
			decoy = story.DecoySource
		}
	}
	if decoy == nil {
		return fail()
	}
	if !add(len(ids)-1, "independent_thread", "A separate similarly named thread with its own reference, owner, provider and final status. Never equate it to another thread.", map[string]string{"person": decoy.Person, "subject": decoy.Alias, "owner": decoy.Owner, "provider": decoy.Provider, "provider_kind": decoy.ProviderKind, "reference": decoy.Reference, "reference_kind": decoy.ReferenceKind, "status": decoy.Status}) {
		return fail()
	}
	if err := ValidateV13FactDocumentRequest(r); err != nil {
		return fail()
	}
	return r, ids, nil
}
