package universe

import (
	_ "embed"
	"sort"
	"strings"
)

// commonLessonData is a frozen, reviewed CC0 subset. Keeping it in the binary
// gives every validator identical bytes and avoids a mutable runtime API.
//
//go:embed assets/common_lessons.txt
var commonLessonData string

// lessonConcept is one key concept of a lesson for story v2 claim-set grading
// (bench_version >= 13): the canonical term plus the reviewed paraphrases an
// honest reply may use instead. A lesson is graded as the fraction of its key
// concepts present, order-free; the "at least two of N" pass threshold belongs
// to the v13 typed-claim grader (#1523) and the Claims carry Weight for it.
type lessonConcept struct {
	term   string
	accept []string
}

// lessonKeyConcepts is the deterministic reviewed key-concept set per lesson.
// Every story v2 lesson MUST have an entry here; corpus lessons without one are
// dropped from the v13 pool (issue #1841: "deterministic reviewed key sets or
// drop"). Free-prose cluster matching was rejected because it zeroes honest
// rewordings; these sets are the positive vectors, not the boundary.
var lessonKeyConcepts = map[string][]lessonConcept{
	"check who owns it before you promise anything":            {{"owner", []string{"who owns", "ownership", "responsible person", "accountable"}}, {"before promising", []string{"before you promise", "before committing", "before making promises", "prior to any commitment"}}},
	"keep draft numbers separate from approved ones":           {{"draft", []string{"drafts", "estimate", "provisional figures", "working numbers"}}, {"approved", []string{"approval", "signed off", "final figures", "authorised", "authorized"}}, {"separate", []string{"apart", "distinct", "don't mix", "do not mix", "keep them separate"}}},
	"decide who owns the handoff before the deadline":          {{"handoff", []string{"hand-off", "handover", "hand over", "transition"}}, {"owner", []string{"who owns", "assign", "responsible person", "accountable"}}, {"deadline", []string{"due date", "before it is due", "before the date", "cutoff"}}},
	"check the current address before sending anything":        {{"current address", []string{"latest address", "up-to-date address", "current route", "current inbox", "right address"}}, {"before sending", []string{"before you send", "before hitting send", "prior to sending", "before mailing"}}},
	"make sure the story and the paperwork match":              {{"story", []string{"narrative", "the account", "what was said", "personal context"}}, {"paperwork", []string{"records", "the record", "documents", "ledger", "the file"}}, {"match", []string{"line up", "agree", "consistent", "reconcile", "align"}}},
	"put corrections next to the decision they changed":        {{"corrections", []string{"correction", "amendments", "revisions", "the fix"}}, {"next to the decision", []string{"with the decision", "attached to the decision", "beside the decision", "alongside the original", "with the record it changed"}}},
	"trace a promise back to the conversation that created it": {{"promise", []string{"commitment", "what was promised", "the pledge", "undertaking"}}, {"source conversation", []string{"conversation that created it", "where it came from", "original conversation", "its origin", "back to the source"}}},
	"keep the client update consistent with the ledger":        {{"client update", []string{"update to the client", "what we tell the client", "client message", "client note"}}, {"ledger", []string{"the books", "the record", "accounts", "the figures"}}, {"consistent", []string{"match", "in step", "agree", "aligned", "the same"}}},

	// Reviewed corpus subset (CC0 proverbs, see assets/README.md).
	"actions speak louder than words":                    {{"actions", []string{"deeds", "what you do", "doing"}}, {"words", []string{"talk", "saying", "what you say", "promises"}}},
	"bad news travels fast":                              {{"bad news", []string{"problems", "trouble", "a mistake"}}, {"travels fast", []string{"spreads quickly", "gets around", "moves fast", "everyone hears"}}},
	"say what you mean and mean what you say":            {{"say what you mean", []string{"be direct", "speak plainly", "be clear", "clarity"}}, {"mean what you say", []string{"stand by it", "follow through", "keep your word", "sincerity"}}},
	"speak clearly if you speak at all":                  {{"speak clearly", []string{"be clear", "clarity", "plain words", "say it plainly"}}, {"if you speak at all", []string{"or say nothing", "or stay quiet", "otherwise stay silent", "only when it helps"}}},
	"stop beating around the bush":                       {{"beating around the bush", []string{"evasive", "avoiding the point", "hedging", "dancing around it"}}, {"stop", []string{"get to the point", "be direct", "come out with it", "say it"}}},
	"a stitch in time saves nine":                        {{"in time", []string{"early", "promptly", "right away", "sooner", "small fix now"}}, {"saves", []string{"prevents", "avoids", "spares", "less work later"}}},
	"four eyes are better than two":                      {{"four eyes", []string{"second pair of eyes", "second person", "someone else check", "another reviewer"}}, {"better", []string{"catches more", "safer", "improves", "helps"}}},
	"two heads are better than one":                      {{"two heads", []string{"two people", "a second opinion", "working together", "another person"}}, {"better than one", []string{"beats one", "better alone", "more than alone", "than going solo"}}},
	"you can't tell a book by its cover":                 {{"cover", []string{"appearance", "surface", "first look", "looks"}}, {"can't tell", []string{"cannot judge", "don't judge", "misleading", "not what it seems"}}},
	"chance favours the prepared mind":                   {{"prepared", []string{"preparation", "be ready", "readiness", "groundwork"}}, {"chance", []string{"luck", "opportunity", "fortune", "openings"}}},
	"a bad workman blames his tools":                     {{"blames", []string{"blaming", "excuses", "points the finger", "blame"}}, {"tools", []string{"equipment", "the kit", "circumstances", "the system"}}},
	"if a job is worth doing it is worth doing well":     {{"worth doing", []string{"if you do it", "worth the effort", "taking it on"}}, {"doing well", []string{"do it properly", "do it right", "done well", "properly"}}},
	"if at first you don't succeed try again":            {{"don't succeed", []string{"fail", "first attempt fails", "doesn't work", "falls flat"}}, {"try again", []string{"retry", "another go", "keep trying", "persist"}}},
	"many hands make light work":                         {{"many hands", []string{"more people", "sharing the work", "help", "a team"}}, {"light work", []string{"easier", "quicker", "less burden", "goes faster"}}},
	"more haste less speed":                              {{"haste", []string{"rushing", "hurrying", "rush", "speeding"}}, {"less speed", []string{"slows you down", "takes longer", "costs time", "backfires"}}},
	"never put off until tomorrow what you can do today": {{"put off", []string{"postpone", "delay", "procrastinate", "defer"}}, {"today", []string{"now", "right away", "immediately", "do it now"}}},
	"a short cut is often a wrong cut":                   {{"short cut", []string{"shortcut", "shortcuts", "quick route", "quick fix"}}, {"wrong", []string{"backfires", "mistake", "costs more", "goes wrong"}}},
	"the first step is the difficult one":                {{"first step", []string{"starting", "getting started", "beginning", "start"}}, {"difficult", []string{"hardest", "hard part", "toughest", "the hard bit"}}},
	"lost time is never found again":                     {{"lost time", []string{"wasted time", "time lost", "time you waste", "time spent"}}, {"never found", []string{"cannot get back", "gone for good", "unrecoverable", "never returns"}}},
	"step by step one goes far":                          {{"step by step", []string{"small steps", "one step at a time", "incrementally", "gradually"}}, {"goes far", []string{"gets there", "adds up", "long way", "progress"}}},
	"a penny saved is a penny earned":                    {{"saved", []string{"saving", "not spending", "kept", "thrift"}}, {"earned", []string{"earning", "as good as income", "gain", "worth the same"}}},
	"never spend your money before you have it":          {{"spend", []string{"spending", "commit money", "pay out", "budget it"}}, {"before you have it", []string{"before it arrives", "not yet received", "money you don't have", "until it lands"}}},
	"waste not want not":                                 {{"waste", []string{"wasting", "squander", "throw away", "wastefulness"}}, {"want", []string{"go short", "run out", "lack", "need later"}}},
	"better safe than sorry":                             {{"safe", []string{"caution", "careful", "cautious", "play it safe"}}, {"sorry", []string{"regret", "regretting it", "pay for it later", "wishing you had"}}},
	"better late than never":                             {{"late", []string{"delayed", "behind schedule", "eventually", "even if late"}}, {"never", []string{"not at all", "never doing it", "skipping it", "than not"}}},
	"business before pleasure":                           {{"business", []string{"work", "the job", "duties", "obligations"}}, {"before pleasure", []string{"before fun", "then relax", "first, then enjoy", "before leisure"}}},
	"don't put all your eggs in one basket":              {{"one basket", []string{"single option", "one place", "one plan", "everything on one"}}, {"eggs", []string{"spread the risk", "diversify", "don't rely on one", "back-up"}}},
	"honesty is the best policy":                         {{"honesty", []string{"be honest", "truthful", "telling the truth", "candour", "candor"}}, {"best policy", []string{"pays off", "works best", "always right", "the right course"}}},
	"if anything can go wrong it will":                   {{"go wrong", []string{"fail", "break", "problems", "mistakes"}}, {"it will", []string{"expect it", "plan for it", "assume it", "inevitably"}}},
	"keep it simple":                                     {{"simple", []string{"simplicity", "uncomplicated", "plain", "straightforward"}}, {"keep", []string{"stay", "remain", "don't overcomplicate", "avoid complexity"}}},
	"look before you leap":                               {{"look", []string{"check", "think first", "consider", "assess"}}, {"leap", []string{"act", "jump in", "commit", "rush in"}}},
	"a problem shared is a problem halved":               {{"shared", []string{"talk about it", "share it", "tell someone", "sharing"}}, {"halved", []string{"lighter", "smaller", "easier", "half the weight"}}},
	"give credit where credit is due":                    {{"credit", []string{"recognition", "acknowledge", "acknowledgement", "acknowledgment", "thanks"}}, {"where due", []string{"who earned it", "the right person", "deserved", "rightful"}}},
	"patience is a virtue":                               {{"patience", []string{"patient", "wait", "waiting", "take your time"}}, {"virtue", []string{"pays", "worth it", "a strength", "good"}}},
}

// storyV13Lessons is the story v2 lesson pool: every lesson with a reviewed
// key-concept set, in a stable order so seed draws are reproducible.
var storyV13Lessons []lessonSet

func init() {
	seen := make(map[string]bool, len(storyLessons))
	for _, lesson := range storyLessons {
		seen[strings.ToLower(lesson.canonical)] = true
	}
	for _, line := range strings.Split(commonLessonData, "\n") {
		lesson := strings.TrimSpace(line)
		if lesson == "" || seen[strings.ToLower(lesson)] {
			continue
		}
		seen[strings.ToLower(lesson)] = true
		storyLessons = append(storyLessons, lessonSet{canonical: lesson})
	}
	canonicals := make([]string, 0, len(lessonKeyConcepts))
	for canonical := range lessonKeyConcepts {
		canonicals = append(canonicals, canonical)
	}
	sort.Strings(canonicals)
	for _, canonical := range canonicals {
		set := lessonSet{canonical: canonical, concepts: lessonKeyConcepts[canonical]}
		for _, base := range storyLessons {
			if strings.EqualFold(base.canonical, canonical) {
				set.accept = append([]string(nil), base.accept...)
			}
		}
		storyV13Lessons = append(storyV13Lessons, set)
	}
}
