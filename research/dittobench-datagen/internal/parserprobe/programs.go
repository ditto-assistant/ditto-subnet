package parserprobe

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Open-program questions are the one memory family whose name carries the
// contract version ("v10-open-program", "v11-open-program", ...): every bench
// version re-renders the program surface, so each contract registers its own
// frame grammar here. The family NAME is derived from the artifact's
// bench_version, never pinned; whether the parser KNOWS the family is decided
// by this registry, so a new contract whose grammar is not registered is
// reported in Report.Unclassified instead of silently scoring zero (which
// would read as surface hardening rather than a parser hole).

// programRecordStyle names how a contract renders its program records.
type programRecordStyle int

const (
	// programRecordsKeyValue is the v10/v11 "label=value; label=value" row.
	programRecordsKeyValue programRecordStyle = iota + 1
	// programRecordsProse is the v12 per-fact prose sentence.
	programRecordsProse
)

// programGrammar is one contract's program question and record surface.
type programGrammar struct {
	records programRecordStyle
	// whole holds complete one-sentence-or-more question templates (v10). When
	// set, the opener / operation clause / unit decomposition below is unused.
	whole      []frame
	wholeRoles [][]string
	// openers, unit, and ops decompose the v11+ question: opener sentence,
	// capitalised operation clause, unit frame. ops and roles are keyed by
	// shape; roles gives the role of each captured slot per form.
	openers []frame
	unit    []frame
	ops     map[string][]frame
	roles   map[string][][]string
}

// programFamily is the artifact QuestionType of benchVersion's program family.
func programFamily(benchVersion int) string {
	return fmt.Sprintf("v%d-open-program", benchVersion)
}

// isProgramFamily reports whether family is any contract's program family.
func isProgramFamily(family string) bool {
	return strings.HasSuffix(family, "-open-program")
}

// programGrammarFor returns the registered grammar for benchVersion.
func programGrammarFor(benchVersion int) (programGrammar, bool) {
	g, ok := programGrammars[benchVersion]
	return g, ok
}

// programShapes is the fixed try order for the operation clause.
var programShapes = []string{"adjust", "latest", "larger", "subtract"}

// programUnitFramesBank is shared by v11 and v12: the unit slot is the
// currency code and the frame carries "cents".
var programUnitFramesBank = []string{
	"Give the result in %s cents as minor units.",
	"Answer as a minor-unit figure under %s cents.",
	"Report minor units, per the %s cents convention.",
	"State the balance in minor units (%s cents).",
}

// programSubjectFrames are the descriptive subject bindings a v11 question
// uses on even groups instead of the alias; slots: draft label, draft value.
var programSubjectFrames = compileFrames("the workstream whose %s was recorded as %s")

// compileOpFrames compiles operation-clause forms as they appear in the
// question: capitalised and terminated with a period.
func compileOpFrames(forms map[string][]string) map[string][]frame {
	out := map[string][]frame{}
	for shape, fs := range forms {
		for _, f := range fs {
			out[shape] = append(out[shape], compileFrame(strings.ToUpper(f[:1])+f[1:]+"."))
		}
	}
	return out
}

// v10 program questions (universe.renderV10Question): four whole templates,
// slots alias, approved label, paid label, unit label; always approved - paid.
var v10ProgramTemplates = []string{
	"Using this run's glossary, reconcile %s: take the latest %s value, subtract %s, and return the result under %s as minor units.",
	"For %s, interpret the local schema rather than guessing field names. After %s replaces the draft, what remains once %s is removed? Answer in %s minor units.",
	"Resolve the %s record from the custom labels, apply its %s correction, then deduct %s. What balance follows using %s?",
	"Read the per-run field meanings for %s. Which minor-unit balance remains after the current %s amount and the settled %s amount are reconciled under %s?",
}

// v11 program question banks (universe.v11QuestionOpeners / v11OperationClause).
var (
	v11Openers = []string{
		"Work from this batch's own field meanings.",
		"Interpret the local labels; do not guess standard names.",
		"Use the conventions established for these records.",
		"Ground every field in this workspace's glossary.",
		"Apply the per-run schema before any arithmetic.",
	}
	v11OpForms = map[string][]string{
		"subtract": {
			"take the governing %s value for %s and remove the %s amount",
			"start from the current %s figure on %s, then deduct %s",
			"reconcile %s: the standing %s value less the %s amount",
		},
		"adjust": {
			"combine the standing %s value for %s with its %s, then deduct %s",
			"apply the recorded %s to the %s figure on %s before removing %s",
			"for %s, fold the %s into the %s amount and settle %s against it",
		},
		"latest": {
			"more than one %s entry touches %s's %s; only the most recent governs — deduct %s from it",
			"%s carries a revised %s after a second %s; use the latest and remove %s",
			"resolve which %s value currently stands for %s (its %s history has two entries), then take away %s",
		},
		"larger": {
			"for %s, keep whichever of %s and %s is larger, then deduct %s",
			"compare %s's %s against its %s, retain the greater, and settle %s",
			"the governing figure for %s is the larger of %s and %s; remove %s from it",
		},
	}
	v11OpRoles = map[string][][]string{
		"subtract": {{"approved", "subject", "paid"}, {"approved", "subject", "paid"}, {"subject", "approved", "paid"}},
		"adjust":   {{"approved", "subject", "adjustment", "paid"}, {"adjustment", "approved", "subject", "paid"}, {"subject", "adjustment", "approved", "paid"}},
		"latest":   {{"correction", "subject", "approved", "paid"}, {"subject", "approved", "correction", "paid"}, {"approved", "subject", "correction", "paid"}},
		"larger":   {{"subject", "draft", "approved", "paid"}, {"subject", "draft", "approved", "paid"}, {"subject", "draft", "approved", "paid"}},
	}
)

