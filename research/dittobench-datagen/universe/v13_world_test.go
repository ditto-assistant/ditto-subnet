package universe

import (
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/internal/appearance"
	"github.com/ditto-assistant/dittobench-datagen/internal/publicdata"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var opaqueSessionID = regexp.MustCompile(`^c[0-9a-f]{16}$`)

// TestFrozenWorldBytesAreUnchangedBelowV13 is the #1825/#1827 byte-identity
// guard at the world layer: Generate and GenerateForVersion agree for every
// contract from v8 through v12, so no public vector below v13 can move.
func TestFrozenWorldBytesAreUnchangedBelowV13(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		frozen := Generate(seed, 3)
		for version := protocol.BenchVersionV8; version < protocol.BenchVersionV13; version++ {
			got := GenerateForVersion(seed, 3, version)
			got.BenchVersion = frozen.BenchVersion
			if len(got.Pairs) != len(frozen.Pairs) {
				t.Fatalf("seed %d v%d pair count %d != %d", seed, version, len(got.Pairs), len(frozen.Pairs))
			}
			for i := range frozen.Pairs {
				if got.Pairs[i] != frozen.Pairs[i] {
					t.Fatalf("seed %d v%d pair %d drifted from the frozen world", seed, version, i)
				}
			}
			if got.UserCompany != frozen.UserCompany || got.Accent != frozen.Accent || len(got.People) != len(frozen.People) {
				t.Fatalf("seed %d v%d world identity drifted", seed, version)
			}
		}
	}
}

