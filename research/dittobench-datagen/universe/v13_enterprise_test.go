package universe

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func TestV13EnterpriseSeedReplayAndQueryRotation(t *testing.T) {
	for seed := int64(0); seed < 100; seed++ {
		w, err := GenerateV13Enterprise(seed, 6, 9)
		if err != nil {
			t.Fatal(err)
		}
		again, _ := GenerateV13Enterprise(seed, 6, 9)
		if !reflect.DeepEqual(w, again) {
			t.Fatal("seed replay")
		}
		for i := 0; i < 6; i++ {
			entity := enterpriseEntityID(seed, fmt.Sprintf("work-%04d", i))
			q := V13EnterpriseQuery{Entity: entity, At: 8, Joins: []string{"team", "vendor", "owner"}, Field: "channel", Operation: "values"}
			answer, err := EvaluateV13Enterprise(w, q)
			if err != nil || len(answer) != 1 {
				t.Fatalf("query rotation: %v %v", answer, err)
			}
			// Derive the independent expected owner from source history.
			vendor := enterpriseEntityID(seed, fmt.Sprintf("vendor-%04d", i))
			owner := ""
			for _, e := range w.Events {
				if e.Entity == vendor && e.Field == "owner" && e.At == 8 {
					owner = e.Value
				}
			}
			channel := ""
			for _, e := range w.Events {
				if e.Entity == owner && e.Field == "channel" {
					channel = e.Value
				}
			}
			if channel == "" || answer[0] != channel {
				t.Fatal("wrong three-hop answer")
			}
			q.Joins = []string{"team"}
			q.Field = "members"
			q.Operation = "count"
			answer, err = EvaluateV13Enterprise(w, q)
			members := map[string]bool{}
			team := enterpriseEntityID(seed, fmt.Sprintf("team-%04d", i))
			for _, e := range w.Events {
				if e.Entity == team && e.Field == "members" && e.At <= 8 {
					if e.Operation == "add" {
						members[e.Value] = true
					} else {
						delete(members, e.Value)
					}
				}
			}
			if err != nil || answer[0] != strconv.Itoa(len(members)) {
				t.Fatalf("set history: %v %v", answer, err)
			}
			q.At = 3
			answer, err = EvaluateV13Enterprise(w, q)
			if err != nil || answer[0] != "2" {
				t.Fatal("historical removal")
			}
		}
	}
}

func TestV13EnterpriseFormatsPreserveWorld(t *testing.T) {
	w, _ := GenerateV13Enterprise(42, 3, 5)
	w.Events = append(w.Events, V13EnterpriseEvent{enterpriseEntityID(42, "work-0000"), "note", "Quoted \"handoff\", owner list\nsecond line | \\ end", 0, "assign"})
	for _, format := range []string{"csv", "json"} {
		for seed := int64(0); seed < 8; seed++ {
			text, err := RenderV13EnterpriseData(w, seed, format)
			if err != nil {
				t.Fatal(err)
			}
			var events []V13EnterpriseEvent
			if format == "json" {
				if err := json.Unmarshal([]byte(text), &events); err != nil {
					t.Fatal(err)
				}
			} else {
				rows, err := csv.NewReader(strings.NewReader(text)).ReadAll()
				if err != nil {
					t.Fatal(err)
				}
				for _, row := range rows[1:] {
					m := map[string]string{}
					for i, k := range rows[0] {
						m[k] = row[i]
					}
					at, err := strconv.Atoi(m["effective_step"])
					if err != nil {
						t.Fatal(err)
					}
					events = append(events, V13EnterpriseEvent{m["entity"], m["field"], m["value"], at, m["operation"]})
				}
			}
			original, err := w.state(10)
			if err != nil {
				t.Fatal(err)
			}
			replayed, err := (V13EnterpriseWorld{w.Entities, events}).state(10)
			if err != nil || !reflect.DeepEqual(original, replayed) {
				t.Fatalf("format changed facts: %s %v", format, err)
			}
			again, _ := RenderV13EnterpriseData(w, seed, format)
			if text != again {
				t.Fatal("unstable rendering")
			}
		}
	}
}

func TestV13EnterpriseRejectsInvalidHistory(t *testing.T) {
	w, _ := GenerateV13Enterprise(3, 2, 3)
	for _, bad := range []V13EnterpriseEvent{
		{"work-0000", "team", "missing", 99, "assign"},
		{"team-0000", "members", "person-0000-1", 0, "add"},
		{"team-0000", "members", "person-0000-1", 0, "remove"},
		{"work-0000", "status", "active", 99, "remove"},
	} {
		bad.Entity = enterpriseEntityID(3, bad.Entity)
		if enterpriseReferenceFields[bad.Field] || enterpriseSetFields[bad.Field] {
			bad.Value = enterpriseEntityID(3, bad.Value)
		}
		copy := w
		copy.Events = append(append([]V13EnterpriseEvent(nil), w.Events...), bad)
		if _, err := copy.state(0); err == nil {
			t.Fatalf("accepted invalid event: %+v", bad)
		}
	}
	for _, q := range []V13EnterpriseQuery{
		{Entity: "missing", At: 5, Field: "status", Operation: "values"},
		{Entity: "work-0000", At: 5, Joins: []string{"status"}, Field: "channel", Operation: "values"},
		{Entity: "work-0000", At: 5, Field: "status", Operation: "guess"},
	} {
		q.Entity = enterpriseEntityID(3, q.Entity)
		if _, err := EvaluateV13Enterprise(w, q); err == nil {
			t.Fatal("accepted unsupported query")
		}
	}
}

func TestV13EnterpriseUnrelatedWorldGrowthPreservesAnswers(t *testing.T) {
	for seed := int64(0); seed < 30; seed++ {
		small, _ := GenerateV13Enterprise(seed, 2, 7)
		large, _ := GenerateV13Enterprise(seed, 20, 7)
		q := V13EnterpriseQuery{Entity: enterpriseEntityID(seed, "work-0000"), At: 6, Joins: []string{"team", "vendor", "owner"}, Field: "channel", Operation: "values"}
		a, err := EvaluateV13Enterprise(small, q)
		if err != nil {
			t.Fatal(err)
		}
		b, err := EvaluateV13Enterprise(large, q)
		if err != nil || !reflect.DeepEqual(a, b) {
			t.Fatal("unrelated world growth changed the answer")
		}
		// Evidence is not privileged by slice position; reverse the entire log.
		for i, j := 0, len(large.Events)-1; i < j; i, j = i+1, j-1 {
			large.Events[i], large.Events[j] = large.Events[j], large.Events[i]
		}
		b, err = EvaluateV13Enterprise(large, q)
		if err != nil || !reflect.DeepEqual(a, b) {
			t.Fatal("document order changed event chronology")
		}
	}
}