// v12 program question banks (universe.v12QuestionOpeners / v12OperationClause):
// opener . operation clause . unit frame. The operation clause names the
// schema labels of the operands, which is the direct role binding a reader
// needs; the glossary parse is the fallback.
var (
	v12Openers = []string{
		"Work only from this batch's own field meanings.",
		"Read the local glossary before naming any field.",
		"Ground every label in this workspace's conventions.",
		"Induce the per-run schema, then compute.",
		"Do not assume standard field names; use the ones defined here.",
	}
	// shape -> forms; slots in generator order: subject, approved, paid[, extra]
	v12OpForms = map[string][]string{
		"subtract": {
			"take the governing %s value for %s and remove the %s amount",
			"start from the standing %s figure on %s, then deduct %s",
			"for %s, reconcile the current %s value against the %s amount",
		},
		"adjust": {
			"apply the recorded %s to the %s figure on %s, then remove %s",
			"for %s, fold the %s into the %s amount before settling %s against it",
			"adjust %s's %s by its %s, then deduct %s",
		},
		"latest": {
			"more than one %s touches %s's %s; only the most recent governs — deduct %s from it",
			"%s carries a revised %s after a later %s; use the standing value and remove %s",
			"resolve which %s value currently stands for %s, then take away %s",
		},
		"larger": {
			"for %s, keep whichever of %s and %s is larger, then deduct %s",
			"compare %s's %s against its %s, retain the greater, and settle %s",
			"the governing figure for %s is the larger of %s and %s; remove %s from it",
		},
	}
	// v12OpRoles gives, per form, the role of each captured slot.
	v12OpRoles = map[string][][]string{
		"subtract": {{"subject", "approved", "paid"}, {"approved", "subject", "paid"}, {"subject", "approved", "paid"}},
		"adjust":   {{"adjustment", "approved", "subject", "paid"}, {"subject", "adjustment", "approved", "paid"}, {"subject", "approved", "adjustment", "paid"}},
		"latest":   {{"correction", "subject", "approved", "paid"}, {"subject", "approved", "correction", "paid"}, {"approved", "subject", "paid"}},
		"larger":   {{"subject", "draft", "approved", "paid"}, {"subject", "draft", "approved", "paid"}, {"subject", "draft", "approved", "paid"}},
	}
)

// programGrammars is the registry of program surfaces the parser can invert.
// A new contract that re-renders the program family must register here (its
// coverage test in gen/parserprobe_test.go fails closed until it does).
var programGrammars = map[int]programGrammar{
	protocol.BenchVersionV10: {
		records:    programRecordsKeyValue,
		whole:      compileFrames(v10ProgramTemplates...),
		wholeRoles: [][]string{{"subject", "approved", "paid", "unit"}, {"subject", "approved", "paid", "unit"}, {"subject", "approved", "paid", "unit"}, {"subject", "approved", "paid", "unit"}},
	},
	protocol.BenchVersionV11: {
		records: programRecordsKeyValue,
		openers: compileFrames(v11Openers...),
		unit:    compileFrames(programUnitFramesBank...),
		ops:     compileOpFrames(v11OpForms),
		roles:   v11OpRoles,
	},
	protocol.BenchVersionV12: {
		records: programRecordsProse,
		openers: compileFrames(v12Openers...),
		unit:    compileFrames(programUnitFramesBank...),
		ops:     compileOpFrames(v12OpForms),
		roles:   v12OpRoles,
	},
}

