package universe

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Values keep ontology identity separate from its rendered alias. A status
// shorthand is evidence of a status, not a second independently mutable fact.
type v13FactValue struct {
	Canonical, Surface, Kind string
	Unit                     string
	Accept                   []string
}

type v13Fact struct {
	Entity, Field string
	Value         v13FactValue
	Mode          string // static, history, dated, or independent evidence
	Order         int
	Date          time.Time
	Record        int
}

type v13FactQuery struct{ Op, Field string }

type v13FactWorld struct {
	Entity, Purpose string
	Facts           []v13Fact
	Query           []v13FactQuery
}

func factValue(text, kind string, accept ...string) v13FactValue {
	return v13FactValue{Canonical: text, Surface: text, Kind: kind, Accept: append([]string{text}, accept...)}
}

func (w v13FactWorld) evaluate() ([]v13FactValue, error) {
	if w.Entity == "" || w.Purpose == "" || len(w.Query) == 0 {
		return nil, fmt.Errorf("incomplete fact-world query")
	}
	for _, f := range w.Facts {
		if f.Entity == "" || f.Field == "" || f.Value.Canonical == "" || f.Value.Surface == "" || f.Value.Kind == "" || f.Record < 0 || f.Record > 2 {
			return nil, fmt.Errorf("incomplete fact")
		}
		aliasBound := f.Value.Surface == f.Value.Canonical
		for _, alias := range f.Value.Accept {
			aliasBound = aliasBound || f.Value.Surface == alias
		}
		if !aliasBound {
			return nil, fmt.Errorf("surface alias is not bound to canonical fact")
		}
		switch f.Mode {
		case "static", "independent":
		case "set_member", "set_remove", "set_add":
			if f.Order < 0 || f.Value.Kind != protocol.ClaimKindSetMember {
				return nil, fmt.Errorf("invalid set assertion")
			}
		case "history":
			if f.Order < 0 {
				return nil, fmt.Errorf("negative fact chronology")
			}
		case "dated":
			if f.Date.IsZero() {
				return nil, fmt.Errorf("missing event date")
			}
		default:
			return nil, fmt.Errorf("unknown fact mode %q", f.Mode)
		}
	}
	var out []v13FactValue
	for _, q := range w.Query {
		var matched []v13Fact
		for _, f := range w.Facts {
			if f.Entity == w.Entity && f.Field == q.Field {
				matched = append(matched, f)
			}
		}
		if len(matched) == 0 {
			return nil, fmt.Errorf("query lacks evidence")
		}
		if q.Op == "set_after_update" {
			values, err := evaluateV13FactSet(matched)
			if err != nil {
				return nil, err
			}
			out = append(out, values...)
			continue
		}
		mode := map[string]string{"read": "static", "latest": "history", "latest_date": "dated", "conflict": "independent"}[q.Op]
		if mode == "" {
			return nil, fmt.Errorf("unsupported query operator")
		}
		for _, f := range matched {
			if f.Mode != mode {
				return nil, fmt.Errorf("mixed evidence semantics")
			}
			if f.Value.Kind != matched[0].Value.Kind || f.Value.Unit != matched[0].Value.Unit {
				return nil, fmt.Errorf("mixed value types for one field")
			}
		}
		switch q.Op {
		case "read":
			if len(matched) != 1 {
				return nil, fmt.Errorf("ambiguous static fact")
			}
			out = append(out, matched[0].Value)
		case "latest", "latest_date":
			sort.Slice(matched, func(i, j int) bool {
				if q.Op == "latest_date" {
					return matched[i].Date.Before(matched[j].Date)
				}
				return matched[i].Order < matched[j].Order
			})
			for i := 1; i < len(matched); i++ {
				if (q.Op == "latest" && matched[i-1].Order == matched[i].Order) || (q.Op == "latest_date" && matched[i-1].Date.Equal(matched[i].Date)) {
					return nil, fmt.Errorf("ambiguous fact chronology")
				}
			}
			out = append(out, matched[len(matched)-1].Value)
		case "conflict":
			if len(matched) != 2 {
				return nil, fmt.Errorf("conflict query requires two independent records")
			}
			// Record identity, not input slice order, defines the two sources.
			if matched[0].Record == matched[1].Record {
				return nil, fmt.Errorf("conflict sources are not independent")
			}
			sort.Slice(matched, func(i, j int) bool { return matched[i].Record < matched[j].Record })
			if matched[0].Value.Canonical == matched[1].Value.Canonical {
				out = append(out, matched[0].Value, factValue("agree", protocol.ClaimKindConflict, "consistent", "match"))
			} else {
				out = append(out, matched[0].Value, matched[1].Value, factValue("disagree", protocol.ClaimKindConflict, V13ConflictAccept...))
			}
		}
	}
	return out, nil
}

