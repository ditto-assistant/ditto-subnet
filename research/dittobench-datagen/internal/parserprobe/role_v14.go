package parserprobe

import (
	"regexp"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Inverse grammar for the v14 role families: holder-vs-actor (SN-360) and
// identity-join (SN-359). Like the v13 frames it reads only the public prose
// of the scoped case file: custody sentences, the contact directory and the
// owner corrections. The question selects the step or the side of the join;
// no seed, ID, claim or expected answer is consulted.

// ── SN-360 holder-vs-actor ──────────────────────────────────────────────────

// Roles: "doer" is a "<person> <step>" run (the generator writes the step
// straight after its doer, so the split comes from the question), "actor" and
// "step" are separated captures, "giver" and "holder" are the two sides of a
// handover. Scoped rows have ";" folded to ",".
var roleV14CustodyFrames = []businessFrame{
	// First hop.
	businessPattern("%s on %s and passed %s to %s.", "doer", "alias", "item", "holder"),
	businessPattern("Once %s had %s for %s, %s went over to %s.", "actor", "step", "alias", "item", "holder"),
	businessPattern("%s collected %s for %s from %s, who had %s.", "holder", "item", "alias", "actor", "step"),
	// Second hop: the giver is named, so the chain links in any filing order.
	businessPattern("%s, and %s handed %s for %s on to %s.", "doer", "giver", "item", "alias", "holder"),
	businessPattern("With %s having %s, %s took %s for %s over from %s.", "actor", "step", "holder", "item", "alias", "giver"),
	businessPattern("%s passed %s for %s to %s after %s.", "giver", "item", "alias", "holder", "doer"),
	// A later step whose doer never takes the item.
	businessPattern("%s on %s, %s did not change hands.", "doer", "alias", "item"),
	businessPattern("Later %s for %s without taking %s.", "doer", "alias", "item"),
	businessPattern("%s on %s, and %s stayed where it was.", "doer", "alias", "item"),
}

type roleV14Custody struct {
	doer, actor, step, giver, holder string
}

// roleV14StepActor returns the person who performed the step the question
// names in this record, and how many words of the step matched.
func roleV14StepActor(c roleV14Custody, question string) (string, int) {
	if c.step != "" {
		if containsPhraseFuzzy(question, c.step) {
			return c.actor, len(strings.Fields(c.step))
		}
		return "", 0
	}
	words := strings.Fields(c.doer)
	// The longest tail of the doer run that the question repeats is the step;
	// the person never appears in an actor question.
	for k := 1; k <= len(words)-2; k++ {
		if step := strings.Join(words[k:], " "); containsPhraseFuzzy(question, step) {
			return strings.Join(words[:k], " "), len(words) - k
		}
	}
	return "", 0
}

func answerHolderVsActorV14(rows []string, question string) (derived, bool) {
	var custody []roleV14Custody
	for _, row := range rows {
		parsed, ok := temporalV14Parse(roleV14CustodyFrames, row)
		if !ok {
			continue
		}
		custody = append(custody, roleV14Custody{
			doer: parsed["doer"], actor: parsed["actor"], step: parsed["step"],
			giver: parsed["giver"], holder: parsed["holder"],
		})
	}
	if len(custody) == 0 {
		return derived{}, false
	}
	result := derived{family: "v13-open-program", kind: protocol.AnswerValue}
	// Actor question: the record whose step the question repeats names the
	// doer. A step is at least two words, so an item noun cannot pass for one.
	votes := map[string]int{}
	bestLen := 0
	for _, c := range custody {
		actor, n := roleV14StepActor(c, question)
		if actor == "" || n < 2 || n < bestLen {
			continue
		}
		if n > bestLen {
			bestLen, votes = n, map[string]int{}
		}
		votes[actor]++
	}
	if actor := roleV14Vote(votes); actor != "" {
		result.value, result.ok = actor, true
		return result, true
	}
	// Holder question: fold the chain. The final holder received the item and
	// never handed it on.
	votes = map[string]int{}
	for _, c := range custody {
		if c.holder == "" {
			continue
		}
		passedOn := false
		for _, other := range custody {
			if other.giver != "" && fuzzyName(other.giver, c.holder) {
				passedOn = true
			}
		}
		if !passedOn {
			votes[c.holder]++
		}
	}
	if holder := roleV14Vote(votes); holder != "" {
		result.value, result.ok = holder, true
		return result, true
	}
	return derived{}, false
}

// roleV14Vote picks the capture the duplicated (noised) records agree on most.
func roleV14Vote(votes map[string]int) string {
	best, bestN := "", 0
	for v, n := range votes {
		if n > bestN || (n == bestN && v < best) {
			best, bestN = v, n
		}
	}
	return best
}

// ── SN-359 identity-join ────────────────────────────────────────────────────

var (
	roleV14Handle = regexp.MustCompile(`^(@[a-z]+-\d{2}|[a-z]+\.\d{2}@[a-z]+\.example\.invalid)$`)
	// Entry forms. Grammar words carry typo noise; names and handles do not.
	roleV14EntryForms = []businessFrame{
		businessPattern("%s belongs to %s", "handle", "name"),
		businessPattern("%s uses %s", "name", "handle"),
		businessPattern("%s is %s", "handle", "name"),
	}
	// The owner corrections are the v13 owner-after-correction frames.
	roleV14OwnerFrames = businessV13Frames[:3]
)

type roleV14Binding struct{ handle, name string }

// roleV14Directory parses the directory record into handle/name bindings. The
// record is "<lead>: <entries>."; it is recognised by its entries (every one a
// binding of a handle-shaped contact to a person), not by the lead wording,
// which carries typo noise.
func roleV14Directory(row string) ([]roleV14Binding, bool) {
	_, entries, found := strings.Cut(row, ": ")
	if !found {
		return nil, false
	}
	entries = strings.TrimSuffix(strings.TrimSpace(entries), ".")
	// The entries are "; "-joined; scoped rows fold ";" to ",". Neither names
	// nor handles contain a comma.
	var out []roleV14Binding
	for _, part := range strings.Split(entries, ",") {
		part = strings.TrimSpace(part)
		var b roleV14Binding
		for _, form := range roleV14EntryForms {
			if e, ok := temporalV14Parse([]businessFrame{form}, part); ok && roleV14Handle.MatchString(e["handle"]) {
				b = roleV14Binding{handle: e["handle"], name: e["name"]}
				break
			}
		}
		if b.handle == "" {
			return nil, false
		}
		out = append(out, b)
	}
	return out, len(out) >= 2
}

// roleV14SameName matches an owner reference against a directory name, either
// whole or as first initial and surname ("E. Page").
func roleV14SameName(ref, name string) bool {
	if fuzzyName(ref, name) {
		return true
	}
	rw, nw := strings.Fields(ref), strings.Fields(name)
	if len(rw) < 2 || len(rw) != len(nw) || rw[0] != nw[0][:1]+"." {
		return false
	}
	return fuzzyName(strings.Join(rw[1:], " "), strings.Join(nw[1:], " "))
}

func answerIdentityJoinV14(rows []string) (derived, bool) {
	var directory []roleV14Binding
	owners := map[string]int{}
	for _, row := range rows {
		if d, ok := roleV14Directory(row); ok {
			directory = d
			continue
		}
		if parsed, ok := temporalV14Parse(roleV14OwnerFrames, row); ok {
			owners[parsed["owner"]]++
		}
	}
	owner := roleV14Vote(owners)
	if len(directory) == 0 || owner == "" {
		return derived{}, false
	}
	// The owner records use one side of the binding; the answer is the other.
	result := derived{family: "v13-open-program", kind: protocol.AnswerValue}
	for _, b := range directory {
		switch {
		case roleV14Handle.MatchString(owner) && b.handle == owner:
			result.value, result.ok = b.name, true
		case !roleV14Handle.MatchString(owner) && roleV14SameName(owner, b.name):
			result.value, result.ok = b.handle, true
		}
		if result.ok {
			return result, true
		}
	}
	return derived{}, false
}

// answerRoleV14 inverts the v14 role families. ok is false when the rows carry
// neither shape.
func answerRoleV14(rows []string, question string) (derived, bool) {
	if d, ok := answerIdentityJoinV14(rows); ok {
		return d, true
	}
	return answerHolderVsActorV14(rows, question)
}