// TestV13WorldDrawsVocabularyFromPublicCorpora is the "hand pools replaced"
// acceptance criterion: every city, role, employer stem, accent, and font in a
// v13 world is an entry of the frozen public corpora, and the world still
// satisfies the identity invariants the frozen world is held to.
func TestV13WorldDrawsVocabularyFromPublicCorpora(t *testing.T) {
	cities := set(publicdata.AllCities())
	occupations := set(publicdata.AllOccupations())
	stems := set(publicdata.AllOrgStems())
	colors := set(publicdata.AllColors())
	fonts := set(publicdata.AllFonts())
	projectPurposes := set(publicdata.Purposes(publicdata.PurposeProject))
	tripPurposes := set(publicdata.Purposes(publicdata.PurposeTrip))
	legacyCities := set(cities12)
	distinctCities, distinctRoles, distinctRelations, distinctContexts := map[string]bool{}, map[string]bool{}, map[string]bool{}, map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		w := GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		if w.BenchVersion != protocol.BenchVersionV13 {
			t.Fatalf("seed %d world records bench_version %d", seed, w.BenchVersion)
		}
		assertUnique(t, seed, "pair id", w.SortedPairIDs())
		heads := map[string]bool{}
		checkCompany := func(company string) {
			head := strings.Fields(company)[0]
			if !stems[head] {
				t.Fatalf("seed %d organisation %q does not start with a corpus stem", seed, company)
			}
			if heads[strings.ToLower(head)] {
				t.Fatalf("seed %d repeats organisation head %q", seed, head)
			}
			heads[strings.ToLower(head)] = true
		}
		checkCompany(w.UserCompany)
		if !colors[w.Accent] {
			t.Fatalf("seed %d accent %q is not a corpus colour", seed, w.Accent)
		}
		for _, p := range w.People {
			if !cities[p.City] {
				t.Fatalf("seed %d city %q is not in the GeoNames corpus", seed, p.City)
			}
			if !occupations[p.Role] {
				t.Fatalf("seed %d role %q is not an O*NET title", seed, p.Role)
			}
			checkCompany(p.Employer)
			checkCompany(p.PreviousEmployer)
			if !strings.HasSuffix(p.Email, "@"+companyDomain(p.Employer)) || !strings.HasSuffix(p.PreviousEmail, "@"+companyDomain(p.PreviousEmployer)) {
				t.Fatalf("seed %d person %q email domains do not follow their employers", seed, p.Name)
			}
			distinctCities[p.City] = true
			distinctRoles[p.Role] = true
			distinctRelations[p.Relation] = true
			distinctContexts[p.Context] = true
		}
		for _, p := range w.Projects {
			checkCompany(p.Client)
			checkCompany(p.Vendor)
			if !projectNamesAreRelated(p.Name, p.Alias) {
				t.Fatalf("seed %d project alias %q is unrelated to formal name %q", seed, p.Alias, p.Name)
			}
			if !projectPurposes[p.Purpose] {
				t.Fatalf("seed %d project purpose %q is not in the purpose bank", seed, p.Purpose)
			}
		}
		for _, trip := range w.Trips {
			if !tripPurposes[trip.Purpose] {
				t.Fatalf("seed %d trip purpose %q is not in the purpose bank", seed, trip.Purpose)
			}
			for word := range tripAliasForbidden {
				if strings.Contains(" "+trip.Alias+" ", " "+word+" ") {
					t.Fatalf("seed %d trip alias %q conflicts with a purpose-shaped word", seed, trip.Alias)
				}
			}
		}
		choice := appearance.ForSeed(seed)
		for _, preference := range w.Preferences {
			switch preference.Domain {
			case "accent color":
				if preference.Value != choice.Accent || !colors[preference.Value] {
					t.Fatalf("seed %d accent preference %q disagrees with the appearance stream", seed, preference.Value)
				}
				for _, rejected := range preference.Rejected {
					if !colors[rejected] || grade.Hit(rejected, preference.Value) {
						t.Fatalf("seed %d rejected accent %q is not a safe corpus decoy for %q", seed, rejected, preference.Value)
					}
				}
			case "interface font":
				if preference.Value != choice.Font || !fonts[preference.Value] {
					t.Fatalf("seed %d font preference %q disagrees with the appearance stream", seed, preference.Value)
				}
				for _, rejected := range preference.Rejected {
					if !fonts[rejected] || grade.Hit(rejected, preference.Value) {
						t.Fatalf("seed %d rejected font %q is not a safe corpus decoy for %q", seed, rejected, preference.Value)
					}
				}
			}
			if len(preference.Rejected) < 2 {
				t.Fatalf("seed %d preference %s has %d rejected alternatives", seed, preference.Domain, len(preference.Rejected))
			}
		}
	}
	// 40 full worlds x 28 people = 1,120 draws: a twelve-entry hand list would
	// saturate immediately; the corpora must show up as breadth.
	if len(distinctCities) < 400 || len(distinctRoles) < 300 || len(distinctRelations) < 150 || len(distinctContexts) < 400 {
		t.Fatalf("v13 vocabulary breadth too low: cities=%d roles=%d relations=%d contexts=%d", len(distinctCities), len(distinctRoles), len(distinctRelations), len(distinctContexts))
	}
	legacyOnly := 0
	for city := range distinctCities {
		if legacyCities[city] {
			legacyOnly++
		}
	}
	if legacyOnly == len(distinctCities) {
		t.Fatal("v13 cities are still the twelve-entry hand list")
	}
}

// cities12 is the pre-v13 hand list, kept here only to prove v13 left it.
var cities12 = []string{"Baltimore", "Providence", "Montreal", "Lisbon", "Osaka", "Durham", "Edinburgh", "Melbourne", "Chicago", "Nairobi", "Portland", "Valencia"}

// TestV13WorldQuestionsRemainAnswerable proves the corpus vocabulary does not
// break the answerability contract: every scored full profile still yields its
// validated question envelope on the pinned qualification seeds.
func TestV13WorldQuestionsRemainAnswerable(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		w := GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		plans, err := w.QuestionPlans(150)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if len(plans) != 150 {
			t.Fatalf("seed %d plans=%d", seed, len(plans))
		}
		for _, plan := range plans {
			if len(plan.Case.DistractorAnswers) < 1 {
				t.Fatalf("seed %d case %s has no distractors", seed, plan.Case.ID)
			}
			if grade.Hit(plan.Case.ExpectedAnswer, plan.Case.Question) {
				t.Fatalf("seed %d case %s leaks its answer", seed, plan.Case.ID)
			}
		}
	}
	for _, tc := range []struct {
		seed  int64
		scale int
		count int
	}{{356, 3, 150}, {611, 3, 150}, {682, 2, 45}, {123456789, 3, 229}} {
		plans, err := GenerateForVersion(tc.seed, tc.scale, protocol.BenchVersionV13).QuestionPlans(tc.count)
		if err != nil || len(plans) != tc.count {
			t.Fatalf("seed %d scale %d: plans=%d err=%v", tc.seed, tc.scale, len(plans), err)
		}
	}
}