func evaluateV13FactSet(facts []v13Fact) ([]v13FactValue, error) {
	var initial, updates []v13Fact
	for _, f := range facts {
		switch f.Mode {
		case "set_member":
			initial = append(initial, f)
		case "set_remove", "set_add":
			updates = append(updates, f)
		default:
			return nil, fmt.Errorf("mixed set evidence semantics")
		}
	}
	if len(initial) == 0 || len(updates) != 2 {
		return nil, fmt.Errorf("unsupported set history")
	}
	sort.Slice(initial, func(i, j int) bool { return initial[i].Order < initial[j].Order })
	sort.Slice(updates, func(i, j int) bool { return updates[i].Order < updates[j].Order })
	if updates[0].Mode != "set_remove" || updates[1].Mode != "set_add" || updates[0].Order != 0 || updates[1].Order != 1 {
		return nil, fmt.Errorf("unsupported set operations")
	}
	present := map[string]bool{}
	for i, f := range initial {
		if present[f.Value.Canonical] || (i > 0 && initial[i-1].Order == f.Order) {
			return nil, fmt.Errorf("ambiguous initial set")
		}
		present[f.Value.Canonical] = true
	}
	drop, add := updates[0].Value.Canonical, updates[1].Value.Canonical
	if !present[drop] || present[add] {
		return nil, fmt.Errorf("invalid set mutation")
	}
	var values []v13FactValue
	for _, f := range initial {
		if f.Value.Canonical != drop {
			values = append(values, f.Value)
		}
	}
	return append(values, updates[1].Value), nil
}

// Build world mutations before rendering. This is the only place that maps
// seeded latent draws into assertions; the renderer never picks an answer.
func buildV13FactWorld(d *v13Draws, s v13Schema, group int, g v13Group, counter bool) (v13FactWorld, error) {
	w := v13FactWorld{Entity: g.Alias, Purpose: g.Purpose}
	person := func(i int) v13FactValue { return factValue(g.People[i], protocol.ClaimKindPerson) }
	current := 1
	if counter {
		current = 2
	}
	add := func(field, mode string, record, order int, value v13FactValue) {
		w.Facts = append(w.Facts, v13Fact{Entity: g.Alias, Field: field, Value: value, Mode: mode, Record: record, Order: order})
	}
	history := func(field string, first, final v13FactValue) {
		add(field, "history", 0, 0, first)
		add(field, "history", 1, 1, final)
		w.Query = append(w.Query, v13FactQuery{"latest", field})
	}
	switch g.Family {
	case V13FamilyOwnerAfterCorrection:
		history(s.Owner, person(0), person(current))
	case V13FamilyStandingStatus:
		status := func(i int) v13FactValue {
			c := V13StatusClasses[g.Classes[i]]
			v := factValue(c.Canonical, protocol.ClaimKindStatus, c.Accept...)
			v.Surface = d.statusAlias(g.Classes[i])
			v.Accept = append(v.Accept, v.Surface)
			return v
		}
		history(s.Status, status(0), status(current))
	case V13FamilyCurrentChannel:
		channel := func(i int) v13FactValue {
			c := V13ChannelClasses[g.Classes[i]]
			return factValue(c.Canonical, protocol.ClaimKindChannel, c.Accept...)
		}
		history(s.Channel, channel(0), channel(current))
	case V13FamilyNextActionResponsible:
		a := V13ActionClasses[g.Classes[0]]
		add(s.Action, "static", 0, 0, factValue(a.Canonical, protocol.ClaimKindAction, a.Accept...))
		w.Query = append(w.Query, v13FactQuery{"read", s.Action})
		history(s.Responsible, person(0), person(current))
	case V13FamilyClientVsVendor:
		client := 0
		if counter {
			client = 2
		}
		add(s.Client, "static", 0, 0, factValue(g.Orgs[client], protocol.ClaimKindOrganisation))
		add(s.Vendor, "static", 1, 0, factValue(g.Orgs[1], protocol.ClaimKindOrganisation))
		w.Query = []v13FactQuery{{"read", s.Client}}
	case V13FamilyLatestEvent:
		for i, e := range g.Events {
			if counter && i == g.CounterEvent {
				e.Date = g.CounterDate
			}
			c := V13EventClasses[e.Class]
			add(s.Event, "dated", i, 0, factValue(c.Canonical, protocol.ClaimKindEvent, c.Accept...))
			w.Facts[len(w.Facts)-1].Date = v13CalendarDate(d.seed, "business", group, e.Date, true)
		}
		w.Query = []v13FactQuery{{"latest_date", s.Event}}
	case V13FamilyRecordsDisagree:
		add(s.Owner, "independent", 0, 0, person(0))
		add(s.Owner, "independent", 1, 0, person(current))
		w.Query = []v13FactQuery{{"conflict", s.Owner}}
	default:
		return w, fmt.Errorf("unsupported fact-world family")
	}
	_, err := w.evaluate()
	return w, err
}

