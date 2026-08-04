package universe

import (
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/internal/assistantvoice"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestWorldIsDeterministicAndSeedVarying(t *testing.T) {
	a := Generate(918273, 3)
	b := Generate(918273, 3)
	c := Generate(918274, 3)
	if !reflect.DeepEqual(a, b) {
		t.Fatal("same seed and scale did not reproduce the same world")
	}
	if reflect.DeepEqual(a, c) {
		t.Fatal("adjacent seeds generated an identical world")
	}
}

func TestWorldIdentitiesAreUnambiguous(t *testing.T) {
	for seed := int64(1); seed <= 100; seed++ {
		w := Generate(seed, 3)
		assertUnique(t, seed, "pair id", w.SortedPairIDs())

		var names, nicknames, emails, projectNames, projectAliases, projectRecords, tripAliases []string
		givenCounts := map[string]int{}
		companyHeads := map[string]bool{}
		checkCompany := func(company string) {
			head := strings.ToLower(strings.Fields(company)[0])
			if companyHeads[head] {
				t.Fatalf("seed %d repeats organization head %q", seed, head)
			}
			companyHeads[head] = true
		}
		checkCompany(w.UserCompany)
		for _, p := range w.People {
			names = append(names, p.Name)
			nicknames = append(nicknames, p.Nickname)
			givenCounts[strings.Fields(p.Name)[0]]++
			emails = append(emails, p.Email, p.PreviousEmail)
			if p.Email == p.PreviousEmail {
				t.Fatalf("seed %d person %q has no effective email correction", seed, p.Name)
			}
			if !strings.HasSuffix(p.Email, "@"+companyDomain(p.Employer)) {
				t.Fatalf("seed %d person %q current email %q does not match employer %q", seed, p.Name, p.Email, p.Employer)
			}
			if !strings.HasSuffix(p.PreviousEmail, "@"+companyDomain(p.PreviousEmployer)) {
				t.Fatalf("seed %d person %q previous email %q does not match employer %q", seed, p.Name, p.PreviousEmail, p.PreviousEmployer)
			}
			if p.Employer == p.PreviousEmployer {
				t.Fatalf("seed %d person %q has no employer transition", seed, p.Name)
			}
			checkCompany(p.Employer)
			checkCompany(p.PreviousEmployer)
			if strings.EqualFold(strings.Fields(p.Name)[0], p.Nickname) {
				t.Fatalf("seed %d person %q repeats their given name as nickname", seed, p.Name)
			}
		}
		for _, p := range w.Projects {
			checkCompany(p.Client)
			checkCompany(p.Vendor)
			projectNames = append(projectNames, p.Name)
			projectAliases = append(projectAliases, p.Alias)
			projectRecords = append(projectRecords, p.RecordID)
			if p.OriginalCents == p.CorrectedCents {
				t.Fatalf("seed %d project %q has a no-op invoice correction", seed, p.Alias)
			}
			if p.OutstandingCents != p.CorrectedCents-p.PaidCents {
				t.Fatalf("seed %d project %q has inconsistent ledger math", seed, p.Alias)
			}
			if p.PaidCents%100 != 0 {
				t.Fatalf("seed %d project %q payment %d is not rounded to whole dollars", seed, p.Alias, p.PaidCents)
			}
			if (p.CorrectedCents-p.OriginalCents)%12_500 == 0 {
				t.Fatalf("seed %d project %q retained the artificial $125 correction grid", seed, p.Alias)
			}
			if !projectNamesAreRelated(p.Name, p.Alias) {
				t.Fatalf("seed %d project alias %q is unrelated to formal name %q", seed, p.Alias, p.Name)
			}
		}
		for _, trip := range w.Trips {
			tripAliases = append(tripAliases, trip.Alias)
			if trip.PreviousDays != sum3(trip.OldLegDays) || trip.CurrentDays != sum3(trip.LegDays) {
				t.Fatalf("seed %d trip %q has inconsistent leg totals", seed, trip.Alias)
			}
			if trip.PreviousDays == trip.CurrentDays {
				t.Fatalf("seed %d trip %q has a no-op itinerary correction", seed, trip.Alias)
			}
			for _, forbidden := range []string{"trip", "tour", "museum", "food", "family", "festival", "archive", "research"} {
				if strings.Contains(" "+trip.Alias+" ", " "+forbidden+" ") {
					t.Fatalf("seed %d trip alias %q conflicts with a purpose-shaped word", seed, trip.Alias)
				}
			}
		}
		assertUnique(t, seed, "person name", names)
		assertUnique(t, seed, "person nickname", nicknames)
		assertUnique(t, seed, "email", emails)
		assertUnique(t, seed, "project name", projectNames)
		assertUnique(t, seed, "project alias", projectAliases)
		assertUnique(t, seed, "project record", projectRecords)
		assertUnique(t, seed, "trip alias", tripAliases)
		if seed == 1 {
			hasRepeatedGiven := false
			for _, count := range givenCounts {
				hasRepeatedGiven = hasRepeatedGiven || count > 1
			}
			if !hasRepeatedGiven {
				t.Fatal("full world lacks an intentional repeated common given name")
			}
		}
	}
}

