package datagen

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// Bench v16 connector cases (docs/dueling-bench.md section 8.1). Reached only
// from bench_version >= 16; v15 and earlier regenerate byte-identically.
//
// v16 connectors carry no link marker, and each seed links a few scripted apps
// (catalog.V16ConnectorPlanForSeed) whose tools execute in the mock endpoint.
// Harnesses may filter connector tools by marker or by request wording (I7 W4
// allowance), so the difficulty is in these cases, about a fifth of the tool
// cases at every public run size:
//
//   - linked read (connector_linked_result_usage): ask for a figure that only
//     a linked app's read tool serves. Graded as an ordinary result-usage case
//     (trajectory times the needle gate), exactly like the v13 decoy-correct
//     cases. Dropping every connector scores 0.
//   - linked action (connector_linked_action): ask a linked app to perform a
//     write with a quoted payload ("Post "..." to our team Slack."). Graded by
//     the strict trajectory rule with the payload as the required argument.
//   - unlinked probe (connector_unlinked_probe): the same surfaces for an app
//     the seed names but does NOT link. Gold is one attempt at the named tool,
//     which answers "not connected", and a reply telling the user so. The probe
//     is the graded expected call (one call, unpenalised); the reply text is
//     not graded in v16.0 because no grader branch reads it. Link state exists
//     only at runtime, so this surface is word-for-word the linked one.
//   - named product (connector_named_product): the request names an app, linked
//     or not, but the right action is a product tool ("Add the ... we set up in
//     Slack to my calendar"). Calling the named app's tools is an extra call and
//     pays the existing over-call price.
//
// A harness that drops every connector loses the linked cases; one that keys
// connector use on the app named in the request loses the named-product cases
// and must still handle a refused probe on the unlinked ones. No new grader
// branch is needed: every case grades through the v13+ tool grader, the v7
// result-usage composition and the existing over-call rule.

// v16 connector case categories. Each starts with V16ConnectorCategoryPrefix;
// only the linked read is a result-usage category.
const (
	V16ConnectorCategoryPrefix = "connector_"
	V16CategoryLinkedRead      = "connector_linked_result_usage"
	V16CategoryLinkedAction    = "connector_linked_action"
	V16CategoryUnlinkedProbe   = "connector_unlinked_probe"
	V16CategoryNamedProduct    = "connector_named_product"
)

// IsV16ConnectorCase reports whether a category is a v16 connector case.
func IsV16ConnectorCase(category string) bool {
	return strings.HasPrefix(category, V16ConnectorCategoryPrefix)
}

// V16ConnectorQuota is the per-run count of each v16 connector family.
type V16ConnectorQuota struct {
	LinkedRead   int
	LinkedAction int
	Unlinked     int
	NamedProduct int
}

// Total is the number of tool cases the quota converts.
func (q V16ConnectorQuota) Total() int {
	return q.LinkedRead + q.LinkedAction + q.Unlinked + q.NamedProduct
}

// fit trims the quota to n convertible slots one case at a time, rotating
// through the families from named-product to linked read. The rotation is
// deliberate: a short seed keeps every family (decoys included) for as long as
// it can, rather than losing all wording decoys first; the last case kept is a
// linked read.
func (q V16ConnectorQuota) fit(n int) V16ConnectorQuota {
	fields := []*int{&q.NamedProduct, &q.Unlinked, &q.LinkedAction, &q.LinkedRead}
	for k := 0; q.Total() > n; k = (k + 1) % len(fields) {
		if *fields[k] > 0 {
			*fields[k]--
		}
	}
	return q
}

// V16ConnectorQuotaFor is the published v16 quota for a tool-case count:
// 11 of 50 (22%) at full, 5 of 24 (21%) at medium, and one linked read in the
// small smoke run when the seed has a free slot (small runs often have none).
func V16ConnectorQuotaFor(n int) V16ConnectorQuota {
	switch {
	case n >= 40:
		return V16ConnectorQuota{LinkedRead: 3, LinkedAction: 3, Unlinked: 2, NamedProduct: 3}
	case n >= 20:
		return V16ConnectorQuota{LinkedRead: 2, LinkedAction: 1, Unlinked: 1, NamedProduct: 1}
	case n >= 6:
		return V16ConnectorQuota{LinkedRead: 1}
	default:
		return V16ConnectorQuota{}
	}
}