// Every renderer form encodes an explicit relation; aliases/names/dates are
// inserted as atoms. No post-render model may reinterpret these assertions.
func renderV13Fact(f v13Fact, correction string, seed int64, key string) (string, error) {
	var forms []string
	switch f.Mode {
	case "static":
		forms = []string{"For %[1]s, the %[2]s is %[3]s.", "The %[2]s recorded for %[1]s is %[3]s.", "%[3]s is listed as %[1]s's %[2]s."}
	case "history":
		if f.Order == 0 {
			forms = []string{"Initially, %[1]s's %[2]s was %[3]s.", "The opening record for %[1]s gives %[3]s as its %[2]s.", "At the outset, %[3]s was recorded as the %[2]s for %[1]s."}
		} else if f.Order == 1 {
			forms = []string{"The final %[4]s for %[1]s gives %[3]s as its %[2]s, superseding the initial entry.", "For %[1]s, the latest %[4]s replaces the original %[2]s with %[3]s.", "%[1]s's %[2]s is now %[3]s: this is the final %[4]s, not the initial entry."}
		} else {
			return "", fmt.Errorf("renderer does not support this history length")
		}
	case "dated":
		forms = []string{"On %[5]s, %[1]s logged the %[2]s %[3]s.", "The %[2]s %[3]s for %[1]s happened on %[5]s.", "%[1]s's %[2]s record dates %[3]s to %[5]s."}
	case "independent":
		forms = []string{"This independent record names %[3]s as the launch approver for %[1]s. It does not supersede any other record.", "For %[1]s, this record credits %[3]s with launch sign-off. This is an independent claim, not a correction to another record.", "Launch approval for %[1]s is attributed to %[3]s here; this record has no priority over the other independent record."}
	case "set_member":
		forms = []string{"The original %[2]s for %[1]s includes %[3]s.", "%[3]s is on the initial %[2]s for %[1]s."}
	case "set_remove":
		forms = []string{"The update removed %[3]s from the %[2]s for %[1]s.", "For %[1]s, %[3]s is no longer required on the %[2]s."}
	case "set_add":
		forms = []string{"The update added %[3]s to the %[2]s for %[1]s; all items not explicitly removed remain required.", "The %[2]s for %[1]s now also requires %[3]s; every original item except the one explicitly removed is still required."}
	default:
		return "", fmt.Errorf("unrenderable fact mode")
	}
	date := ""
	if !f.Date.IsZero() {
		forms := []string{f.Date.Format("2006-01-02"), f.Date.Format("January 2, 2006"), f.Date.Format("2 January 2006")}
		date = v13Pick(seed, key+":date", forms)
	}
	return fmt.Sprintf(v13Pick(seed, key, forms), f.Entity, f.Field, f.Value.Surface, correction, date), nil
}

func renderV13FactQuery(w v13FactWorld, seed int64) (string, error) {
	subject := fmt.Sprintf(v13Pick(seed, "fact-subject", []string{"the workstream whose remit is %s", "the workstream responsible for %s", "the workstream tasked with %s"}), w.Purpose)
	var parts []string
	for i, q := range w.Query {
		var forms []string
		switch q.Op {
		case "read":
			forms = []string{"what is the %[2]s recorded for %[1]s?", "for %[1]s, give the recorded %[2]s."}
		case "latest":
			forms = []string{"after all recorded changes, what is the %[2]s for %[1]s?", "for %[1]s, give the final recorded %[2]s."}
		case "latest_date":
			forms = []string{"which %[2]s happened most recently for %[1]s? Name the event, not its date.", "for %[1]s, name the last %[2]s by occurrence date, not by note order."}
		case "conflict":
			forms = []string{"do the independent records agree about launch approval for %[1]s? Name everyone they credit and state agree or disagree.", "for %[1]s, give the launch approver named in each independent record and say whether those records agree."}
		case "set_after_update":
			forms = []string{"what items remain on the %[2]s for %[1]s after the recorded changes? Give the complete list."}
		default:
			return "", fmt.Errorf("unrenderable fact query")
		}
		parts = append(parts, fmt.Sprintf(v13Pick(seed, fmt.Sprintf("fact-query-%d", i), forms), subject, q.Field))
	}
	return strings.Join(parts, " "), nil
}

