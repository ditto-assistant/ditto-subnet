package catalog

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v16 connector surface (docs/dueling-bench.md section 8.1).
//
// v15 ended every connector description with the fixed suffix "Needs the <App>
// connector to be linked in Settings.", and no case ever needed a connector,
// so a harness could drop all 200 with one string filter and the
// production-scale menu added almost no difficulty. Operator ruling
// (2026-11-03): harnesses MAY filter connector tools by a marker or by the
// request wording (an I7 W4 allowance), so v16 does not ban filters; it moves
// the difficulty into the cases:
//
//  1. No marker. A v16 connector description uses the product-tool grammar
//     ("Search message history in the user's Slack workspace.", like "Create
//     an event on the user's Google Calendar."). The suffix is gone and the
//     link-state wording some v15 variants carried ("the connected
//     workspace", "the linked database") is rewritten, so nothing in a tool
//     definition says whether its app is linked.
//  2. Linked connectors. Each seed links V16LinkedMin..V16LinkedMax apps drawn
//     from the scripted set below. Their tools execute against the seed's
//     fixtures like product tools (toolexec); every other connector still
//     answers "not connected". Some tool cases need a linked connector.
//  3. Named-but-unlinked apps. Each seed also names V16NamedUnlinked scripted
//     apps it does NOT link; some cases ask for them, so link state is
//     learned only at runtime.
//
// Every linked and named app has all of its tools in the seed's catalog, so
// presence in the menu never reveals link state either. v15 bytes are
// untouched: the v15 suffix, pick and splice are unchanged and every v16
// lever is reached only through CatalogForSeed at bench_version >= 16.

const (
	// V16LinkedMin and V16LinkedMax bound how many scripted apps a v16 seed
	// links.
	V16LinkedMin = 2
	V16LinkedMax = 4
	// V16NamedUnlinked is how many scripted apps a seed names in cases
	// without linking them.
	V16NamedUnlinked = 2
)

// v16DescriptionRewrites replaces the v15 description variants that named a
// link state with the product-tool grammar. Every other variant is already
// neutral. TestV16ConnectorDescriptionsCarryNoLinkState fails on any variant
// a future pool edit adds without a rewrite.
var v16DescriptionRewrites = map[string]string{
	"List the %[1]s channels in the connected workspace.":      "List the channels of the user's %[1]s workspace.",
	"Show the channels of the linked %[1]s workspace.":         "Show the channels of the user's %[1]s workspace.",
	"Search message history in the connected %[1]s workspace.": "Search message history in the user's %[1]s workspace.",
	"Create a page in the connected %[1]s workspace.":          "Create a page in the user's %[1]s workspace.",
	"Search pages in the connected %[1]s workspace.":           "Search pages in the user's %[1]s workspace.",
	"Open an issue in the connected %[1]s team.":               "Open an issue in the user's %[1]s team.",
	"Create a ticket in a connected %[1]s project.":            "Create a ticket in one of the user's %[1]s projects.",
	"Create a task in a connected %[1]s project.":              "Create a task in one of the user's %[1]s projects.",
	"Run a query against the connected %[1]s org.":             "Run a query against the user's %[1]s org.",
	"List S3 buckets in the connected %[1]s account.":          "List S3 buckets in the user's %[1]s account.",
	"Describe EC2 instances in the connected %[1]s account.":   "Describe EC2 instances in the user's %[1]s account.",
	"Run a read-only query on the connected %[1]s database.":   "Run a read-only query on the user's %[1]s database.",
	"Query the linked %[1]s database.":                         "Query the user's %[1]s database.",
	"List bank accounts linked through %[1]s.":                 "List the bank accounts the user holds in %[1]s.",
}

// V16LinkStatePhrases are the substrings no v16 tool description may carry
// (lowercased match): the v15 suffix and every phrase that states or hints at
// whether a connector is linked.
var V16LinkStatePhrases = []string{
	"connector", "connectors", "connected", "linked", "not linked", "in settings", "needs the",
	"authorize", "authorise", "integration", "requires access", "if available",
}

func connectorDefinitionV16(seed int64, t connectorTool) protocol.ToolDefinition {
	does := t.action.Does[v13Pick(seed, "connector-desc:"+t.name(), len(t.action.Does))]
	if rewrite, ok := v16DescriptionRewrites[does]; ok {
		does = rewrite
	}
	return protocol.ToolDefinition{
		Name:        t.name(),
		Description: fmt.Sprintf(does, t.app.Name),
		Parameters: schema([]string{t.action.Param},
			prop{name: t.action.Param, typ: "string", desc: t.action.ParamDesc}),
	}
}

