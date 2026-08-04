package universe

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/textnoise"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// StoryKind is the broad real-life context represented by a long conversation
// memory. Domains provide the finer-grained topic mix inside each kind.
type StoryKind string

const (
	StoryPersonal StoryKind = "personal"
	StoryBusiness StoryKind = "business"
)

// StorySection is the structured state from which story prose is compiled. The
// generator owns meaning first and surface text second: tests and the oracle can
// reason about the beginning, middle, and end without parsing generated prose.
type StorySection struct {
	Summary string   `json:"summary"`
	Events  []string `json:"events"`
}

// StoryFact is one exact, seed-derived fact rendered somewhere inside the story.
// Renderings are semantically equivalent sentence shapes; Value is retained for
// evidence-placement and exclusivity tests, not emitted as separate metadata.
type StoryFact struct {
	Key        string   `json:"key"`
	Value      string   `json:"value"`
	Renderings []string `json:"renderings"`
	Phase      string   `json:"phase"`
	AfterEvent int      `json:"after_event"`
}

// StoryCharacter keeps the people in a long memory explicit before prose is
// compiled. Role describes their place in this story, while Relationship keeps
// the user's own connection to them separate from a business title.
type StoryCharacter struct {
	Name         string `json:"name"`
	Role         string `json:"role"`
	Relationship string `json:"relationship,omitempty"`
}

// StoryProblem and StoryResolution model causal state, not prose templates.
// Each problem key is resolved exactly once so a story can be audited as a
// beginning-to-end sequence before any sentence variation is applied.
type StoryProblem struct {
	Key         string `json:"key"`
	Description string `json:"description"`
	RaisedIn    string `json:"raised_in"`
}

type StoryResolution struct {
	ProblemKey string `json:"problem_key"`
	Action     string `json:"action"`
	Outcome    string `json:"outcome"`
}

// Story is a deterministic story-to-prose program. A story compiles to one
// realistic user/agent MemoryPair, while the structured object keeps its state,
// themes, lessons, and planted facts auditable before prose exists.
type Story struct {
	ID             string            `json:"id"`
	PairID         string            `json:"pair_id"`
	SessionID      string            `json:"session_id"`
	Kind           StoryKind         `json:"kind"`
	Domain         string            `json:"domain"`
	Title          string            `json:"title"`
	Beginning      StorySection      `json:"beginning"`
	Middle         StorySection      `json:"middle"`
	End            StorySection      `json:"end"`
	Characters     []StoryCharacter  `json:"characters"`
	Problems       []StoryProblem    `json:"problems"`
	Resolutions    []StoryResolution `json:"resolutions"`
	Themes         []string          `json:"themes"`
	LessonsLearned []string          `json:"lessons_learned"`
	Facts          []StoryFact       `json:"facts"`
	TargetBytes    int               `json:"target_bytes"`
}

// StoryArc joins a personal origin, a business decision, and a later outcome.
// None of the bridge/decision identities or story-only values are rendered in a
// short memory. Questions enter through personal context and must recover both
// hidden joins before they can apply the final state.
type StoryArc struct {
	ID                  string
	PersonIndex         int
	ProjectIndex        int
	TripIndex           int
	OriginDetail        string
	CaseID              string
	PurchaseOrder       string
	OriginalContact     string
	CurrentContact      string
	BaseBudgetCents     int
	BudgetDeltaCents    int
	PaidCents           int
	UnexpectedCostCents int
	CreditCents         int
	CurrentBalanceCents int
	Lesson              string
	LessonAcceptAny     []string
	StoryPairIDs        [3]string
}

type lessonSet struct {
	canonical string
	accept    []string
}

var storyLessons = []lessonSet{
	{canonical: "check who owns it before you promise anything", accept: []string{"verify the owner before committing", "confirm who owns it before making promises"}},
	{canonical: "keep draft numbers separate from approved ones", accept: []string{"keep draft figures separate from approved numbers", "distinguish drafts from approvals"}},
	{canonical: "decide who owns the handoff before the deadline", accept: []string{"identify the handoff owner before the due date", "assign the handoff before the deadline"}},
	{canonical: "check the current address before sending anything", accept: []string{"check the latest contact route before sending", "confirm the current address before you send"}},
	{canonical: "make sure the story and the paperwork match", accept: []string{"connect the personal context to the work record", "align the story with the business records"}},
	{canonical: "put corrections next to the decision they changed", accept: []string{"keep corrections with the decision", "attach corrections to the decision record"}},
	{canonical: "trace a promise back to the conversation that created it", accept: []string{"follow commitments to their source", "track promises back to the original conversation"}},
	{canonical: "keep the client update consistent with the ledger", accept: []string{"align the client update with the ledger", "keep the client story and ledger consistent"}},
}

