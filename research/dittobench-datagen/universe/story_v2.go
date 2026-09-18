package universe

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Story v2 arc construction (bench_version >= 13). See story_events.go for the
// catalog and the design note; this file draws the arcs, applies event effects
// to the typed state, folds the timeline into memories and sessions, and
// compiles each memory into a Story object that the shared render path
// (Story.render / renderDraft) turns into prose.

// storyV2MoneyArcCap bounds the money-bearing story quantities per seed
// (issue #1841: money in <= 5 of 13 arcs).
const storyV2MoneyArcCap = 3

// storyV2LessonArcs is the number of arcs whose rotating sixth oracle is the
// lesson claim set instead of a quantity (<= 1 lesson per arc).
func storyV2LessonArcs(arcN int) int {
	switch {
	case arcN >= 13:
		return 4
	case arcN >= 6:
		return 2
	default:
		return 0
	}
}

// storyV2DisagreeArcs is the number of "records disagree" arcs per seed.
func storyV2DisagreeArcs(arcN int) int {
	switch {
	case arcN >= 13:
		return 2
	case arcN >= 6:
		return 1
	default:
		return 0
	}
}

func storyV2BusinessArcs(arcN int) int {
	switch {
	case arcN >= 13:
		return 7
	case arcN >= 6:
		return 4
	default:
		return 1
	}
}

// storyV2Seed is an independent stream: adding or changing story v2 surface
// cannot perturb the people/projects/trips already established by the world.
func storyV2Seed(seed int64) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-stories:%d", seed)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

// storyV2Hex is a deterministic hex source for join keys and coins.
func storyV2Hex(seed int64, salt string, ordinal int) string {
	return protocol.OpaqueCaseID(seed, "world-story-v2-"+salt, ordinal)[1:]
}

// storyV2StatusSurfaces draws the per-seed status vocabulary: one surface term
// per canonical status. Grading accepts this term ∪ the canonical synonyms.
func storyV2StatusSurfaces(r *rand.Rand) map[string]string {
	out := make(map[string]string, len(storyStatusOrder))
	for _, status := range storyStatusOrder {
		terms := storyStatusVocabulary[status]
		out[status] = terms[r.Intn(len(terms))]
	}
	return out
}

type storyV2Builder struct {
	seed      int64
	r         *rand.Rand
	w         World
	surfaces  map[string]string
	usedStems map[string]bool
	usedAlias []string
	moneyArcs int
	// textureSeen dedupes life-texture sentences across every memory of the
	// arc being built, so no two records of one thread share filler.
	textureSeen map[string]bool
}

func (b *storyV2Builder) coinProvider(frames []string) string {
	for {
		stem := familyStarts[b.r.Intn(len(familyStarts))] + familyEnds[b.r.Intn(len(familyEnds))]
		if b.usedStems[strings.ToLower(stem)] {
			continue
		}
		b.usedStems[strings.ToLower(stem)] = true
		return strings.Replace(frames[b.r.Intn(len(frames))], "{coin}", stem, 1)
	}
}

// coinAlias draws a subject alias at least 4 edits from every alias already in
// use, so the anchor-uniqueness property survives 1–3 typo edits.
func (b *storyV2Builder) coinAlias(frames []string) string {
	mods := []string{"elm", "maple", "harbour", "hillside", "orchard", "riverside", "north", "old town", "lakeside", "garden", "quarry", "station", "bramble", "willow", "copper", "ferry", "chapel", "market"}
	for attempt := 0; ; attempt++ {
		mod := mods[b.r.Intn(len(mods))]
		candidate := strings.Replace(frames[b.r.Intn(len(frames))], "{mod}", mod, 1)
		if attempt > 200 {
			candidate = fmt.Sprintf("%s %d", candidate, attempt)
		}
		if b.aliasIsFar(candidate) {
			b.usedAlias = append(b.usedAlias, candidate)
			return candidate
		}
	}
}

func (b *storyV2Builder) aliasIsFar(candidate string) bool {
	for _, used := range b.usedAlias {
		if levenshtein(used, candidate) < 4 {
			return false
		}
	}
	return true
}

// decoyAlias replaces the last word of the arc alias so the near-name thread
// reads as a sibling ("the maple street move" / "the maple street insurance
// claim") while staying >= 7 edits from its own alias and >= 4 from every other
// anchor: after up to 3 typo edits the real alias is still strictly nearer.
func (b *storyV2Builder) decoyAlias(alias string) string {
	tails := []string{"insurance claim", "budget spreadsheet", "photo backup", "warranty paperwork", "calendar invite", "playlist share", "receipt folder", "deposit refund", "parking permit", "reading list"}
	words := strings.Fields(alias)
	for attempt := 0; attempt < 50; attempt++ {
		replaced := append([]string(nil), words...)
		replaced[len(replaced)-1] = tails[b.r.Intn(len(tails))]
		candidate := strings.Join(replaced, " ")
		if b.aliasIsFar(candidate) && levenshtein(alias, candidate) >= 7 {
			b.usedAlias = append(b.usedAlias, candidate)
			return candidate
		}
	}
	candidate := alias + " archive folder"
	b.usedAlias = append(b.usedAlias, candidate)
	return candidate
}

