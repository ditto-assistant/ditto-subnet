package universe

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13OrdinaryDocumentAuthority(t *testing.T) {
	for seed := int64(1); seed <= 12; seed++ {
		w := v13World(seed)
		docs, err := w.v13OrdinaryDocuments()
		if err != nil {
			t.Fatal(err)
		}
		if len(docs) != len(w.People)+len(w.Projects)+len(w.Trips)+len(w.Preferences)+1+len(w.Probes.NearMiss)+len(w.Probes.Handles)+len(w.Probes.Threads) {
			t.Fatal("incomplete document coverage")
		}
		records := map[string]map[string]string{}
		relations := map[string]string{}
		for _, doc := range docs {
			for i, id := range doc.ids {
				if _, ok := records[id]; ok {
					t.Fatal("duplicated source")
				}
				args := map[string]string{}
				for _, a := range doc.request.Records[i].Assertions {
					for role, token := range a.Arguments {
						args[role] = doc.request.Bindings[token]
					}
					relations[id] += a.Relation
				}
				records[id] = args
			}
		}
		for _, p := range w.People {
			if !reflect.DeepEqual(records[p.EmailPairID], map[string]string{"previous_employer": p.PreviousEmployer, "previous_email": p.PreviousEmail}) {
				t.Fatal("historical email join changed")
			}
			if !reflect.DeepEqual(records[p.CorrectionPairID], map[string]string{"nickname": p.Nickname, "current_employer": p.Employer, "current_email": p.Email}) {
				t.Fatal("current email replacement changed")
			}
		}
		for _, p := range w.Projects {
			if !reflect.DeepEqual(records[p.LedgerPairID], map[string]string{"reference": p.RecordID, "original_invoice": money(p.OriginalCents), "paid": money(p.PaidCents)}) {
				t.Fatal("ledger operands changed")
			}
			if records[p.CorrectionPairID]["approved_invoice"] != money(p.CorrectedCents) || !strings.Contains(relations[p.CorrectionPairID], "partial payment is unchanged") {
				t.Fatal("invoice replacement changed payment")
			}
		}
		if !reflect.DeepEqual(records[w.Integrity.CanaryPairIDs[0]], map[string]string{"event_context": w.People[0].Context, "attendee_code": w.Integrity.CanaryNonce}) || !reflect.DeepEqual(records[w.Integrity.CanaryPairIDs[1]], map[string]string{"colleague": w.People[0].Name, "crew_code": w.Integrity.CanaryBaits[0]}) || !reflect.DeepEqual(records[w.Integrity.CanaryPairIDs[2]], map[string]string{"badge_owner": w.People[1].Name, "badge_code": w.Integrity.CanaryBaits[1]}) {
			t.Fatal("registration ownership changed")
		}
		// Computed answers, distractors and old prose are not author inputs.
		before, _ := json.Marshal(docsForTest(docs))
		for i := range w.Projects {
			w.Projects[i].OutstandingCents = -123
		}
		for i := range w.Trips {
			w.Trips[i].CurrentDays = -123
			w.Trips[i].PreviousDays = -456
		}
		for i := range w.Preferences {
			w.Preferences[i].Rejected = []string{"poisoned answer"}
		}
		for i := range w.Pairs {
			w.Pairs[i].Prompt = "poisoned prose"
			w.Pairs[i].Response = "poisoned response"
		}
		after, err := w.v13OrdinaryDocuments()
		if err != nil {
			t.Fatal(err)
		}
		encoded, _ := json.Marshal(docsForTest(after))
		if string(before) != string(encoded) {
			t.Fatal("compiler consumed prose, computed answers or distractors")
		}
	}
}

func docsForTest(docs []v13WorldDocument) []any {
	out := []any{}
	for _, doc := range docs {
		out = append(out, doc.request, doc.ids)
	}
	return out
}

type ordinaryDocumentFixture struct {
	documentRendererFixture
	rejectAt int
}

func (f *ordinaryDocumentFixture) CheckDocument(ctx context.Context, r V13FactDocumentRequest, p V13FactDocumentPlan) error {
	f.reject = f.rejectAt > 0 && f.checks+1 == f.rejectAt
	return f.documentRendererFixture.CheckDocument(ctx, r, p)
}