func renderV13FactMember(w v13FactWorld, s v13Schema, seed int64, milestone string) (v13Member, error) {
	m := v13Member{Kind: protocol.AnswerValue}
	values, err := w.evaluate()
	if err != nil {
		return m, err
	}
	// Fail closed on histories the prose cannot represent, including a lone
	// "final" event whose predecessor would otherwise be invented by prose.
	histories := map[string][]int{}
	for _, f := range w.Facts {
		if f.Mode == "history" {
			histories[f.Entity+"\x00"+f.Field] = append(histories[f.Entity+"\x00"+f.Field], f.Order)
		}
	}
	for _, orders := range histories {
		sort.Ints(orders)
		if len(orders) != 2 || orders[0] != 0 || orders[1] != 1 {
			return m, fmt.Errorf("unsupported rendered history")
		}
	}
	for i, f := range w.Facts {
		row, err := renderV13Fact(f, s.Correction, seed, fmt.Sprintf("fact-%d", i))
		if err != nil {
			return m, err
		}
		if m.Records[f.Record] != "" {
			m.Records[f.Record] += " "
		}
		m.Records[f.Record] += row
		m.Protected = append(m.Protected, f.Entity, f.Field, f.Value.Surface)
	}
	for i, row := range m.Records {
		if row == "" {
			m.Records[i] = fmt.Sprintf("A milestone review for %s is planned for %s; this planning note changes none of the recorded roles or decisions.", w.Entity, milestone)
		}
	}
	m.Question, err = renderV13FactQuery(w, seed)
	if err != nil {
		return m, err
	}
	var expected []string
	for _, v := range values {
		expected = append(expected, v.Canonical)
		m.Claims = append(m.Claims, v13Claim(v.Kind, v.Canonical, v.Accept, 1/float64(len(values))))
		m.Claims[len(m.Claims)-1].Unit = v.Unit
		if len(values) > 1 {
			m.Items = append(m.Items, v.Canonical)
			m.ItemKinds = append(m.ItemKinds, "")
			m.ItemAccept = append(m.ItemAccept, append([]string(nil), v.Accept...))
		}
	}
	m.Expected = strings.Join(expected, "; ")
	if len(values) > 1 {
		m.Kind = protocol.AnswerList
	} else {
		m.AcceptAny = append([]string(nil), values[0].Accept...)
	}
	resolve := V10QueryNode{Op: "resolve_entity", Field: s.Alias}
	for _, q := range w.Query {
		op := q.Op
		if op == "latest_date" {
			op = "latest"
		}
		node := V10QueryNode{Op: op, Field: q.Field, Children: []V10QueryNode{resolve}}
		if q.Op == "latest_date" {
			node.Children = append(node.Children, V10QueryNode{Op: "order_by_date", Field: q.Field})
		}
		if len(w.Query) == 1 {
			m.Program = node
		} else {
			m.Program.Op = "tuple"
			m.Program.Children = append(m.Program.Children, node)
		}
		m.Operations = append(m.Operations, q.Op)
	}
	m.Facts = []string{"per-run schema glossary", "entity remit", "typed assertions with explicit temporal or source relations"}
	m.Constraints = []string{w.Entity, w.Purpose}
	m.Protected = append(m.Protected, s.Entity, s.Alias, s.Correction)
	return m, nil
}

