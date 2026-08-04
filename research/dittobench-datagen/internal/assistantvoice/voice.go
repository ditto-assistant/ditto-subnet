// Package assistantvoice projects deterministic benchmark acknowledgements into
// Ditto's product voice without changing any user-authored text or factual
// assistant content.
package assistantvoice

import (
	"fmt"
	"hash/fnv"
	"strings"
)

var genericAcknowledgements = map[string]bool{
	"good to know.":                       true,
	"got it.":                             true,
	"nice.":                               true,
	"noted.":                              true,
	"noted from the subscribed graph.":    true,
	"okay.":                               true,
	"recorded.":                           true,
	"saved.":                              true,
	"thanks.":                             true,
	"thanks for the update.":              true,
	"understood.":                         true,
	"understood, thanks for the update.":  true,
	"understood, thanks for flagging it.": true,
	"updated.":                            true,
}

var factLeads = []string{
	"Got you",
	"I hear you",
	"With you",
	"I’ve got it",
	"Thanks",
	"I’m with you",
}

var genericWarmReplies = []string{
	"Got you 💛",
	"I hear you.",
	"With you.",
	"Thanks for sharing!",
	"I’ve got you.",
	"That makes sense.",
	"Thanks for telling me!",
	"Here with you.",
	"I’m following.",
	"I’ll remember.",
	"Holding onto that.",
	"I see you.",
	"Keeping that in mind.",
	"Thanks for trusting me.",
	"Okay, I hear you.",
	"Right beside you.",
	"I’m here.",
	"Got you, truly.",
	"That matters.",
	"Thanks — I’ve got it.",
	"Keeping it close.",
	"I’m listening.",
	"Right here.",
	"I’ll hold onto it.",
}

var relationshipWarmReplies = []string{
	"I’ll remember them.",
	"Got you — connection included.",
	"I’ve got the history.",
	"That bond matters.",
	"I’ll remember the person, too.",
	"I’ll keep the bond clear.",
	"I hear the shared history.",
	"Thanks — I know how they fit.",
	"I’ll hold that history.",
	"I’ve got the human part.",
	"With you — bond and all.",
	"I know your world better.",
}

var workWarmReplies = []string{
	"Got you — work included.",
	"I’ll keep the project straight.",
	"I’ve got where that fits.",
	"Thanks — I’ll keep the roles clear.",
	"I hear you — work detail included.",
	"I won’t mix up the project.",
	"I’ll keep the work history.",
	"I’ve got the work context.",
	"That fits the project.",
	"I’m following the work thread.",
	"I’ll remember the context.",
	"I’ve got the bigger picture.",
}

var travelWarmReplies = []string{
	"Got you — trip included.",
	"I’ll remember where that fits.",
	"I’ve got that part of the trip.",
	"Places and timing connected.",
	"I’ll remember the adventure.",
	"With you — travel and all.",
	"I’ll keep that with the trip.",
	"That fits the journey.",
	"I’m following the travel thread.",
	"I’ll remember that part.",
	"I’ve got the trip context.",
	"With you on the trip.",
}

var preferenceWarmReplies = []string{
	"Got you — preference kept.",
	"I’ll keep that in mind.",
	"I’ve got what works for you.",
	"Thanks — choice remembered.",
	"I’ll keep that preference.",
	"With you — choice included.",
	"I’ll hold onto that detail.",
	"That helps me know you better.",
	"I know what feels right.",
	"I’ll remember that when it matters.",
	"I’ve got your preference.",
	"Right here with you.",
}

var updateWarmReplies = []string{
	"Got you — state updated.",
	"I’ll remember what changed.",
	"I’ve got the update.",
	"I’ll keep both versions straight.",
	"I’ll use the update now.",
	"The timeline stays intact.",
	"I’ll keep the before and after clear.",
	"That change makes sense.",
	"I’m following the update.",
	"I’ll remember that change.",
	"I’ve got the latest state.",
	"With you on the update.",
}

