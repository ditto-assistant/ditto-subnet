package universe

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
)

// Enterprise facts have no relevance/noise bit. The same corpus can answer
// different queries; rendering must never receive the selected query or answer.
// This foundational slice is opt-in until the versioned envelope is integrated.
type V13EnterpriseEvent struct {
	Entity    string `json:"entity"`
	Field     string `json:"field"`
	Value     string `json:"value"`
	At        int    `json:"effective_step"`
	Operation string `json:"operation"` // assign, add or remove
}

type V13EnterpriseWorld struct {
	Entities []string
	Events   []V13EnterpriseEvent
}

type V13EnterpriseQuery struct {
	Entity    string
	At        int
	Joins     []string
	Field     string
	Operation string // values or count
}

var enterpriseReferenceFields = map[string]bool{"team": true, "vendor": true, "owner": true}
var enterpriseSetFields = map[string]bool{"members": true}

func enterpriseEntityID(seed int64, raw string) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("enterprise-identity-v2/%d/%s", seed, raw)))
	return fmt.Sprintf("entity-%x", h[:12])
}

// State validation is independent of prose and rejects ambiguous histories.
// Numeric steps are explicit event chronology, not document/ingest order.
func (w V13EnterpriseWorld) state(at int) (map[string]map[string][]string, error) {
	if at < 0 || len(w.Entities) == 0 || len(w.Entities) > 10000 || len(w.Events) > 100000 {
		return nil, fmt.Errorf("enterprise world: invalid bounds")
	}
	state := map[string]map[string][]string{}
	for _, id := range w.Entities {
		if id == "" || state[id] != nil {
			return nil, fmt.Errorf("enterprise world: duplicate/empty entity")
		}
		state[id] = map[string][]string{}
	}
	events := append([]V13EnterpriseEvent(nil), w.Events...)
	sort.Slice(events, func(i, j int) bool { return events[i].At < events[j].At })
	seen := map[string]bool{}
	for _, e := range events {
		if state[e.Entity] == nil || e.Field == "" || e.Value == "" || e.At < 0 {
			return nil, fmt.Errorf("enterprise world: invalid event")
		}
		if enterpriseReferenceFields[e.Field] || e.Field == "members" {
			if state[e.Value] == nil {
				return nil, fmt.Errorf("enterprise world: dangling reference")
			}
		}
		key := fmt.Sprintf("%q/%q/%d", e.Entity, e.Field, e.At)
		if seen[key] {
			return nil, fmt.Errorf("enterprise world: ambiguous chronology")
		}
		seen[key] = true
		if enterpriseSetFields[e.Field] {
			if e.Operation != "add" && e.Operation != "remove" {
				return nil, fmt.Errorf("enterprise world: invalid set edit")
			}
		} else if e.Operation != "assign" {
			return nil, fmt.Errorf("enterprise world: invalid scalar edit")
		}
		// Validate the entire history, including events beyond the query point.
	}
	full := map[string][]string{}
	for _, e := range events {
		key := fmt.Sprintf("%q/%q", e.Entity, e.Field)
		values := append([]string(nil), full[key]...)
		switch e.Operation {
		case "assign":
			values = []string{e.Value}
		case "add":
			for _, value := range values {
				if value == e.Value {
					return nil, fmt.Errorf("enterprise world: duplicate set addition")
				}
			}
			values = append(values, e.Value)
		case "remove":
			index := -1
			for i, value := range values {
				if value == e.Value {
					index = i
				}
			}
			if index < 0 {
				return nil, fmt.Errorf("enterprise world: removal of absent member")
			}
			values = append(values[:index], values[index+1:]...)
		}
		sort.Strings(values)
		full[key] = values
		if e.At <= at {
			state[e.Entity][e.Field] = append([]string{}, values...)
		}
	}
	return state, nil
}

// EvaluateV13Enterprise follows typed references, then projects a value/set or
// counts it. Missing evidence is an error, not an invented empty answer.
func EvaluateV13Enterprise(w V13EnterpriseWorld, q V13EnterpriseQuery) ([]string, error) {
	if len(q.Joins) > 8 || (q.Operation != "values" && q.Operation != "count") {
		return nil, fmt.Errorf("enterprise query: invalid program")
	}
	s, err := w.state(q.At)
	if err != nil {
		return nil, err
	}
	entity := q.Entity
	for _, field := range q.Joins {
		if !enterpriseReferenceFields[field] {
			return nil, fmt.Errorf("enterprise query: not a reference")
		}
		values := s[entity][field]
		if len(values) != 1 || s[values[0]] == nil {
			return nil, fmt.Errorf("enterprise query: missing join")
		}
		entity = values[0]
	}
	values, present := s[entity][q.Field]
	if !present {
		return nil, fmt.Errorf("enterprise query: missing evidence")
	}
	if q.Operation == "count" {
		return []string{strconv.Itoa(len(values))}, nil
	}
	return append([]string(nil), values...), nil
}