// ConnectorScript is one app a v16 seed may link or name: the read action a
// linked-connector case asks for a figure from, the write action an action
// case asks to perform, and their case surfaces.
type ConnectorScript struct {
	Slug string
	// Read is the action key whose served result carries the case needle on a
	// linked read case. ReadPrompts take %[1]s = app name, %[2]s = needle
	// subject; ReadResult takes %[1]s = app name, %[2]s = needle sentence.
	Read        string
	ReadPrompts []string
	ReadResult  string
	// Write is the action key an action case performs; its single parameter
	// carries the quoted payload. WritePrompts take %[1]s = app name and
	// %[2]s = payload.
	Write        string
	WritePrompts []string
}

// v16ConnectorScripts are the apps a v16 seed can link or name. Each has a
// read and a write action in the frozen pool (TestV16ScriptsResolveInPool).
var v16ConnectorScripts = []ConnectorScript{
	{Slug: "slack", Read: "search_messages", Write: "post_message",
		ReadPrompts:  []string{"Search our Slack for the latest message about %[2]s and tell me the exact figure someone posted.", "Check the Slack history for %[2]s; what precise number was shared there?"},
		ReadResult:   "%[1]s search, 1 matching message: %[2]s",
		WritePrompts: []string{"Post \"%[2]s\" to our team Slack.", "Send \"%[2]s\" as a Slack message to the team."}},
	{Slug: "notion", Read: "query_database", Write: "create_page",
		ReadPrompts:  []string{"Look up %[2]s in our Notion database and give me the exact number recorded there.", "Query the Notion tracker for %[2]s; I need the precise figure in that row."},
		ReadResult:   "%[1]s database, 1 row: %[2]s",
		WritePrompts: []string{"Create a Notion page titled \"%[2]s\".", "Start a new page in Notion called \"%[2]s\"."}},
	{Slug: "linear", Read: "list_issues", Write: "create_issue",
		ReadPrompts:  []string{"Check my Linear issues for the one tracking %[2]s and tell me the exact figure noted on it.", "Pull up the Linear issue on %[2]s; what precise number does it record?"},
		ReadResult:   "%[1]s issues (1 match): %[2]s",
		WritePrompts: []string{"File a Linear issue titled \"%[2]s\".", "Open a new issue in Linear called \"%[2]s\"."}},
	{Slug: "jira", Read: "search_tickets", Write: "create_ticket",
		ReadPrompts:  []string{"Search Jira for the ticket about %[2]s and tell me the exact figure in its description.", "Find %[2]s in our Jira tickets; what precise number does the ticket give?"},
		ReadResult:   "%[1]s search (1 ticket): %[2]s",
		WritePrompts: []string{"Create a Jira ticket with the summary \"%[2]s\".", "Open a Jira ticket: \"%[2]s\"."}},
	{Slug: "github", Read: "list_pull_requests", Write: "create_issue",
		ReadPrompts:  []string{"Look through our open GitHub pull requests for the one on %[2]s and tell me the exact figure in its description.", "Check GitHub for the open PR about %[2]s; what precise number does it quote?"},
		ReadResult:   "%[1]s pull requests (1 match): %[2]s",
		WritePrompts: []string{"Open a GitHub issue titled \"%[2]s\".", "File \"%[2]s\" as a new issue on GitHub."}},
	{Slug: "asana", Read: "list_tasks", Write: "create_task",
		ReadPrompts:  []string{"Check the Asana task list for %[2]s and tell me the exact figure on that task.", "Find the Asana task about %[2]s; what precise number is written on it?"},
		ReadResult:   "%[1]s tasks (1 match): %[2]s",
		WritePrompts: []string{"Add an Asana task called \"%[2]s\".", "Create a task in Asana: \"%[2]s\"."}},
	{Slug: "trello", Read: "list_boards", Write: "create_card",
		ReadPrompts:  []string{"Look at my Trello boards for the card on %[2]s and tell me the exact figure on it.", "Find %[2]s on our Trello boards; what precise number does the card show?"},
		ReadResult:   "%[1]s boards (1 matching card): %[2]s",
		WritePrompts: []string{"Make a Trello card named \"%[2]s\".", "Add \"%[2]s\" as a card in Trello."}},
	{Slug: "clickup", Read: "list_spaces", Write: "create_task",
		ReadPrompts:  []string{"Check our ClickUp spaces for %[2]s and tell me the exact figure listed.", "Find %[2]s in ClickUp; what precise number does it show?"},
		ReadResult:   "%[1]s spaces (1 match): %[2]s",
		WritePrompts: []string{"Create a ClickUp task named \"%[2]s\".", "Add \"%[2]s\" to ClickUp as a task."}},
	{Slug: "hubspot", Read: "list_deals", Write: "create_deal",
		ReadPrompts:  []string{"Look up the HubSpot deal for %[2]s and tell me the exact figure on it.", "Check our HubSpot pipeline for %[2]s; what precise number does the deal carry?"},
		ReadResult:   "%[1]s deals (1 match): %[2]s",
		WritePrompts: []string{"Create a HubSpot deal named \"%[2]s\".", "Add a new deal in HubSpot called \"%[2]s\"."}},
	{Slug: "salesforce", Read: "query_records", Write: "create_lead",
		ReadPrompts:  []string{"Query Salesforce for %[2]s and tell me the exact figure on the record.", "Pull the Salesforce record on %[2]s; what precise number does it hold?"},
		ReadResult:   "%[1]s query (1 record): %[2]s",
		WritePrompts: []string{"Create a Salesforce lead named \"%[2]s\".", "Add \"%[2]s\" to Salesforce as a new lead."}},
	{Slug: "confluence", Read: "get_page", Write: "create_page",
		ReadPrompts:  []string{"Open the Confluence page on %[2]s and tell me the exact figure it states.", "Read our Confluence page about %[2]s; what precise number is on it?"},
		ReadResult:   "%[1]s page: %[2]s",
		WritePrompts: []string{"Publish a Confluence page titled \"%[2]s\".", "Create a new Confluence page called \"%[2]s\"."}},
	{Slug: "freshdesk", Read: "list_tickets", Write: "create_ticket",
		ReadPrompts:  []string{"Check Freshdesk for the ticket on %[2]s and tell me the exact figure in it.", "Find the Freshdesk ticket about %[2]s; what precise number does it give?"},
		ReadResult:   "%[1]s tickets (1 match): %[2]s",
		WritePrompts: []string{"Open a Freshdesk ticket with the subject \"%[2]s\".", "Create a Freshdesk ticket: \"%[2]s\"."}},
	{Slug: "gitlab", Read: "list_merge_requests", Write: "create_issue",
		ReadPrompts:  []string{"Look through our GitLab merge requests for the one on %[2]s and tell me the exact figure in it.", "Check GitLab for the merge request about %[2]s; what precise number does it quote?"},
		ReadResult:   "%[1]s merge requests (1 match): %[2]s",
		WritePrompts: []string{"Open a GitLab issue titled \"%[2]s\".", "File \"%[2]s\" as an issue in GitLab."}},
	{Slug: "monday", Read: "list_boards", Write: "create_item",
		ReadPrompts:  []string{"Check my monday.com boards for %[2]s and tell me the exact figure on that item.", "Find %[2]s on our monday.com board; what precise number does the item show?"},
		ReadResult:   "%[1]s boards (1 matching item): %[2]s",
		WritePrompts: []string{"Add a monday.com item named \"%[2]s\".", "Create \"%[2]s\" as an item on monday.com."}},
	{Slug: "basecamp", Read: "list_todos", Write: "post_update",
		ReadPrompts:  []string{"Check the Basecamp to-dos for %[2]s and tell me the exact figure on that item.", "Find the Basecamp to-do about %[2]s; what precise number does it note?"},
		ReadResult:   "%[1]s to-dos (1 match): %[2]s",
		WritePrompts: []string{"Post \"%[2]s\" as an update in Basecamp.", "Share \"%[2]s\" with the team as a Basecamp update."}},
	{Slug: "teams", Read: "list_teams", Write: "post_message",
		ReadPrompts:  []string{"Look through our Microsoft Teams for the latest on %[2]s and tell me the exact figure mentioned.", "Check Microsoft Teams for %[2]s; what precise number was given?"},
		ReadResult:   "%[1]s (1 match): %[2]s",
		WritePrompts: []string{"Post \"%[2]s\" in Microsoft Teams.", "Send \"%[2]s\" to the team on Microsoft Teams."}},
	{Slug: "discord", Read: "list_servers", Write: "send_message",
		ReadPrompts:  []string{"Check our Discord servers for the latest on %[2]s and tell me the exact figure someone posted.", "Look in Discord for %[2]s; what precise number came up?"},
		ReadResult:   "%[1]s servers (1 match): %[2]s",
		WritePrompts: []string{"Send \"%[2]s\" as a Discord message.", "Post \"%[2]s\" on our Discord."}},
	{Slug: "miro", Read: "list_boards", Write: "create_sticky",
		ReadPrompts:  []string{"Check my Miro boards for the note on %[2]s and tell me the exact figure on it.", "Find %[2]s on our Miro board; what precise number does the note show?"},
		ReadResult:   "%[1]s boards (1 matching note): %[2]s",
		WritePrompts: []string{"Add a Miro sticky that says \"%[2]s\".", "Put \"%[2]s\" on a sticky note in Miro."}},
	{Slug: "wordpress", Read: "list_comments", Write: "create_post",
		ReadPrompts:  []string{"Check the WordPress comments for the one about %[2]s and tell me the exact figure it quotes.", "Find the WordPress comment on %[2]s; what precise number does it mention?"},
		ReadResult:   "%[1]s comments (1 match): %[2]s",
		WritePrompts: []string{"Draft a WordPress post titled \"%[2]s\".", "Create a new WordPress post called \"%[2]s\"."}},
	{Slug: "typeform", Read: "list_responses", Write: "create_form",
		ReadPrompts:  []string{"Check the Typeform responses for %[2]s and tell me the exact figure reported.", "Look up %[2]s in our Typeform responses; what precise number came back?"},
		ReadResult:   "%[1]s responses (1 match): %[2]s",
		WritePrompts: []string{"Create a Typeform form titled \"%[2]s\".", "Set up a new Typeform called \"%[2]s\"."}},
	{Slug: "statuspage", Read: "list_components", Write: "create_incident",
		ReadPrompts:  []string{"Check the Statuspage components for %[2]s and tell me the exact figure it shows.", "Look up %[2]s on our Statuspage; what precise number does it list?"},
		ReadResult:   "%[1]s components (1 match): %[2]s",
		WritePrompts: []string{"Open a Statuspage incident titled \"%[2]s\".", "Create a Statuspage incident called \"%[2]s\"."}},
	{Slug: "googledrive", Read: "search_files", Write: "create_folder",
		ReadPrompts:  []string{"Search my Google Drive for the file on %[2]s and tell me the exact figure in it.", "Find the Google Drive doc about %[2]s; what precise number does it give?"},
		ReadResult:   "%[1]s search (1 file): %[2]s",
		WritePrompts: []string{"Make a Google Drive folder named \"%[2]s\".", "Create a folder called \"%[2]s\" in Google Drive."}},
	{Slug: "pipedrive", Read: "list_deals", Write: "add_activity",
		ReadPrompts:  []string{"Look up the Pipedrive deal for %[2]s and tell me the exact figure on it.", "Check Pipedrive for %[2]s; what precise number does the deal show?"},
		ReadResult:   "%[1]s deals (1 match): %[2]s",
		WritePrompts: []string{"Schedule a Pipedrive activity with the subject \"%[2]s\".", "Add a Pipedrive activity called \"%[2]s\"."}},
	{Slug: "quickbooks", Read: "list_invoices", Write: "create_bill",
		ReadPrompts:  []string{"Check the QuickBooks invoices for %[2]s and tell me the exact figure on it.", "Find %[2]s in QuickBooks; what precise number does the invoice carry?"},
		ReadResult:   "%[1]s invoices (1 match): %[2]s",
		WritePrompts: []string{"Enter a QuickBooks bill for the vendor \"%[2]s\".", "Record a bill in QuickBooks from \"%[2]s\"."}},
}