var personalDomains = []string{
	"family and friendship", "community volunteering", "creative practice", "travel and place",
	"health and routines", "learning and mentorship", "neighborhood life", "personal milestones",
}

var businessDomains = []string{
	"client operations", "finance and approvals", "hiring and teams", "partnerships",
	"product launches", "research programs", "events and production", "fundraising",
}

var originDetails = []string{
	"the backup venue plan", "the supplier introduction", "the volunteer rota",
	"the accessibility walkthrough", "the late train home", "the studio key handoff",
	"the rainy setup morning", "the community dinner", "the missing print proofs",
	"the borrowed camera kit", "the revised guest list", "the closing-night cleanup",
	"the shared taxi receipt", "the rehearsal schedule", "the catering mix-up",
	"the last-minute room change",
}

func storyArcCount(scale int) int {
	switch scale {
	case 1:
		return 1
	case 2:
		return 6
	default:
		return 13
	}
}

func buildStories(seed int64, scale int, w World) ([]StoryArc, []Story) {
	r := rand.New(rand.NewSource(storySeed(seed)))
	arcN := storyArcCount(scale)
	people := r.Perm(len(w.People))
	projects := r.Perm(len(w.Projects))
	trips := r.Perm(len(w.Trips))

	seenEmails := map[string]bool{}
	seenAmounts := map[int]bool{}
	for _, p := range w.People {
		seenEmails[p.Email] = true
		seenEmails[p.PreviousEmail] = true
	}
	for _, project := range w.Projects {
		seenAmounts[project.OriginalCents] = true
		seenAmounts[project.CorrectedCents] = true
		seenAmounts[project.PaidCents] = true
		seenAmounts[project.OutstandingCents] = true
	}

	arcs := make([]StoryArc, 0, arcN)
	stories := make([]Story, 0, 3*arcN)
	lessonOffset := r.Intn(len(storyLessons))
	detailOffset := r.Intn(len(originDetails))
	for i := 0; i < arcN; i++ {
		personIndex := people[i%len(people)]
		projectIndex := projects[i%len(projects)]
		tripIndex := trips[i%len(trips)]
		person := w.People[personIndex]
		project := w.Projects[projectIndex]
		trip := w.Trips[tripIndex]

		// This is a project-specific reviewer channel, not the person's ordinary
		// mailbox. A role address is both more believable and avoids encoding the
		// hidden owner identity into a second, unintended lookup path.
		originalContact := uniqueEmail("Review Team", project.Name, 7000+i*2, true, seenEmails)
		currentContact := uniqueEmail("Review Team", project.Name, 7001+i*2, true, seenEmails)
		base := uniqueStoryAmount(r, seenAmounts, 1_200_000, 4_800_000, 137)
		deltaMagnitude := uniqueStoryAmount(r, seenAmounts, 50_000, 450_000, 137)
		delta := deltaMagnitude
		if i%3 == 1 {
			delta = -deltaMagnitude
		}
		paid := uniqueStoryAmount(r, seenAmounts, 175_000, 900_000, 137)
		cost := uniqueStoryAmount(r, seenAmounts, 50_000, 375_000, 137)
		credit := uniqueStoryAmount(r, seenAmounts, 25_000, 250_000, 137)
		if delta-cost+credit == 0 {
			credit = uniqueStoryAmount(r, seenAmounts, credit+137, 250_000, 137)
		}
		balance := base + delta - paid - cost + credit
		if balance <= 250_000 {
			base += 1_000_000
			balance += 1_000_000
		}

		caseID := fmt.Sprintf("CASE-%d-%s", 2025+i%2, strings.ToUpper(protocol.OpaqueCaseID(seed, "world-story-case", i)[:6]))
		purchaseOrder := "PO-" + strings.ToUpper(protocol.OpaqueCaseID(seed, "world-story-purchase-order", i)[:8])
		lesson := storyLessons[(lessonOffset+i)%len(storyLessons)]
		arc := StoryArc{
			ID:                  protocol.OpaqueCaseID(seed, "world-story-arc", i),
			PersonIndex:         personIndex,
			ProjectIndex:        projectIndex,
			TripIndex:           tripIndex,
			OriginDetail:        originDetails[(detailOffset+i)%len(originDetails)],
			CaseID:              caseID,
			PurchaseOrder:       purchaseOrder,
			OriginalContact:     originalContact,
			CurrentContact:      currentContact,
			BaseBudgetCents:     base,
			BudgetDeltaCents:    delta,
			PaidCents:           paid,
			UnexpectedCostCents: cost,
			CreditCents:         credit,
			CurrentBalanceCents: balance,
			Lesson:              lesson.canonical,
			LessonAcceptAny:     append([]string(nil), lesson.accept...),
		}

		for part := 0; part < 3; part++ {
			pairID := protocol.OpaqueCaseID(seed, "world-story-pair", i*3+part)
			arc.StoryPairIDs[part] = pairID
		}
		arcStories := storiesForArc(seed, i, arc, person, project, trip, r)
		stories = append(stories, arcStories...)
		arcs = append(arcs, arc)
	}
	return arcs, stories
}