// classifyProgramQuestion recognises benchVersion's open-program question.
// The returned family is derived from benchVersion; the caller has already
// established that the frame banks know it (knownFamilies).
func classifyProgramQuestion(benchVersion int, q string) (parsedQuestion, bool) {
	g, ok := programGrammarFor(benchVersion)
	if !ok {
		return parsedQuestion{}, false
	}
	family := programFamily(benchVersion)
	if len(g.whole) > 0 {
		i, slots, ok := matchAny(g.whole, q)
		if !ok {
			return parsedQuestion{}, false
		}
		pq := parsedQuestion{family: family, slots: slots, frame: i, shape: "subtract", roles: map[string]string{}}
		for k, role := range g.wholeRoles[i] {
			if k < len(slots) {
				pq.roles[role] = slots[k]
			}
		}
		pq.subject, pq.unit = pq.roles["subject"], pq.roles["unit"]
		return pq, true
	}
	sents := sentences(q)
	if len(sents) < 3 {
		return parsedQuestion{}, false
	}
	// Openers end in '.', so the first sentence is the opener, the last the
	// unit frame, and the middle (possibly split by "; ") is the clause.
	if _, _, ok := matchAny(g.openers, sents[0]); !ok {
		return parsedQuestion{}, false
	}
	_, unitSlots, ok := matchAny(g.unit, sents[len(sents)-1])
	if !ok {
		return parsedQuestion{}, false
	}
	clause := strings.Join(sents[1:len(sents)-1], " ")
	for _, shape := range programShapes {
		i, slots, ok := matchAny(g.ops[shape], clause)
		if !ok {
			continue
		}
		roles := map[string]string{}
		for k, role := range g.roles[shape][i] {
			if k < len(slots) {
				roles[role] = strings.TrimSuffix(slots[k], "'s")
			}
		}
		return parsedQuestion{family: family, slots: slots, frame: i, shape: shape, roles: roles, unit: unitSlots[0], subject: roles["subject"]}, true
	}
	return parsedQuestion{}, false
}

// answerProgram evaluates the program question over the store. v12 binds its
// subject relationally, so the group is selected by question order (see
// answerV12Program); v10/v11 name the subject (alias or descriptive draft
// binding) and carry the operation shape in the question.
func answerProgram(st *store, pq parsedQuestion, ordinal int) derived {
	g, ok := programGrammarFor(st.version)
	if !ok {
		return derived{family: pq.family}
	}
	if g.records == programRecordsProse {
		return answerV12Program(st, pq, ordinal)
	}
	return answerKeyValueProgram(st, pq, ordinal)
}

// answerKeyValueProgram evaluates a v10/v11 program: the subject names the
// thread (alias, or the v11 "workstream whose <draft> was recorded as <n>"
// binding), and the question's operation clause names the shape.
func answerKeyValueProgram(st *store, pq parsedQuestion, ordinal int) derived {
	d := derived{family: pq.family}
	pick := st.programBySubject(pq.subject, ordinal)
	if pick == nil || !pick.hasPaid || !pick.hasApproved {
		return d
	}
	var answer int
	switch pq.shape {
	case "adjust":
		if !pick.hasAdjust {
			return d
		}
		answer = pick.approved + pick.adjustment - pick.paid
	case "latest":
		if !pick.hasLatest {
			return d
		}
		answer = pick.approved2 - pick.paid
	case "larger":
		if !pick.hasDraft {
			return d
		}
		answer = maxInt(pick.draft, pick.approved) - pick.paid
	default:
		answer = pick.approved - pick.paid
	}
	if answer <= 0 {
		return d
	}
	return d.val(protocol.AnswerMoney, fmt.Sprint(answer))
}

// programBySubject resolves the thread a v10/v11 question is about. A
// descriptive draft binding is shared by a group's base thread and its
// counterfactual "-revision" thread (same draft, different approval), so when
// both are present the fourth question of the group — the counterfactual — is
// the one asked about the later thread, exactly as in answerV12Program.
func (s *store) programBySubject(subject string, ordinal int) *program {
	subject = strings.TrimSpace(subject)
	if _, slots, ok := matchAny(programSubjectFrames, subject); ok {
		draft := atoi(slots[1])
		var candidates []*program
		for _, alias := range s.programOrder {
			if pg := s.programs[alias]; pg != nil && pg.hasDraft && pg.draft == draft {
				candidates = append(candidates, pg)
			}
		}
		switch {
		case len(candidates) == 0:
			return nil
		case len(candidates) == 1 || ordinal%4 != 3:
			return candidates[0]
		default:
			return candidates[len(candidates)-1]
		}
	}
	for alias, pg := range s.programs {
		if strings.EqualFold(alias, subject) {
			return pg
		}
	}
	return nil
}

var (
	reRelation   = regexp.MustCompile(`^([A-Za-z]+)->([A-Za-z]+)$`)
	reUnitValue  = regexp.MustCompile(`^(USD|CAD|EUR) cents$`)
	reSignedInt  = regexp.MustCompile(`^[+-][0-9]+$`)
	reUnsigned   = regexp.MustCompile(`^[0-9]+$`)
	reThreadTied = regexp.MustCompile(`\b([A-Za-z][A-Za-z0-9-]*) reconciliation set\b`)
)