func buildStoriesV2(seed int64, scale int, w World) ([]StoryArc, []Story) {
	b := &storyV2Builder{seed: seed, r: rand.New(rand.NewSource(storyV2Seed(seed))), w: w, usedStems: map[string]bool{}}
	b.surfaces = storyV2StatusSurfaces(b.r)
	for _, company := range worldCompanies(w) {
		b.usedStems[strings.ToLower(strings.Fields(company)[0])] = true
	}
	for _, p := range w.People {
		b.usedStems[strings.ToLower(strings.Fields(p.Name)[len(strings.Fields(p.Name))-1])] = true
	}
	for _, project := range w.Projects {
		b.usedAlias = append(b.usedAlias, project.Alias)
	}

	arcN := storyArcCount(scale)
	businessN := storyV2BusinessArcs(arcN)
	kinds := make([]StoryKind, arcN)
	for i := range kinds {
		kinds[i] = StoryPersonal
		if i < businessN {
			kinds[i] = StoryBusiness
		}
	}
	b.r.Shuffle(len(kinds), func(i, j int) { kinds[i], kinds[j] = kinds[j], kinds[i] })
	slots := b.r.Perm(arcN) // rotation for lesson / disagree / forced-revision slots
	lessonN := storyV2LessonArcs(arcN)
	disagreeN := storyV2DisagreeArcs(arcN)
	themes := append([]StoryEventKind(nil), personalThemes...)
	b.r.Shuffle(len(themes), func(i, j int) { themes[i], themes[j] = themes[j], themes[i] })
	people := b.r.Perm(len(w.People))
	projects := b.r.Perm(len(w.Projects))
	trips := b.r.Perm(len(w.Trips))

	arcs := make([]StoryArc, 0, arcN)
	stories := make([]Story, 0, 6*arcN)
	themeIndex := 0
	for i := 0; i < arcN; i++ {
		kind := kinds[i]
		theme := EventKickoff
		if kind == StoryPersonal {
			theme = themes[themeIndex%len(themes)]
			themeIndex++
		}
		lessonSlot := slots[i] < lessonN
		disagree := slots[i] >= lessonN && slots[i] < lessonN+disagreeN
		forceRevision := slots[i]%2 == 0
		arc, arcStories := b.buildArc(i, kind, theme, people[i%len(people)], projects[i%len(projects)], trips[i%len(trips)], lessonSlot, disagree, forceRevision)
		arcs = append(arcs, arc)
		stories = append(stories, arcStories...)
	}
	return arcs, stories
}

func worldCompanies(w World) []string {
	out := []string{w.UserCompany}
	for _, p := range w.People {
		out = append(out, p.Employer, p.PreviousEmployer)
	}
	for _, p := range w.Projects {
		out = append(out, p.Client, p.Vendor, p.Name)
	}
	return out
}

// pickPerson draws a world person distinct from every excluded index.
func (b *storyV2Builder) pickPerson(excluded ...int) int {
	for {
		candidate := b.r.Intn(len(b.w.People))
		blocked := false
		for _, x := range excluded {
			if x == candidate {
				blocked = true
			}
		}
		if !blocked {
			return candidate
		}
	}
}

// Keep story operands out of ordinary invoice records. Independent random
// streams can still draw identical amounts; that must not make a valid seed
// fail the story-only evidence invariant. Walk deterministically on collision
// without perturbing the RNG stream for unaffected seeds.
func (b *storyV2Builder) privateMoneyCents(candidate, minimum, span int) int {
	for offset := 0; offset < span; offset++ {
		value := minimum + (candidate-minimum+offset)%span
		blocked := false
		for _, p := range b.w.Projects {
			if value == p.OriginalCents || value == p.CorrectedCents || value == p.PaidCents {
				blocked = true
				break
			}
		}
		if !blocked {
			return value
		}
	}
	panic("story money range exhausted")
}