// ConnectorApp is a scripted app resolved against the frozen pool.
type ConnectorApp struct {
	Slug   string
	Name   string
	Script ConnectorScript
	// ReadTool, WriteTool and WriteParam are the resolved tool names and the
	// write action's single parameter.
	ReadTool   string
	WriteTool  string
	WriteParam string
	// Tools are every tool the app has in the pool.
	Tools []string
}

func resolveScript(s ConnectorScript) (ConnectorApp, bool) {
	for _, app := range connectorPool {
		if app.Slug != s.Slug {
			continue
		}
		out := ConnectorApp{Slug: app.Slug, Name: app.Name, Script: s}
		for _, action := range app.Actions {
			name := app.Slug + "_" + action.Key
			out.Tools = append(out.Tools, name)
			if action.Key == s.Read {
				out.ReadTool = name
			}
			if action.Key == s.Write {
				out.WriteTool, out.WriteParam = name, action.Param
			}
		}
		return out, out.ReadTool != "" && out.WriteTool != ""
	}
	return ConnectorApp{}, false
}

// V16ConnectorScripts returns every scripted app resolved against the pool,
// in declaration order. A script that does not resolve is a contract defect
// and panics at first use (TestV16ScriptsResolveInPool keeps it unreachable).
func V16ConnectorScripts() []ConnectorApp {
	out := make([]ConnectorApp, 0, len(v16ConnectorScripts))
	for _, s := range v16ConnectorScripts {
		app, ok := resolveScript(s)
		if !ok {
			panic(fmt.Sprintf("catalog v16: connector script %q does not resolve in the frozen pool", s.Slug))
		}
		out = append(out, app)
	}
	return out
}

