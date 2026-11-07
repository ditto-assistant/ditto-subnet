package catalog

import (
	"fmt"
	"strings"
)

// V18FixtureSnapshot is a deterministic, synthetic stand-in for the pinned
// production snapshot. Test binaries use it when no snapshot file is set
// (V18SnapshotReady); it is never a contract. Its toolkit sizes follow the
// production skew: the fifteen largest are the sizes one production owner
// account enabled on 2026-11-07, and a long tail of smaller toolkits follows.
func V18FixtureSnapshot() *V18Snapshot {
	snap := &V18Snapshot{Source: "synthetic fixture", CapturedAt: "2026-11-07"}
	sizes := []int{328, 314, 127, 92, 70, 61, 57, 50, 47, 46, 26, 25, 16, 16, 7}
	for i := 0; i < 120; i++ {
		sizes = append(sizes, 3+(i*37)%45)
	}
	verbs := []string{"LIST", "GET", "CREATE", "UPDATE", "DELETE", "SEARCH", "ARCHIVE", "RESTORE", "EXPORT", "COPY"}
	nouns := []string{"RECORD", "ITEM", "FOLDER", "PROJECT", "MEMBER", "COMMENT", "LABEL", "FIELD", "VIEW", "REPORT",
		"TEMPLATE", "WEBHOOK", "ATTACHMENT", "TEAM", "WORKSPACE", "PERMISSION", "HISTORY", "SETTING", "AUTOMATION", "FORM",
		"DASHBOARD", "STATUS", "TAG", "EXPORT_JOB", "INVITE", "AUDIT_ENTRY", "GROUP", "POLICY", "VERSION", "LINK", "QUEUE", "RULE", "SECTION"}
	for k, size := range sizes {
		slug := fmt.Sprintf("fixturekit%03d", k)
		name := fmt.Sprintf("Fixture Kit %d", k)
		kit := V18SnapshotToolkit{Slug: slug, Name: name}
		for t := 0; t < size; t++ {
			verb := verbs[t%len(verbs)]
			noun := nouns[(t/len(verbs))%len(nouns)]
			suffix := ""
			if t >= len(verbs)*len(nouns) {
				suffix = fmt.Sprintf("_%d", t/(len(verbs)*len(nouns)))
			}
			kit.Tools = append(kit.Tools, V18SnapshotTool{
				Slug: strings.ToUpper(slug) + "_" + verb + "_" + noun + suffix,
				Description: fmt.Sprintf("%s retrieves or changes a %s in the user's %s account using the specified identifier. Returns the existing %s information as the toolkit reports it, with pagination where it applies.",
					strings.ToUpper(verb[:1])+strings.ToLower(verb[1:]), strings.ToLower(noun), name, strings.ToLower(noun)),
			})
		}
		snap.Toolkits = append(snap.Toolkits, kit)
	}
	// Capability-owned toolkits that must never be sampled.
	snap.Toolkits = append(snap.Toolkits,
		V18SnapshotToolkit{Slug: "gmail", Name: "Gmail", Tools: []V18SnapshotTool{{Slug: "GMAIL_SEND_EMAIL", Description: "Sends an email."}}},
		V18SnapshotToolkit{Slug: "googlecalendar", Name: "Google Calendar", Tools: []V18SnapshotTool{{Slug: "GOOGLECALENDAR_CREATE_EVENT", Description: "Creates an event."}}},
	)
	snap.FirstParty = []V18SnapshotFirstTool{
		{Name: "fixture_first_party_lookup", Description: "Looks up a Ditto workspace setting by name and returns its current value.", Parameters: schema([]string{"name"}, prop{name: "name", typ: "string", desc: "setting name"})},
		{Name: "fixture_first_party_note", Description: "Adds a short note to the user's Ditto workspace activity log.", Parameters: schema([]string{"text"}, prop{name: "text", typ: "string", desc: "note text"})},
	}
	return snap
}