// v16PayloadNouns complete a coined stem into a quoted payload ("Velmora
// rollout checklist"). The stem is per seed and per case, so a payload is
// never a pool value a harness could bake.
var v16PayloadNouns = []string{
	"rollout checklist", "launch notes", "vendor review", "budget sync", "onboarding plan", "retro actions",
	"hiring update", "pricing memo", "partner brief", "migration plan", "QA signoff", "release notes",
	"roadmap draft", "support handoff", "renewal reminder", "design critique", "offsite agenda", "security review",
	"customer recap", "travel plan", "status digest", "contract summary", "workshop outline", "quarterly goals",
}

// v16ProductScripts are the named-product surfaces: %[1]s is the app named as
// context, %[2]s the coined payload, %[3]s a coined email address.
var v16ProductScripts = []struct {
	Tool     string
	ArgKey   string // the required argument ("" = name-only)
	Prompts  []string
	Behavior string
}{
	{Tool: "calendar_create_event", ArgKey: "title", Prompts: []string{
		"Add the %[2]s we set up in %[1]s to my calendar for Thursday at 3pm.",
		"Put \"%[2]s\" on my calendar for next Monday at 10am; the details are already in %[1]s.",
	}, Behavior: "create the calendar event; %s is only where the plan was made"},
	{Tool: "gmail_send", ArgKey: "to", Prompts: []string{
		"Email %[3]s to let them know I wrapped up the %[2]s in %[1]s.",
		"Send %[3]s an email letting them know I finished the %[2]s in %[1]s.",
	}, Behavior: "send the email; %s is only where the work lives"},
	{Tool: "search_web", Prompts: []string{
		"Look up online what %[1]s announced in its latest product update and give me the highlights.",
		"Search the web for %[1]s's newest pricing changes and summarize them for me.",
	}, Behavior: "search the web; %s is the subject of the search, not a tool to call"},
}

func v16Payload(seed int64, index int) string {
	stem := catalog.CoinedStem(seed, fmt.Sprintf("v16-payload-%d", index))
	noun := v16PayloadNouns[v13ToolPick(seed, index, "v16-payload-noun", len(v16PayloadNouns))]
	return strings.ToUpper(stem[:1]) + stem[1:] + " " + noun
}

func v16Email(seed int64, index int) string {
	return catalog.CoinedStem(seed, fmt.Sprintf("v16-email-user-%d", index)) + "@" +
		catalog.CoinedStem(seed, fmt.Sprintf("v16-email-domain-%d", index)) + ".com"
}

// v16ConnectorSlots ranks the cases the v16 pass may replace: the v13
// conversion order (ordinary duplicates, then world cases above the floor,
// then singletons), minus every case a later or earlier pass depends on and
// every case that carries prerequisite pairs (a replacement would drop them).
func v16ConnectorSlots(seed int64, cases []protocol.ToolCase) []int {
	return v16ConnectorSlotsForVersion(seed, protocol.BenchVersionV16, cases)
}

// v16ConnectorSlotsForVersion is v16ConnectorSlots under an explicit contract:
// from v18 the conversion order reads the v18 world floor.
func v16ConnectorSlotsForVersion(seed int64, benchVersion int, cases []protocol.ToolCase) []int {
	referenced := map[string]bool{}
	for _, tc := range cases {
		if tc.RunAfterCaseID != "" {
			referenced[tc.RunAfterCaseID] = true
		}
	}
	free := func(tc protocol.ToolCase) bool {
		return tc.Restraint == nil && tc.EffectAnswer == "" && tc.TwinGroup == "" && tc.RunAfterCaseID == "" &&
			len(tc.ForbiddenTools) == 0 && len(tc.AlternativeExpectedTools) == 0 && len(tc.PrerequisitePairs) == 0 && !referenced[tc.ID] &&
			!IsV13Restraint(tc.Category) && !v13IsUnexpectedFamily(tc.Category) && !IsV16ConnectorCase(tc.Category)
	}
	var out []int
	used := map[int]bool{}
	orderVersion := protocol.BenchVersionV16
	if benchVersion >= protocol.BenchVersionV18 {
		orderVersion = benchVersion
	}
	for _, i := range v13ConversionOrderForVersion(seed, orderVersion, cases) {
		if free(cases[i]) {
			out = append(out, i)
			used[i] = true
		}
	}
	// Second tier: ordinary (non-world) product result-usage cases, which the
	// v13 order never offers. Duplicates first, then singletons. A duplicate is
	// counted over the whole family, eligible or not: the point is that the
	// family keeps at least one case in the run, and an ineligible sibling
	// stays in the run.
	counts := map[string]int{}
	for _, tc := range cases {
		counts[tc.Category]++
	}
	var dup, single []int
	for i, tc := range cases {
		if used[i] || !free(tc) || !IsResultUsage(tc.Category) || v9WorldFamily(tc.Category) ||
			IsDecoyCorrect(tc.Category) || v13DiscoveryFamily(tc.Category) || len(tc.PrerequisitePairs) != 0 || len(tc.ExpectedTools) == 0 {
			continue
		}
		if counts[tc.Category] > 1 {
			dup = append(dup, i)
			counts[tc.Category]--
		} else {
			single = append(single, i)
		}
	}
	return append(append(out, dup...), single...)
}

