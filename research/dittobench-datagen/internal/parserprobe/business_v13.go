package parserprobe

import (
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The public case-file reference is semantic context, not a grader label.
// Resolve it from the question and records; never use seed, IDs, Claims,
// provenance, expected answers, or artifact ordering to derive an answer.
var caseFileRef = regexp.MustCompile(`\bc[0-9a-f]{16}\b`)

func scopedV13Rows(st *store, question string) []string {
	scope := caseFileRef.FindString(question)
	if scope == "" {
		return nil
	}
	seen := map[string]bool{}
	var rows []string
	for _, p := range st.pairs {
		marker := scope + ": "
		if i := strings.Index(p.Prompt, marker); i >= 0 {
			row := strings.TrimSpace(p.Prompt[i+len(marker):])
			row = strings.TrimSpace(strings.TrimRight(row, "|}\""))
			// The operations-dump renderer uses commas in place of semicolons.
			row = strings.ReplaceAll(row, ";", ",")
			if !seen[row] {
				seen[row] = true
				rows = append(rows, row)
			}
		}
	}
	return rows
}

type businessFrame struct {
	frame frame
	roles []string
}

func businessPattern(pattern string, roles ...string) businessFrame {
	f := compileFrame(strings.ReplaceAll(pattern, ";", ","))
	f.literalBudget = 2
	return businessFrame{f, roles}
}

// These inverse frames mirror the PUBLIC prose grammar. Captures are runtime
// values, not a vocabulary of answers. A new renderer without an inverse stays
// an unparsed case rather than being credited as a resistant surface.
var businessV13Frames = []businessFrame{
	businessPattern("Under this batch's %s, the %s on %s passed to %s; the earlier assignment no longer stands.", "correction", "owner_label", "subject", "owner"),
	businessPattern("A later %s hands %s's %s to %s — the newest entry governs.", "correction", "subject", "owner_label", "owner"),
	businessPattern("%s note: %s now holds %s on %s, replacing whoever held it before.", "correction", "owner", "owner_label", "subject"),
	businessPattern("A later %s moved %s's %s to %s; the newest entry governs.", "correction", "subject", "value_label", "value"),
	businessPattern("Under this batch's %s the %s on %s now stands at %s.", "correction", "value_label", "subject", "value"),
	businessPattern("%s: %s's %s was revised to %s, replacing the earlier reading.", "correction", "subject", "value_label", "value"),
	businessPattern("Under this batch's %s the %s for %s is now %s.", "correction", "value_label", "subject", "value"),
	businessPattern("%s: %s's %s switched to %s, replacing the earlier arrangement.", "correction", "subject", "value_label", "value"),
	businessPattern("On %s, %s logged a %s: %s.", "date", "subject", "event_label", "event"),
	businessPattern("%s's %s entry dated %s reads %s.", "subject", "event_label", "date", "event"),
	businessPattern("A %s for %s — %s — is on record for %s.", "event_label", "subject", "event", "date"),
	businessPattern("The %s on %s is to %s, with %s down as %s.", "action_label", "subject", "action", "old_owner", "responsible_label"),
	businessPattern("%s's %s: %s; %s at the time: %s.", "subject", "action_label", "action", "responsible_label", "old_owner"),
	businessPattern("Next up for %s, per its %s, is to %s — %s was named %s.", "subject", "action_label", "action", "old_owner", "responsible_label"),
	businessPattern("Under this batch's %s, %s for that step on %s is now %s; the step itself is unchanged.", "correction", "responsible_label", "subject", "responsible"),
	businessPattern("%s: the %s on %s's next step moved to %s. The step stays as recorded.", "correction", "responsible_label", "subject", "responsible"),
	businessPattern("A later %s names %s as %s for %s's pending step, replacing the earlier holder.", "correction", "responsible", "responsible_label", "subject"),
	businessPattern("%s's launch sign-off is recorded as given by %s.", "subject", "signer"),
	businessPattern("One entry has %s signing off %s.", "signer", "subject"),
	businessPattern("Per the first note, %s approved %s for launch.", "signer", "subject"),
	businessPattern("A separate note records the sign-off on %s as coming from %s. Neither note cites the other.", "subject", "signer"),
	businessPattern("Elsewhere %s is named as the person who signed off %s; the two entries are independent.", "signer", "subject"),
	businessPattern("Another record lists %s as %s's launch approver, with no reference to any earlier entry.", "signer", "subject"),
	businessPattern("%s's %s is %s.", "subject", "party_label", "party"),
	businessPattern("For %s the %s on record is %s.", "subject", "party_label", "party"),
	businessPattern("%s is logged as the %s on %s.", "party", "party_label", "subject"),
}

var clientLabelsV13 = compileFrames(
	"%s is the organisation that commissions it and pays us",
	"%s names our paying customer for it",
	"%s is the party that pays us for it",
)

func answerBusinessV13(st *store, question string) derived {
	rows := scopedV13Rows(st, question)
	if len(rows) == 0 {
		return derived{}
	}
	values := map[string]string{}
	signers := map[string]bool{}
	parties := map[string]string{}
	clientLabel := ""
	var latest time.Time
	latestEvent := ""
	for _, row := range rows {
		// Glossary clauses may follow a prose opener, so try token suffixes
		// inside each delimited clause. No role label is known beforehand.
		for _, clause := range strings.Split(strings.TrimSuffix(row, "."), ";") {
			// scopedV13Rows normalizes the ops renderer to commas.
			for _, part := range strings.Split(clause, ",") {
				words := strings.Fields(part)
				for i := range words {
					if _, slots, ok := matchAny(clientLabelsV13, strings.Join(words[i:], " ")); ok {
						clientLabel = slots[0]
					}
				}
			}
		}
		for _, p := range businessV13Frames {
			slots, ok := p.frame.match(row)
			if !ok {
				continue
			}
			parsed := map[string]string{}
			for i, role := range p.roles {
				parsed[role] = slots[i]
			}
			if signer := parsed["signer"]; signer != "" {
				signers[signer] = true
			}
			if party := parsed["party"]; party != "" {
				parties[parsed["party_label"]] = party
			}
			if event := parsed["event"]; event != "" {
				if date, ok := publicEventDate(parsed["date"]); ok && date.After(latest) {
					latest, latestEvent = date, event
				}
			}
			for _, role := range []string{"owner", "value", "action", "responsible"} {
				if value := parsed[role]; value != "" {
					values[role] = value
				}
			}
			break
		}
	}
	result := derived{family: "v13-open-program", kind: protocol.AnswerValue}
	switch {
	case len(signers) == 2:
		for name := range signers {
			result.items = append(result.items, name)
		}
		sort.Strings(result.items)
		result.items = append(result.items, "disagree")
		result.kind, result.ok = protocol.AnswerList, true
	case values["action"] != "" && values["responsible"] != "":
		result.items = []string{values["action"], values["responsible"]}
		result.kind, result.ok = protocol.AnswerList, true
	case values["owner"] != "":
		result.value, result.ok = values["owner"], true
	case values["value"] != "":
		result.value, result.ok = values["value"], true
	case clientLabel != "" && parties[clientLabel] != "":
		result.value, result.ok = parties[clientLabel], true
	case latestEvent != "":
		result.value, result.ok = latestEvent, true
	}
	return result
}

func publicEventDate(raw string) (time.Time, bool) {
	// Prefer explicit years. Yearless prose cannot establish ordering across
	// years; keep it unparsed rather than consulting the generator's seed.
	for _, layout := range []string{"2006-01-02", "January 2, 2006", "Jan 2, 2006", "2 January 2006"} {
		if at, err := time.Parse(layout, raw); err == nil {
			return at, true
		}
	}
	return time.Time{}, false
}
