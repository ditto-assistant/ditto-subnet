package catalog

import (
	"fmt"
	"strings"
	"sync"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v18 production-sized connector catalog.
//
// A real Ditto workspace with its connectors enabled offers far more than the
// v15/v16 surface (about 235 tools). Measured read-only in production on
// 2026-11-07 for one owner account: 128 first-party tools plus 1,282 enabled
// toolkit tools across 15 toolkits, with a steep skew (the two largest
// toolkits hold 328 and 314 tools, the smallest 7), plus custom MCP servers.
// A harness that hands the whole menu to the model on every turn cannot keep
// that up; one that defers or searches tools can, which is what Ditto's own
// harness does. V18 therefore splices V18BulkToolkits into every seed's v16
// surface: 1,282 more connector tools at the production toolkit sizes, so
// tool search becomes part of the run's priced cost as it is in the product.
//
// The toolkits reuse the production SIZES, not the production apps. The
// largest production toolkits (email, calendar, project boards, issue
// trackers, wikis) are either capabilities the product tools own or apps a
// v16 seed can link, and a bulk tool there would either be a defensible answer
// to a generated request or contradict the app's linked state. Every bulk
// toolkit is an app no v16 seed links (v16ConnectorScripts) and whose
// capability no product tool or coined decoy owns. Like every unlinked
// connector, each bulk tool is answered "not connected" by the mock endpoint,
// performs nothing, and is never expected by a scored case, so calling one is
// an ordinary extra call. Descriptions carry no link-state wording
// (HasLinkStatePhrase) and run 140 to 280 characters, the production range.
//
// The bank is frozen: changing a toolkit, a noun, a verb or a template
// changes v18 bytes and needs a new bench version. Membership is the same on
// every seed (the production size is the point); the description variants and
// the splice positions are seeded.

// V18BulkToolkit is one production-sized connector toolkit.
type V18BulkToolkit struct {
	Slug string
	Name string
	// Size is the number of tools the toolkit contributes.
	Size int
	// Space names where the toolkit's objects live ("workspace", "account").
	Space string
	// Objects are the top-level resources, in tool order; Aspects are the
	// sub-resources each object carries.
	Objects []string
	Aspects []string
}

// V18BulkToolkits are the frozen v18 toolkits, largest first, at the
// production toolkit sizes (sum 1,282).
var V18BulkToolkits = []V18BulkToolkit{
	{"smartsheet", "Smartsheet", 328, "workspace", []string{"sheet", "row", "column", "report", "dashboard", "workspace folder", "form", "automation rule", "proof", "update request", "template", "cross-sheet reference"}, []string{"attachment", "comment", "share", "cell history", "summary field", "discussion", "filter"}},
	{"zendesk", "Zendesk", 314, "help center", []string{"ticket", "requester", "organization", "agent group", "macro", "view", "trigger", "help center article", "satisfaction rating", "ticket field", "brand", "SLA policy"}, []string{"tag", "attachment", "audit", "custom field", "follower", "internal note", "metric"}},
	{"bitbucket", "Bitbucket", 127, "workspace", []string{"repository", "pull request", "pipeline", "branch restriction", "deploy key", "snippet", "project"}, []string{"comment", "permission", "webhook"}},
	{"dropbox", "Dropbox", 92, "account", []string{"file", "folder", "shared link", "file request", "team folder"}, []string{"revision", "member", "lock"}},
	{"coda", "Coda", 70, "workspace", []string{"doc", "table", "formula", "control", "pack"}, []string{"permission", "row"}},
	{"intercom", "Intercom", 61, "workspace", []string{"contact", "company", "segment", "help article", "data attribute"}, []string{"tag", "note"}},
	{"quip", "Quip", 57, "site", []string{"document", "spreadsheet", "folder", "live app"}, []string{"member", "section"}},
	{"sheets", "Google Sheets", 50, "account", []string{"spreadsheet", "worksheet", "named range", "pivot table"}, []string{"protection", "filter view"}},
	{"box", "Box", 47, "enterprise account", []string{"file", "folder", "collaboration", "retention policy"}, []string{"version", "metadata"}},
	{"excel", "Excel Online", 46, "OneDrive", []string{"workbook", "worksheet", "table", "chart"}, []string{"range", "format"}},
	{"airtable", "Airtable", 26, "workspace", []string{"base", "table", "field", "view"}, []string{"record"}},
	{"zoom", "Zoom", 25, "account", []string{"webinar", "recording", "room", "phone queue"}, []string{"registrant"}},
	{"evernote", "Evernote", 16, "account", []string{"notebook", "note", "tag"}, nil},
	{"hootsuite", "Hootsuite", 16, "dashboard", []string{"social profile", "scheduled post", "stream"}, nil},
	{"figma", "Figma", 7, "team", []string{"design file", "component library"}, nil},
}

// v18Verb is one action every bulk object offers.
type v18Verb struct {
	Key string
	// Does renders the first sentence: %[1]s = resource, %[2]s = app,
	// %[3]s = space.
	Does string
	// Needs is the required parameter shape: "id", "name" or "query".
	Needs string
}

// v18Verbs are the bulk actions, in tool order.
var v18Verbs = []v18Verb{
	{"list", "List every %[1]s in the user's %[2]s %[3]s, newest first", "query"},
	{"get", "Fetch one %[1]s from the user's %[2]s %[3]s by its identifier", "id"},
	{"create", "Create a new %[1]s in the user's %[2]s %[3]s", "name"},
	{"update", "Change the fields of an existing %[1]s in the user's %[2]s %[3]s", "id"},
	{"delete", "Permanently delete a %[1]s from the user's %[2]s %[3]s", "id"},
	{"search", "Search the %[1]s records of the user's %[2]s %[3]s by keyword", "query"},
	{"archive", "Archive a %[1]s in the user's %[2]s %[3]s so it stops appearing in lists", "id"},
	{"restore", "Restore an archived %[1]s in the user's %[2]s %[3]s", "id"},
	{"export", "Export a %[1]s from the user's %[2]s %[3]s as a downloadable file", "id"},
	{"copy", "Duplicate a %[1]s inside the user's %[2]s %[3]s", "id"},
}

// v18DetailTemplates are the second sentence of a bulk description, seeded
// per tool: %[1]s = resource, %[2]s = app.
var v18DetailTemplates = []string{
	"Returns the %[1]s's identifier, title, owner and last-modified time exactly as %[2]s reports them.",
	"Pass identifiers exactly as %[2]s shows them; the response echoes the %[1]s's id and its current state.",
	"Results follow %[2]s's own field names and page in batches of fifty, with a cursor for the next page of %[1]s data.",
	"Only fields the user can already see in %[2]s are returned, and the %[1]s's audit trail records the call.",
	"The call is applied with the user's own %[2]s permissions and fails if the %[1]s is outside their access.",
	"Use the %[1]s id from an earlier %[2]s list or search result; display names are not accepted here.",
}

// v18BulkTool is one rendered bulk tool before seeding.
type v18BulkTool struct {
	kit      V18BulkToolkit
	verb     v18Verb
	resource string // display form ("sheet comment")
	key      string // tool-name form ("sheet_comment")
}

func (t v18BulkTool) name() string { return t.kit.Slug + "_" + t.verb.Key + "_" + t.key }

// v18BulkUniverse enumerates every toolkit's tools in frozen order: objects
// first (every verb), then each object's aspects (every verb), until the
// toolkit reaches its Size. Names that collide with the v15 connector pool are
// skipped so a bulk tool never shares a name with a pool tool.
func v18BulkUniverse() []v18BulkTool {
	pool := map[string]bool{}
	for _, t := range connectorUniverse() {
		pool[t.name()] = true
	}
	out := make([]v18BulkTool, 0, 1300)
	for _, kit := range V18BulkToolkits {
		var resources [][2]string
		for _, object := range kit.Objects {
			resources = append(resources, [2]string{object, v18Key(object)})
		}
		for _, aspect := range kit.Aspects {
			for _, object := range kit.Objects {
				resources = append(resources, [2]string{object + " " + aspect, v18Key(object + " " + aspect)})
			}
		}
		count := 0
		for _, resource := range resources {
			for _, verb := range v18Verbs {
				if count == kit.Size {
					break
				}
				t := v18BulkTool{kit: kit, verb: verb, resource: resource[0], key: resource[1]}
				if pool[t.name()] {
					continue
				}
				out = append(out, t)
				count++
			}
		}
	}
	return out
}

func v18Key(display string) string {
	return strings.NewReplacer(" ", "_", "-", "_").Replace(strings.ToLower(display))
}

// V18BulkToolCount is the number of bulk tools every v18 seed carries.
func V18BulkToolCount() int { return len(v18BulkUniverse()) }

// v18BulkDefinition renders one bulk tool for a seed.
func v18BulkDefinition(seed int64, t v18BulkTool) protocol.ToolDefinition {
	does := fmt.Sprintf(t.verb.Does, t.resource, t.kit.Name, t.kit.Space)
	detail := v18DetailTemplates[v13Pick(seed, "v18-bulk-desc:"+t.name(), len(v18DetailTemplates))]
	description := does + ". " + fmt.Sprintf(detail, t.resource, t.kit.Name)
	idParam := t.key + "_id"
	var parameters []byte
	switch t.verb.Needs {
	case "id":
		parameters = schema([]string{idParam},
			prop{name: idParam, typ: "string", desc: fmt.Sprintf("%s %s identifier", t.kit.Name, t.resource)})
	case "name":
		parameters = schema([]string{"name"},
			prop{name: "name", typ: "string", desc: fmt.Sprintf("name of the new %s", t.resource)},
			prop{name: "parent_id", typ: "string", desc: "optional identifier of the containing item"})
	default:
		parameters = schema(nil,
			prop{name: "query", typ: "string", desc: "optional keyword filter"},
			prop{name: "cursor", typ: "string", desc: "optional cursor from the previous page"})
	}
	return protocol.ToolDefinition{Name: t.name(), Description: description, Parameters: parameters}
}

// V18BulkConnectorsForSeed returns the seed's bulk tools in toolkit order.
func V18BulkConnectorsForSeed(seed int64) []protocol.ToolDefinition {
	universe := v18BulkUniverse()
	out := make([]protocol.ToolDefinition, 0, len(universe))
	for _, t := range universe {
		out = append(out, v18BulkDefinition(seed, t))
	}
	return out
}

// IsV18BulkConnectorTool reports whether name is a v18 bulk tool, with its
// toolkit's display name. It is seed-independent, as IsConnectorTool is.
func IsV18BulkConnectorTool(name string) (appName string, ok bool) {
	appName, ok = v18BulkIndex()[name]
	return appName, ok
}

// v18BulkIndex maps every bulk tool name to its toolkit's display name. The
// bank is frozen, so it is built once.
var v18BulkIndex = sync.OnceValue(func() map[string]string {
	index := map[string]string{}
	for _, t := range v18BulkUniverse() {
		index[t.name()] = t.kit.Name
	}
	return index
})

// IsConnectorToolForVersion is IsConnectorTool under an explicit contract:
// from v18 the bulk toolkits are connectors too.
func IsConnectorToolForVersion(name string, benchVersion int) (appName string, ok bool) {
	if appName, ok := IsConnectorTool(name); ok {
		return appName, true
	}
	if benchVersion >= protocol.BenchVersionV18 {
		return IsV18BulkConnectorTool(name)
	}
	return "", false
}

// v18SeededCatalog is the v16 surface with the bulk toolkits spliced in at
// seeded positions (their own slot keys, so the v16 order is kept).
func v18SeededCatalog(seed int64) []protocol.ToolDefinition {
	base := v16SeededCatalog(seed)
	bulk := V18BulkConnectorsForSeed(seed)
	slots := make([]int, len(bulk))
	for i := range bulk {
		slots[i] = v13Pick(seed, fmt.Sprintf("v18-bulk-slot:%d", i), len(base)+1)
	}
	byPos := make([][]protocol.ToolDefinition, len(base)+1)
	for i, def := range bulk {
		byPos[slots[i]] = append(byPos[slots[i]], def)
	}
	out := make([]protocol.ToolDefinition, 0, len(base)+len(bulk))
	for pos := 0; pos <= len(base); pos++ {
		out = append(out, byPos[pos]...)
		if pos < len(base) {
			out = append(out, base[pos])
		}
	}
	return out
}