func TestV13OrdinaryDocumentAtomicAndProtected(t *testing.T) {
	for _, rejectAt := range []int{0, 2} {
		w := v13World(1)
		original := append([]protocol.MemoryPair(nil), w.Pairs...)
		docs, err := w.v13OrdinaryDocuments()
		if err != nil {
			t.Fatal(err)
		}
		target := map[string]bool{}
		for _, doc := range docs {
			for _, id := range doc.ids {
				target[id] = true
			}
		}
		f := &ordinaryDocumentFixture{rejectAt: rejectAt}
		err = w.RenderV13FactOrdinaryWorld(context.Background(), f)
		if rejectAt > 0 {
			if err == nil || !reflect.DeepEqual(w.Pairs, original) {
				t.Fatal("partial prompts escaped")
			}
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		changed := 0
		for i, p := range w.Pairs {
			if target[p.PairID] {
				if p.Prompt == original[i].Prompt {
					t.Fatal("unrendered record")
				}
				changed++
				p.Prompt = original[i].Prompt
			}
			if p != original[i] {
				t.Fatal("identity, chronology, response or protected record changed")
			}
		}
		if changed != len(target) || f.checks != len(docs) {
			t.Fatal("incomplete checked coverage")
		}
	}
}

func TestV13OrdinaryDocumentTripMutation(t *testing.T) {
	w := v13World(1)
	before, err := w.v13OrdinaryDocuments()
	if err != nil {
		t.Fatal(err)
	}
	trip := &w.Trips[0]
	changed := -1
	for i := range trip.LegDays {
		if trip.LegDays[i] != trip.OldLegDays[i] {
			changed = i
		}
	}
	if changed < 0 {
		t.Fatal("fixture missing correction")
	}
	trip.LegDays[changed] = trip.OldLegDays[changed] + 7
	after, err := w.v13OrdinaryDocuments()
	if err != nil {
		t.Fatal(err)
	}
	for i := range before {
		a, _ := json.Marshal(before[i].request)
		b, _ := json.Marshal(after[i].request)
		isTrip := before[i].ids[0] == trip.ContextPairID
		if (string(a) != string(b)) != isTrip {
			t.Fatal("counterfactual changed unrelated document")
		}
	}
	trip.LegDays[(changed+1)%3]++
	if _, err := w.v13OrdinaryDocuments(); err == nil {
		t.Fatal("unsupported multi-leg delta silently accepted")
	}
}

func TestV13ProbeDocumentEvidenceBoundaries(t *testing.T) {
	w := v13World(1)
	docs, err := w.v13OrdinaryDocuments()
	if err != nil {
		t.Fatal(err)
	}
	values := map[string]map[string]string{}
	for _, doc := range docs {
		for i, id := range doc.ids {
			args := map[string]string{}
			for _, a := range doc.request.Records[i].Assertions {
				for role, token := range a.Arguments {
					args[role] = doc.request.Bindings[token]
				}
			}
			values[id] = args
		}
	}
	for _, probe := range w.Probes.NearMiss {
		p := w.People[probe.Person]
		if !reflect.DeepEqual(values[probe.MentionPairID], map[string]string{"known_person": p.Nickname, "colleague": probe.Name, "employer": p.Employer, "event_context": p.Context}) {
			t.Fatal("near-name contact invented or identity collapsed")
		}
	}
	for _, probe := range w.Probes.Handles {
		if !reflect.DeepEqual(values[probe.HandlePairID], map[string]string{"nickname": w.People[probe.Person].Nickname, "signal_identifier": probe.Handle}) {
			t.Fatal("messaging identity changed")
		}
		if probe.Removed && !reflect.DeepEqual(values[probe.RemovalPairID], map[string]string{"nickname": w.People[probe.Person].Nickname}) {
			t.Fatal("withdrawal leaks old or replacement contact")
		}
	}
	for _, probe := range w.Probes.Threads {
		if !reflect.DeepEqual(values[probe.PaymentPairID], map[string]string{"alias": probe.Alias, "vendor": probe.Vendor, "invoice": probe.InvoiceID}) {
			t.Fatal("payment amount or balance invented")
		}
	}
	if err := w.RenderV13FactOrdinaryWorld(context.Background(), &ordinaryDocumentFixture{}); err != nil {
		t.Fatal(err)
	}
	byID := map[string]protocol.MemoryPair{}
	for _, p := range w.Pairs {
		byID[p.PairID] = p
	}
	for _, p := range w.Probes.Pairs {
		if p != byID[p.PairID] {
			t.Fatal("probe projection stale")
		}
	}
}

func TestV13ProbeProjectionPreflight(t *testing.T) {
	w := v13World(1)
	w.Probes.Pairs[0].PairID = "missing"
	before := append([]protocol.MemoryPair(nil), w.Pairs...)
	f := &ordinaryDocumentFixture{}
	if err := w.RenderV13FactOrdinaryWorld(context.Background(), f); err == nil || f.plans != 0 || !reflect.DeepEqual(w.Pairs, before) {
		t.Fatal("invalid probe projection dispatched or mutated world")
	}
}

func TestV13WorldFactSourceCoverage(t *testing.T) {
	for scale := 1; scale <= 3; scale++ {
		for seed := int64(1); seed <= 8; seed++ {
			w := GenerateForVersion(seed, scale, protocol.BenchVersionV13)
			counts := map[string]int{w.BusinessPairID: 1}
			docs, err := w.v13OrdinaryDocuments()
			if err != nil {
				t.Fatal(err)
			}
			for _, doc := range docs {
				for _, id := range doc.ids {
					counts[id]++
				}
			}
			for i := range w.StoryArcs {
				_, ids, err := w.V13StoryDocument(i)
				if err != nil {
					t.Fatal(err)
				}
				for _, id := range ids {
					counts[id]++
				}
			}
			if _, err := w.v13BusinessDocuments(); err != nil {
				t.Fatal(err)
			}
			if len(counts) != len(w.Pairs) {
				t.Fatal("source coverage differs from world pair set")
			}
			for _, p := range w.Pairs {
				if counts[p.PairID] != 1 {
					t.Fatal("world pair not owned by exactly one fact compiler")
				}
			}
		}
	}
}