func TestContactCorrectionsNameThePersonAndOnlyStateTheNewAddress(t *testing.T) {
	for seed := int64(1); seed <= 50; seed++ {
		w := Generate(seed, 3)
		pairs := map[string]protocol.MemoryPair{}
		for _, pair := range w.Pairs {
			pairs[pair.PairID] = pair
		}
		for _, p := range w.People {
			body := pairs[p.CorrectionPairID].Prompt
			if !strings.Contains(body, p.Nickname) || !strings.Contains(body, p.Email) {
				t.Fatalf("seed %d correction %q omits person or new address", seed, body)
			}
			if strings.Contains(body, p.PreviousEmail) {
				t.Fatalf("seed %d correction unnecessarily repeats old address: %q", seed, body)
			}
			identity := pairs[p.IdentityPairID].Prompt
			if strings.Count(identity, p.Name) != 1 || !strings.Contains(identity, "calls them “"+p.Nickname+".”") {
				t.Fatalf("seed %d identity has contrived nickname prose: %q", seed, identity)
			}
		}
	}
}

func TestWorldRepresentsShortNotesAndMessyBusinessPastes(t *testing.T) {
	w := Generate(42, 3)
	minLen, maxLen := int(^uint(0)>>1), 0
	for _, pair := range w.Pairs {
		n := len(strings.TrimSpace(pair.Prompt))
		if n < minLen {
			minLen = n
		}
		if n > maxLen {
			maxLen = n
		}
	}
	if minLen > 100 {
		t.Fatalf("shortest user memory is %d bytes; expected terse real-user notes", minLen)
	}
	if maxLen < 1200 || maxLen < 8*minLen {
		t.Fatalf("message lengths do not include a realistic wall-of-text tail: min=%d max=%d", minLen, maxLen)
	}
}

func TestWorldQuestionsHaveThreeNearMissesAndDoNotLeakAnswers(t *testing.T) {
	for _, scale := range []int{1, 2, 3} {
		w := Generate(991+int64(scale), scale)
		for _, c := range w.MemoryCases(24) {
			if len(c.DistractorAnswers) != 3 {
				t.Fatalf("scale %d case %s has %d distractors", scale, c.ID, len(c.DistractorAnswers))
			}
			assertUnique(t, w.Seed, "distractor", c.DistractorAnswers)
			for _, distractor := range c.DistractorAnswers {
				if distractor == c.ExpectedAnswer {
					t.Fatalf("case %s includes its answer as a distractor", c.ID)
				}
			}
			if grade.Hit(c.ExpectedAnswer, c.Question) {
				t.Fatalf("case %s leaks its answer in the question", c.ID)
			}
		}
	}
}