func storiesForArc(seed int64, index int, arc StoryArc, person Person, project Project, trip Trip, r *rand.Rand) []Story {
	personalDomain := personalDomains[(index+r.Intn(len(personalDomains)))%len(personalDomains)]
	businessDomain := businessDomains[(index+r.Intn(len(businessDomains)))%len(businessDomains)]
	origin := Story{
		ID: protocol.OpaqueCaseID(seed, "world-story-origin", index), PairID: arc.StoryPairIDs[0],
		SessionID: fmt.Sprintf("story-%02d-origin", index), Kind: StoryPersonal, Domain: personalDomain,
		Title: "how a personal conversation became a work thread",
		Beginning: StorySection{
			Summary: fmt.Sprintf("I ran into %s, my %s, during the %s. We had not properly caught up since the %s, so we found a quiet corner and stayed there much longer than either of us expected.", person.Name, person.Relation, person.Context, trip.Alias),
			Events: []string{
				fmt.Sprintf("We started with ordinary life in %s, then laughed about the %s trip and the slightly chaotic %s planning behind it.", person.City, trip.Alias, trip.Purpose),
				fmt.Sprintf("Everyone calls them %s. I do too, and hearing it brought back a dozen small memories at once.", person.Nickname),
				"We traded stories for a while without talking about work at all. It felt good to be somewhere neither of us had an agenda or a calendar open.",
			},
		},
		Middle: StorySection{
			Summary: fmt.Sprintf("Eventually %s showed me a message they had been stuck on for days. As we talked it through, I realized I might know somebody who could help.", person.Nickname),
			Events: []string{
				"At first I thought they were only venting, because two other half-finished ideas came up in the same breath. Then they stopped, scrolled back through a message, and said this one was genuinely blocking them.",
				fmt.Sprintf("%s handed me their phone and we read the exchange together. The details were buried between travel photos, an apology for a late reply, and a long update about mutual friends.", person.Nickname),
				"I said I knew someone who might help, although I made it clear that I was offering an introduction rather than promising a job or speaking for anyone else.",
				fmt.Sprintf("Before I sent anything, we talked about what the %s had meant to both of us and why neither of us wanted a personal favor to become an awkward obligation.", trip.Alias),
				fmt.Sprintf("I asked %s to tell me the problem back in one sentence. That made it easier to separate this request from the other people and projects we had mentioned that afternoon.", person.Nickname),
				"I opened my notes app, wrote down the next step, and promised to check with everyone before sharing names or contact details.",
			},
		},
		End: StorySection{
			Summary: "By the time we left, I had a possible next step and a much better sense of why the request mattered to them. I also felt responsible for keeping the friendship separate from whatever happened afterward at work.",
			Events: []string{
				"I felt protective of the trust behind the introduction. I had offered to help as a friend, and I did not want anybody to mistake that for an approved budget or a done deal.",
				"On the way home I drafted a short message, deleted the overconfident first version, and sent a calmer note asking whether everyone was comfortable being introduced.",
				"Everyone said yes the following morning. Only then did I move the practical details into my work notes and set up the first proper call.",
			},
		},
		Characters: []StoryCharacter{
			{Name: person.Name, Role: "the person who brought up the problem", Relationship: person.Relation},
			{Name: "the narrator", Role: "friend considering an introduction"},
		},
		Problems: []StoryProblem{
			{Key: "blocked-request", Description: arc.OriginDetail, RaisedIn: "middle"},
			{Key: "consent-before-introduction", Description: "the introduction could expose personal contact details before everybody agreed", RaisedIn: "middle"},
		},
		Resolutions: []StoryResolution{
			{ProblemKey: "blocked-request", Action: "restate the request clearly and identify a possible introduction", Outcome: "the request became specific enough to carry into a work conversation"},
			{ProblemKey: "consent-before-introduction", Action: "ask every participant before sharing names or contact details", Outcome: "the introduction moved forward only after everyone agreed"},
		},
		Themes:         []string{"identity across contexts", "personal trust", "careful handoffs"},
		LessonsLearned: []string{"preserve why an introduction mattered"},
		Facts: []StoryFact{
			storyFactAt("middle", 1, "origin-detail", arc.OriginDetail,
				"The message was really about %s, which was the first detail that made the problem concrete.",
				"What we kept circling back to was %s; that was the part they actually needed help untangling.",
				"Underneath the long exchange, the thing they needed help with was %s."),
			storyFactAt("middle", 3, "support-case", arc.CaseID,
				"The forwarded support thread was already filed as case %s, so I copied that case number into my note.",
				"Their message included support case %s, which I added to the note for the introduction.",
				"The issue had already been logged as support case %s, and I kept that number with my follow-up."),
		},
		TargetBytes: storyTargetBytes(r),
	}

	decision := Story{
		ID: protocol.OpaqueCaseID(seed, "world-story-decision", index), PairID: arc.StoryPairIDs[1],
		SessionID: fmt.Sprintf("story-%02d-decision", index), Kind: StoryBusiness, Domain: businessDomain,
		Title: "turning an informal introduction into a project",
		Beginning: StorySection{
			Summary: fmt.Sprintf("A week later we held the first planning call for %s, a %s project for %s. In chat everyone shortened it to %q, which was convenient until another project with a similar name came up.", project.Name, project.Purpose, project.Client, project.Alias),
			Events: []string{
				fmt.Sprintf("The call included two people from %s, our project lead, and someone from %s. We spent the first few minutes working out who was speaking for the client and who was handling the outside work.", project.Client, project.Vendor),
				"The conversation jumped from introductions to dates, then to money, then back to who needed to review the work. I kept a running list because none of it arrived in a tidy order.",
				"The first estimate on the whiteboard changed twice before anyone approved it. I almost pasted the earliest number into the client update before finance stopped me.",
			},
		},
		Middle: StorySection{
			Summary: "Once everyone understood the roles, we worked through the actual setup: where the introduction came from, which decision finance was approving, how much we could spend, and where the first review should go.",
			Events: []string{
				"I started by adding the original support case to the agenda. That reminded me to tell the room how the introduction had happened before we treated it like an ordinary sales lead.",
				"Finance opened a purchase order while we were on the call. I copied it next to the project name because the shorthand in chat was already causing confusion.",
				"The client asked for a shared review inbox rather than anyone's personal address. I added the first address to the invite while they were still deciding who would monitor it.",
				fmt.Sprintf("We then settled the first working budget for the %s. People had mentioned three rough numbers, so I waited until the client and finance both said yes before writing one down.", project.Purpose),
				fmt.Sprintf("Someone called the project %q again while the invoice screen showed %s. We laughed about the names, then checked the payment that had already gone out.", project.Alias, project.Name),
				fmt.Sprintf("Before the call ended, I read back that %s was the client and %s was the vendor. One person had swapped them in their notes, which would have sent both the review and the invoice the wrong way.", project.Client, project.Vendor),
			},
		},
		End: StorySection{
			Summary: "The meeting ran over by twenty minutes, but we finally had enough agreed detail to start. A few things were still provisional, so I resisted the urge to make the recap sound more final than it was.",
			Events: []string{
				"I marked the budget as a starting point rather than a final balance. We already knew there could be later costs, and the payment on the invoice meant the whole amount was no longer available anyway.",
				fmt.Sprintf("My recap kept %s, %s, and %s on separate lines. It looked fussy, but after the earlier mix-up nobody complained.", project.Client, project.Vendor, project.Name),
				"I sent the notes just after the call and closed my laptop. For the first time that week, the introduction felt like a real project rather than a favor I was still trying to explain.",
			},
		},
		Characters: []StoryCharacter{
			{Name: person.Name, Role: "source of the original introduction", Relationship: person.Relation},
			{Name: project.Client, Role: "client"},
			{Name: project.Vendor, Role: "vendor"},
			{Name: "the narrator", Role: "project coordinator"},
		},
		Problems: []StoryProblem{
			{Key: "ambiguous-project-name", Description: "two projects had similar shorthand names", RaisedIn: "beginning"},
			{Key: "unapproved-estimate", Description: "several draft figures circulated before finance approved a budget", RaisedIn: "beginning"},
			{Key: "review-routing", Description: "the client needed a shared review route instead of a personal mailbox", RaisedIn: "middle"},
		},
		Resolutions: []StoryResolution{
			{ProblemKey: "ambiguous-project-name", Action: "tie the shorthand to the formal project and client", Outcome: "the recap kept the project, client, and vendor on separate lines"},
			{ProblemKey: "unapproved-estimate", Action: "wait for finance and the client to approve one amount", Outcome: "the starting budget was recorded only after approval"},
			{ProblemKey: "review-routing", Action: "record the shared review mailbox", Outcome: "the first review route was added to the engagement"},
		},
		Themes:         []string{"governed decisions", "financial state", "contextual identity"},
		LessonsLearned: []string{"keep draft and approved state distinguishable"},
		Facts: []StoryFact{
			storyFactAt("middle", 0, "support-case", arc.CaseID,
				"The support case from the original message was %s, and I put it in the agenda.",
				"I added support case %s to the agenda so the client could connect the call to the original issue.",
				"The agenda linked the introduction to support case %s."),
			storyFactAt("middle", 1, "purchase-order", arc.PurchaseOrder,
				"Finance opened purchase order %s for the approved work.",
				"The purchase order finance created was %s.",
				"The approved work went into procurement as purchase order %s."),
			storyFactAt("middle", 2, "original-contact", arc.OriginalContact,
				"The reviewer route originally written into the decision was %s.",
				"At that point the work-only contact channel on file was %s.",
				"The first review address recorded for the engagement was %s."),
			storyFactAt("middle", 3, "base-budget", money(arc.BaseBudgetCents),
				"The amount everyone finally approved was %s.",
				"The starting budget we agreed on came to %s.",
				"After the rough estimates, the room settled on %s."),
			storyFactAt("middle", 4, "paid", money(arc.PaidCents),
				"Of that envelope, %s had already gone out as the first payment.",
				"The ledger note said the team had already paid %s against the work.",
				"A payment of %s was complete before the follow-up meeting."),
		},
		TargetBytes: storyTargetBytes(r),
	}

	deltaDirection := "increased"
	if arc.BudgetDeltaCents < 0 {
		deltaDirection = "reduced"
	}
	deltaValue := money(absInt(arc.BudgetDeltaCents))
	outcomeKind := StoryBusiness
	if index%2 == 1 {
		outcomeKind = StoryPersonal
	}
	outcome := Story{
		ID: protocol.OpaqueCaseID(seed, "world-story-outcome", index), PairID: arc.StoryPairIDs[2],
		SessionID: fmt.Sprintf("story-%02d-outcome", index), Kind: outcomeKind, Domain: "mixed personal and work follow-up",
		Title: "the correction and what happened afterward",
		Beginning: StorySection{
			Summary: "The following Thursday, the project coordinator called while I was walking between meetings. What sounded like one small update turned into a budget change, a surprise bill, a credit, and a new place to send the review.",
			Events: []string{
				"The street was loud and the call kept cutting out, so I wrote fragments on the back of a receipt. Two follow-up emails arrived before I made it back to my desk.",
				"My first instinct was to edit the old number and reply to the old inbox. Then I noticed that the emails described several different changes, not one replacement record.",
				"One message came from finance, another from the vendor, and the new review address was buried at the bottom of the coordinator's note. I made coffee and started again from the beginning.",
			},
		},
		Middle: StorySection{
			Summary: "I opened the notes from the original call and worked down the updates one by one. That was slower than editing the headline figure, but it stopped me from counting the old payment twice or losing the separate credit.",
			Events: []string{
				"The first thing I checked was the purchase order in the subject line. It matched the approved work from the meeting, so I knew I had the right project despite the familiar shorthand.",
				"Finance's note changed the approved envelope, but said nothing about the invoice that had already been paid. I left the payment where it was and changed only the budget.",
				"The vendor's email added a cost that had not been known during the meeting. It was tempting to fold it into the correction, but the explanation made clear that it was a separate charge.",
				"The client had also issued a credit after a cancelled piece of work. I put it on its own line so nobody would mistake money coming back for another expense.",
				"I drafted the update to the old review inbox, caught myself just before sending, and went back to the coordinator's message for the replacement address.",
				"When the arithmetic finally balanced, I showed it to my boss. They checked the sequence, pointed out the mistake I had nearly made, and gave me one piece of advice for next time.",
			},
		},
		End: StorySection{
			Summary: "I sent the corrected update late that afternoon. The client never saw the wrong figure, but I was embarrassed by how close I had come and relieved that the second pass had caught it.",
			Events: []string{
				"My boss was kind about it, which somehow made the lesson land harder. I wrote their advice on a sticky note beside my monitor instead of pretending the near-miss had not happened.",
				fmt.Sprintf("Later I told %s that their introduction had turned into real work and that the messy part had finally settled. I left out the financial details, but thanked them for trusting me with it.", person.Nickname),
				"That evening I cleared the receipt scraps out of my bag. I kept the sticky note, though; I knew I would need the reminder the next time a simple update arrived in five separate pieces.",
			},
		},
		Characters: []StoryCharacter{
			{Name: person.Name, Role: "person who made the original introduction", Relationship: person.Relation},
			{Name: "the project coordinator", Role: "source of the correction"},
			{Name: "the narrator's boss", Role: "reviewer of the reconciliation"},
		},
		Problems: []StoryProblem{
			{Key: "compound-financial-update", Description: "a budget correction, paid amount, new cost, and credit arrived separately", RaisedIn: "beginning"},
			{Key: "stale-review-route", Description: "the first review inbox was no longer current", RaisedIn: "middle"},
			{Key: "near-miss-client-update", Description: "the draft almost combined the changes incorrectly", RaisedIn: "middle"},
		},
		Resolutions: []StoryResolution{
			{ProblemKey: "compound-financial-update", Action: "reconcile every change against the approved purchase order", Outcome: "the current balance was calculated without double-counting the payment"},
			{ProblemKey: "stale-review-route", Action: "replace the original review inbox with the coordinator's corrected address", Outcome: "the update went to the current review route"},
			{ProblemKey: "near-miss-client-update", Action: "ask a second person to check the sequence", Outcome: "the client received the corrected figure"},
		},
		Themes:         []string{"correction over time", "cross-domain reasoning", "accountable follow-through"},
		LessonsLearned: append([]string{arc.Lesson}, arc.LessonAcceptAny...),
		Facts: []StoryFact{
			storyFactAt("middle", 0, "purchase-order", arc.PurchaseOrder,
				"The subject line named purchase order %s.",
				"The purchase order in the email was %s.",
				"I matched every update to purchase order %s."),
			storyFactAt("middle", 1, "budget-delta", deltaValue,
				fmt.Sprintf("The approved envelope was %s by %%s; this was a delta, not a replacement total.", deltaDirection),
				fmt.Sprintf("Finance %s the existing budget by %%s rather than issuing a new standalone amount.", deltaDirection),
				fmt.Sprintf("The correction changed the earlier envelope by %%s, and the direction was %s.", deltaDirection)),
			storyFactAt("middle", 2, "unexpected-cost", money(arc.UnexpectedCostCents),
				"The separate vendor charge was %s.",
				"That newly confirmed cost came to %s.",
				"The extra expense in the vendor's email was %s."),
			storyFactAt("middle", 3, "credit", money(arc.CreditCents),
				"A distinct credit of %s flowed back into the available amount.",
				"The reconciliation also added a %s credit, separate from the budget correction.",
				"There was a return credit worth %s that needed to be added rather than subtracted."),
			storyFactAt("middle", 4, "current-contact", arc.CurrentContact,
				"The coordinator said to use %s from now on.",
				"The replacement review inbox was %s.",
				"At the bottom of the message, the new review address was %s."),
			storyFactAt("middle", 5, "lesson", arc.Lesson,
				"Before we wrapped, the advice that stuck with me was simple: “%s.”",
				"I was embarrassed that I had nearly sent the wrong update, but the takeaway was useful: %s.",
				"The mess left me with one practical rule for next time: %s."),
		},
		TargetBytes: storyTargetBytes(r),
	}

	return []Story{origin, decision, outcome}
}

