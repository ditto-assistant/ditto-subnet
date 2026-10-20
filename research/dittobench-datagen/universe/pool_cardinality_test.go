package universe

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/internal/poolcanary"
	"github.com/ditto-assistant/dittobench-datagen/internal/publicdata"
)

// TestPoolCardinalityCanary is the world generator's pool-cardinality canary
// (#492, item 4). See internal/poolcanary for the floor and the ratchet.
//
// Most of the scored v13/v14 memory surface lives here. The issue's
// recurring-topic and relation tables have their closest live counterparts in
// the v13 personal programs (personal.go): v13Relations, v13Trips, v13Clubs,
// and the others are the subjects a question names. #1825 already replaced
// the v8..v12 hand lists with corpus-backed or composed banks. Composed
// surfaces are measured by their product here, so those replacements pass on
// their own merits.
//
// The classification was audited by covering GenerateDatasetWithSurface at
// v13 and v14 across all three run sizes and several surface salts, then
// matching each reached pool against the emitted questions, answers, and
// seeded records.
func TestPoolCardinalityCanary(t *testing.T) {
	orgStems := len(publicdata.AllOrgStems())
	poolcanary.Check(t, ".", poolcanary.Ledger{
		Scored: []poolcanary.Pool{
			// Composed identifier surfaces (#1825), measured by product: a
			// fixed table covers the draw only if it covers every combination.
			{Name: "relationKinds × relationQualifiers", Parts: []string{"relationKinds", "relationQualifiers"},
				Size: len(relationKinds) * len(relationQualifiers), Role: "question entity (person relation)"},
			{Name: "v13OrgStems × v13OrgSuffixes", Parts: []string{"v13OrgStems", "v13OrgSuffixes"},
				Size: len(v13OrgStems) * len(v13OrgSuffixes), Role: "answer (business organisation)"},
			{Name: "v13PurposeAdjectives × v13PurposeNouns", Parts: []string{"v13PurposeAdjectives", "v13PurposeNouns"},
				Size: len(v13PurposeAdjectives) * len(v13PurposeNouns), Role: "question entity (workstream remit)"},
			{Name: "v10LabelStarts × v12LabelMids × v10LabelEnds", Parts: []string{"v10LabelStarts", "v12LabelMids", "v10LabelEnds"},
				Size: len(v10LabelStarts) * len(v12LabelMids) * len(v10LabelEnds), Role: "question entity (workstream alias)"},
			{Name: "publicdata org stems × contextEventNouns", Parts: []string{"publicdata.AllOrgStems", "contextEventNouns"},
				Size: orgStems * len(contextEventNouns), Role: "question entity (event context)"},
			{Name: "contextSeasons × publicdata project purposes", Parts: []string{"contextSeasons", "publicdata.Purposes"},
				Size: len(contextSeasons) * len(publicdata.Purposes(publicdata.PurposeProject)), Role: "question entity (event context)"},
			{Name: "publicdata org stems × projectNameSuffixPairs", Parts: []string{"publicdata.AllOrgStems", "projectNameSuffixPairs"},
				Size: orgStems * len(projectNameSuffixPairs), Role: "question entity (project name and alias)"},
			{Name: "publicdata colours × tripRouteNouns", Parts: []string{"publicdata.AllColors", "tripRouteNouns"},
				Size: len(publicdata.AllColors()) * len(tripRouteNouns), Role: "question entity (trip alias)"},
			{Name: "familyStarts × familyEnds × businessProviderFrames", Parts: []string{"familyStarts", "familyEnds", "businessProviderFrames"},
				Size: len(familyStarts) * len(familyEnds) * len(businessProviderFrames), Role: "answer (story provider)"},

			// v13 personal programs (personal.go): the subjects a question
			// names. These are the live counterparts of #492's recurring topics.
			{Name: "v13Relations", Size: len(v13Relations), Role: "question cue (personal subject)"},
			{Name: "v13ChildRelations", Size: len(v13ChildRelations), Role: "question cue (personal subject)"},
			{Name: "v13Chores", Size: len(v13Chores), Role: "question cue (personal subject)"},
			{Name: "v13Appointments", Size: len(v13Appointments), Role: "question cue (personal subject)"},
			{Name: "v13SchoolEvents", Size: len(v13SchoolEvents), Role: "question cue (personal subject)"},
			{Name: "v13Milestones", Size: len(v13Milestones), Role: "question cue (personal subject)"},
			{Name: "v13Services", Size: len(v13Services), Role: "question cue (personal subject)"},
			{Name: "v13Trips", Size: len(v13Trips), Role: "question cue (personal subject)"},
			{Name: "v13Legs", Size: len(v13Legs), Role: "question cue (personal subject)"},
			{Name: "v13Clubs", Size: len(v13Clubs), Role: "question cue (personal subject)"},
			{Name: "v13SchoolItems", Size: len(v13SchoolItems), Role: "answer (packing list)"},
			{Name: "V13PersonalStatusClasses", Size: len(V13PersonalStatusClasses), Role: "answer (status class)"},
			{Name: "V13PersonalActionClasses", Size: len(V13PersonalActionClasses), Role: "answer (action class)"},
			{Name: "v13PersonalQuestionOpeners", Size: len(v13PersonalQuestionOpeners), Role: "question template"},
			{Name: "v13PersonalAcks", Size: len(v13PersonalAcks), Role: "record surface"},

			// v13 business programs (v13_contract.go). Families beyond the
			// public envelopes' program count (current-channel,
			// next-action-responsible, records-disagree) are only drawn at larger
			// counts. Their classes are still listed here, one count away from
			// live.
			{Name: "V13StatusClasses", Size: len(V13StatusClasses), Role: "answer (status class)"},
			{Name: "v13StatusAliasTerms", Size: len(v13StatusAliasTerms), Role: "record and question cue (per-seed status jargon)"},
			{Name: "V13EventClasses", Size: len(V13EventClasses), Role: "answer (event class)"},
			{Name: "V13ChannelClasses", Size: len(V13ChannelClasses), Role: "answer (channel class)"},
			{Name: "V13ActionClasses", Size: len(V13ActionClasses), Role: "answer (action class)"},
			{Name: "V13ConflictAccept", Size: len(V13ConflictAccept), Role: "accepted answer forms"},
			{Name: "v13DateMonths", Size: len(v13DateMonths), Role: "answer (date)"},
			{Name: "v13SubjectForms", Size: len(v13SubjectForms), Role: "question template"},
			{Name: "v13QuestionOpeners", Size: len(v13QuestionOpeners), Role: "question template"},
			{Name: "v13GlossaryOpeners", Size: len(v13GlossaryOpeners), Role: "record surface"},
			{Name: "v13AckLeads", Size: len(v13AckLeads), Role: "record surface"},
			{Name: "v13AckBodies", Size: len(v13AckBodies), Role: "record surface"},
			{Name: "v13ConversationLeads", Size: len(v13ConversationLeads), Role: "record surface"},
			{Name: "v11NamesVerbs", Size: len(v11NamesVerbs), Role: "record surface (glossary)"},
			{Name: "v11AliasNouns", Size: len(v11AliasNouns), Role: "record surface (glossary)"},
			{Name: "v11CorrectionNouns", Size: len(v11CorrectionNouns), Role: "record surface (glossary)"},
			{Name: "enterpriseReferenceFields", Size: len(enterpriseReferenceFields), Role: "question cue (enterprise join field)"},
			{Name: "enterpriseSetFields", Size: len(enterpriseSetFields), Role: "question cue (enterprise set field)"},

			// Story arcs (story_events.go, story_v2.go, story.go).
			{Name: "storyStatusVocabulary", Size: len(storyStatusVocabulary), Role: "answer (story status)"},
			{Name: "storyChannels", Size: len(storyChannels), Role: "answer (contact channel)"},
			{Name: "storyNextActions", Size: len(storyNextActions), Role: "answer (next action)"},
			{Name: "storyDisagreeMarkers", Size: len(storyDisagreeMarkers), Role: "accepted answer forms"},
			{Name: "storyJoinKeyShapes", Size: len(storyJoinKeyShapes), Role: "record and question cue (join key shape)"},
			{Name: "storyV2Titles", Size: len(storyV2Titles), Role: "record surface"},
			{Name: "personalDomains", Size: len(personalDomains), Role: "record surface (story domain)"},
			{Name: "businessDomains", Size: len(businessDomains), Role: "record surface (story domain)"},
			{Name: "storyLessons", Size: len(storyLessons), Role: "answer (story lesson)", Grown: true}, // merged with the CC0 lesson corpus at init

			// Absence false premises (v13_absence.go).
			{Name: "countryPool", Size: len(countryPool), Role: "question cue (false-premise country)"},
		},

		// Known-small scored pools when the canary landed (#492). Each value is
		// the exact current size. Growing a pool to the floor fails until its
		// entry is deleted, so this list only shrinks. These pools are frozen
		// into the v13/v14 known vectors: a fix ships with the next bench_version,
		// in the new contract's own bank, preferably composed with a corpus as
		// #1825 did.
		Allow: map[string]int{
			// Personal-program subjects. These carry the highest risk: the
			// question names the subject, so a table keyed on these phrases routes
			// the question.
			"v13Relations":      10,
			"v13ChildRelations": 6,
			"v13Chores":         8,
			"v13Appointments":   7,
			"v13SchoolEvents":   5,
			"v13Milestones":     6,
			"v13Services":       7,
			"v13Trips":          6,
			"v13Legs":           4,
			"v13Clubs":          7,
			"v13SchoolItems":    14,
			"countryPool":       15,

			// Answer classes and accepted forms. Several are closed by meaning
			// (statuses, months, channels): growing them changes the task, so
			// the remedy is per-seed aliasing (as v13StatusAliasTerms does), not
			// more labels.
			"V13PersonalStatusClasses":  4,
			"V13PersonalActionClasses":  5,
			"V13StatusClasses":          6,
			"v13StatusAliasTerms":       8,
			"V13EventClasses":           7,
			"V13ChannelClasses":         5,
			"V13ActionClasses":          6,
			"V13ConflictAccept":         9,
			"v13DateMonths":             12,
			"storyStatusVocabulary":     17,
			"storyChannels":             4,
			"storyNextActions":          12,
			"storyDisagreeMarkers":      11,
			"storyJoinKeyShapes":        9,
			"enterpriseReferenceFields": 3,
			"enterpriseSetFields":       1,

			// Templates and record surface. Lower risk: they carry no answer, but
			// a fixed set of frames is still a parser template.
			"v13PersonalQuestionOpeners": 6,
			"v13PersonalAcks":            6,
			"v13SubjectForms":            4,
			"v13QuestionOpeners":         6,
			"v13GlossaryOpeners":         6,
			"v13AckLeads":                6,
			"v13AckBodies":               6,
			"v13ConversationLeads":       4,
			"v11NamesVerbs":              5,
			"v11AliasNouns":              4,
			"v11CorrectionNouns":         3,
			"storyV2Titles":              6,
			"personalDomains":            8,
			"businessDomains":            8,
		},

		Exempt: map[string]string{
			// Re-statements of a scored pool.
			"storyStatusOrder":         "the storyStatusVocabulary keys in lifecycle order (scored there)",
			"businessTerminalStatuses": "subset of storyStatusVocabulary (scored there)",

			// Published case-structure enums.
			"V13Families":          poolcanary.Schema,
			"V13PersonalDomains":   poolcanary.Schema,
			"V13AbsenceFamilies":   poolcanary.Schema,
			"v13PersonalRenderers": poolcanary.Schema,
			"v10Renderers":         poolcanary.Schema,
			"storyEventCatalog":    poolcanary.Schema,
			"personalThemes":       poolcanary.Schema,

			// Tables keyed by oracle or event kind. Each value is a grammar bank.
			"personalThemeBank":    poolcanary.Keyed,
			"storyFactGrammars":    poolcanary.Keyed,
			"storyEventGrammars":   poolcanary.Keyed,
			"v13QuestionGrammars":  poolcanary.Keyed,
			"storyV13TaskGrammars": poolcanary.Keyed,
			"v13StoryTaskGrammars": poolcanary.Keyed,

			// Configuration and filters.
			"monetaryOracles":    poolcanary.Internal,
			"storyDropRotation":  poolcanary.Internal,
			"tripAliasForbidden": poolcanary.Internal, // words a trip alias must avoid

			// v8..v12 world lists that v13 draws only to hold the rng phase
			// (world.go's v13 branch replaces the value from publicdata).
			"colors":    poolcanary.PhaseOnly,
			"relations": poolcanary.PhaseOnly,
			"roles":     poolcanary.PhaseOnly,
			"cities":    poolcanary.PhaseOnly,
			"contexts":  poolcanary.PhaseOnly,

			"coinedStarts": "declared but never referenced",
			"coinedEnds":   "declared but never referenced",

			// Frozen v8..v12 contracts.
			"originDetails":      poolcanary.Frozen,
			"v11GlossaryOpeners": poolcanary.Frozen, "v11QuestionOpeners": poolcanary.Frozen,
			"v11AckLeads": poolcanary.Frozen, "v11AckBodies": poolcanary.Frozen, "v11UnitFrames": poolcanary.Frozen,
			"v11DraftNouns": poolcanary.Frozen, "v11ApprovedNouns": poolcanary.Frozen, "v11PaidNouns": poolcanary.Frozen,
			"v11UnitNouns": poolcanary.Frozen, "v11AdjustmentNouns": poolcanary.Frozen,
			"v12SubjectForms": poolcanary.Frozen, "v12QuestionOpeners": poolcanary.Frozen,
			"v12AckLeads": poolcanary.Frozen, "v12AckBodies": poolcanary.Frozen, "v12UnitFrames": poolcanary.Frozen,
			"v12DraftForms": poolcanary.Frozen, "v12PaidForms": poolcanary.Frozen, "v12ApprovedForms": poolcanary.Frozen,
			"v12AdjustForms": poolcanary.Frozen, "v12LatestForms": poolcanary.Frozen, "v12LargerForms": poolcanary.Frozen,
		},
	})
}
