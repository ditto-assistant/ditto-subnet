package datagen

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// The negative control for the encounter shapes: a template script with no
// judgment step, written for the plain path of each archetype, run against the
// real mock endpoint and graded by the real grader.
//
// The script is generous. It is told the archetype and which tools to call,
// and the values it searches for, so its failures come from judgment rather
// than parsing. It follows the first link a page points to, takes the first
// or last figure the plain path puts in front of it, polls the job id the run
// returned, and trusts the first incident on a status page. That earns full
// credit on the plain variants and fails the trap variants (a notice for
// another plan, a superseded correction, an edit still awaiting approval, an
// automatic retry, a credit note, another region's tracked incident). A
// harness that reads, or asks the decision model to choose among the
// candidates it extracted, passes both.

type encounterScript struct {
	t     *testing.T
	srv   *toolexec.Server
	id    string
	tc    protocol.ToolCase
	reply string
}

func (s *encounterScript) call(name string, args map[string]any) string {
	raw, _ := json.Marshal(args)
	res := serveOnce(s.t, s.srv, s.id, protocol.ObservedToolCall{Name: name, Args: raw})
	if res.Error != "" {
		return "ERROR " + res.Error
	}
	return res.Result
}

var (
	scriptURL     = regexp.MustCompile(`https?://[^\s)]+[^\s).,;]`)
	scriptPerSeat = regexp.MustCompile(`\$([\d,]+)(?: per seat|/seat| a seat)`)
	scriptMoney   = regexp.MustCompile(`\$([\d,]+)`)
	scriptSeats   = regexp.MustCompile(`(\d+) seats`)
	scriptDate    = regexp.MustCompile(`(January|February|March|April|May|June|July|August|September|October|November|December) (\d+)`)
	scriptBy      = regexp.MustCompile(`(?:approved by|signed off by|awaiting approval from|by) ([A-Z][a-z]+ [A-Z][a-z]+)`)
	scriptJob     = regexp.MustCompile(`job-\d{5}`)
	scriptEmail   = regexp.MustCompile(`[a-z.]+@[a-z]+\.com`)
	scriptInvoice = regexp.MustCompile(`INV-\d{4}`)
	scriptRef     = regexp.MustCompile(`\b[A-Z]{3}-\d{3}\b`)
)

func atoi(s string) int {
	n, _ := strconv.Atoi(strings.ReplaceAll(s, ",", ""))
	return n
}

func (s *encounterScript) errata() {
	enc := s.tc.Encounter
	host := enc.Required[0].Contains[0]
	results := s.call("search_web", map[string]any{"queries": []string{host + " pricing"}})
	var pricing string
	for _, u := range scriptURL.FindAllString(results, -1) {
		if strings.Contains(u, "/pricing") && !strings.Contains(u, "notice") {
			pricing = u
			break
		}
	}
	page := s.call("read_links", map[string]any{"urls": []string{pricing}})
	price := 0
	if m := scriptPerSeat.FindStringSubmatch(page[strings.Index(page, ".")+1:]); m != nil {
		price = atoi(m[1])
	}
	for _, u := range scriptURL.FindAllString(page, -1) {
		if strings.Contains(u, "notice") {
			notice := s.call("read_links", map[string]any{"urls": []string{u}})
			if all := scriptPerSeat.FindAllStringSubmatch(notice, -1); len(all) > 0 {
				price = atoi(all[len(all)-1][1])
			}
			break
		}
	}
	seats := atoi(scriptSeats.FindStringSubmatch(s.tc.Prompt)[1])
	s.reply = fmt.Sprintf("The plan is $%d per seat; %d seats cost $%s a year.", price, seats, commaInt(price*seats*12))
}

func (s *encounterScript) reconcile() {
	var records []string
	for _, required := range s.tc.Encounter.Required {
		for _, part := range strings.Split(s.call(required.Tool, map[string]any{"query": "renewal"}), ". ") {
			if strings.Contains(part, "renewal") {
				records = append(records, part)
			}
		}
	}
	best, bestDay := "", -1
	for _, r := range records {
		if m := scriptDate.FindStringSubmatch(r); m != nil && atoi(m[2]) > bestDay {
			best, bestDay = r, atoi(m[2])
		}
	}
	value, who := "", ""
	if m := scriptMoney.FindStringSubmatch(best); m != nil {
		value = m[1]
	}
	if m := scriptBy.FindStringSubmatch(best); m != nil {
		who = m[1]
	}
	s.reply = fmt.Sprintf("The current value is $%s, approved by %s.", value, who)
}

func (s *encounterScript) job() {
	enc := s.tc.Encounter
	post := enc.Required[len(enc.Required)-1].Tool
	run := s.call("run_workflow", map[string]any{"name": "workflow"})
	job := scriptJob.FindString(run)
	figure := ""
	for poll := 0; poll < 4 && figure == "" && job != ""; poll++ {
		jobs := s.call("list_agent_jobs", map[string]any{})
		for _, seg := range strings.Split(strings.TrimPrefix(jobs, "Recent jobs: "), "; ") {
			if strings.HasPrefix(seg, job) && strings.Contains(seg, "completed") {
				if m := regexp.MustCompile(`completed, ([\d,]+)`).FindStringSubmatch(seg); m != nil {
					figure = m[1]
					break
				}
			}
		}
	}
	if figure == "" {
		s.reply = "The workflow job did not complete."
		return
	}
	s.call(post, map[string]any{"text": "This run: " + figure})
	s.reply = "Posted " + figure + "."
}