func storyFactAt(phase string, afterEvent int, key, value string, renderings ...string) StoryFact {
	return StoryFact{Key: key, Value: value, Renderings: append([]string(nil), renderings...), Phase: phase, AfterEvent: afterEvent}
}

func (s Story) render(seed int64) (string, string) {
	draft, response := s.renderDraft(seed)
	protected := make([]string, 0, len(s.Facts))
	for _, fact := range s.Facts {
		protected = append(protected, fact.Value)
	}
	prompt, _ := textnoise.Project(draft, seed, "story:"+s.ID, textnoise.Options{
		MaxEdits: 6, Grammar: true, Protected: protected,
	})
	return prompt, response
}

func (s Story) renderDraft(seed int64) (string, string) {
	r := rand.New(rand.NewSource(storyRenderSeed(seed, s.ID)))
	var b strings.Builder

	renderSection := func(phase string, section StorySection) {
		if b.Len() > 0 {
			b.WriteString("\n\n")
		}
		b.WriteString(section.Summary)
		for eventIndex, event := range section.Events {
			b.WriteString("\n\n")
			b.WriteString(event)
			for _, fact := range s.Facts {
				if fact.Phase != phase || fact.AfterEvent != eventIndex {
					continue
				}
				b.WriteString(" ")
				rendering := fact.Renderings[r.Intn(len(fact.Renderings))]
				b.WriteString(fmt.Sprintf(rendering, fact.Value))
			}
		}
	}
	renderSection("beginning", s.Beginning)
	renderSection("middle", s.Middle)
	renderSection("end", s.End)

	response := warmResponse(seed, s.PairID,
		"Thanks for the whole story. I’ll remember what connects.",
		"I can feel its shape now — what changed and why.",
		"I’m with you. I’ll remember where it landed.",
	)
	return b.String(), response
}