// TestV13WorldSessionIDsAndTimestampsCarryNoLabels is the world-layer half of
// #1827: every pair's session id is an OpaqueCaseID and no timestamp follows
// the frozen 137-hour stride, while chronological order within each entity's
// record chain is preserved for the oracles.
func TestV13WorldSessionIDsAndTimestampsCarryNoLabels(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		w := GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		byID := map[string]protocol.MemoryPair{}
		sessions := map[string]bool{}
		deltas := map[time.Duration]int{}
		var stamps []time.Time
		for _, pair := range w.Pairs {
			if !opaqueSessionID.MatchString(pair.SessionID) {
				t.Fatalf("seed %d pair %s session %q is not opaque", seed, pair.PairID, pair.SessionID)
			}
			if sessions[pair.SessionID] {
				t.Fatalf("seed %d repeats session %q", seed, pair.SessionID)
			}
			sessions[pair.SessionID] = true
			at, err := time.Parse(time.RFC3339, pair.Timestamp)
			if err != nil {
				t.Fatalf("seed %d pair %s timestamp %q: %v", seed, pair.PairID, pair.Timestamp, err)
			}
			if at.Hour() < 8 || at.Hour() >= 18 || at.Weekday() == time.Saturday || at.Weekday() == time.Sunday {
				t.Fatalf("seed %d pair %s timestamp %s is outside business hours", seed, pair.PairID, pair.Timestamp)
			}
			stamps = append(stamps, at)
			byID[pair.PairID] = pair
		}
		for i := 1; i < len(stamps); i++ {
			deltas[stamps[i].Sub(stamps[i-1])]++
		}
		for delta, count := range deltas {
			if count > len(stamps)/20 {
				t.Fatalf("seed %d: %d consecutive pairs share the delta %s (a fixed step survived)", seed, count, delta)
			}
		}
		for _, story := range w.Stories {
			if !opaqueSessionID.MatchString(story.SessionID) || story.SessionID != byID[story.PairID].SessionID {
				t.Fatalf("seed %d story %s session %q leaks or disagrees with its pair", seed, story.ID, story.SessionID)
			}
		}
		before := func(a, b string) {
			if !timeOf(t, byID[a]).Before(timeOf(t, byID[b])) {
				t.Fatalf("seed %d record %s is not before %s", seed, a, b)
			}
		}
		for _, p := range w.People {
			before(p.IdentityPairID, p.WorkPairID)
			before(p.EmailPairID, p.CorrectionPairID)
		}
		for _, p := range w.Projects {
			before(p.LedgerPairID, p.CorrectionPairID)
		}
		for _, trip := range w.Trips {
			before(trip.PlanPairID, trip.CorrectionPairID)
		}
		for _, arc := range w.StoryArcs {
			before(arc.StoryPairIDs[0], arc.StoryPairIDs[1])
			before(arc.StoryPairIDs[1], arc.StoryPairIDs[2])
		}
	}
	// The frozen contract must still carry its labelled ids and stride.
	frozen := Generate(7, 3)
	labelled := 0
	for _, pair := range frozen.Pairs {
		if !opaqueSessionID.MatchString(pair.SessionID) {
			labelled++
		}
	}
	if labelled != len(frozen.Pairs) {
		t.Fatalf("frozen world session ids changed: %d/%d labelled", labelled, len(frozen.Pairs))
	}
}

func timeOf(t *testing.T, pair protocol.MemoryPair) time.Time {
	at, err := time.Parse(time.RFC3339, pair.Timestamp)
	if err != nil {
		t.Fatalf("pair %s timestamp %q: %v", pair.PairID, pair.Timestamp, err)
	}
	return at
}

func set(values []string) map[string]bool {
	out := make(map[string]bool, len(values))
	for _, v := range values {
		out[v] = true
	}
	return out
}
