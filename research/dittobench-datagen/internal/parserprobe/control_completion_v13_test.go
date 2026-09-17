package parserprobe

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13ScopedEffortControlFollowsRecordNotReference(t *testing.T) {
	const ref = "c0123456789abcdef"
	prompt := "For planning context " + ref + ": Set my usual thinking level."
	for _, value := range []string{"low", "medium", "high"} {
		st := newStore(13)
		st.pairs = []protocol.MemoryPair{{PairID: "same-record", Prompt: "This note applies only to planning context " + ref + ". For my Ditto chats, default the reasoning effort to " + value + " — that's my standing preference."}}
		p, matched := restraintContextPredictionV13(prompt, st)
		if !matched || !p.ok || len(p.tools) != 1 || p.tools[0].RequiredArgs["effort"] != value {
			t.Fatalf("record value %s not followed: %+v", value, p)
		}
		st.pairs[0].Prompt = "This note applies only to planning context " + ref + ". I have not decided."
		st.pairs[0].Response = "For planning context " + ref + " only: Understood — no default on file yet; tell me when you settle on one."
		p, matched = restraintContextPredictionV13(prompt, st)
		if !matched || !p.ok || len(p.tools) != 0 {
			t.Fatal("acted without a stored default")
		}
		st.pairs[0].Prompt = strings.ReplaceAll(st.pairs[0].Prompt, ref, "c1111111111111111")
		if _, matched := restraintContextPredictionV13(prompt, st); matched {
			t.Fatal("used unrelated scope")
		}
	}
}

func TestV13NamedControlGeneratedCases(t *testing.T) {
	profile, _ := gen.ProfileForVersion("full", 13)
	for seed := int64(1); seed <= 10; seed++ {
		a, err := gen.GenerateDataset(seed, profile, 13)
		if err != nil {
			t.Fatal(err)
		}
		parser := newToolParser(13)
		parser.addV13WireCatalog(a.Catalog)
		st := buildStores(a)[gen.PrimaryUser]
		needles := fixtureNeedles(a)
		for _, c := range a.ToolCases {
			switch c.Category {
			case "world_memory_update", "world_memory_delete":
			case "v13_state_dependent_calendar", "v13_state_dependent_email", "v13_restraint_abstention_web", "v13_restraint_declarative_preference":
			case "set_effort", "v10_state_dependent_routing", "world_contact_research_email_result_usage", "world_theme_discover_set", "world_business_workflow", "v13_restraint_effort", "v13_restraint_email", "v13_restraint_calendar":
			default:
				continue
			}
			p := parser.classifyTool(c.Prompt, st)
			excluded := resultUsageArgs(c.Category, needleValue(needles[c.ID]))
			if !p.ok || toolSignature(p.tools, c.FuzzyTrajectory, excluded) != toolSignature(c.ExpectedTools, c.FuzzyTrajectory, excluded) {
				t.Errorf("seed %d %s prompt=%q predicted=%q expected=%q", seed, c.Category, c.Prompt, toolSignature(p.tools, c.FuzzyTrajectory, excluded), toolSignature(c.ExpectedTools, c.FuzzyTrajectory, excluded))
			}
		}
	}
}

func TestV13SeatControlRejectsHypotheticalQuantity(t *testing.T) {
	st := newStore(13)
	st.pairs = []protocol.MemoryPair{{Prompt: "Note on the Acme event: if the larger venue had been approved we would have booked 900 seats, but it was rejected. The number actually booked is 120 seats."}}
	d := answerBookedSeatsV13(st, "How many seats were actually booked for the Acme event?")
	if !d.ok || d.kind != protocol.AnswerNumber || d.value != "120" {
		t.Fatalf("wrong actual quantity: %+v", d)
	}
	if d := answerBookedSeatsV13(st, "How many seats were actually booked for the Other event?"); d.ok {
		t.Fatal("used another event")
	}
}

func TestV13ControlStateComesFromVisibleRecords(t *testing.T) {
	const alias = "example lane"
	st := newStore(13)
	st.pairs = []protocol.MemoryPair{{Prompt: `Calendar state for "example lane" at Example Ltd.`, Response: "Nothing for the example lane review is on the calendar yet — when I ask, create it, Taylor."}}
	p := stateRoutePredictionV13("v13_state_dependent_calendar", alias, st)
	if !p.ok || len(p.tools) != 1 || p.tools[0].Name != "calendar_create_event" {
		t.Fatalf("absent event: %+v", p)
	}
	st.pairs[0].Response = `The example lane review is already on the calendar as "example lane review" — I'd rather not double-book it, so when I ask, find the existing entry and tell me where it sits.`
	p = stateRoutePredictionV13("v13_state_dependent_calendar", alias, st)
	if !p.ok || len(p.tools) != 1 || p.tools[0].Name != "calendar_search_events" || p.tools[0].RequiredArgs["query"] != "example lane review" {
		t.Fatalf("existing event: %+v", p)
	}
	st.pairs = append(st.pairs, protocol.MemoryPair{Prompt: st.pairs[0].Prompt, Response: "conflicting state"})
	if p := stateRoutePredictionV13("v13_state_dependent_calendar", alias, st); p.ok {
		t.Fatal("accepted conflicting records")
	}
	if p := stateRoutePredictionV13("v13_state_dependent_calendar", "other lane", st); p.ok {
		t.Fatal("used unrelated state")
	}
}

func TestV13ComposedNoiseDoesNotAlterCapturedValues(t *testing.T) {
	f := compileFrame("Clean up the post-%s note after the dependency-risk review.")
	f.literalBudget = 3
	slots, ok := f.match("Cleean up the psot-Acme rollout note after the dpedenncy-rsik review.")
	if !ok || len(slots) != 1 || slots[0] != "Acme rollout" {
		t.Fatalf("capture: %v %v", slots, ok)
	}
	f.literalBudget = 0
	if _, ok := f.match("Clean up the psot-Acme rollout note after the dependency-risk review."); ok {
		t.Fatal("changed legacy prefix matching")
	}
	f.literalBudget = 3
	if _, ok := f.match("Do not clean up the post-Acme rollout note after the dependency-risk review."); ok {
		t.Fatal("swallowed negation")
	}
}