func (w World) validateStoryPlan(plan QuestionPlan) error {
	if !isStoryOracle(plan.oracleKind) {
		return nil
	}
	if len(plan.RequiredPairIDs) != 3 {
		return fmt.Errorf("story plan requires %d memories, want exactly 3", len(plan.RequiredPairIDs))
	}
	arc := w.StoryArcs[plan.oracleIndex]
	question := strings.ToLower(plan.Case.Question)
	for _, hidden := range []string{arc.CaseID, arc.PurchaseOrder, arc.OriginalContact, arc.CurrentContact} {
		if strings.Contains(question, strings.ToLower(hidden)) {
			return fmt.Errorf("story question leaks hidden join/state value %q", hidden)
		}
	}
	pairs := make(map[string]protocol.MemoryPair, len(w.Pairs))
	storyPair := make(map[string]bool, len(w.Stories))
	stories := make(map[string]Story, len(w.Stories))
	for _, pair := range w.Pairs {
		pairs[pair.PairID] = pair
	}
	for _, story := range w.Stories {
		storyPair[story.PairID] = true
		stories[story.PairID] = story
	}
	kinds := map[StoryKind]bool{}
	for _, pairID := range plan.RequiredPairIDs {
		story, ok := stories[pairID]
		if !ok {
			return fmt.Errorf("story evidence %s is not a story memory", pairID)
		}
		pair := pairs[pairID]
		if len(pair.Prompt)+len(pair.Response) < 1_800 {
			return fmt.Errorf("story evidence %s is only %d bytes", pairID, len(pair.Prompt)+len(pair.Response))
		}
		kinds[story.Kind] = true
		if len(story.Facts) < 2 {
			return fmt.Errorf("story evidence %s has only %d planted facts", pairID, len(story.Facts))
		}
		if err := validateStoryStructure(story); err != nil {
			return fmt.Errorf("story evidence %s: %w", pairID, err)
		}
		for _, fact := range story.Facts {
			pos := strings.Index(pair.Prompt, fact.Value)
			if pos < 0 {
				return fmt.Errorf("story evidence %s omits fact %s=%q", pairID, fact.Key, fact.Value)
			}
			ratio := float64(pos) / float64(len(pair.Prompt))
			if ratio < 0.15 || ratio > 0.85 {
				return fmt.Errorf("story evidence %s places fact %s at %.2f, outside interior", pairID, fact.Key, ratio)
			}
			if strings.Contains(pair.Response, fact.Value) {
				return fmt.Errorf("agent response duplicates story fact %s=%q", fact.Key, fact.Value)
			}
			for _, other := range w.Pairs {
				if storyPair[other.PairID] {
					continue
				}
				if strings.Contains(other.Prompt+" "+other.Response, fact.Value) {
					return fmt.Errorf("story-only fact %s=%q leaks into short memory %s", fact.Key, fact.Value, other.PairID)
				}
			}
		}
	}
	if !kinds[StoryPersonal] || !kinds[StoryBusiness] {
		return fmt.Errorf("story plan does not cross personal and business memories")
	}
	return nil
}