var duplicateCodas = []string{
	"I’m keeping the context around it too.",
	"That detail is staying with me.",
	"I can see where it belongs now.",
	"I’ll keep its place in the story clear.",
	"I’m holding onto how it connects.",
	"The surrounding thread is clear to me.",
	"I’ll remember what sits around it too.",
	"I can follow where this fits.",
	"I’m keeping the wider picture with it.",
	"That gives me another piece of your world.",
	"I’ll keep the reason behind it close.",
	"I’m following how this came together.",
	"I can place it in the timeline now.",
	"I’ll remember the path that led here.",
	"The before and after make sense to me.",
	"I’m keeping its connections intact.",
}

// Render returns a deterministic V8 assistant response. It replaces generic or
// transactional acknowledgements and rewrites cold prefixes while retaining any
// substantive fact already present in the response. When userName is available,
// a bounded share of replies address the user by first name; most deliberately
// do not, matching a warm companion rather than a scripted support bot.
func Render(seed int64, pairID, sessionID, userName, prompt, response string) string {
	response = strings.TrimSpace(response)
	rendered := response
	if response == "" || genericAcknowledgements[strings.ToLower(response)] {
		rendered = warmAcknowledgement(seed, pairID, sessionID, prompt)
	} else if rewritten, ok := rewriteTransactional(seed, pairID, response); ok {
		rendered = rewritten
	}
	return addressUserByName(seed, pairID, userName, rendered)
}

// DiversifyDuplicate adds a deterministic, natural coda when a complete V8
// artifact would otherwise repeat the exact same acknowledgement more than
// twice. attempt walks the coda ring rather than making a random promise, so an
// artifact-level caller can fail closed if every available rendering is already
// occupied. The substantive response is preserved verbatim.
func DiversifyDuplicate(seed int64, pairID, response string, attempt int) string {
	response = strings.TrimSpace(response)
	if response == "" || attempt < 1 {
		return response
	}
	start := hashIndex(seed, pairID, "duplicate-coda", len(duplicateCodas))
	coda := duplicateCodas[(start+attempt-1)%len(duplicateCodas)]
	return response + " " + coda
}

// IsCold identifies response surfaces that violate the V8 transcript standard.
// It is exported so artifact-level tests and review tooling can enforce the same
// rule used by generation.
func IsCold(response string) bool {
	response = strings.TrimSpace(response)
	lower := strings.ToLower(response)
	if response == "" || genericAcknowledgements[lower] {
		return true
	}
	for _, prefix := range []string{
		"got it ", "got it,", "got it.", "got it —",
		"noted ", "noted,", "noted.", "noted —",
		"recorded ", "recorded,", "recorded.", "recorded —",
		"saved ", "saved,", "saved.", "saved —",
		"understood ", "understood,", "understood.", "understood —",
		"updated ", "updated,", "updated.", "updated —",
	} {
		if strings.HasPrefix(lower, prefix) {
			return true
		}
	}
	return false
}

func rewriteTransactional(seed int64, pairID, response string) (string, bool) {
	response = humanizeThirdPartyAttribution(response)
	type rewrite struct {
		prefix string
		keep   string
	}
	patterns := []rewrite{
		{"Noted that ", ""},
		{"Noted your ", "your "},
		{"Noted the ", "the "},
		{"Noted. ", ""},
		{"Noted, ", ""},
		{"Noted — ", ""},
		{"Got it. ", ""},
		{"Got it — ", ""},
		{"Got it, ", ""},
		{"Understood. ", ""},
		{"Understood — ", ""},
		{"Understood, ", ""},
		{"Recorded. ", ""},
		{"Recorded, ", ""},
		{"Recorded — ", ""},
		{"Saved. ", ""},
		{"Saved, ", ""},
		{"Saved — ", ""},
		{"Updated. ", ""},
		{"Updated, ", ""},
		{"Updated — ", ""},
	}
	for _, pattern := range patterns {
		if !strings.HasPrefix(response, pattern.prefix) {
			continue
		}
		tail := strings.TrimSpace(strings.TrimPrefix(response, pattern.prefix))
		if tail == "" {
			return "", false
		}
		lead := factLeads[hashIndex(seed, pairID, "fact-lead", len(factLeads))]
		return fmt.Sprintf("%s — %s%s", lead, pattern.keep, tail), true
	}
	return "", false
}