func TestWorldNaturalJoinsForceMultiRowRetrieval(t *testing.T) {
	w := Generate(123456789, 3)
	pairBody := map[string]string{}
	for _, pair := range w.Pairs {
		pairBody[pair.PairID] = pair.Prompt + " " + pair.Response
	}
	for _, project := range w.Projects {
		if !strings.Contains(pairBody[project.ContextPairID], project.RecordID) {
			t.Fatalf("project %s context does not seed record join", project.Alias)
		}
		for _, pairID := range []string{project.LedgerPairID, project.CorrectionPairID} {
			body := pairBody[pairID]
			if strings.Contains(body, project.Alias) || strings.Contains(body, project.Client) || strings.Contains(body, project.Vendor) {
				t.Fatalf("project record %s leaks a query-facing identifier", pairID)
			}
		}
	}
	for _, trip := range w.Trips {
		companion := w.People[trip.Companion]
		for _, pairID := range []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID} {
			body := pairBody[pairID]
			if !strings.Contains(body, companion.Nickname) {
				t.Fatalf("trip memory %s does not use the natural companion join %q", pairID, companion.Nickname)
			}
			for _, synthetic := range []string{"IT-", "itinerary record", "Original itinerary", "Itinerary correction"} {
				if strings.Contains(body, synthetic) {
					t.Fatalf("trip memory %s leaks synthetic planner language %q", pairID, synthetic)
				}
			}
		}
		for _, pairID := range []string{trip.PlanPairID, trip.CorrectionPairID} {
			body := pairBody[pairID]
			if strings.Contains(body, trip.Alias) || strings.Contains(body, trip.Purpose) || strings.Contains(body, trip.When) {
				t.Fatalf("trip record %s leaks a query-facing identifier", pairID)
			}
		}
	}
}

func TestWorldAssistantRepliesSoundLikeACompanion(t *testing.T) {
	w := Generate(123456789, 3)
	for _, pair := range w.Pairs {
		response := strings.TrimSpace(pair.Response)
		if assistantvoice.IsCold(response) {
			t.Fatalf("pair %s uses a cold transactional reply %q", pair.PairID, response)
		}
		if len(response) < 8 {
			t.Fatalf("pair %s reply is too terse to carry a human acknowledgement: %q", pair.PairID, response)
		}
	}
}

func TestWorldTripQuestionsUseExactNumberGrading(t *testing.T) {
	w := Generate(123456789, 3)
	plans, err := w.QuestionPlans(150)
	if err != nil {
		t.Fatal(err)
	}
	for _, plan := range plans {
		if !strings.HasPrefix(plan.Case.QuestionType, "world-trip-") {
			continue
		}
		if plan.Case.AnswerKind != protocol.AnswerNumber {
			t.Fatalf("trip case %s uses tolerant kind %q", plan.Case.ID, plan.Case.AnswerKind)
		}
		if strings.Contains(plan.Case.ExpectedAnswer, " ") {
			t.Fatalf("trip case %s expected answer is not one exact number token: %q", plan.Case.ID, plan.Case.ExpectedAnswer)
		}
		answer, err := strconv.Atoi(plan.Case.ExpectedAnswer)
		if err != nil {
			t.Fatalf("trip case %s expected answer is not numeric: %q", plan.Case.ID, plan.Case.ExpectedAnswer)
		}
		wrong := protocol.RunResponse{Answer: strconv.Itoa(answer + 1)}
		if verdict := grade.Memory(plan.Case, wrong); verdict.Score != 0 {
			t.Fatalf("trip case %s accepted adjacent wrong number %q: %+v", plan.Case.ID, wrong.Answer, verdict)
		}
	}
}

func TestWorldTripFixedGuessCeiling(t *testing.T) {
	counts := map[string]int{}
	total := 0
	for seed := int64(1); seed <= 200; seed++ {
		plans, err := Generate(seed, 3).QuestionPlans(150)
		if err != nil {
			t.Fatal(err)
		}
		for _, plan := range plans {
			if strings.HasPrefix(plan.Case.QuestionType, "world-trip-") {
				counts[plan.Case.ExpectedAnswer]++
				total++
			}
		}
	}
	best := 0
	for _, count := range counts {
		if count > best {
			best = count
		}
	}
	if best*100 >= total*15 {
		t.Fatalf("one fixed number passes %.2f%% of trip cases, want less than 15%%", 100*float64(best)/float64(total))
	}
}

