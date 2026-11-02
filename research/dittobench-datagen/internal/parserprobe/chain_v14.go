package parserprobe

import (
	"regexp"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v14 supersession chains (SN-353 / SN-354). The chain frames are
// public (universe.V14ChainFrames), so the generator-inverse harness folds a
// chain exactly as an honest reader does: take the initial record, then apply
// each later record in filing order. Adopted changes push a value, proposals
// wait for a decision, an entry logged in error pops the last change, and an
// unnamed revert returns to the original or previous value. This measures
// what a model-free adversary that knows every public frame can do. The
// surface-cue shortcuts SN-354 removes are measured separately in
// universe/v14_supersession_test.go.

// chainInitialFrames mirror the first record of each chained family. They are
// the v13 first-record grammar, which v14 keeps.
var chainInitialFrames = []struct {
	family string
	frame  businessFrame
}{
	{"v13-open-program", businessPattern("%s was first assigned to %s as its %s.", "subject", "value", "label")},
	{"v13-open-program", businessPattern("At the outset the %s on %s was %s.", "label", "subject", "value")},
	{"v13-open-program", businessPattern("%s took %s as %s when it opened.", "value", "subject", "label")},
	{"v13-open-program", businessPattern("%s's %s opened as %s.", "subject", "label", "value")},
	{"v13-open-program", businessPattern("When %s was logged its %s read %s.", "subject", "label", "value")},
	{"v13-open-program", businessPattern("The initial %s on %s was %s.", "label", "subject", "value")},
	{"v13-open-program", businessPattern("%s's %s started out as %s.", "subject", "label", "value")},
	{"v13-open-program", businessPattern("The first %s agreed for %s was %s.", "label", "subject", "value")},
	{"v13-open-program", businessPattern("Early on, %s ran its %s over %s.", "subject", "label", "value")},
	{"v13-personal-program", businessPattern("On the rota, %s is %s's job.", "subject", "value")},
	{"v13-personal-program", businessPattern("%s has %s on the chores rota.", "value", "subject")},
	{"v13-personal-program", businessPattern("We put %s down for %s this term.", "value", "subject")},
	{"v13-personal-program", businessPattern("%s's %s was booked for %s.", "person", "subject", "value")},
	{"v13-personal-program", businessPattern("Booked %s in for the %s on %s.", "person", "subject", "value")},
	{"v13-personal-program", businessPattern("%s's %s is %s for %s.", "person", "subject", "value", "date")},
	{"v13-personal-program", businessPattern("%s has the %s down as %s on %s.", "person", "subject", "value", "date")},
	{"v13-personal-program", businessPattern("The %s for %s: %s, %s.", "subject", "person", "value", "date")},
	{"v13-personal-program", businessPattern("The %s for %s: %s.", "subject", "person", "value")},
	{"v13-personal-program", businessPattern("%s renews on %s; the plan was to %s before then.", "subject", "date", "value")},
	{"v13-personal-program", businessPattern("Renewal for %s lands %s — I'd decided to %s.", "subject", "date", "value")},
	{"v13-personal-program", businessPattern("%s: renewal %s, decision so far is to %s.", "subject", "date", "value")},
	{"v13-personal-program", businessPattern("For %s the %s leaves at %s.", "subject", "leg", "value")},
	{"v13-personal-program", businessPattern("%s: %s departure %s.", "subject", "leg", "value")},
	{"v13-personal-program", businessPattern("Our %s for %s is the %s.", "leg", "subject", "value")},
	{"v13-personal-program", businessPattern("%s is run by %s.", "subject", "value")},
	{"v13-personal-program", businessPattern("%s organises %s.", "value", "subject")},
	{"v13-personal-program", businessPattern("The person behind %s is %s.", "subject", "value")},
}

// chainSubjectHeads are the personal subject heads, after "the".
var chainSubjectHeads = [][]string{
	{"person", "doing"}, {"organiser", "of"}, {"date", "of"}, {"status", "of"}, {"plan", "for"}, {"departure", "time", "of"},
}

// chainSubjectOK reports whether a captured subject has the shape of a chain
// subject phrase: "the <label> on|of|for <alias>" for a business family, or
// one of the personal heads. The bare plain frame ("<subject> is <value>.")
// only counts when its subject has this shape, so a neutral note such as "The
// first milestone review for X is ..." is not a step. Words are compared with
// the same typo budget as the frames, since records carry writing noise.
func chainSubjectOK(subject string) bool {
	words := strings.Fields(strings.ToLower(subject))
	like := func(lit, word string) bool { return fuzzyWordBudget(lit, word, 2) }
	if len(words) < 3 || !like("the", words[0]) {
		return false
	}
	if len(words) == 4 && (like("on", words[2]) || like("of", words[2]) || like("for", words[2])) {
		return true
	}
	for _, head := range chainSubjectHeads {
		if len(words) <= len(head)+1 {
			continue
		}
		ok := true
		for i, lit := range head {
			ok = ok && like(lit, words[i+1])
		}
		if ok {
			return true
		}
	}
	return false
}

type chainStepFrame struct {
	kind  string
	frame frame
	roles []string
}

var chainStepFrames = compileChainStepFrames()

// compileChainStepFrames turns the indexed public frames into inverse frames,
// once per copula. Specific frames come first; the bare plain frames last.
func compileChainStepFrames() []chainStepFrame {
	banks := universe.V14ChainFrames()
	order := []string{
		"announced", "propose-cued", "propose", "withdraw", "adopt", "error", "confirm",
		"revert-original-cued", "revert-previous-cued", "revert-original", "revert-previous", "plain",
	}
	index := regexp.MustCompile(`%\[(\d)\]s`)
	var out []chainStepFrame
	for _, kind := range order {
		for _, form := range banks[kind] {
			for _, copula := range []string{"is to", "is"} {
				var roles []string
				pattern := index.ReplaceAllStringFunc(form, func(m string) string {
					switch m {
					case "%[1]s":
						roles = append(roles, "subject")
						return "%s"
					case "%[2]s":
						roles = append(roles, "value")
						return "%s"
					}
					return copula
				})
				bf := businessPattern(pattern, roles...)
				out = append(out, chainStepFrame{kind: kind, frame: bf.frame, roles: bf.roles})
				if !strings.Contains(form, "%[3]s") {
					break
				}
			}
		}
	}
	return out
}

// scopedV14Records returns the case file's records in filing (timestamp)
// order. Every member of a metamorphic group seeds its own copy of each
// record, with independent writing noise, under the same filing timestamp, so
// one record is the group of rows that share a timestamp.
func scopedV14Records(st *store, question string) [][]string {
	scope := caseFileRef.FindString(question)
	if scope == "" {
		return nil
	}
	marker := scope + ": "
	byStamp := map[string][]string{}
	var stamps []string
	for _, p := range st.pairs {
		i := strings.Index(p.Prompt, marker)
		if i < 0 {
			continue
		}
		row := strings.TrimSpace(p.Prompt[i+len(marker):])
		row = strings.TrimSpace(strings.TrimRight(row, "|}\""))
		row = strings.ReplaceAll(row, ";", ",")
		if _, ok := byStamp[p.Timestamp]; !ok {
			stamps = append(stamps, p.Timestamp)
		}
		byStamp[p.Timestamp] = append(byStamp[p.Timestamp], row)
	}
	sort.Strings(stamps)
	out := make([][]string, len(stamps))
	for i, at := range stamps {
		out[i] = byStamp[at]
	}
	return out
}

// scopedV14Rows returns the case-file rows in filing (timestamp) order.
func scopedV14Rows(st *store, question string) []string {
	scope := caseFileRef.FindString(question)
	if scope == "" {
		return nil
	}
	type stamped struct{ at, row string }
	seen := map[string]bool{}
	var rows []stamped
	marker := scope + ": "
	for _, p := range st.pairs {
		i := strings.Index(p.Prompt, marker)
		if i < 0 {
			continue
		}
		row := strings.TrimSpace(p.Prompt[i+len(marker):])
		row = strings.TrimSpace(strings.TrimRight(row, "|}\""))
		row = strings.ReplaceAll(row, ";", ",")
		if !seen[row] {
			seen[row] = true
			rows = append(rows, stamped{p.Timestamp, row})
		}
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].at < rows[j].at })
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.row
	}
	return out
}