func humanizeThirdPartyAttribution(response string) string {
	// Attribution is useful; explaining Ditto's internal ownership boundary to
	// the user is not. Keep every named person, label, and exact value while
	// removing the robotic "not yours / not about you" commentary.
	response = strings.ReplaceAll(response, ", not yours.", ".")
	response = strings.ReplaceAll(response, ", not something about you.", ".")
	response = strings.ReplaceAll(response, ", kept separate from yours.", "; I’ll keep it with their details.")
	return response
}

func warmAcknowledgement(seed int64, pairID, sessionID, prompt string) string {
	replies := repliesFor(sessionID, prompt)
	return replies[hashIndex(seed, pairID, "reply", len(replies))]
}

func addressUserByName(seed int64, pairID, userName, response string) string {
	fields := strings.Fields(userName)
	if len(fields) == 0 {
		return response
	}
	firstName := fields[0]
	if hashIndex(seed, pairID, "use-name:"+firstName, 4) != 0 {
		return response
	}
	if strings.Contains(strings.ToLower(response), strings.ToLower(firstName)) {
		return response
	}

	// Alternate the placement so name use itself does not become a repeated
	// template. An em-dash acknowledgement reads best with an inline vocative;
	// other replies rotate between a leading and trailing address.
	if divider := strings.Index(response, " — "); divider > 0 && supportsInlineVocative(response[:divider]) && hashIndex(seed, pairID, "name-place:"+firstName, 2) == 0 {
		return response[:divider] + ", " + firstName + response[divider:]
	}
	if hashIndex(seed, pairID, "name-place:"+firstName, 2) == 0 && startsFirstPerson(response) {
		return firstName + ", " + response
	}

	ending := ""
	if strings.HasSuffix(response, ".") || strings.HasSuffix(response, "!") || strings.HasSuffix(response, "?") {
		ending = response[len(response)-1:]
		response = strings.TrimSpace(response[:len(response)-1])
	}
	if ending == "" {
		ending = "."
	}
	return response + ", " + firstName + ending
}

func startsFirstPerson(response string) bool {
	return strings.HasPrefix(response, "I ") ||
		strings.HasPrefix(response, "I’") ||
		strings.HasPrefix(response, "I'm")
}

func supportsInlineVocative(lead string) bool {
	if startsFirstPerson(lead) {
		return true
	}
	for _, conversationalLead := range []string{
		"Aw", "Good catch", "Got you", "Lovely", "Okay", "Ooh", "Perfect",
		"Right here", "Sounds good", "Thanks", "That makes sense", "With you",
	} {
		if lead == conversationalLead || strings.HasPrefix(lead, conversationalLead+",") {
			return true
		}
	}
	return false
}

func repliesFor(sessionID, prompt string) []string {
	text := strings.ToLower(sessionID + " " + prompt)
	switch {
	case containsAny(text, "changed", "correction", "current", "earlier", "old ", "original", "replace", "stale", "update", "used to"):
		return updateWarmReplies
	case containsAny(text, "trip", "travel", "journey", "flight", "hotel", "country", "itinerary"):
		return travelWarmReplies
	case containsAny(text, "friend", "family", "partner", "roommate", "cousin", "neighbor", "person", "relationship"):
		return relationshipWarmReplies
	case containsAny(text, "project", "work", "client", "vendor", "invoice", "payment", "job", "business", "meeting"):
		return workWarmReplies
	case containsAny(text, "prefer", "favorite", "favourite", "like ", "love ", "choice", "usual"):
		return preferenceWarmReplies
	default:
		return genericWarmReplies
	}
}

func containsAny(text string, values ...string) bool {
	for _, value := range values {
		if strings.Contains(text, value) {
			return true
		}
	}
	return false
}

func hashIndex(seed int64, pairID, salt string, n int) int {
	if n <= 0 {
		return 0
	}
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v8-assistant-voice:%d:%s:%s", seed, pairID, salt)
	return int(h.Sum64() % uint64(n))
}