func validateStoryStructure(story Story) error {
	if len(story.Characters) < 2 || len(story.Problems) < 2 || len(story.Resolutions) < 2 {
		return fmt.Errorf("incomplete story object: characters=%d problems=%d resolutions=%d", len(story.Characters), len(story.Problems), len(story.Resolutions))
	}
	for _, character := range story.Characters {
		if strings.TrimSpace(character.Name) == "" || strings.TrimSpace(character.Role) == "" {
			return fmt.Errorf("story has an unnamed or roleless character")
		}
	}
	problems := make(map[string]bool, len(story.Problems))
	for _, problem := range story.Problems {
		if problem.Key == "" || problem.Description == "" || (problem.RaisedIn != "beginning" && problem.RaisedIn != "middle" && problem.RaisedIn != "end") {
			return fmt.Errorf("invalid story problem %+v", problem)
		}
		if problems[problem.Key] {
			return fmt.Errorf("duplicate story problem %q", problem.Key)
		}
		problems[problem.Key] = true
	}
	resolved := make(map[string]bool, len(story.Resolutions))
	for _, resolution := range story.Resolutions {
		if !problems[resolution.ProblemKey] || resolution.Action == "" || resolution.Outcome == "" {
			return fmt.Errorf("invalid story resolution %+v", resolution)
		}
		if resolved[resolution.ProblemKey] {
			return fmt.Errorf("story problem %q is resolved more than once", resolution.ProblemKey)
		}
		resolved[resolution.ProblemKey] = true
	}
	for problem := range problems {
		if !resolved[problem] {
			return fmt.Errorf("story problem %q has no resolution", problem)
		}
	}
	return nil
}

func storyTargetBytes(r *rand.Rand) int {
	// Three independent draws form a deterministic bell-shaped distribution
	// around 2.8KB while retaining a realistic 1.8-3.9KB tail. Story count, not
	// length, is fixed per profile so difficulty stays comparable across seeds.
	return 1_800 + r.Intn(700) + r.Intn(700) + r.Intn(700)
}

func uniqueStoryAmount(r *rand.Rand, seen map[int]bool, min, spread, quantum int) int {
	for attempt := 0; ; attempt++ {
		value := min + (r.Intn(spread/quantum)+attempt)*quantum
		if !seen[value] {
			seen[value] = true
			return value
		}
	}
}

func storySeed(seed int64) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v8-stories:%d", seed)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

func storyRenderSeed(seed int64, id string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v8-story-render:%d:%s", seed, id)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

func absInt(v int) int {
	if v < 0 {
		return -v
	}
	return v
}