func TestWorldEvidenceDeclarationsMatchOracleDependencies(t *testing.T) {
	w := Generate(123456789, 3)
	plans, err := w.QuestionPlans(150)
	if err != nil {
		t.Fatal(err)
	}
	for _, plan := range plans {
		available := map[string]bool{}
		for _, pairID := range plan.RequiredPairIDs {
			available[pairID] = true
		}
		for _, omitted := range plan.RequiredPairIDs {
			delete(available, omitted)
			if got, ok := w.resolveWithEvidence(plan, available); ok && got == plan.Case.ExpectedAnswer {
				t.Fatalf("case %s still resolves after omitting %s", plan.Case.ID, omitted)
			}
			available[omitted] = true
		}

		broken := plan
		broken.RequiredPairIDs = append([]string(nil), plan.RequiredPairIDs[:len(plan.RequiredPairIDs)-1]...)
		if err := w.validatePlan(broken); err == nil {
			t.Fatalf("case %s accepted incomplete evidence declaration: %v", plan.Case.ID, err)
		}

		switch plan.oracleKind {
		case oracleContactPrevious:
			if available[w.People[plan.oracleIndex].CorrectionPairID] {
				t.Fatalf("previous contact case %s decoratively requires its correction row", plan.Case.ID)
			}
		case oracleProjectLeadPrevious:
			lead := w.People[w.Projects[plan.oracleIndex].Lead]
			if available[lead.CorrectionPairID] {
				t.Fatalf("previous project-lead case %s decoratively requires its correction row", plan.Case.ID)
			}
		case oracleTripChangedLegPrevious:
			trip := w.Trips[plan.oracleIndex]
			if strings.Contains(plan.Case.Question, trip.Countries[changedLeg(trip)]) {
				t.Fatalf("previous trip case %s reveals the corrected leg and bypasses correction evidence", plan.Case.ID)
			}
		}
	}
}

func TestWorldProjectLeadQuestionsRequireTheProjectOwnershipEdge(t *testing.T) {
	w := Generate(123456789, 3)
	plans, err := w.QuestionPlans(150)
	if err != nil {
		t.Fatal(err)
	}
	normalize := func(value string) string {
		words := strings.FieldsFunc(strings.ToLower(value), func(r rune) bool {
			return (r < 'a' || r > 'z') && (r < '0' || r > '9')
		})
		return " " + strings.Join(words, " ") + " "
	}
	var businessWall string
	for _, pair := range w.Pairs {
		if pair.PairID == w.BusinessPairID {
			businessWall = normalize(pair.Prompt + " " + pair.Response)
			break
		}
	}
	if businessWall == "" {
		t.Fatal("missing seeded business wall")
	}
	for _, plan := range plans {
		if plan.oracleKind != oracleProjectLeadCurrent && plan.oracleKind != oracleProjectLeadPrevious {
			continue
		}
		lead := w.People[w.Projects[plan.oracleIndex].Lead]
		question := normalize(plan.Case.Question)
		for label, value := range map[string]string{
			"name": lead.Name, "nickname": lead.Nickname, "relation": lead.Relation,
			"employer": lead.Employer, "role": lead.Role,
		} {
			if strings.Contains(question, normalize(value)) {
				t.Fatalf("case %s leaks owner %s %q and bypasses the project context row", plan.Case.ID, label, value)
			}
		}
		for label, value := range map[string]string{"name": lead.Name, "nickname": lead.Nickname} {
			if strings.Contains(businessWall, normalize(value)) {
				t.Fatalf("business wall leaks owner %s %q and duplicates the project context edge", label, value)
			}
		}
	}
}

