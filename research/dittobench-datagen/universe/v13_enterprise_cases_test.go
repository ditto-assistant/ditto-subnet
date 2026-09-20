package universe

import (
	"encoding/csv"
	"encoding/json"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// Replay only the served CSV/JSON and public question. This intentionally does
// not call the world evaluator or read the query program from provenance.
func TestV13EnterpriseCaseAnswersFromServedRecords(t *testing.T) {
	entityPattern := regexp.MustCompile(`entity-[a-f0-9]+`)
	checked := 0
	for seed := int64(0); seed < 18; seed++ {
		cases, err := GenerateV13EnterprisePrograms(seed, 12)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range cases {
			format := string(c.Provenance.Renderer)
			if format != "csv" && format != "json" {
				continue
			}
			var events []V13EnterpriseEvent
			for _, p := range c.Pairs {
				_, body, ok := strings.Cut(p.Prompt, "\n")
				if !ok {
					t.Fatal("missing document")
				}
				if format == "json" {
					var batch []V13EnterpriseEvent
					if err := json.Unmarshal([]byte(body), &batch); err != nil {
						t.Fatal(err)
					}
					events = append(events, batch...)
				} else {
					rows, err := csv.NewReader(strings.NewReader(body)).ReadAll()
					if err != nil {
						t.Fatal(err)
					}
					for _, row := range rows[1:] {
						cells := map[string]string{}
						for i, key := range rows[0] {
							cells[key] = row[i]
						}
						at, err := strconv.Atoi(cells["effective_step"])
						if err != nil {
							t.Fatal(err)
						}
						events = append(events, V13EnterpriseEvent{cells["entity"], cells["field"], cells["value"], at, cells["operation"]})
					}
				}
			}
			sort.Slice(events, func(i, j int) bool { return events[i].At < events[j].At })
			scalar := map[string]map[string]string{}
			members := map[string]map[string]bool{}
			for _, e := range events {
				if e.At > 12 {
					continue
				}
				if scalar[e.Entity] == nil {
					scalar[e.Entity] = map[string]string{}
				}
				if members[e.Entity] == nil {
					members[e.Entity] = map[string]bool{}
				}
				switch e.Operation {
				case "assign":
					scalar[e.Entity][e.Field] = e.Value
				case "add":
					members[e.Entity][e.Value] = true
				case "remove":
					delete(members[e.Entity], e.Value)
				default:
					t.Fatal("unknown operation")
				}
			}
			q := c.Plan.Case.Question
			target := entityPattern.FindString(q)
			team := scalar[target]["team"]
			if target == "" || team == "" {
				t.Fatal("unanswerable query binding")
			}
			var want string
			if strings.Contains(q, "contact channel") {
				want = scalar[scalar[scalar[team]["vendor"]]["owner"]]["channel"]
			} else {
				n := 0
				for id := range members[team] {
					if scalar[id]["availability"] != "available" {
						continue
					}
					if strings.Contains(q, "How many") {
						n++
					} else {
						hours, err := strconv.Atoi(scalar[id]["allocation_hours"])
						if err != nil {
							t.Fatal(err)
						}
						n += hours
					}
				}
				want = strconv.Itoa(n)
			}
			if want == "" || c.Plan.Case.ExpectedAnswer != want {
				t.Fatalf("served evidence gives %q, oracle %q", want, c.Plan.Case.ExpectedAnswer)
			}
			checked++
		}
	}
	if checked < 60 {
		t.Fatalf("insufficient served-record checks: %d", checked)
	}
}