func (b *storyV2Builder) buildArc(index int, kind StoryKind, theme StoryEventKind, personIndex, projectIndex, tripIndex int, lessonSlot, disagree, forceRevision bool) (StoryArc, []Story) {
	r := b.r
	w := b.w
	person := w.People[personIndex]
	v2 := &StoryArcV2{Kind: kind, Theme: theme, Disagree: disagree}

	// Subject and vocabulary.
	var themeBank personalTheme
	providerNoun := "vendor"
	providerFrames := businessProviderFrames
	unit := []string{"seats", "days"}[r.Intn(2)]
	if kind == StoryBusiness {
		project := w.Projects[projectIndex]
		v2.Subject = project.Name
		v2.SubjectAlias = project.Alias
	} else {
		themeBank = personalThemeBank[theme]
		providerNoun = themeBank.provider
		providerFrames = themeBank.providers
		unit = themeBank.unit
		v2.SubjectAlias = b.coinAlias(themeBank.subjects)
		v2.Subject = v2.SubjectAlias
	}
	v2.DecoyAlias = b.decoyAlias(v2.SubjectAlias)

	// Hidden join keys in two distinct shapes.
	shape1 := r.Intn(len(storyJoinKeyShapes))
	shape2 := (shape1 + 1 + r.Intn(len(storyJoinKeyShapes)-1)) % len(storyJoinKeyShapes)
	v2.JoinKey1 = storyJoinKey(r, shape1, storyV2Hex(b.seed, "key1", index))
	v2.JoinKey2 = storyJoinKey(r, shape2, storyV2Hex(b.seed, "key2", index))
	key1Noun, key2Noun := storyJoinKeyShapes[shape1].noun, storyJoinKeyShapes[shape2].noun

	// Money budget: only a business arc with an approval cap can carry money,
	// and only while the per-seed cap allows it.
	wantMoney := kind == StoryBusiness && !lessonSlot && b.moneyArcs < storyV2MoneyArcCap && r.Intn(3) > 0
	var forced []StoryEventKind
	if wantMoney {
		forced = append(forced, EventApprovalCapped)
		b.moneyArcs++
	}
	events := drawStoryEvents(r, kind, theme, forceRevision, disagree, forced)
	v2.Events = events

	// Memories and sessions: root alone, hop alone, the rest split contiguously.
	rest := 0
	for _, e := range events {
		if e.Position >= 2 && e.Kind != EventOutcomeDisputed {
			rest++
		}
	}
	memories := 3 + r.Intn(3)
	if memories-2 > rest {
		memories = rest + 2
	}
	sessions := 2 + r.Intn(memories-1)
	v2.Memories, v2.Sessions = memories, sessions
	// Chunk boundaries for the rest events.
	perChunk := rest / (memories - 2)
	extra := rest % (memories - 2)
	chunkOf := make([]int, len(events))
	chunk, filled := 2, 0
	for i := range events {
		switch {
		case events[i].Kind == EventOutcomeDisputed:
			chunkOf[i] = memories // its own memory and session
		case events[i].Position < 2:
			chunkOf[i] = events[i].Position
		default:
			size := perChunk
			if chunk-2 < extra {
				size++
			}
			if filled >= size && chunk < memories-1 {
				chunk++
				filled = 0
			}
			chunkOf[i] = chunk
			filled++
		}
		events[i].Memory = chunkOf[i]
	}
	totalMemories := memories
	if disagree {
		totalMemories++
	}
	v2.PairIDs = make([]string, totalMemories)
	v2.SessionIDs = make([]string, totalMemories)
	for c := 0; c < totalMemories; c++ {
		v2.PairIDs[c] = protocol.OpaqueCaseID(b.seed, "world-story-v2-pair", index*8+c)
		// The root memory is alone in session 0; every later memory sits in a
		// session >= 1, so any oracle (which always needs the root plus a later
		// record) spans at least two sessions.
		session := 0
		if c >= 1 {
			session = 1 + (c-1)*(sessions-1)/(memories-1)
		}
		if c >= memories {
			session = sessions // the disputed record is its own session
		}
		v2.SessionIDs[c] = protocol.OpaqueCaseID(b.seed, "world-story-v2-session", index*8+session)
	}
	v2.DecoyPairID = protocol.OpaqueCaseID(b.seed, "world-story-v2-decoy", index)

	// Timeline: arc-chronological instants with jitter.
	base := time.Date(2024, 1, 8, 9, 0, 0, 0, time.UTC).Add(time.Duration(20+r.Intn(400)) * 24 * time.Hour)
	eventTimes := make([]time.Time, len(events))
	clock := base
	for i := range events {
		if events[i].Kind != EventOutcomeDisputed {
			// The disputed record shares the outcome's instant (jitter only), so no
			// timestamp ranks the two disagreeing records.
			clock = clock.Add(time.Duration(8+r.Intn(150)) * time.Hour)
		}
		eventTimes[i] = clock
	}
	memoryTime := make([]time.Time, totalMemories)
	for i := range events {
		t := eventTimes[i].Add(time.Duration(r.Intn(180)) * time.Minute)
		if t.After(memoryTime[events[i].Memory]) {
			memoryTime[events[i].Memory] = t
		}
	}

	// Apply effects in timeline order.
	owner := b.pickPerson(personIndex)
	v2.OwnerInitial, v2.Owner = owner, owner
	v2.OwnerHistory = []int{owner}
	provider1 := b.coinProvider(providerFrames)
	provider2 := b.coinProvider(providerFrames)
	v2.Sequence = [2]string{provider1, provider2}
	v2.SequenceNoun = providerNoun
	status := "started"
	if kind == StoryPersonal {
		status = "planned"
	}
	v2.Status = status
	baseQty := 0
	switch unit {
	case "seats":
		baseQty = 20 + r.Intn(140)
	case "days":
		baseQty = 6 + r.Intn(30)
	case "nights":
		baseQty = 2 + r.Intn(9)
	case "percent":
		baseQty = 20 + r.Intn(50)
	}
	deltaQty := 1 + r.Intn(maxInt(2, baseQty/3))
	quantityKind := unit
	if wantMoney {
		quantityKind = "money"
	}
	actionIndex := r.Perm(len(storyNextActions))
	actionCursor := 0
	nextAction := func(who int) StoryNextAction {
		pick := storyNextActions[actionIndex[actionCursor%len(actionIndex)]]
		actionCursor++
		channel := storyChannels[r.Intn(len(storyChannels))]
		return StoryNextAction{Who: who, What: pick.what, WhatAccept: append([]string(nil), pick.accept...), Channel: channel.channel}
	}
	var routeFrom, routeTo string
	slotsBase := map[string]string{
		"alias": v2.SubjectAlias, "subject": v2.Subject, "nick": person.Nickname, "city": person.City, "context": person.Context,
		"key1": v2.JoinKey1, "key1noun": key1Noun, "key2": v2.JoinKey2, "key2noun": key2Noun, "noun": providerNoun,
	}
	quantityStated := false
	for i := range events {
		e := &events[i]
		e.Slots = map[string]string{}
		for k, v := range slotsBase {
			e.Slots[k] = v
		}
		switch e.Kind {
		case EventKickoff, EventMove, EventMedicalCourse, EventSchoolLogistics, EventWedding, EventRenovation, EventBillDispute, EventTripReplan, EventSubscriptionCancelled:
			e.Slots["who"] = w.People[owner].Name
		case EventQuote, EventBooking:
			e.Slots["from"] = provider1
			v2.SequenceMemories[0] = e.Memory
			if kind == StoryBusiness {
				status = "quoted"
			} else {
				status = "booked"
			}
			if quantityKind != "money" {
				e.Slots["qtybase"] = fmt.Sprintf("%d %s", baseQty, unit)
			}
		case EventApprovalCapped:
			status = "approved"
			if wantMoney {
				capCents := b.privateMoneyCents(800_000+r.Intn(4_000_000), 800_000, 4_000_000)
				spentCents := b.privateMoneyCents(100_000+r.Intn(capCents/2), 100_000, capCents/2)
				e.Slots["cap"] = money(capCents)
				e.Slots["spent"] = money(spentCents)
				v2.Quantity = &StoryQuantity{Kind: "money", Value: capCents - spentCents, Base: capCents, Delta: spentCents, Op: "subtract", Memory: e.Memory, Operand: money(capCents), Operand2: money(spentCents)}
				quantityStated = true
			} else {
				capCents := b.privateMoneyCents(800_000+r.Intn(4_000_000), 800_000, 4_000_000)
				e.Slots["cap"] = money(capCents)
				e.Slots["spent"] = money(b.privateMoneyCents(100_000+r.Intn(capCents/2), 100_000, capCents/2))
			}
		case EventContactRouteChanged:
			if routeFrom == "" {
				routeFrom = uniqueEmail("Review Team", v2.Subject, 9000+index*4, true, map[string]bool{})
				routeTo = uniqueEmail("Review Desk", v2.Subject, 9001+index*4, true, map[string]bool{})
			} else {
				routeFrom, routeTo = routeTo, uniqueEmail("Review Desk", v2.Subject, 9002+index*4+i, true, map[string]bool{})
			}
			e.Slots["from"], e.Slots["to"] = routeFrom, routeTo
		case EventVendorSwapped, EventProviderSwapped:
			e.Slots["from"], e.Slots["to"] = provider1, provider2
			v2.SequenceMemories[1] = e.Memory
			if quantityKind != "money" && !hasEvent(events, EventCorrection) {
				op := []string{"add", "subtract"}[r.Intn(2)]
				if unit == "percent" {
					op = "subtract"
				}
				if deltaQty >= baseQty {
					// A subtraction must leave a positive count (a 2-night stay
					// cannot lose 2 nights): the swap adds instead. Every graded
					// StoryQuantity.Value is >= 1 by construction.
					op = "add"
				}
				value := baseQty + deltaQty
				phrase := fmt.Sprintf("their version adds %d %s to the %d %s %s had quoted", deltaQty, unit, baseQty, unit, provider1)
				if op == "subtract" {
					value = baseQty - deltaQty
					phrase = fmt.Sprintf("their version takes %d %s off the %d %s %s had quoted", deltaQty, unit, baseQty, unit, provider1)
				}
				e.Slots["qtyphrase"] = phrase
				v2.Quantity = &StoryQuantity{Kind: unit, Value: value, Base: baseQty, Delta: deltaQty, Op: op, Memory: e.Memory, Operand: fmt.Sprintf("%d %s", baseQty, unit), Operand2: fmt.Sprintf("%d %s", deltaQty, unit)}
				quantityStated = true
			}
		case EventIncident:
			if kind == StoryBusiness {
				status = "on hold"
			} else {
				status = "postponed"
			}
			e.Slots["status"] = b.surfaces[status]
			e.Slots["qtyphrase"] = fmt.Sprintf("the schedule slipped by %d %s", 1+r.Intn(9), []string{"days", "weeks"}[r.Intn(2)])
		case EventCorrection:
			status = "corrected"
			e.Slots["status"] = b.surfaces[status]
			if quantityKind != "money" {
				newQty := baseQty + deltaQty
				if unit == "percent" || r.Intn(2) == 0 {
					newQty = maxInt(1, baseQty-deltaQty)
				}
				e.Slots["qtyphrase"] = fmt.Sprintf("the count is now %d %s, not the %d %s first written down", newQty, unit, baseQty, unit)
				v2.Quantity = &StoryQuantity{Kind: unit, Value: newQty, Base: baseQty, Delta: newQty, Op: "replace", Memory: e.Memory, Operand: fmt.Sprintf("%d %s", newQty, unit), Operand2: fmt.Sprintf("%d %s", baseQty, unit)}
				quantityStated = true
			} else {
				e.Slots["qtyphrase"] = fmt.Sprintf("the count is now %d %s, not the %d %s first written down", baseQty+deltaQty, unit, baseQty, unit)
			}
		case EventHandoffAssigned:
			prev := owner
			owner = b.pickPerson(append([]int{personIndex}, v2.OwnerHistory...)...)
			v2.OwnerHistory = append(v2.OwnerHistory, owner)
			v2.Owner, v2.OwnerMemory = owner, e.Memory
			e.Slots["prev"], e.Slots["who"] = w.People[prev].Name, w.People[owner].Name
			next := nextAction(owner)
			v2.Next, v2.NextMemory = next, e.Memory
			e.Slots["what"], e.Slots["channel"] = next.What, next.Channel
		case EventFollowUp:
			status = "in progress"
			who := owner
			if r.Intn(3) == 0 {
				who = b.pickPerson(personIndex)
			}
			next := nextAction(who)
			v2.Next, v2.NextMemory = next, e.Memory
			e.Slots["who"], e.Slots["what"], e.Slots["channel"] = w.People[who].Name, next.What, next.Channel
		case EventOutcome:
			terminal := businessTerminalStatuses
			if kind == StoryPersonal {
				terminal = themeBank.statuses
			}
			status = terminal[r.Intn(len(terminal))]
			v2.Status, v2.StatusMemory = status, e.Memory
			e.Slots["status"] = b.surfaces[status]
		case EventOutcomeDisputed:
			alt := storyDisagreeStatus(r, v2.Status, kind, themeBank)
			v2.StatusAlt, v2.StatusAltMemory = alt, e.Memory
			e.Slots["status"] = b.surfaces[alt]
			e.Slots["source"] = []string{"the " + providerNoun, w.People[v2.OwnerInitial].Nickname, "the client", "the other note", person.Nickname}[r.Intn(5)]
			if kind == StoryPersonal {
				e.Slots["source"] = []string{"the " + providerNoun, w.People[v2.OwnerInitial].Nickname, person.Nickname, "the confirmation email"}[r.Intn(4)]
			}
		}
		if _, ok := e.Slots["status"]; !ok {
			e.Slots["status"] = b.surfaces[status]
		}
		if len(v2.StatusHistory) == 0 || v2.StatusHistory[len(v2.StatusHistory)-1] != status {
			v2.StatusHistory = append(v2.StatusHistory, status)
		}
		if e.Revision {
			v2.HasRevision = true
		}
	}
	if !quantityStated && !lessonSlot {
		// Every quantity arc must state its operation in a record: fall back to a
		// replacement stated by the swap record when no correction was drawn.
		for i := range events {
			if events[i].Kind == EventVendorSwapped || events[i].Kind == EventProviderSwapped {
				newQty := baseQty + deltaQty
				events[i].Slots["qtyphrase"] = fmt.Sprintf("their version adds %d %s to the %d %s %s had quoted", deltaQty, unit, baseQty, unit, provider1)
				v2.Quantity = &StoryQuantity{Kind: unit, Value: newQty, Base: baseQty, Delta: deltaQty, Op: "add", Memory: events[i].Memory, Operand: fmt.Sprintf("%d %s", baseQty, unit), Operand2: fmt.Sprintf("%d %s", deltaQty, unit)}
				quantityStated = true
				break
			}
		}
	}
	if lessonSlot {
		lesson := storyV13Lessons[r.Intn(len(storyV13Lessons))]
		v2.Lesson = &lesson
		v2.LessonMemory = v2.StatusMemory
		v2.Quantity = nil
	}
	v2.StatusSurface = b.surfaces[v2.Status]
	if v2.Disagree {
		v2.StatusAltSurface = b.surfaces[v2.StatusAlt]
	}

	arc := StoryArc{
		ID: protocol.OpaqueCaseID(b.seed, "world-story-arc", index), PersonIndex: personIndex, ProjectIndex: projectIndex, TripIndex: tripIndex,
		CaseID: v2.JoinKey1, PurchaseOrder: v2.JoinKey2, OriginalContact: routeFrom, CurrentContact: routeTo, V2: v2,
	}
	if v2.Lesson != nil {
		arc.Lesson = v2.Lesson.canonical
		arc.LessonAcceptAny = append([]string(nil), v2.Lesson.accept...)
	}

	// Compile memories.
	b.textureSeen = map[string]bool{}
	stories := make([]Story, 0, totalMemories+1)
	for c := 0; c < totalMemories; c++ {
		stories = append(stories, b.compileMemory(index, c, arc, person, events, memoryTime[c], key1Noun, key2Noun))
	}
	stories = append(stories, b.compileDecoy(index, arc, person, providerFrames, base.Add(time.Duration(r.Intn(600))*time.Hour)))
	return arc, stories
}