// GenerateV13Enterprise creates multiple equally queryable workstreams. The
// entity count and edit depth are bounded knobs; no target is selected here.
func GenerateV13Enterprise(seed int64, teams, edits int) (V13EnterpriseWorld, error) {
	if teams < 2 || teams > 100 || edits < 1 || edits > 30 {
		return V13EnterpriseWorld{}, fmt.Errorf("enterprise generation: invalid bounds")
	}
	pick := func(domain string, n int) int {
		h := sha256.Sum256([]byte(fmt.Sprintf("enterprise-world-v2/%d/%s", seed, domain)))
		return int(binary.BigEndian.Uint64(h[:8]) % uint64(n))
	}
	w := V13EnterpriseWorld{}
	add := func(entity, field, value string, at int, op string) {
		w.Events = append(w.Events, V13EnterpriseEvent{entity, field, value, at, op})
	}
	domains := []string{"procurement", "product release", "cloud capacity", "customer support", "AI evaluation", "compliance change"}
	for i := 0; i < teams; i++ {
		project, team, vendor := fmt.Sprintf("work-%04d", i), fmt.Sprintf("team-%04d", i), fmt.Sprintf("vendor-%04d", i)
		w.Entities = append(w.Entities, project, team, vendor)
		add(project, "team", team, 0, "assign")
		add(project, "domain", domains[pick(fmt.Sprintf("domain/%d", i), len(domains))], 0, "assign")
		add(team, "vendor", vendor, 0, "assign")
		add(vendor, "channel", fmt.Sprintf("vendor-%04d@fictional.example", i), 0, "assign")
		for j := 0; j < 3; j++ {
			person := fmt.Sprintf("person-%04d-%d", i, j)
			w.Entities = append(w.Entities, person)
			add(person, "channel", fmt.Sprintf("person-%04d-%d@fictional.example", i, j), 0, "assign")
			add(team, "members", person, j, "add")
			add(person, "allocation_hours", strconv.Itoa(1+pick(fmt.Sprintf("allocation/%d/%d", i, j), 20)), 0, "assign")
			add(person, "availability", []string{"available", "reserved"}[pick(fmt.Sprintf("availability/%d/%d", i, j), 2)], 0, "assign")
		}
		for j := 0; j < edits; j++ {
			person := fmt.Sprintf("person-%04d-%d", i, pick(fmt.Sprintf("owner/%d/%d", i, j), 3))
			add(vendor, "owner", person, j, "assign")
			add(project, "status", []string{"planned", "active", "paused", "complete"}[pick(fmt.Sprintf("status/%d/%d", i, j), 4)], j, "assign")
		}
		// Draw which member changes at every step; only valid removal/addition
		// is emitted for that member's current state, including empty sets.
		present := [3]bool{true, true, true}
		for j := 0; j < edits; j++ {
			memberIndex := pick(fmt.Sprintf("member/%d/%d", i, j), 3)
			member := fmt.Sprintf("person-%04d-%d", i, memberIndex)
			op := "add"
			if present[memberIndex] {
				op = "remove"
			}
			present[memberIndex] = !present[memberIndex]
			add(team, "members", member, j+3, op)
		}
	}
	// IDs must not encode joins (work-0000 -> team-0000 -> vendor-0000).
	// Channels are independently drawn too, not derivable from an entity ID.
	for i, id := range w.Entities {
		w.Entities[i] = enterpriseEntityID(seed, id)
	}
	for i := range w.Events {
		e := &w.Events[i]
		e.Entity = enterpriseEntityID(seed, e.Entity)
		if enterpriseReferenceFields[e.Field] || enterpriseSetFields[e.Field] {
			e.Value = enterpriseEntityID(seed, e.Value)
		}
		if e.Field == "channel" {
			h := sha256.Sum256([]byte(fmt.Sprintf("enterprise-channel-v2/%d/%s", seed, e.Value)))
			e.Value = fmt.Sprintf("contact-%x@fictional.example", h[:8])
		}
	}
	_, err := w.state(edits + 3)
	return w, err
}

// RenderV13EnterpriseData serializes all events with no oracle/provenance or
// relevance metadata. Presentation entropy only permutes rows and CSV columns.
// Structured formats are lossless; later prose adapters must preserve this same
// relation contract rather than creating a separate noise grammar.
func RenderV13EnterpriseData(w V13EnterpriseWorld, presentationSeed int64, format string) (string, error) {
	if _, err := w.state(0); err != nil {
		return "", err
	}
	events := append([]V13EnterpriseEvent(nil), w.Events...)
	rank := func(e V13EnterpriseEvent) [32]byte {
		b, _ := json.Marshal(e)
		return sha256.Sum256([]byte(fmt.Sprintf("enterprise-presentation-v1/%d/%s", presentationSeed, b)))
	}
	sort.Slice(events, func(i, j int) bool { a, b := rank(events[i]), rank(events[j]); return bytes.Compare(a[:], b[:]) < 0 })
	if format == "json" {
		b, err := json.MarshalIndent(events, "", "  ")
		return string(b), err
	}
	if format != "csv" {
		return "", fmt.Errorf("enterprise render: unsupported format")
	}
	return enterpriseCSVGroup(events, presentationSeed)
}

func enterpriseCSVGroup(events []V13EnterpriseEvent, presentationSeed int64) (string, error) {
	columns := []string{"entity", "field", "value", "effective_step", "operation"}
	h := sha256.Sum256([]byte(fmt.Sprintf("enterprise-columns-v1/%d", presentationSeed)))
	shift := int(h[0]) % len(columns)
	columns = append(columns[shift:], columns[:shift]...)
	var b bytes.Buffer
	c := csv.NewWriter(&b)
	if err := c.Write(columns); err != nil {
		return "", err
	}
	for _, e := range events {
		values := map[string]string{"entity": e.Entity, "field": e.Field, "value": e.Value, "effective_step": strconv.Itoa(e.At), "operation": e.Operation}
		row := make([]string, len(columns))
		for i, col := range columns {
			row[i] = values[col]
		}
		if err := c.Write(row); err != nil {
			return "", err
		}
	}
	c.Flush()
	return b.String(), c.Error()
}