// applyV16ConnectorBench installs the v16 connector families. It runs after
// every v13 pass, so it never replaces a case those passes built or depend on.
func applyV16ConnectorBench(seed int64, cases []protocol.ToolCase) {
	applyV16ConnectorBenchForVersion(seed, protocol.BenchVersionV16, cases)
}

// applyV16ConnectorBenchForVersion is applyV16ConnectorBench under an explicit
// contract; only the slot order depends on the version.
func applyV16ConnectorBenchForVersion(seed int64, benchVersion int, cases []protocol.ToolCase) {
	quota := V16ConnectorQuotaFor(len(cases))
	if quota.Total() == 0 {
		return
	}
	plan := catalog.V16ConnectorPlanForSeed(seed)
	slots := v16ConnectorSlotsForVersion(seed, benchVersion, cases)
	// The published quota fits every full and medium seed swept by
	// TestV16ConnectorQuotaHoldsAcrossSeeds (at least 13 and 8 free slots for
	// 11 and 5). Generation never fails on a short seed: the quota is trimmed
	// in rotation (fit), and the last case kept is a linked read.
	quota = quota.fit(len(slots))
	next := 0
	take := func() int {
		i := slots[next]
		next++
		return i
	}
	for k := 0; k < quota.LinkedRead; k++ {
		i := take()
		cases[i] = v16LinkedReadCase(seed, cases[i], plan.Linked[k%len(plan.Linked)], i)
	}
	for k := 0; k < quota.LinkedAction; k++ {
		i := take()
		// Offset by one so a two-app plan still exercises both apps' writes.
		cases[i] = v16LinkedActionCase(seed, cases[i], plan.Linked[(k+1)%len(plan.Linked)], i)
	}
	for k := 0; k < quota.Unlinked; k++ {
		i := take()
		cases[i] = v16UnlinkedProbeCase(seed, cases[i], plan.Named[k%len(plan.Named)], k, i)
	}
	for k := 0; k < quota.NamedProduct; k++ {
		i := take()
		// Alternate a linked and a named-unlinked app: naming a linked app is
		// the sharper decoy, since its tools would actually execute.
		app := plan.Linked[(k/2)%len(plan.Linked)]
		if k%2 == 1 {
			app = plan.Named[(k/2)%len(plan.Named)]
		}
		cases[i] = v16NamedProductCase(seed, cases[i], app, k, i)
	}
}

func v16AppProtected(app catalog.ConnectorApp) []string {
	return strings.Fields(app.Name)
}

func v16LinkedReadCase(seed int64, prior protocol.ToolCase, app catalog.ConnectorApp, index int) protocol.ToolCase {
	needle := toolexec.NeedleForVersion(seed, prior.ID, protocol.BenchVersionV16)
	prompts := app.Script.ReadPrompts
	return protocol.ToolCase{
		ID:               prior.ID,
		Category:         V16CategoryLinkedRead,
		Prompt:           fmt.Sprintf(prompts[v13ToolPick(seed, index, "v16-read-prompt", len(prompts))], app.Name, needle.Subject),
		ExpectedTools:    []protocol.ToolSpec{{Name: app.ReadTool}},
		MaxToolCalls:     1,
		ExpectedBehavior: fmt.Sprintf("call %s exactly once and report the figure it serves", app.ReadTool),
		WritingProtected: append(v16AppProtected(app), needle.Subject),
	}
}