// V16ConnectorPlan is a seed's connector state: the apps it links and the
// apps its cases name without linking them. Both are drawn from
// V16ConnectorScripts and are disjoint.
type V16ConnectorPlan struct {
	Linked []ConnectorApp
	Named  []ConnectorApp
}

// V16ConnectorPlanForSeed derives the seed's linked and named-unlinked apps.
// The plan is a pure function of the seed, shared by the catalog, the mock
// tool endpoint (toolexec) and the case generator (datagen).
func V16ConnectorPlanForSeed(seed int64) V16ConnectorPlan {
	scripts := V16ConnectorScripts()
	perm := v13Perm(seed, "v16-connector-plan", len(scripts))
	linked := V16LinkedMin + v13Pick(seed, "v16-linked-count", V16LinkedMax-V16LinkedMin+1)
	var plan V16ConnectorPlan
	for i, index := range perm {
		switch {
		case i < linked:
			plan.Linked = append(plan.Linked, scripts[index])
		case i < linked+V16NamedUnlinked:
			plan.Named = append(plan.Named, scripts[index])
		}
	}
	return plan
}

// LinkedApp reports whether a tool name belongs to one of the plan's linked
// apps.
func (p V16ConnectorPlan) LinkedApp(toolName string) (ConnectorApp, bool) {
	for _, app := range p.Linked {
		for _, tool := range app.Tools {
			if tool == toolName {
				return app, true
			}
		}
	}
	return ConnectorApp{}, false
}

