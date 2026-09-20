package universe

import (
	"reflect"
	"testing"
)

func TestV13EnterpriseComposedPrograms(t *testing.T) {
	w := V13EnterpriseWorld{Entities: []string{"work", "team", "a", "b", "c"}, Events: []V13EnterpriseEvent{
		{"work", "team", "team", 0, "assign"},
		{"team", "members", "a", 0, "add"}, {"team", "members", "b", 1, "add"}, {"team", "members", "c", 2, "add"},
		{"a", "availability", "available", 0, "assign"}, {"b", "availability", "reserved", 0, "assign"}, {"c", "availability", "available", 0, "assign"},
		{"a", "allocation_hours", "7", 0, "assign"}, {"b", "allocation_hours", "100", 0, "assign"}, {"c", "allocation_hours", "11", 0, "assign"},
		{"team", "members", "a", 3, "remove"}, {"c", "allocation_hours", "13", 4, "assign"}, {"team", "members", "a", 5, "add"},
	}}
	base := []V13EnterpriseStep{{Op: "follow", Field: "team"}, {Op: "follow", Field: "members"}, {Op: "filter", Field: "availability", Value: "available"}}
	for _, tc := range []struct {
		at       int
		terminal V13EnterpriseStep
		want     []string
	}{
		{2, V13EnterpriseStep{Op: "sum", Field: "allocation_hours"}, []string{"18"}},
		{4, V13EnterpriseStep{Op: "sum", Field: "allocation_hours"}, []string{"13"}},
		{5, V13EnterpriseStep{Op: "sum", Field: "allocation_hours"}, []string{"20"}},
		{4, V13EnterpriseStep{Op: "count"}, []string{"1"}},
		{5, V13EnterpriseStep{Op: "project", Field: "allocation_hours"}, []string{"13", "7"}},
	} {
		got, err := EvaluateV13EnterpriseProgram(w, "work", tc.at, append(append([]V13EnterpriseStep(nil), base...), tc.terminal))
		if err != nil || !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("at %d: %v %v", tc.at, got, err)
		}
	}
	for _, steps := range [][]V13EnterpriseStep{
		{{Op: "follow", Field: "unknown"}, {Op: "count"}},
		{{Op: "filter", Field: "missing", Value: "x"}, {Op: "count"}},
		{{Op: "filter", Field: "team", Value: "not-selected"}, {Op: "sum", Field: "untyped"}},
		{{Op: "count"}, {Op: "follow", Field: "team"}},
		{{Op: "follow", Field: "team"}},
	} {
		if _, err := EvaluateV13EnterpriseProgram(w, "work", 5, steps); err == nil {
			t.Fatal("accepted invalid program")
		}
	}
}

func TestV13EnterpriseIDsDoNotRevealJoins(t *testing.T) {
	w, err := GenerateV13Enterprise(123, 4, 8)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range w.Events {
		if e.Field == "channel" && e.Value == e.Entity+"@fictional.example" {
			t.Fatal("channel derived from entity")
		}
		if enterpriseReferenceFields[e.Field] && e.Value == e.Entity {
			t.Fatal("identity encodes self-join")
		}
	}
}
