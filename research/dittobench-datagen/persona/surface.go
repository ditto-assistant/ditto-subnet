package persona

import (
	"math/rand"
	"strings"
	"unicode"
)

// Surface variation (v2 hardening 1a). The haystack is rendered from a small
// set of statement frames per fact type. With the generator public, a fixed
// frame is a fixed regex: an inverse parser anchors on "I just moved to %s."
// and reads the slot. Wrapping each rendered statement with a seed-chosen
// lead-in and trailer multiplies the surface a parser must cover without
// touching the fact value (which stays verbatim for the containment grader),
// so a real reader is unaffected while a frame matcher must handle the cross
// product of frames x lead-ins x trailers. The lead-in/trailer pools carry no
// ground truth of their own (they are inconsequential filler, so they also act
// as GSM-NoOp-style no-op clauses that pressure a naive retriever).

// stmtLeadIns front a statement. The empty entries weight toward no lead-in so
// most statements stay plain; the rest add a conversational opener.
var stmtLeadIns = []string{
	"", "", "", "",
	"By the way, ", "Oh, ", "Just so you know, ", "For what it's worth, ",
	"Quick update: ", "Meant to mention, ", "On a side note, ", "Also, ",
}

// stmtTrailers close a statement with an inconsequential remark. Empty entries
// weight toward no trailer. None carry a recoverable fact.
var stmtTrailers = []string{
	"", "", "", "",
	" Anyway.", " Thought I'd mention it.", " Just flagging it.",
	" No big deal.", " Figured you should know.", " That's the latest.",
	" Nothing urgent.", " Just keeping you posted.",
}

// varySurface wraps a rendered statement with a seed-chosen lead-in and trailer
// from the persona statement pools. See Wrap for the contract.
func varySurface(r *rand.Rand, s, value string) string {
	return Wrap(r, s, value, stmtLeadIns, stmtTrailers)
}

// humanizeV8Fact removes benchmark-like connective tissue after all legacy
// facts have been deterministically selected. It is deliberately V8-only at
// the call site: V2-V7 retain their frozen bytes and draw sequence.
func humanizeV8Fact(user, assistant string) (string, string) {
	for _, prefix := range []string{"For reference, ", "Oh, "} {
		if strings.HasPrefix(user, prefix) {
			user = strings.TrimPrefix(user, prefix)
			user = capitalizeFirst(user)
			break
		}
	}
	for _, suffix := range []string{
		" Nothing urgent.", " Just keeping you posted.", " Thought I'd mention it.",
		" Just flagging it.", " No big deal.", " Figured you should know.",
	} {
		user = strings.TrimSuffix(user, suffix)
	}
	if strings.Contains(assistant, ", not something about you.") {
		assistant = strings.Replace(assistant, "Got it, ", "Thanks for telling me about ", 1)
		assistant = strings.Replace(assistant, ", not something about you.", " — I’ll keep that straight.", 1)
	}
	return user, assistant
}

func capitalizeFirst(s string) string {
	if s == "" {
		return s
	}
	runes := []rune(s)
	runes[0] = unicode.ToUpper(runes[0])
	return string(runes)
}

// Wrap is the shared Tier-1a surface-variation engine: it wraps a rendered
// statement with a lead-in and trailer drawn from the given pools. It always
// consumes exactly two draws from r (so the RNG stream is independent of which
// variant lands, keeping generation byte-reproducible). When a lead-in is
// prepended, the statement's first letter is lowercased unless it is a
// standalone "I" or the statement begins with the protected value (a canonical
// fact value or pinned argument must survive realization verbatim, case
// included — "Copper Rain" must never become "copper Rain"). protect may be ""
// when the statement carries no such value. Exported so the tool-prompt wrap
// (datagen) runs THIS engine with its own pools instead of a drifting copy.
func Wrap(r *rand.Rand, s, protect string, leadIns, trailers []string) string {
	lead := leadIns[r.Intn(len(leadIns))]
	trail := trailers[r.Intn(len(trailers))]
	if s == "" {
		return s
	}
	if lead != "" {
		s = lowerFirstUnlessProtected(s, protect)
	}
	return lead + s + trail
}

// lowerFirstUnlessProtected lowercases the first rune of s unless the first
// word is the pronoun "I" or s begins with the verbatim fact value (so a
// value-leading template survives a mid-sentence lead-in unmangled).
func lowerFirstUnlessProtected(s, value string) string {
	if strings.HasPrefix(s, "I ") || s == "I" || strings.HasPrefix(s, "I'") {
		return s
	}
	if value != "" && strings.HasPrefix(s, value) {
		return s
	}
	rs := []rune(s)
	rs[0] = unicode.ToLower(rs[0])
	return string(rs)
}

// notYouFrames render a decoy person's fact (a near-miss the user never stated).
// Varying the frame stops a matcher from excluding every distractor by a single
// fixed "By the way, X's Y is Z." signature; the false-premise grounding test
// then actually requires reading whose fact it is. %[1]s=relation, %[2]s=name,
// %[3]s=label, %[4]s=value.
var notYouFrames = []string{
	"By the way, %[1]s %[2]s's %[3]s is %[4]s.",
	"Oh, %[1]s %[2]s mentioned their %[3]s is %[4]s.",
	"%[2]s, %[1]s, has a %[3]s of %[4]s if that's useful.",
	"Not mine, but %[1]s %[2]s's %[3]s is %[4]s.",
	"For reference, %[1]s %[2]s said their %[3]s is %[4]s.",
}

// notYouAcks acknowledge a decoy fact, attributing it away from the user.
var notYouAcks = []string{
	"Noted — that %[1]s is %[2]s's, not yours.",
	"Got it, %[2]s's %[1]s, not something about you.",
	"Understood, that %[1]s belongs to %[2]s.",
}

// (surface variation is exercised in surface_test.go: value preservation and
// cross-seed diversity of the rendered haystack.)