func (s *encounterScript) handoff() {
	enc := s.tc.Encounter
	var memory string
	for _, pair := range s.tc.PrerequisitePairs {
		memory += pair.Prompt + "\n"
	}
	emails := scriptEmail.FindAllString(memory, -1)
	to := emails[len(emails)-1]
	record := s.call(enc.Required[0].Tool, map[string]any{"query": "invoice"})
	money, invoice := scriptMoney.FindStringSubmatch(record), scriptInvoice.FindString(record)
	if money == nil || invoice == "" {
		s.reply = "I could not read the invoice."
		return
	}
	amount := money[1]
	s.call("gmail_send", map[string]any{"to": to, "subject": "Invoice " + invoice, "body": "Balance $" + amount + " for invoice " + invoice})
	s.reply = fmt.Sprintf("Emailed %s: $%s due on %s.", to, amount, invoice)
}

func (s *encounterScript) triage() {
	enc := s.tc.Encounter
	host := enc.Required[0].Contains[0]
	tracker := enc.Required[2].Tool
	write := ""
	for _, rule := range enc.Rules {
		if strings.Contains(rule.Result, "created the item") {
			write = rule.Tool
		}
	}
	results := s.call("search_web", map[string]any{"queries": []string{host + " status"}})
	status := ""
	for _, u := range scriptURL.FindAllString(results, -1) {
		if strings.Contains(u, "status.") {
			status = u
		}
	}
	page := s.call("read_links", map[string]any{"urls": []string{status}})
	incident := regexp.MustCompile(`incident ([A-Z]{3}-\d{4})|Incident ([A-Z]{3}-\d{4})`).FindStringSubmatch(page)
	if incident == nil {
		s.reply = "No incident found on the status page."
		return
	}
	id := incident[1] + incident[2]
	items := s.call(tracker, map[string]any{"query": id})
	if strings.Contains(items, id) && !strings.Contains(items, "no item") {
		s.reply = fmt.Sprintf("Incident %s is already tracked as %s.", id, scriptRef.FindString(items))
		return
	}
	s.call(write, map[string]any{"title": "Outage " + id})
	s.reply = fmt.Sprintf("Filed incident %s.", id)
}

func runEncounterScript(t *testing.T, seed int64, tc protocol.ToolCase) float64 {
	t.Helper()
	srv := toolexec.NewServer()
	srv.Register(tc.ID, toolexec.BuildFixtureForVersion(seed, tc, protocol.BenchVersionV18))
	s := &encounterScript{t: t, srv: srv, id: tc.ID, tc: tc}
	switch tc.Encounter.Archetype {
	case V18EncounterErrata:
		s.errata()
	case V18EncounterReconcile:
		s.reconcile()
	case V18EncounterJob:
		s.job()
	case V18EncounterHandoff:
		s.handoff()
	default:
		s.triage()
	}
	return grade.Encounter(tc.Encounter, srv.Observed(tc.ID), protocol.RunResponse{FinalText: s.reply}).Score
}

// v18EncounterTrapVariants are the variants a plain-path script must fail.
var v18EncounterTrapVariants = map[string]bool{
	v18ErrataOtherPlan: true, v18ErrataSuperseded: true, v18ReconcilePending: true,
	v18JobRetried: true, v18HandoffCredit: true, v18TriageRegion: true,
}

// The plain-path script earns full credit where the plain path is the right
// one (so the control is competent) and fails every trap variant (so the
// shapes need judgment, not a template). 2026-11-07, seeds 1..40: see the
// logged per-variant means.
func TestV18EncountersDefeatAPlainPathScript(t *testing.T) {
	seeds := 40
	if testing.Short() {
		seeds = 8
	}
	sum, n := map[string]float64{}, map[string]int{}
	plainSum, plainN, trapSum, trapN := 0.0, 0, 0.0, 0
	for seed := int64(1); seed <= int64(seeds); seed++ {
		for _, tc := range v18Encounters(t, seed, V18FullToolHalf) {
			score := runEncounterScript(t, seed, tc)
			key := tc.Encounter.Archetype + "/" + tc.Encounter.Variant
			sum[key] += score
			n[key]++
			if v18EncounterTrapVariants[tc.Encounter.Variant] {
				trapSum += score
				trapN++
			} else {
				plainSum += score
				plainN++
			}
		}
	}
	for key := range n {
		t.Logf("%-45s n=%3d script mean %.3f", key, n[key], sum[key]/float64(n[key]))
	}
	plain, trap := plainSum/float64(plainN), trapSum/float64(trapN)
	t.Logf("script: plain variants %.3f over %d, trap variants %.3f over %d, all %.3f",
		plain, plainN, trap, trapN, (plainSum+trapSum)/float64(plainN+trapN))
	if plain < 0.9 {
		t.Fatalf("the control script is not competent on the plain path (%.3f): its trap failures prove nothing", plain)
	}
	if trap > 0.15 {
		t.Fatalf("a plain-path script scores %.3f on the trap variants", trap)
	}
	if all := (plainSum + trapSum) / float64(plainN+trapN); all > 0.65 {
		t.Fatalf("a plain-path script averages %.3f on encounters", all)
	}
}