func v16LinkedActionCase(seed int64, prior protocol.ToolCase, app catalog.ConnectorApp, index int) protocol.ToolCase {
	payload := v16Payload(seed, index)
	prompts := app.Script.WritePrompts
	return protocol.ToolCase{
		ID:               prior.ID,
		Category:         V16CategoryLinkedAction,
		Prompt:           fmt.Sprintf(prompts[v13ToolPick(seed, index, "v16-write-prompt", len(prompts))], app.Name, payload),
		ExpectedTools:    []protocol.ToolSpec{{Name: app.WriteTool, RequiredArgs: map[string]string{app.WriteParam: payload}}},
		MaxToolCalls:     1,
		ExpectedBehavior: fmt.Sprintf("call %s exactly once with the quoted %s", app.WriteTool, app.WriteParam),
		WritingProtected: append(v16AppProtected(app), strings.Fields(payload)...),
	}
}

// v16UnlinkedProbeCase uses the linked surfaces word for word (write on even
// ordinals, read on odd), so the request alone never says the app is unlinked.
func v16UnlinkedProbeCase(seed int64, prior protocol.ToolCase, app catalog.ConnectorApp, ordinal, index int) protocol.ToolCase {
	tc := protocol.ToolCase{ID: prior.ID, Category: V16CategoryUnlinkedProbe, MaxToolCalls: 1}
	if ordinal%2 == 0 {
		payload := v16Payload(seed, index)
		prompts := app.Script.WritePrompts
		tc.Prompt = fmt.Sprintf(prompts[v13ToolPick(seed, index, "v16-write-prompt", len(prompts))], app.Name, payload)
		tc.ExpectedTools = []protocol.ToolSpec{{Name: app.WriteTool, RequiredArgs: map[string]string{app.WriteParam: payload}}}
		tc.WritingProtected = append(v16AppProtected(app), strings.Fields(payload)...)
		tc.ExpectedBehavior = fmt.Sprintf("try %s once; %s is not linked in this workspace, so tell the user it is not connected and that nothing was done", app.WriteTool, app.Name)
		return tc
	}
	needle := toolexec.NeedleForVersion(seed, prior.ID, protocol.BenchVersionV16)
	prompts := app.Script.ReadPrompts
	tc.Prompt = fmt.Sprintf(prompts[v13ToolPick(seed, index, "v16-read-prompt", len(prompts))], app.Name, needle.Subject)
	tc.ExpectedTools = []protocol.ToolSpec{{Name: app.ReadTool}}
	tc.WritingProtected = append(v16AppProtected(app), needle.Subject)
	tc.ExpectedBehavior = fmt.Sprintf("try %s once; %s is not linked in this workspace, so tell the user it is not connected instead of inventing a figure", app.ReadTool, app.Name)
	return tc
}

func v16NamedProductCase(seed int64, prior protocol.ToolCase, app catalog.ConnectorApp, ordinal, index int) protocol.ToolCase {
	script := v16ProductScripts[ordinal%len(v16ProductScripts)]
	payload := v16Payload(seed, index)
	email := v16Email(seed, index)
	prompt := fmt.Sprintf(script.Prompts[v13ToolPick(seed, index, "v16-product-prompt", len(script.Prompts))], app.Name, payload, email)
	spec := protocol.ToolSpec{Name: script.Tool}
	switch script.ArgKey {
	case "title":
		spec.RequiredArgs = map[string]string{"title": payload}
	case "to":
		spec.RequiredArgs = map[string]string{"to": email}
	}
	protected := append(v16AppProtected(app), strings.Fields(payload)...)
	if script.ArgKey == "to" {
		protected = append(protected, email)
	}
	return protocol.ToolCase{
		ID:               prior.ID,
		Category:         V16CategoryNamedProduct,
		Prompt:           prompt,
		ExpectedTools:    []protocol.ToolSpec{spec},
		MaxToolCalls:     1,
		ExpectedBehavior: fmt.Sprintf("call %s once; ", script.Tool) + fmt.Sprintf(script.Behavior, app.Name),
		WritingProtected: protected,
	}
}
