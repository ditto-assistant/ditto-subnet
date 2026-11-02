package parserprobe

import (
	"encoding/csv"
	"encoding/json"
	"html"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Public-grammar diagnostic inverse. Reads served documents and question only;
// never imports the world evaluator, seed, grading oracle or provenance.
type enterpriseEvent struct {
	Entity    string `json:"entity"`
	Field     string `json:"field"`
	Value     string `json:"value"`
	At        int    `json:"effective_step"`
	Operation string `json:"operation"`
}

const enterpriseQuoted = `("(?:\\.|[^"\\])*")`

var enterpriseTarget = regexp.MustCompile(`entity-[a-f0-9]+`)
var enterpriseSentences = []struct {
	re    *regexp.Regexp
	roles [4]int // entity, field, value, step capture indexes
	op    string
}{
	{regexp.MustCompile(`Effective at step ([0-9]+), ` + enterpriseQuoted + ` has ` + enterpriseQuoted + ` set to ` + enterpriseQuoted), [4]int{2, 3, 4, 1}, "assign"},
	{regexp.MustCompile(`For ` + enterpriseQuoted + `, the value of ` + enterpriseQuoted + ` becomes ` + enterpriseQuoted + ` at effective step ([0-9]+)`), [4]int{1, 2, 3, 4}, "assign"},
	{regexp.MustCompile(`The assignment of ` + enterpriseQuoted + ` to field ` + enterpriseQuoted + ` of ` + enterpriseQuoted + ` takes effect at step ([0-9]+)`), [4]int{3, 2, 1, 4}, "assign"},
	{regexp.MustCompile(`At effective step ([0-9]+), add ` + enterpriseQuoted + ` to the ` + enterpriseQuoted + ` set of ` + enterpriseQuoted), [4]int{4, 3, 2, 1}, "add"},
	{regexp.MustCompile(`At effective step ([0-9]+), remove ` + enterpriseQuoted + ` from the ` + enterpriseQuoted + ` set of ` + enterpriseQuoted), [4]int{4, 3, 2, 1}, "remove"},
}

func parseEnterpriseDocument(body string) ([]enterpriseEvent, bool) {
	var out []enterpriseEvent
	if strings.HasPrefix(body, "[") && json.Unmarshal([]byte(body), &out) == nil {
		return out, len(out) > 0
	}
	rows, err := csv.NewReader(strings.NewReader(body)).ReadAll()
	if err == nil && len(rows) > 1 && len(rows[0]) == 5 {
		for _, row := range rows[1:] {
			m := map[string]string{}
			for i, k := range rows[0] {
				m[k] = row[i]
			}
			at, err := strconv.Atoi(m["effective_step"])
			if err != nil {
				return nil, false
			}
			out = append(out, enterpriseEvent{m["entity"], m["field"], m["value"], at, m["operation"]})
		}
		return out, true
	}
	if strings.HasPrefix(body, "# Operations log") {
		for _, line := range strings.Split(body, "\n") {
			if !strings.HasPrefix(line, "| <code>") {
				continue
			}
			cells := strings.Split(line, "|")
			if len(cells) != 7 {
				return nil, false
			}
			for i := 1; i <= 5; i++ {
				cells[i] = html.UnescapeString(strings.TrimSuffix(strings.TrimPrefix(strings.TrimSpace(cells[i]), "<code>"), "</code>"))
			}
			at, err := strconv.Atoi(cells[4])
			if err != nil {
				return nil, false
			}
			out = append(out, enterpriseEvent{cells[1], cells[2], cells[3], at, cells[5]})
		}
		return out, len(out) > 0
	}
	for _, pattern := range enterpriseSentences {
		for _, m := range pattern.re.FindAllStringSubmatch(body, -1) {
			var values [3]string
			for i := 0; i < 3; i++ {
				v, err := strconv.Unquote(m[pattern.roles[i]])
				if err != nil {
					return nil, false
				}
				values[i] = v
			}
			at, err := strconv.Atoi(m[pattern.roles[3]])
			if err != nil {
				return nil, false
			}
			out = append(out, enterpriseEvent{values[0], values[1], values[2], at, pattern.op})
		}
	}
	return out, len(out) > 0
}

func answerEnterpriseV13(st *store, question string) derived {
	scope, target := caseFileRef.FindString(question), enterpriseTarget.FindString(question)
	if scope == "" || target == "" || !strings.Contains(question, "At effective step 12,") {
		return derived{}
	}
	var events []enterpriseEvent
	for _, p := range st.pairs {
		if !strings.HasPrefix(p.Prompt, "Case file "+scope+".") {
			continue
		}
		_, body, ok := strings.Cut(p.Prompt, "\n")
		if !ok {
			return derived{}
		}
		batch, ok := parseEnterpriseDocument(body)
		if !ok {
			return derived{}
		}
		events = append(events, batch...)
	}
	if len(events) == 0 {
		return derived{}
	}
	sort.Slice(events, func(i, j int) bool { return events[i].At < events[j].At })
	scalars := map[string]map[string]string{}
	members := map[string]map[string]bool{}
	for _, e := range events {
		if e.At > 12 {
			continue
		}
		if scalars[e.Entity] == nil {
			scalars[e.Entity] = map[string]string{}
		}
		if e.Operation == "assign" {
			scalars[e.Entity][e.Field] = e.Value
			continue
		}
		if e.Field != "members" {
			return derived{}
		}
		if members[e.Entity] == nil {
			members[e.Entity] = map[string]bool{}
		}
		switch e.Operation {
		case "add":
			members[e.Entity][e.Value] = true
		case "remove":
			delete(members[e.Entity], e.Value)
		default:
			return derived{}
		}
	}
	team := scalars[target]["team"]
	if team == "" {
		return derived{}
	}
	d := derived{family: "enterprise-composed-program", kind: protocol.AnswerNumber}
	if strings.Contains(question, "which contact channel") {
		d.value = scalars[scalars[scalars[team]["vendor"]]["owner"]]["channel"]
		d.kind = protocol.AnswerValue
		d.ok = d.value != ""
		return d
	}
	if members[team] == nil || !strings.Contains(question, "availability is available") {
		return derived{}
	}
	count := strings.Contains(question, "How many distinct members")
	if !count && !strings.Contains(question, "total allocation_hours") {
		return derived{}
	}
	total := 0
	for id := range members[team] {
		available := scalars[id]["availability"]
		if available == "" {
			return derived{}
		}
		if available != "available" {
			continue
		}
		if count {
			total++
		} else {
			hours, err := strconv.Atoi(scalars[id]["allocation_hours"])
			if err != nil {
				return derived{}
			}
			total += hours
		}
	}
	d.value = strconv.Itoa(total)
	d.ok = true
	return d
}