func parseChainStep(row string) (string, string, bool) {
	for _, f := range chainStepFrames {
		slots, ok := f.frame.match(row)
		if !ok {
			continue
		}
		parsed := map[string]string{}
		for i, role := range f.roles {
			parsed[role] = strings.TrimSpace(slots[i])
		}
		if !chainSubjectOK(parsed["subject"]) {
			continue
		}
		return f.kind, parsed["value"], true
	}
	return "", "", false
}

// answerChainV14 folds a v14 supersession chain. It answers only when it has
// parsed an initial record and at least one later step.
func answerChainV14(st *store, question string) derived {
	if st.version < protocol.BenchVersionV14 {
		return derived{}
	}
	var adopted []string
	family := ""
	pending := ""
	steps := 0
	for _, copies := range scopedV14Records(st, question) {
		if family == "" {
			matched := false
			for _, row := range copies {
				for _, ini := range chainInitialFrames {
					slots, ok := ini.frame.frame.match(row)
					if !ok {
						continue
					}
					for i, role := range ini.frame.roles {
						if role == "value" {
							adopted = []string{strings.TrimSpace(slots[i])}
						}
					}
					family, matched = ini.family, true
					break
				}
				if matched {
					break
				}
			}
			if matched {
				continue
			}
		}
		if family == "" {
			continue
		}
		var kind, value string
		ok := false
		for _, row := range copies {
			if kind, value, ok = parseChainStep(row); ok {
				break
			}
		}
		if !ok {
			continue
		}
		steps++
		switch kind {
		case "plain", "announced":
			adopted = append(adopted, value)
		case "propose", "propose-cued":
			pending = value
		case "adopt":
			if pending == "" {
				pending = value
			}
			adopted = append(adopted, pending)
			pending = ""
		case "withdraw":
			pending = ""
		case "error":
			if len(adopted) > 1 {
				adopted = adopted[:len(adopted)-1]
			}
		case "revert-original", "revert-original-cued":
			adopted = append(adopted, adopted[0])
		case "revert-previous", "revert-previous-cued":
			if len(adopted) > 1 {
				adopted = append(adopted, adopted[len(adopted)-2])
			}
		}
	}
	if family == "" || steps == 0 || len(adopted) == 0 {
		return derived{}
	}
	return derived{family: family, kind: protocol.AnswerValue, value: adopted[len(adopted)-1], ok: true}
}