// threadAliasFromResponse reads the thread alias a v10/v11 acknowledgement
// carries: "(thread: <alias>)" (v11) or "tied to the <alias> reconciliation
// set" (v10). The alias itself is a protected label and is never edited.
func threadAliasFromResponse(resp string) string {
	if i := strings.LastIndex(resp, "(thread: "); i >= 0 {
		rest := resp[i+len("(thread: "):]
		if j := strings.Index(rest, ")"); j > 0 {
			return strings.TrimSpace(rest[:j])
		}
	}
	if m := reThreadTied.FindStringSubmatch(resp); m != nil {
		return m[1]
	}
	return ""
}

// stripKeyValueRenderer removes the v10/v11 renderer wrapper around one
// key=value record row. The v11 wrappers are the v12 ones; v10 adds its own
// conversation lead and email subject.
func stripKeyValueRenderer(prompt string) string {
	row := strings.TrimSpace(prompt)
	const v10Lead = "I am going to describe one line from a custom workspace schema: "
	if len(row) > len(v10Lead) && fuzzyPrefix(row, v10Lead) {
		return strings.TrimSpace(row[len(v10Lead):])
	}
	if fuzzyPrefix(row, "Subject: workspace record") {
		if i := strings.Index(row, "\n\n"); i >= 0 {
			return strings.TrimSpace(row[i+2:])
		}
	}
	return stripV12Renderer(row)
}

// ingestKeyValueProgram reads one v10/v11 program record. Roles are inferred
// from the row's structure alone — the draft row carries the unit, the
// correction row carries a "draft->approved" relation (or "approved->approved"
// for a later revision), a signed value is the adjustment, and a lone figure
// is the settled payment — so the glossary is only consulted for labels.
func (s *store) ingestKeyValueProgram(p protocol.MemoryPair) bool {
	alias := threadAliasFromResponse(p.Response)
	if alias == "" {
		return false
	}
	row := stripKeyValueRenderer(p.Prompt)
	pg, ok := s.programs[alias]
	if !ok {
		pg = &program{alias: alias, labels: map[string]string{}}
		s.programs[alias] = pg
		s.programOrder = append(s.programOrder, alias)
	}
	type binding struct{ label, value string }
	var bindings []binding
	decoy := false
	for _, seg := range strings.FieldsFunc(row, func(r rune) bool { return r == ';' || r == ',' }) {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		if words := strings.Fields(seg); len(words) > 1 && fuzzyWord("unrelated", strings.ToLower(words[0])) {
			decoy = true
			seg = strings.TrimSpace(seg[len(words[0]):])
		}
		eq := strings.Index(seg, "=")
		if eq <= 0 {
			// Not a key=value row (a glossary sentence): fall back to the
			// glossary clause parse for labels.
			s.ingestGlossary(pg, row)
			return true
		}
		label, value := strings.TrimSpace(seg[:eq]), strings.TrimSpace(seg[eq+1:])
		if decoy {
			if reUnsigned.MatchString(value) {
				pg.decoyApproved, pg.hasDecoy = atoi(value), true
			}
			continue
		}
		bindings = append(bindings, binding{label, value})
	}
	var relation *binding
	hasUnit := false
	for i := range bindings {
		b := &bindings[i]
		switch {
		case strings.EqualFold(b.value, alias):
			pg.labels[b.label] = firstNonEmpty(pg.labels[b.label], "entity-or-alias")
		case reRelation.MatchString(b.value):
			relation = b
			pg.labels[b.label] = "correction"
		case reUnitValue.MatchString(b.value):
			hasUnit = true
			pg.unit = strings.Fields(b.value)[0]
			pg.labels[b.label] = "unit"
		case reSignedInt.MatchString(b.value):
			pg.adjustment, pg.hasAdjust = atoi(b.value), true
			pg.labels[b.label] = "adjustment"
		}
	}
	for _, b := range bindings {
		if !reUnsigned.MatchString(b.value) {
			continue
		}
		n := atoi(b.value)
		switch {
		case relation != nil:
			m := reRelation.FindStringSubmatch(relation.value)
			if b.label == m[2] {
				if m[1] == m[2] {
					pg.approved2, pg.hasLatest = n, true
					pg.labels[b.label] = "approved"
				} else {
					pg.approved, pg.hasApproved = n, true
					pg.labels[b.label] = "approved"
					pg.labels[m[1]] = firstNonEmpty(pg.labels[m[1]], "draft")
				}
			} else {
				pg.paid, pg.hasPaid = n, true
				pg.labels[b.label] = "paid"
			}
		case hasUnit:
			pg.draft, pg.hasDraft = n, true
			pg.labels[b.label] = "draft"
		default:
			pg.paid, pg.hasPaid = n, true
			pg.labels[b.label] = "paid"
		}
	}
	return true
}