// Distractors are separate-entity assertions, never alternative answers to
// silently insert into the queried world's history.
func v13FactDistractor(d *v13Draws, s v13Schema, g v13Group, counter bool) ([]v13Fact, []string) {
	makeFact := func(field string, value v13FactValue) v13Fact {
		return v13Fact{Entity: g.DecoyAlias, Field: field, Value: value, Mode: "static", Record: 0}
	}
	person := factValue(g.People[3], protocol.ClaimKindPerson)
	switch g.Family {
	case V13FamilyOwnerAfterCorrection:
		return []v13Fact{makeFact(s.Owner, person)}, []string{person.Canonical}
	case V13FamilyStandingStatus:
		c := V13StatusClasses[g.Classes[3]]
		v := factValue(c.Canonical, protocol.ClaimKindStatus, c.Accept...)
		v.Surface = d.statusAlias(g.Classes[3])
		v.Accept = append(v.Accept, v.Surface)
		return []v13Fact{makeFact(s.Status, v)}, []string{v.Canonical, v.Surface}
	case V13FamilyCurrentChannel:
		c := V13ChannelClasses[g.Classes[3]]
		v := factValue(c.Canonical, protocol.ClaimKindChannel, c.Accept...)
		return []v13Fact{makeFact(s.Channel, v)}, []string{v.Canonical}
	case V13FamilyClientVsVendor:
		v := factValue(g.Orgs[3], protocol.ClaimKindOrganisation)
		return []v13Fact{makeFact(s.Client, v)}, []string{g.Orgs[1], v.Canonical}
	case V13FamilyNextActionResponsible:
		c := V13ActionClasses[g.Classes[1]]
		v := factValue(c.Canonical, protocol.ClaimKindAction, c.Accept...)
		return []v13Fact{makeFact(s.Action, v), makeFact(s.Responsible, person)}, []string{person.Canonical, v.Canonical}
	case V13FamilyRecordsDisagree:
		return []v13Fact{makeFact("launch approver", person)}, nil
	case V13FamilyLatestEvent:
		c := V13EventClasses[g.DecoyEvent]
		v := factValue(c.Canonical, protocol.ClaimKindEvent, c.Accept...)
		events := g.Events
		if counter {
			events[g.CounterEvent].Date = g.CounterDate
		}
		latest := v13LatestEventIndex(events)
		var distractors []string
		for i, e := range events {
			if i != latest {
				distractors = append(distractors, V13EventClasses[e.Class].Canonical)
			}
		}
		return []v13Fact{makeFact(s.Event, v)}, append(distractors, v.Canonical)
	}
	return nil, nil
}

// GenerateV13FactPrograms replaces string-rewriting with jointly rendered
// evidence/questions over typed worlds for all seven business families. It is
// opt-in until private-artifact production/validation and qualification are
// migrated together. It must not be presented as a full private dataset.
func GenerateV13FactPrograms(worldSeed, presentationSeed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("fact program count must be a positive multiple of four")
	}
	d := newV13Draws(worldSeed, "business")
	s := generateV13Schema(worldSeed)
	digest, err := digestV13Schema(s)
	if err != nil {
		return nil, err
	}
	var out []V10GeneratedCase
	for group := 0; group < count/4; group++ {
		g := d.drawGroup(group)
		var baseAnswer string
		for variant, relation := range []string{protocol.RelationBase, protocol.RelationRendererInvariant, protocol.RelationDistractorInvariant, protocol.RelationCausalCounterfactual} {
			w, err := buildV13FactWorld(d, s, group, g, variant == 3)
			if err != nil {
				return nil, err
			}
			renderSeed := v10Seed(presentationSeed, fmt.Sprintf("fact-presentation-%d", group))
			if variant == 1 {
				renderSeed = v10Seed(renderSeed, "equivalent-presentation")
			}
			milestoneDate := v13CalendarDate(worldSeed, "business", group, g.Milestone, false)
			milestone := milestoneDate.Format("January 2, 2006")
			m, err := renderV13FactMember(w, s, renderSeed, milestone)
			if err != nil {
				return nil, err
			}
			decoys, distractors := v13FactDistractor(d, s, g, variant == 3)
			m.Distractors = distractors
			for i, f := range decoys {
				row, err := renderV13Fact(f, s.Correction, renderSeed, fmt.Sprintf("decoy-%d", i))
				if err != nil {
					return nil, err
				}
				m.DecoyClause += " " + row
				m.Protected = append(m.Protected, f.Entity, f.Field, f.Value.Surface)
			}
			if variant == 0 {
				baseAnswer = m.Expected
			}
			if (variant == 3 && m.Expected == baseAnswer) || (variant > 0 && variant < 3 && m.Expected != baseAnswer) {
				return nil, fmt.Errorf("fact-world metamorphic violation")
			}
			answerRelation := "same"
			if variant == 0 {
				answerRelation = "base"
			}
			if variant == 3 {
				answerRelation = "changed"
			}
			generated := materializeV13Case(d, s, digest, v13Ontology(s), group, variant, protocol.OpaqueCaseID(worldSeed, "v13-metamorphic-group", group), v10Renderers[(group+variant)%len(v10Renderers)], g, m, relation, answerRelation, variant == 2)
			generated.Provenance.Revision = "dittobench-v13-fact-world-v1"
			out = append(out, generated)
		}
	}
	return out, nil
}