// ConnectorsForSeedV16 returns the seed's ConnectorToolsPerSeed connector
// tools in pool order, rendered without a link marker. Every tool of every
// linked and named app is included; the rest of the draw follows the v15
// seeded permutation.
func ConnectorsForSeedV16(seed int64) []protocol.ToolDefinition {
	universe := connectorUniverse()
	n := ConnectorToolsPerSeed
	if n > len(universe) {
		n = len(universe)
	}
	plan := V16ConnectorPlanForSeed(seed)
	required := map[string]bool{}
	for _, app := range append(append([]ConnectorApp(nil), plan.Linked...), plan.Named...) {
		for _, tool := range app.Tools {
			required[tool] = true
		}
	}
	picked := make([]int, 0, n)
	taken := make([]bool, len(universe))
	for i, t := range universe {
		if required[t.name()] {
			picked = append(picked, i)
			taken[i] = true
		}
	}
	for _, index := range v13Perm(seed, "connector-pick", len(universe)) {
		if len(picked) >= n {
			break
		}
		if !taken[index] {
			picked = append(picked, index)
			taken[index] = true
		}
	}
	sort.Ints(picked)
	out := make([]protocol.ToolDefinition, 0, len(picked))
	for _, index := range picked {
		out = append(out, connectorDefinitionV16(seed, universe[index]))
	}
	return out
}

// v16SeededCatalog is the v13/v14 seeded surface with the seed's v16
// connectors spliced in at the v15 seeded positions.
func v16SeededCatalog(seed int64) []protocol.ToolDefinition {
	return spliceConnectors(seed, v13SeededCatalog(seed), ConnectorsForSeedV16(seed))
}

// HasLinkStatePhrase reports the first V16LinkStatePhrases entry a
// description carries as whole words, if any ("LinkedIn" is an app name, not
// a link state).
func HasLinkStatePhrase(description string) (string, bool) {
	lower := strings.ToLower(description)
	for i, pattern := range v16LinkStatePatterns {
		if pattern.MatchString(lower) {
			return V16LinkStatePhrases[i], true
		}
	}
	return "", false
}

var v16LinkStatePatterns = func() []*regexp.Regexp {
	out := make([]*regexp.Regexp, len(V16LinkStatePhrases))
	for i, phrase := range V16LinkStatePhrases {
		out[i] = regexp.MustCompile(`\b` + regexp.QuoteMeta(phrase) + `\b`)
	}
	return out
}()