func TestWorldProjectContextIsTheOnlySeededOwnershipEdge(t *testing.T) {
	normalize := func(value string) string {
		words := strings.FieldsFunc(strings.ToLower(value), func(r rune) bool {
			return (r < 'a' || r > 'z') && (r < '0' || r > '9')
		})
		return " " + strings.Join(words, " ") + " "
	}
	for seed := int64(1); seed <= 100; seed++ {
		w := Generate(seed, 3)
		for _, project := range w.Projects {
			lead := w.People[project.Lead]
			for _, pair := range w.Pairs {
				if pair.PairID == project.ContextPairID {
					continue
				}
				body := normalize(pair.Prompt + " " + pair.Response)
				mentionsProject := strings.Contains(body, normalize(project.Alias)) ||
					(strings.Contains(body, normalize(project.Client)) && strings.Contains(body, normalize(project.Purpose)))
				mentionsOwner := strings.Contains(body, normalize(lead.Name))
				if mentionsProject && mentionsOwner {
					t.Fatalf("seed %d pair %s duplicates ownership edge for %s -> %s", seed, pair.PairID, project.Alias, lead.Nickname)
				}
			}
		}
	}
}

func TestWorldShortcutFilteringKeepsKnownValidSeedsGeneratable(t *testing.T) {
	for _, tc := range []struct {
		seed  int64
		scale int
		count int
	}{{356, 3, 150}, {611, 3, 150}, {682, 2, 45}} {
		plans, err := Generate(tc.seed, tc.scale).QuestionPlans(tc.count)
		if err != nil {
			t.Fatalf("seed %d scale %d: %v", tc.seed, tc.scale, err)
		}
		if len(plans) != tc.count {
			t.Fatalf("seed %d scale %d plans=%d, want %d", tc.seed, tc.scale, len(plans), tc.count)
		}
	}
}

func TestWorldQuestionPlansAreAnswerableComposedAndLongDistance(t *testing.T) {
	w := Generate(123456789, 3)
	plans, err := w.QuestionPlans(150)
	if err != nil {
		t.Fatal(err)
	}
	if len(plans) != 150 {
		t.Fatalf("plans=%d, want 150", len(plans))
	}
	questions := map[string]bool{}
	for _, plan := range plans {
		minConstraints := 3
		if isStoryOracle(plan.oracleKind) {
			minConstraints = 2
		}
		if len(plan.Facts) < 4 || len(plan.Constraints) < minConstraints || len(plan.Operations) < 2 {
			t.Fatalf("under-composed plan %s: facts=%d constraints=%d operations=%d", plan.Case.ID, len(plan.Facts), len(plan.Constraints), len(plan.Operations))
		}
		if len(plan.RequiredPairIDs) < 3 {
			t.Fatalf("plan %s has only %d planted needles", plan.Case.ID, len(plan.RequiredPairIDs))
		}
		if questions[plan.Case.Question] {
			t.Fatalf("duplicate question %q", plan.Case.Question)
		}
		questions[plan.Case.Question] = true
	}
}

func TestWorldQuestionUnlocksAfterEveryNeedleIsSeeded(t *testing.T) {
	w := Generate(8128, 3)
	plans, err := w.QuestionPlans(150)
	if err != nil {
		t.Fatal(err)
	}
	waves := w.PairWaves(5)
	waveOf := map[string]int{}
	for wave, pairs := range waves {
		for _, pair := range pairs {
			waveOf[pair.PairID] = wave
		}
	}
	for _, plan := range plans {
		unlock := w.UnlockWave(plan, 5)
		for _, id := range plan.RequiredPairIDs {
			if waveOf[id] > unlock {
				t.Fatalf("plan %s unlocks at %d before evidence %s in wave %d", plan.Case.ID, unlock, id, waveOf[id])
			}
		}
	}
}

func assertUnique(t *testing.T, seed int64, label string, values []string) {
	t.Helper()
	seen := map[string]bool{}
	for _, value := range values {
		if seen[value] {
			t.Fatalf("seed %d generated duplicate %s %q", seed, label, value)
		}
		seen[value] = true
	}
}