func storyDisagreeStatus(r *rand.Rand, status string, kind StoryKind, theme personalTheme) string {
	pool := append([]string(nil), businessTerminalStatuses...)
	pool = append(pool, "on hold", "in progress")
	if kind == StoryPersonal {
		pool = append(append([]string(nil), theme.statuses...), "postponed", "disputed")
	}
	for attempt := 0; attempt < 50; attempt++ {
		candidate := pool[r.Intn(len(pool))]
		if candidate != status && !statusVocabularyOverlaps(candidate, status) {
			return candidate
		}
	}
	for _, candidate := range storyStatusOrder {
		if candidate != status && !statusVocabularyOverlaps(candidate, status) {
			return candidate
		}
	}
	return "disputed"
}

// statusVocabularyOverlaps reports whether two canonical statuses share any
// surface term ("closed"/"completed" both accept "finished"), so neither can be
// a distractor or a disagreeing alternative for the other.
func statusVocabularyOverlaps(a, b string) bool {
	for _, x := range storyStatusVocabulary[a] {
		for _, y := range storyStatusVocabulary[b] {
			if strings.EqualFold(x, y) {
				return true
			}
		}
	}
	return false
}

func hasEvent(events []StoryEvent, kind StoryEventKind) bool {
	for _, e := range events {
		if e.Kind == kind {
			return true
		}
	}
	return false
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

var storyV2OpenerGrammar = persona.Grammar{
	"open": {
		"Catching up on {alias} again, mostly because {nick} asked how it was going.",
		"Another note on {alias}. {nick} will want the details straight, so here they are.",
		"This is the {alias} thread, the one that started with {nick}.",
		"Writing down where {alias} got to before I forget; {nick} keeps asking.",
		"{alias}, continued. Same thread as the conversation with {nick}.",
		"A longer note than usual on {alias}, since {nick} and I talked it through again.",
		"For {nick}'s benefit as much as mine: the state of {alias}.",
		"Back to {alias}. I owe {nick} a proper update, so I am writing it here first.",
		"{nick} and I circled back to {alias} today, hence this record.",
		"Notes on {alias}, prompted by a message from {nick}.",
	},
	"middle": {
		"Here is the sequence as I have it.", "The records, in the order they reached me:", "What actually happened, step by step:",
		"The part that matters, in order:", "Now the actual thread.", "The substance, roughly in order:",
		"Here is what I have written down.", "The records themselves:", "In order, as best I can reconstruct it:",
	},
	"close": {
		"That is where {alias} sits for now.", "I will update this when {alias} moves again.", "Enough on {alias} for one evening.",
		"If {nick} asks, this is the version I will give.", "I would rather have this written down than argue about it later.",
		"Next time {alias} comes up, this is the note to read first.", "Filed, so I stop carrying it around in my head.",
		"That was the useful part of the day.", "More when there is more.", "End of the {alias} note.",
	},
}

var storyV2Titles = []string{"a thread that kept moving", "where things stand", "the messy middle", "a record for later", "what changed and why", "keeping the sequence straight"}

// compileMemory turns one chunk of the timeline into a Story object. Facts are
// the hidden join keys (inserted by the render path after a chosen event) plus
// inline facts already carried by an event sentence, which are declared with no
// renderings so the interior/exclusivity validation still covers them.
func (b *storyV2Builder) compileMemory(arcIndex, chunk int, arc StoryArc, person Person, events []StoryEvent, at time.Time, key1Noun, key2Noun string) Story {
	r := b.r
	v2 := arc.V2
	slots := map[string]string{"alias": v2.SubjectAlias, "nick": person.Nickname, "city": person.City, "context": person.Context, "relation": person.Relation, "employer": person.Employer, "key1noun": key1Noun, "key2noun": key2Noun}
	texture := storyTexture(r, slots, b.textureSeen)

	var middle []string
	var facts []StoryFact
	characters := []StoryCharacter{{Name: person.Name, Role: "the person the thread started with", Relationship: person.Relation}, {Name: "the narrator", Role: "keeper of the record"}}
	var problems []StoryProblem
	var resolutions []StoryResolution
	seenCharacter := map[string]bool{person.Name: true}
	for i, e := range events {
		if e.Memory != chunk {
			continue
		}
		g := storyEventGrammars[e.Kind]
		if isRootEvent(e.Kind) && e.Kind != EventKickoff {
			g = personalRootGrammar
		}
		sentence := storySentence(storyFill(persona.Expand(r, g, "frame"), e.Slots))
		// Swap renderers describe the provider change, but do not contain a
		// quantity slot. Their arithmetic must also reach the wire: recording
		// qtyphrase only in the hidden event state creates an impossible task.
		if (e.Kind == EventVendorSwapped || e.Kind == EventProviderSwapped) && e.Slots["qtyphrase"] != "" {
			sentence += " " + storySentence(e.Slots["qtyphrase"]) + "."
		}
		if base, ok := e.Slots["qtybase"]; ok {
			sentence += " " + storyFill(persona.Expand(r, persona.Grammar{"q": {"The scope on the table is {q}.", "It covers {q} as first written.", "First version: {q}.", "That opening version was for {q}."}}, "q"), map[string]string{"q": base})
		}
		middle = append(middle, sentence)
		eventIndex := len(middle) - 1
		if who, ok := e.Slots["who"]; ok && !seenCharacter[who] {
			seenCharacter[who] = true
			characters = append(characters, StoryCharacter{Name: who, Role: "owner of the thread at this point"})
		}
		problem, resolution := storyEventProblem(e.Kind)
		key := fmt.Sprintf("%s-%d", e.Kind, i)
		problems = append(problems, StoryProblem{Key: key, Description: problem, RaisedIn: "middle"})
		resolutions = append(resolutions, StoryResolution{ProblemKey: key, Action: resolution, Outcome: "recorded against the thread"})
		// Planted facts.
		switch {
		case e.Position == 0:
			facts = append(facts, StoryFact{Key: "join-key-1", Value: v2.JoinKey1, Renderings: storyExpandFact(r, "key1", slots), Phase: "middle", AfterEvent: eventIndex})
		case e.Position == 1:
			facts = append(facts, StoryFact{Key: "join-key-1-link", Value: v2.JoinKey1, Renderings: storyExpandFact(r, "key1link", slots), Phase: "middle", AfterEvent: eventIndex})
			facts = append(facts, StoryFact{Key: "join-key-2", Value: v2.JoinKey2, Renderings: storyExpandFact(r, "key2", slots), Phase: "middle", AfterEvent: eventIndex})
			facts = append(facts, StoryFact{Key: "sequence-first", Value: v2.Sequence[0], Phase: "middle", AfterEvent: eventIndex})
		default:
			if eventIndex == 0 {
				facts = append(facts, StoryFact{Key: "join-key-2-link", Value: v2.JoinKey2, Renderings: storyExpandFact(r, "key2link", slots), Phase: "middle", AfterEvent: 0})
			}
		}
		switch e.Kind {
		case EventVendorSwapped, EventProviderSwapped:
			facts = append(facts, StoryFact{Key: "sequence-second", Value: v2.Sequence[1], Phase: "middle", AfterEvent: eventIndex})
		case EventApprovalCapped:
			if v2.Quantity != nil && v2.Quantity.Kind == "money" && v2.Quantity.Memory == chunk {
				facts = append(facts, StoryFact{Key: "quantity-cap", Value: v2.Quantity.Operand, Phase: "middle", AfterEvent: eventIndex})
				facts = append(facts, StoryFact{Key: "quantity-spent", Value: v2.Quantity.Operand2, Phase: "middle", AfterEvent: eventIndex})
			}
		}
		if v2.Lesson != nil && e.Kind == EventOutcome {
			facts = append(facts, StoryFact{Key: "lesson", Value: v2.Lesson.canonical, Renderings: storyExpandFact(r, "lesson", slots), Phase: "middle", AfterEvent: eventIndex})
		}
	}
	if len(problems) < 2 {
		problems = append(problems, StoryProblem{Key: "which-reference", Description: "two similarly named threads share people and dates", RaisedIn: "beginning"})
		resolutions = append(resolutions, StoryResolution{ProblemKey: "which-reference", Action: "quote the reference on every record", Outcome: "the thread stays distinguishable from its near-namesake"})
	}

	beginning := StorySection{Summary: storySentence(storyFill(persona.Expand(r, storyV2OpenerGrammar, "open"), slots)), Events: []string{texture(), texture()}}
	end := StorySection{Summary: storySentence(storyFill(persona.Expand(r, storyV2OpenerGrammar, "close"), slots)), Events: []string{texture()}}
	// Interleave texture between records so the middle reads as conversation,
	// not a ledger; facts stay attached to their record by index.
	target := 1_900 + r.Intn(400) + r.Intn(400) + r.Intn(300)
	length := func() int {
		n := len(beginning.Summary) + len(end.Summary) + 40
		for _, s := range beginning.Events {
			n += len(s) + 2
		}
		for _, s := range middle {
			n += len(s) + 2
		}
		for _, s := range end.Events {
			n += len(s) + 2
		}
		for _, f := range facts {
			if len(f.Renderings) > 0 {
				n += len(f.Renderings[0]) + len(f.Value)
			}
		}
		return n
	}
	// Keep the middle inside the interior: beginning and end each carry at least
	// a fifth of the bytes, then the remainder pads the middle with texture.
	for i := 0; length() < target; i++ {
		switch {
		case beginningBytes(beginning) < target/5:
			beginning.Events = append(beginning.Events, texture())
		case sectionBytes(end) < target/5:
			end.Events = append(end.Events, texture())
		default:
			// Texture appended AFTER records only, so no fact index moves.
			middle = append(middle, texture())
		}
	}
	story := Story{
		ID: protocol.OpaqueCaseID(b.seed, "world-story-v2-memory", arcIndex*8+chunk), PairID: v2.PairIDs[chunk], SessionID: v2.SessionIDs[chunk],
		Kind: v2.Kind, Domain: storyV2Domain(r, v2.Kind), Title: storyV2Titles[r.Intn(len(storyV2Titles))],
		Beginning: beginning, Middle: StorySection{Summary: storyFill(persona.Expand(r, storyV2OpenerGrammar, "middle"), slots), Events: middle}, End: end,
		Characters: characters, Problems: problems, Resolutions: resolutions,
		Themes: []string{"correction over time", "identity across contexts", string(v2.Kind) + " continuity"},
		Facts:  facts, TargetBytes: target, Timestamp: at.Format(time.RFC3339), ArcIndex: arcIndex,
	}
	if v2.Lesson != nil && chunk == v2.LessonMemory {
		story.LessonsLearned = []string{v2.Lesson.canonical}
	} else {
		story.LessonsLearned = []string{"quote the reference, not the nickname"}
	}
	return story
}

func beginningBytes(s StorySection) int { return sectionBytes(s) }

// storyTexture returns a generator of distinct, well-formed texture sentences:
// life detail and anchor-person context that never carries a graded value.
// seen is shared across the memories of one arc.
func storyTexture(r *rand.Rand, slots map[string]string, seen map[string]bool) func() string {
	if seen == nil {
		seen = map[string]bool{}
	}
	return func() string {
		for attempt := 0; ; attempt++ {
			root := "texture"
			if r.Intn(3) == 0 {
				root = "context"
			}
			sentence := storySentence(storyFill(persona.Expand(r, storyTextureGrammar, root), slots))
			if !seen[sentence] || attempt > 30 {
				seen[sentence] = true
				return sentence
			}
		}
	}
}

// storySentence capitalises the first letter so a slot value at the head of a
// frame ("the maple street move, continued.") reads as a sentence.
func storySentence(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}

func sectionBytes(s StorySection) int {
	n := len(s.Summary)
	for _, e := range s.Events {
		n += len(e) + 2
	}
	return n
}

func storyV2Domain(r *rand.Rand, kind StoryKind) string {
	if kind == StoryBusiness {
		return businessDomains[r.Intn(len(businessDomains))]
	}
	return personalDomains[r.Intn(len(personalDomains))]
}

func storyEventProblem(kind StoryEventKind) (string, string) {
	switch kind {
	case EventQuote, EventBooking:
		return "the first figure arrived before anyone had agreed it", "record it as an opening position, not an approval"
	case EventApprovalCapped:
		return "approval came with a ceiling that was easy to overlook", "write the cap next to the approval"
	case EventContactRouteChanged:
		return "mail kept going to a retired inbox", "replace the route on the invite and the record"
	case EventVendorSwapped, EventProviderSwapped:
		return "the original provider fell through", "record who came first and who replaced them"
	case EventIncident:
		return "a problem stalled the thread", "record the pause and its cause rather than editing history"
	case EventCorrection:
		return "an earlier figure turned out to be wrong", "supersede it explicitly instead of overwriting"
	case EventHandoffAssigned:
		return "ownership moved and nobody wrote it down", "name the new owner and their first action"
	case EventFollowUp:
		return "the next step was implied but not stated", "state who does what over which channel"
	case EventOutcome:
		return "several people held different versions of the ending", "record the current state as of the latest note"
	case EventOutcomeDisputed:
		return "two sources disagree about the state", "record both without pretending they agree"
	default:
		return "a personal conversation turned into a thread with paperwork", "give it a reference and an owner"
	}
}

// compileDecoy builds the spurious near-name thread: a sibling subject with its
// own owner, provider, reference, and status, mentioning the same person, so a
// name-only matcher binds to the wrong thread. It carries none of the arc's
// graded values.
func (b *storyV2Builder) compileDecoy(arcIndex int, arc StoryArc, person Person, providerFrames []string, at time.Time) Story {
	r := b.r
	v2 := arc.V2
	owner := b.pickPerson(append([]int{arc.PersonIndex}, v2.OwnerHistory...)...)
	provider := b.coinProvider(providerFrames)
	shape := r.Intn(len(storyJoinKeyShapes))
	key := storyJoinKey(r, shape, storyV2Hex(b.seed, "decoy-key", arcIndex))
	status := storyDisagreeStatus(r, v2.Status, v2.Kind, personalThemeBank[v2.Theme])
	slots := map[string]string{
		"alias": v2.DecoyAlias, "subject": v2.DecoyAlias, "nick": person.Nickname, "city": person.City, "context": person.Context, "relation": person.Relation, "employer": person.Employer,
		"who": b.w.People[owner].Name, "from": provider, "noun": v2.SequenceNoun, "status": b.surfaces[status],
		"key1noun": storyJoinKeyShapes[shape].noun, "key2noun": storyJoinKeyShapes[shape].noun,
	}
	texture := storyTexture(r, slots, b.textureSeen)
	rootGrammar := storyEventGrammars[EventKickoff]
	hopKind := EventQuote
	if v2.Kind == StoryPersonal {
		rootGrammar = personalRootGrammar
		hopKind = EventBooking
	}
	middle := []string{
		storySentence(storyFill(persona.Expand(r, rootGrammar, "frame"), slots)),
		storySentence(storyFill(persona.Expand(r, storyEventGrammars[hopKind], "frame"), slots)),
		storySentence(storyFill(persona.Expand(r, storyEventGrammars[EventOutcome], "frame"), slots)),
	}
	facts := []StoryFact{{Key: "decoy-key", Value: key, Renderings: storyExpandFact(r, "key1", slots), Phase: "middle", AfterEvent: 0}}
	beginning := StorySection{Summary: storySentence(storyFill(persona.Expand(r, storyV2OpenerGrammar, "open"), slots)), Events: []string{texture(), texture()}}
	end := StorySection{Summary: storySentence(storyFill(persona.Expand(r, storyV2OpenerGrammar, "close"), slots)), Events: []string{texture()}}
	target := 1_850 + r.Intn(500)
	total := func() int {
		n := sectionBytes(beginning) + sectionBytes(end) + len(facts[0].Renderings[0]) + len(key)
		for _, s := range middle {
			n += len(s) + 2
		}
		return n
	}
	for total() < target {
		switch {
		case sectionBytes(beginning) < target/5:
			beginning.Events = append(beginning.Events, texture())
		case sectionBytes(end) < target/5:
			end.Events = append(end.Events, texture())
		default:
			middle = append(middle, texture())
		}
	}
	return Story{
		ID: protocol.OpaqueCaseID(b.seed, "world-story-v2-decoy-memory", arcIndex), PairID: v2.DecoyPairID,
		SessionID: protocol.OpaqueCaseID(b.seed, "world-story-v2-session", arcIndex*8+7),
		Kind:      v2.Kind, Domain: storyV2Domain(r, v2.Kind), Title: storyV2Titles[r.Intn(len(storyV2Titles))],
		Beginning: beginning, Middle: StorySection{Summary: storyFill(persona.Expand(r, storyV2OpenerGrammar, "middle"), slots), Events: middle}, End: end,
		Characters: []StoryCharacter{{Name: person.Name, Role: "the person the thread started with", Relationship: person.Relation}, {Name: b.w.People[owner].Name, Role: "owner of the sibling thread"}},
		Problems: []StoryProblem{
			{Key: "near-name", Description: "a sibling thread shares a name and the same people", RaisedIn: "beginning"},
			{Key: "own-reference", Description: "the sibling needs its own reference", RaisedIn: "middle"},
		},
		Resolutions: []StoryResolution{
			{ProblemKey: "near-name", Action: "keep the sibling thread in its own note", Outcome: "the two threads are not merged"},
			{ProblemKey: "own-reference", Action: "quote the sibling's own reference", Outcome: "records stay attributable"},
		},
		Themes: []string{"identity across contexts", "near-namesakes"}, LessonsLearned: []string{"similar names are not the same thread"},
		Facts: facts, TargetBytes: target, Timestamp: at.Format(time.RFC3339), ArcIndex: arcIndex,
	}
}
