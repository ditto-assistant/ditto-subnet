package catalog

import (
	"fmt"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v15 production-scale tool surface.
//
// A real Ditto workspace offers the product tools plus every tool of every
// connector the user could link: hundreds of entries, most of which do not
// apply to the request in hand. v13/v14 offered about thirty tools, so a
// harness could hand the whole menu to the model on every turn and tool
// selection was never the hard part. From v15 every seed's catalog also
// carries ConnectorToolsPerSeed connector tools drawn from a frozen pool of
// third-party apps, spliced in at seeded positions, so selecting the right
// tool out of a production-sized menu becomes a scored difficulty axis.
//
// The connector tools are NOT connected in the benchmark workspace. The mock
// tool endpoint answers every one of them with a "not connected" error and
// performs nothing, so calling one is an ordinary extra call that the
// existing over-call and catalog gates already price. No scored case expects
// a connector tool, and the pool deliberately excludes every capability the
// product tools or the coined decoys own (email, calendar, web search,
// images, memory, reminders, weather, maps, translation, video), so a
// connector is never a defensible answer to a generated request.
//
// The pool is frozen: enlarging it, renaming an entry or changing a template
// changes v15 bytes and needs a new bench version.

// ConnectorToolsPerSeed is how many connector tools one v15 seed's catalog
// carries in addition to the product tools and coined decoys.
const ConnectorToolsPerSeed = 200

type connectorAction struct {
	// Key names the action inside the tool name: <app>_<key>.
	Key string
	// Does describes the action; %[1]s is the app's display name.
	Does []string
	// Param and ParamDesc are the single required argument.
	Param     string
	ParamDesc string
}

type connectorApp struct {
	Slug    string
	Name    string
	Actions []connectorAction
}

func ca(key, param, paramDesc string, does ...string) connectorAction {
	return connectorAction{Key: key, Does: does, Param: param, ParamDesc: paramDesc}
}

// connectorPool is the frozen v15 connector catalog.
var connectorPool = []connectorApp{
	{"slack", "Slack", []connectorAction{
		ca("post_message", "text", "message text", "Post a message to a %[1]s channel.", "Send a message into a %[1]s channel the user belongs to."),
		ca("list_channels", "filter", "optional channel-name filter", "List the %[1]s channels in the connected workspace.", "Show the channels of the linked %[1]s workspace."),
		ca("search_messages", "query", "message search terms", "Search message history in the connected %[1]s workspace.", "Find %[1]s messages matching a query."),
		ca("set_status", "status", "status text", "Set the user's %[1]s status line.", "Update the status shown on the user's %[1]s profile."),
	}},
	{"notion", "Notion", []connectorAction{
		ca("create_page", "title", "page title", "Create a page in the connected %[1]s workspace.", "Add a new %[1]s page."),
		ca("search_pages", "query", "page search terms", "Search pages in the connected %[1]s workspace.", "Find %[1]s pages by title or content."),
		ca("append_block", "content", "block text", "Append a block to an existing %[1]s page.", "Add content to the end of a %[1]s page."),
		ca("query_database", "database", "database name", "Query rows of a %[1]s database.", "Read entries from a %[1]s database."),
	}},
	{"linear", "Linear", []connectorAction{
		ca("create_issue", "title", "issue title", "Open an issue in the connected %[1]s team.", "File a new %[1]s issue."),
		ca("list_issues", "filter", "optional issue filter", "List %[1]s issues assigned to the user.", "Show the user's open %[1]s issues."),
		ca("update_issue", "issue", "issue identifier", "Change the state or fields of a %[1]s issue.", "Edit an existing %[1]s issue."),
		ca("list_cycles", "team", "team key", "List the current and upcoming %[1]s cycles.", "Show %[1]s cycles for a team."),
	}},
	{"jira", "Jira", []connectorAction{
		ca("create_ticket", "summary", "ticket summary", "Create a ticket in a connected %[1]s project.", "Open a new %[1]s ticket."),
		ca("search_tickets", "jql", "search expression", "Search %[1]s tickets with a query expression.", "Find %[1]s tickets matching a filter."),
		ca("transition_ticket", "ticket", "ticket key", "Move a %[1]s ticket to another workflow state.", "Transition a %[1]s ticket."),
		ca("add_worklog", "ticket", "ticket key", "Log time against a %[1]s ticket.", "Record a %[1]s work log entry."),
	}},
	{"github", "GitHub", []connectorAction{
		ca("list_pull_requests", "repo", "owner/name", "List open pull requests in a %[1]s repository.", "Show a %[1]s repository's open pull requests."),
		ca("create_issue", "title", "issue title", "Open an issue in a %[1]s repository.", "File a %[1]s issue."),
		ca("get_file", "path", "repository file path", "Read one file from a %[1]s repository.", "Fetch a file's contents from %[1]s."),
		ca("list_workflow_runs", "repo", "owner/name", "List recent CI workflow runs of a %[1]s repository.", "Show %[1]s Actions runs."),
		ca("merge_pull_request", "number", "pull request number", "Merge a %[1]s pull request.", "Merge an approved %[1]s pull request."),
	}},
	{"gitlab", "GitLab", []connectorAction{
		ca("list_merge_requests", "project", "project path", "List open merge requests in a %[1]s project.", "Show a %[1]s project's merge requests."),
		ca("create_issue", "title", "issue title", "Open an issue in a %[1]s project.", "File a %[1]s issue."),
		ca("list_pipelines", "project", "project path", "List recent %[1]s CI pipelines.", "Show %[1]s pipeline runs."),
	}},
	{"asana", "Asana", []connectorAction{
		ca("create_task", "name", "task name", "Create a task in a connected %[1]s project.", "Add a new %[1]s task."),
		ca("list_tasks", "project", "project name", "List tasks in an %[1]s project.", "Show an %[1]s project's tasks."),
		ca("complete_task", "task", "task identifier", "Mark an %[1]s task complete.", "Close an %[1]s task."),
	}},
	{"trello", "Trello", []connectorAction{
		ca("create_card", "name", "card title", "Create a card on a %[1]s board.", "Add a %[1]s card."),
		ca("move_card", "card", "card identifier", "Move a %[1]s card to another list.", "Shift a %[1]s card between lists."),
		ca("list_boards", "filter", "optional board filter", "List the user's %[1]s boards.", "Show %[1]s boards."),
	}},
	{"clickup", "ClickUp", []connectorAction{
		ca("create_task", "name", "task name", "Create a task in a %[1]s list.", "Add a %[1]s task."),
		ca("list_spaces", "filter", "optional space filter", "List %[1]s spaces.", "Show the user's %[1]s spaces."),
		ca("track_time", "task", "task identifier", "Start a time entry on a %[1]s task.", "Track time on a %[1]s task."),
	}},
	{"monday", "monday.com", []connectorAction{
		ca("create_item", "name", "item name", "Create an item on a %[1]s board.", "Add a %[1]s board item."),
		ca("list_boards", "filter", "optional board filter", "List %[1]s boards.", "Show the user's %[1]s boards."),
		ca("update_column", "item", "item identifier", "Change a column value of a %[1]s item.", "Edit a %[1]s item column."),
	}},
	{"airtable", "Airtable", []connectorAction{
		ca("list_records", "table", "table name", "List records from an %[1]s table.", "Read rows of an %[1]s table."),
		ca("create_record", "table", "table name", "Create a record in an %[1]s table.", "Insert an %[1]s row."),
		ca("list_bases", "filter", "optional base filter", "List the user's %[1]s bases.", "Show %[1]s bases."),
	}},
	{"confluence", "Confluence", []connectorAction{
		ca("create_page", "title", "page title", "Create a page in a %[1]s space.", "Publish a new %[1]s page."),
		ca("get_page", "page", "page identifier", "Read a %[1]s page.", "Fetch one %[1]s page."),
		ca("list_spaces", "filter", "optional space filter", "List %[1]s spaces.", "Show %[1]s spaces."),
	}},
	{"figma", "Figma", []connectorAction{
		ca("list_files", "project", "project name", "List files in a %[1]s project.", "Show %[1]s design files."),
		ca("get_comments", "file", "file key", "Read the comments on a %[1]s file.", "Fetch %[1]s file comments."),
		ca("export_frame", "frame", "frame identifier", "Export a %[1]s frame as an image asset.", "Render a %[1]s frame for download."),
	}},
	{"salesforce", "Salesforce", []connectorAction{
		ca("query_records", "soql", "query", "Run a query against the connected %[1]s org.", "Query %[1]s records."),
		ca("create_lead", "name", "lead name", "Create a lead in %[1]s.", "Add a %[1]s lead."),
		ca("update_opportunity", "opportunity", "opportunity identifier", "Update a %[1]s opportunity.", "Edit a %[1]s opportunity's fields."),
		ca("log_call", "contact", "contact name", "Log a call activity in %[1]s.", "Record a %[1]s call."),
	}},
	{"hubspot", "HubSpot", []connectorAction{
		ca("create_deal", "name", "deal name", "Create a deal in %[1]s.", "Add a %[1]s deal."),
		ca("list_deals", "stage", "pipeline stage", "List %[1]s deals in a pipeline stage.", "Show %[1]s deals."),
		ca("enroll_sequence", "contact", "contact email", "Enroll a contact in a %[1]s sequence.", "Add a contact to a %[1]s sequence."),
	}},
	{"pipedrive", "Pipedrive", []connectorAction{
		ca("list_deals", "filter", "optional deal filter", "List %[1]s deals.", "Show the user's %[1]s deals."),
		ca("add_activity", "subject", "activity subject", "Schedule a %[1]s activity on a deal.", "Add a %[1]s activity."),
	}},
	{"zendesk", "Zendesk", []connectorAction{
		ca("search_tickets", "query", "ticket search terms", "Search %[1]s support tickets.", "Find %[1]s tickets."),
		ca("reply_ticket", "ticket", "ticket number", "Post a public reply on a %[1]s ticket.", "Answer a %[1]s ticket."),
		ca("assign_ticket", "ticket", "ticket number", "Assign a %[1]s ticket to an agent.", "Reassign a %[1]s ticket."),
	}},
	{"intercom", "Intercom", []connectorAction{
		ca("list_conversations", "filter", "optional conversation filter", "List open %[1]s conversations.", "Show %[1]s inbox conversations."),
		ca("reply_conversation", "conversation", "conversation identifier", "Reply in an %[1]s conversation.", "Send an %[1]s conversation reply."),
		ca("tag_user", "user", "user identifier", "Tag a user in %[1]s.", "Apply an %[1]s tag to a user."),
	}},
	{"freshdesk", "Freshdesk", []connectorAction{
		ca("create_ticket", "subject", "ticket subject", "Create a %[1]s ticket.", "Open a %[1]s ticket."),
		ca("list_tickets", "filter", "optional ticket filter", "List %[1]s tickets.", "Show %[1]s tickets."),
	}},
	{"stripe", "Stripe", []connectorAction{
		ca("list_charges", "customer", "customer identifier", "List %[1]s charges for a customer.", "Show a customer's %[1]s charges."),
		ca("create_invoice", "customer", "customer identifier", "Draft a %[1]s invoice.", "Create a %[1]s invoice."),
		ca("refund_charge", "charge", "charge identifier", "Refund a %[1]s charge.", "Issue a %[1]s refund."),
		ca("list_subscriptions", "customer", "customer identifier", "List a customer's %[1]s subscriptions.", "Show %[1]s subscriptions."),
	}},
	{"shopify", "Shopify", []connectorAction{
		ca("list_orders", "status", "order status", "List %[1]s store orders.", "Show %[1]s orders."),
		ca("update_inventory", "sku", "product SKU", "Adjust %[1]s inventory for a product.", "Change %[1]s stock levels."),
		ca("create_discount", "code", "discount code", "Create a %[1]s discount code.", "Add a %[1]s discount."),
	}},
	{"quickbooks", "QuickBooks", []connectorAction{
		ca("list_invoices", "status", "invoice status", "List %[1]s invoices.", "Show %[1]s invoices."),
		ca("create_bill", "vendor", "vendor name", "Enter a vendor bill in %[1]s.", "Record a %[1]s bill."),
		ca("run_report", "report", "report name", "Run a %[1]s accounting report.", "Generate a %[1]s report."),
	}},
	{"xero", "Xero", []connectorAction{
		ca("list_invoices", "status", "invoice status", "List %[1]s invoices.", "Show %[1]s invoices."),
		ca("reconcile_transaction", "transaction", "bank transaction identifier", "Reconcile a %[1]s bank transaction.", "Match a %[1]s bank line."),
	}},
	{"dropbox", "Dropbox", []connectorAction{
		ca("list_folder", "path", "folder path", "List a %[1]s folder.", "Show the files in a %[1]s folder."),
		ca("share_link", "path", "file path", "Create a %[1]s share link.", "Share a %[1]s file by link."),
		ca("upload_file", "path", "destination path", "Upload a file to %[1]s.", "Save a file into %[1]s."),
	}},
	{"box", "Box", []connectorAction{
		ca("search_files", "query", "file search terms", "Search files in %[1]s.", "Find %[1]s files."),
		ca("list_collaborators", "folder", "folder identifier", "List collaborators on a %[1]s folder.", "Show who shares a %[1]s folder."),
	}},
	{"onedrive", "OneDrive", []connectorAction{
		ca("list_items", "path", "folder path", "List items in a %[1]s folder.", "Show %[1]s files."),
		ca("share_item", "path", "item path", "Share a %[1]s item.", "Create a %[1]s sharing link."),
	}},
	{"googledrive", "Google Drive", []connectorAction{
		ca("search_files", "query", "file search terms", "Search files in %[1]s.", "Find files in %[1]s."),
		ca("create_folder", "name", "folder name", "Create a folder in %[1]s.", "Add a %[1]s folder."),
		ca("move_file", "file", "file identifier", "Move a %[1]s file to another folder.", "Relocate a file in %[1]s."),
	}},
	{"sheets", "Google Sheets", []connectorAction{
		ca("append_row", "spreadsheet", "spreadsheet identifier", "Append a row to a %[1]s spreadsheet.", "Add a row in %[1]s."),
		ca("create_spreadsheet", "title", "spreadsheet title", "Create a %[1]s spreadsheet.", "Start a new %[1]s spreadsheet."),
	}},
	{"excel", "Excel Online", []connectorAction{
		ca("read_range", "range", "worksheet range", "Read a range from an %[1]s workbook.", "Fetch cells from %[1]s."),
		ca("write_range", "range", "worksheet range", "Write values into an %[1]s range.", "Update cells in %[1]s."),
	}},
	{"teams", "Microsoft Teams", []connectorAction{
		ca("post_message", "text", "message text", "Post a message in a %[1]s channel.", "Send a %[1]s channel message."),
		ca("list_teams", "filter", "optional team filter", "List the user's %[1]s teams.", "Show %[1]s teams."),
	}},
	{"discord", "Discord", []connectorAction{
		ca("send_message", "text", "message text", "Send a message to a %[1]s channel.", "Post in a %[1]s channel."),
		ca("list_servers", "filter", "optional server filter", "List the user's %[1]s servers.", "Show %[1]s servers."),
	}},
	{"zoom", "Zoom", []connectorAction{
		ca("list_recordings", "from", "start date", "List %[1]s cloud recordings.", "Show %[1]s recordings."),
	}},
	{"sentry", "Sentry", []connectorAction{
		ca("list_issues", "project", "project slug", "List unresolved %[1]s issues.", "Show %[1]s errors."),
		ca("resolve_issue", "issue", "issue identifier", "Resolve a %[1]s issue.", "Mark a %[1]s issue resolved."),
	}},
	{"pagerduty", "PagerDuty", []connectorAction{
		ca("list_incidents", "status", "incident status", "List %[1]s incidents.", "Show %[1]s incidents."),
		ca("acknowledge_incident", "incident", "incident identifier", "Acknowledge a %[1]s incident.", "Ack a %[1]s incident."),
		ca("who_is_on_call", "schedule", "schedule name", "Show who is on call in %[1]s.", "Read the %[1]s on-call roster."),
	}},
	{"datadog", "Datadog", []connectorAction{
		ca("query_metrics", "query", "metric query", "Query %[1]s metrics.", "Read a %[1]s timeseries."),
		ca("list_monitors", "filter", "optional monitor filter", "List %[1]s monitors.", "Show %[1]s alert monitors."),
		ca("mute_monitor", "monitor", "monitor identifier", "Mute a %[1]s monitor.", "Silence a %[1]s monitor."),
	}},
	{"vercel", "Vercel", []connectorAction{
		ca("list_deployments", "project", "project name", "List %[1]s deployments.", "Show recent %[1]s deploys."),
		ca("promote_deployment", "deployment", "deployment identifier", "Promote a %[1]s deployment to production.", "Roll a %[1]s deployment to prod."),
	}},
	{"netlify", "Netlify", []connectorAction{
		ca("list_sites", "filter", "optional site filter", "List %[1]s sites.", "Show %[1]s sites."),
		ca("trigger_build", "site", "site identifier", "Trigger a %[1]s build.", "Start a %[1]s deploy build."),
	}},
	{"cloudflare", "Cloudflare", []connectorAction{
		ca("list_dns_records", "zone", "zone name", "List %[1]s DNS records for a zone.", "Show %[1]s DNS entries."),
		ca("purge_cache", "zone", "zone name", "Purge the %[1]s cache for a zone.", "Clear %[1]s cached content."),
	}},
	{"aws", "AWS", []connectorAction{
		ca("list_buckets", "filter", "optional bucket filter", "List S3 buckets in the connected %[1]s account.", "Show %[1]s S3 buckets."),
		ca("describe_instances", "region", "region name", "Describe EC2 instances in the connected %[1]s account.", "List %[1]s EC2 instances."),
	}},
	{"twilio", "Twilio", []connectorAction{
		ca("send_sms", "to", "destination phone number", "Send an SMS through %[1]s.", "Text a phone number via %[1]s."),
		ca("list_calls", "from", "start date", "List %[1]s call logs.", "Show %[1]s calls."),
	}},
	{"docusign", "DocuSign", []connectorAction{
		ca("send_envelope", "template", "template name", "Send a %[1]s envelope for signature.", "Request a %[1]s signature."),
		ca("envelope_status", "envelope", "envelope identifier", "Check a %[1]s envelope's status.", "Read %[1]s signing status."),
	}},
	{"greenhouse", "Greenhouse", []connectorAction{
		ca("list_candidates", "job", "job identifier", "List %[1]s candidates for a job.", "Show %[1]s applicants."),
		ca("schedule_interview", "candidate", "candidate identifier", "Schedule a %[1]s interview.", "Book a %[1]s interview slot."),
	}},
	{"workday", "Workday", []connectorAction{
		ca("request_time_off", "dates", "date range", "Submit a %[1]s time-off request.", "Request leave in %[1]s."),
		ca("get_payslip", "period", "pay period", "Fetch a %[1]s payslip.", "Read a %[1]s pay statement."),
	}},
	{"gusto", "Gusto", []connectorAction{
		ca("run_payroll", "period", "pay period", "Preview a %[1]s payroll run.", "Prepare %[1]s payroll."),
		ca("list_employees", "filter", "optional employee filter", "List %[1]s employees.", "Show %[1]s staff."),
	}},
	{"spotify", "Spotify", []connectorAction{
		ca("play_playlist", "playlist", "playlist name", "Start playing a %[1]s playlist.", "Play a %[1]s playlist."),
		ca("add_to_queue", "track", "track name", "Add a track to the %[1]s queue.", "Queue a %[1]s song."),
	}},
	{"strava", "Strava", []connectorAction{
		ca("list_activities", "from", "start date", "List %[1]s activities.", "Show recent %[1]s workouts."),
		ca("upload_activity", "file", "activity file", "Upload an activity to %[1]s.", "Add a %[1]s workout."),
	}},
	{"uber", "Uber", []connectorAction{
		ca("request_ride", "pickup", "pickup address", "Request an %[1]s ride.", "Book an %[1]s trip."),
		ca("ride_history", "from", "start date", "List past %[1]s rides.", "Show %[1]s trip history."),
	}},
	{"doordash", "DoorDash", []connectorAction{
		ca("search_restaurants", "query", "cuisine or name", "Search %[1]s restaurants.", "Find food on %[1]s."),
		ca("track_order", "order", "order identifier", "Track a %[1]s order.", "Check a %[1]s delivery."),
	}},
	{"instacart", "Instacart", []connectorAction{
		ca("add_to_cart", "item", "grocery item", "Add an item to the %[1]s cart.", "Put a grocery item in %[1]s."),
		ca("list_orders", "filter", "optional order filter", "List %[1]s orders.", "Show %[1]s grocery orders."),
	}},
	{"opentable", "OpenTable", []connectorAction{
		ca("find_table", "restaurant", "restaurant name", "Find available %[1]s reservations.", "Search %[1]s tables."),
		ca("cancel_reservation", "reservation", "reservation identifier", "Cancel an %[1]s reservation.", "Drop an %[1]s booking."),
	}},
	{"airbnb", "Airbnb", []connectorAction{
		ca("search_stays", "destination", "destination", "Search %[1]s stays.", "Find %[1]s listings."),
		ca("list_trips", "filter", "optional trip filter", "List the user's %[1]s trips.", "Show %[1]s bookings."),
	}},
	{"reddit", "Reddit", []connectorAction{
		ca("submit_post", "subreddit", "subreddit name", "Submit a post to %[1]s.", "Create a %[1]s post."),
		ca("list_saved", "filter", "optional filter", "List the user's saved %[1]s items.", "Show saved %[1]s posts."),
	}},
	{"linkedin", "LinkedIn", []connectorAction{
		ca("share_post", "text", "post text", "Share a post on %[1]s.", "Publish a %[1]s update."),
		ca("list_connections", "filter", "optional filter", "List %[1]s connections.", "Show the user's %[1]s network."),
	}},
	{"mixpanel", "Mixpanel", []connectorAction{
		ca("query_funnel", "funnel", "funnel name", "Query a %[1]s funnel.", "Read %[1]s funnel conversion."),
		ca("list_cohorts", "filter", "optional cohort filter", "List %[1]s cohorts.", "Show %[1]s cohorts."),
	}},
	{"amplitude", "Amplitude", []connectorAction{
		ca("event_segmentation", "event", "event name", "Run an %[1]s event segmentation.", "Segment an %[1]s event."),
		ca("list_charts", "filter", "optional chart filter", "List saved %[1]s charts.", "Show %[1]s charts."),
	}},
	{"snowflake", "Snowflake", []connectorAction{
		ca("run_query", "sql", "SQL statement", "Run a SQL query in %[1]s.", "Execute %[1]s SQL."),
		ca("list_warehouses", "filter", "optional filter", "List %[1]s warehouses.", "Show %[1]s compute warehouses."),
	}},
	{"bigquery", "BigQuery", []connectorAction{
		ca("run_query", "sql", "SQL statement", "Run a %[1]s SQL query.", "Execute SQL in %[1]s."),
		ca("list_datasets", "project", "project identifier", "List %[1]s datasets.", "Show %[1]s datasets."),
	}},
	{"postgres", "Postgres", []connectorAction{
		ca("run_query", "sql", "SQL statement", "Run a read-only query on the connected %[1]s database.", "Query the linked %[1]s database."),
		ca("list_tables", "schema", "schema name", "List tables in a %[1]s schema.", "Show %[1]s tables."),
	}},
	{"webflow", "Webflow", []connectorAction{
		ca("publish_site", "site", "site identifier", "Publish a %[1]s site.", "Push a %[1]s site live."),
		ca("list_collection_items", "collection", "collection name", "List %[1]s CMS items.", "Show %[1]s collection entries."),
	}},
	{"wordpress", "WordPress", []connectorAction{
		ca("create_post", "title", "post title", "Draft a %[1]s post.", "Create a %[1]s blog post."),
		ca("list_comments", "post", "post identifier", "List comments on a %[1]s post.", "Show %[1]s post comments."),
	}},
	{"typeform", "Typeform", []connectorAction{
		ca("list_responses", "form", "form identifier", "List %[1]s form responses.", "Read %[1]s submissions."),
		ca("create_form", "title", "form title", "Create a %[1]s form.", "Build a new %[1]s form."),
	}},
	{"1password", "1Password", []connectorAction{
		ca("list_vaults", "filter", "optional vault filter", "List %[1]s vaults the user can open.", "Show %[1]s vaults."),
		ca("share_item", "item", "item name", "Create a %[1]s share link for an item.", "Share a %[1]s item."),
	}},
	{"okta", "Okta", []connectorAction{
		ca("list_apps", "filter", "optional app filter", "List %[1]s apps assigned to the user.", "Show %[1]s assignments."),
		ca("reset_factor", "factor", "factor type", "Reset an %[1]s sign-in factor.", "Re-enroll an %[1]s factor."),
	}},
	{"basecamp", "Basecamp", []connectorAction{
		ca("post_update", "text", "update text", "Post a check-in update to a %[1]s project.", "Share a %[1]s project update."),
		ca("list_todos", "project", "project name", "List %[1]s to-dos.", "Show a %[1]s project's to-dos."),
		ca("complete_todo", "todo", "to-do identifier", "Complete a %[1]s to-do.", "Check off a %[1]s to-do."),
	}},
	{"smartsheet", "Smartsheet", []connectorAction{
		ca("list_sheets", "filter", "optional sheet filter", "List %[1]s sheets.", "Show %[1]s sheets."),
		ca("update_row", "row", "row identifier", "Update a %[1]s row.", "Edit a %[1]s row."),
	}},
	{"miro", "Miro", []connectorAction{
		ca("create_sticky", "text", "sticky note text", "Add a sticky note to a %[1]s board.", "Place a %[1]s sticky."),
		ca("list_boards", "filter", "optional board filter", "List %[1]s boards.", "Show %[1]s boards."),
		ca("export_board", "board", "board identifier", "Export a %[1]s board as a PDF.", "Download a %[1]s board."),
	}},
	{"loom", "Loom", []connectorAction{
		ca("list_videos", "filter", "optional video filter", "List the user's %[1]s recordings.", "Show %[1]s recordings."),
		ca("share_video", "video", "video identifier", "Share a %[1]s recording link.", "Create a %[1]s share link."),
	}},
	{"gong", "Gong", []connectorAction{
		ca("list_calls", "from", "start date", "List recorded %[1]s sales calls.", "Show %[1]s call recordings."),
		ca("call_stats", "call", "call identifier", "Read talk-ratio stats for a %[1]s call.", "Fetch %[1]s call analytics."),
	}},
	{"outreach", "Outreach", []connectorAction{
		ca("add_prospect", "email", "prospect email", "Add a prospect to %[1]s.", "Create an %[1]s prospect."),
		ca("list_sequences", "filter", "optional sequence filter", "List %[1]s sequences.", "Show %[1]s sequences."),
	}},
	{"marketo", "Marketo", []connectorAction{
		ca("list_programs", "filter", "optional program filter", "List %[1]s programs.", "Show %[1]s programs."),
		ca("add_to_list", "lead", "lead identifier", "Add a lead to a %[1]s static list.", "Put a lead on a %[1]s list."),
	}},
	{"segment", "Segment", []connectorAction{
		ca("list_sources", "filter", "optional source filter", "List %[1]s sources.", "Show %[1]s data sources."),
		ca("track_event", "event", "event name", "Send a track event through %[1]s.", "Emit a %[1]s event."),
	}},
	{"looker", "Looker", []connectorAction{
		ca("run_look", "look", "look identifier", "Run a saved %[1]s look.", "Execute a %[1]s look."),
		ca("list_dashboards", "filter", "optional dashboard filter", "List %[1]s dashboards.", "Show %[1]s dashboards."),
	}},
	{"tableau", "Tableau", []connectorAction{
		ca("list_workbooks", "filter", "optional workbook filter", "List %[1]s workbooks.", "Show %[1]s workbooks."),
		ca("refresh_extract", "datasource", "data source name", "Refresh a %[1]s extract.", "Run a %[1]s extract refresh."),
	}},
	{"metabase", "Metabase", []connectorAction{
		ca("run_question", "question", "question identifier", "Run a saved %[1]s question.", "Execute a %[1]s question."),
		ca("list_collections", "filter", "optional filter", "List %[1]s collections.", "Show %[1]s collections."),
	}},
	{"databricks", "Databricks", []connectorAction{
		ca("run_job", "job", "job identifier", "Trigger a %[1]s job run.", "Start a %[1]s job."),
		ca("list_clusters", "filter", "optional cluster filter", "List %[1]s clusters.", "Show %[1]s clusters."),
	}},
	{"mongodb", "MongoDB Atlas", []connectorAction{
		ca("find_documents", "collection", "collection name", "Find documents in a %[1]s collection.", "Query %[1]s documents."),
		ca("list_clusters", "project", "project identifier", "List %[1]s clusters.", "Show %[1]s clusters."),
	}},
	{"supabase", "Supabase", []connectorAction{
		ca("run_sql", "sql", "SQL statement", "Run SQL against a %[1]s project.", "Execute %[1]s SQL."),
		ca("list_projects", "filter", "optional filter", "List %[1]s projects.", "Show %[1]s projects."),
	}},
	{"firebase", "Firebase", []connectorAction{
		ca("list_users", "filter", "optional user filter", "List %[1]s auth users.", "Show %[1]s users."),
		ca("deploy_rules", "project", "project identifier", "Deploy %[1]s security rules.", "Publish %[1]s rules."),
	}},
	{"heroku", "Heroku", []connectorAction{
		ca("list_apps", "filter", "optional app filter", "List %[1]s apps.", "Show %[1]s apps."),
		ca("restart_dynos", "app", "app name", "Restart %[1]s dynos.", "Cycle %[1]s dynos."),
		ca("tail_logs", "app", "app name", "Read recent %[1]s logs.", "Fetch %[1]s app logs."),
	}},
	{"digitalocean", "DigitalOcean", []connectorAction{
		ca("list_droplets", "filter", "optional filter", "List %[1]s droplets.", "Show %[1]s droplets."),
		ca("resize_droplet", "droplet", "droplet identifier", "Resize a %[1]s droplet.", "Change a %[1]s droplet size."),
	}},
	{"azure", "Azure", []connectorAction{
		ca("list_resource_groups", "subscription", "subscription identifier", "List %[1]s resource groups.", "Show %[1]s resource groups."),
		ca("start_vm", "vm", "virtual machine name", "Start an %[1]s virtual machine.", "Boot an %[1]s VM."),
	}},
	{"gcp", "Google Cloud", []connectorAction{
		ca("list_instances", "project", "project identifier", "List %[1]s compute instances.", "Show %[1]s VMs."),
		ca("read_logs", "filter", "log filter", "Read %[1]s logs.", "Query %[1]s logging."),
	}},
	{"kubernetes", "Kubernetes", []connectorAction{
		ca("list_pods", "namespace", "namespace", "List %[1]s pods in a namespace.", "Show %[1]s pods."),
		ca("scale_deployment", "deployment", "deployment name", "Scale a %[1]s deployment.", "Change %[1]s replica count."),
	}},
	{"terraform", "Terraform Cloud", []connectorAction{
		ca("list_runs", "workspace", "workspace name", "List %[1]s runs.", "Show %[1]s plans and applies."),
		ca("queue_plan", "workspace", "workspace name", "Queue a %[1]s plan.", "Start a %[1]s plan."),
	}},
	{"circleci", "CircleCI", []connectorAction{
		ca("list_pipelines", "project", "project slug", "List %[1]s pipelines.", "Show %[1]s pipelines."),
		ca("rerun_workflow", "workflow", "workflow identifier", "Rerun a %[1]s workflow.", "Restart a %[1]s workflow."),
	}},
	{"bitbucket", "Bitbucket", []connectorAction{
		ca("list_pull_requests", "repo", "repository slug", "List %[1]s pull requests.", "Show %[1]s pull requests."),
		ca("create_branch", "name", "branch name", "Create a %[1]s branch.", "Open a %[1]s branch."),
	}},
	{"grafana", "Grafana", []connectorAction{
		ca("list_dashboards", "filter", "optional filter", "List %[1]s dashboards.", "Show %[1]s dashboards."),
		ca("silence_alert", "alert", "alert identifier", "Silence a %[1]s alert.", "Mute a %[1]s alert rule."),
	}},
	{"statuspage", "Statuspage", []connectorAction{
		ca("create_incident", "title", "incident title", "Open a %[1]s incident.", "Post a %[1]s incident."),
		ca("list_components", "filter", "optional filter", "List %[1]s components.", "Show %[1]s components."),
	}},
	{"opsgenie", "Opsgenie", []connectorAction{
		ca("list_alerts", "status", "alert status", "List %[1]s alerts.", "Show %[1]s alerts."),
		ca("close_alert", "alert", "alert identifier", "Close an %[1]s alert.", "Resolve an %[1]s alert."),
	}},
	{"brex", "Brex", []connectorAction{
		ca("list_expenses", "from", "start date", "List %[1]s card expenses.", "Show %[1]s expenses."),
		ca("upload_receipt", "expense", "expense identifier", "Attach a receipt to a %[1]s expense.", "Add a %[1]s receipt."),
	}},
	{"ramp", "Ramp", []connectorAction{
		ca("list_transactions", "from", "start date", "List %[1]s card transactions.", "Show %[1]s transactions."),
		ca("request_card", "purpose", "card purpose", "Request a %[1]s virtual card.", "Ask for a %[1]s card."),
	}},
	{"expensify", "Expensify", []connectorAction{
		ca("create_report", "title", "report title", "Create an %[1]s expense report.", "Start an %[1]s report."),
		ca("submit_report", "report", "report identifier", "Submit an %[1]s report.", "Send an %[1]s report for approval."),
	}},
	{"paypal", "PayPal", []connectorAction{
		ca("list_transactions", "from", "start date", "List %[1]s transactions.", "Show %[1]s activity."),
		ca("send_invoice", "recipient", "recipient email", "Send a %[1]s invoice.", "Bill someone through %[1]s."),
	}},
	{"square", "Square", []connectorAction{
		ca("list_payments", "location", "location identifier", "List %[1]s payments.", "Show %[1]s sales."),
		ca("update_item", "item", "catalog item", "Edit a %[1]s catalog item.", "Change a %[1]s item."),
	}},
	{"plaid", "Plaid", []connectorAction{
		ca("list_accounts", "filter", "optional filter", "List bank accounts linked through %[1]s.", "Show %[1]s accounts."),
		ca("refresh_balances", "account", "account identifier", "Refresh %[1]s balances.", "Re-sync %[1]s balances."),
	}},
	{"etsy", "Etsy", []connectorAction{
		ca("list_listings", "state", "listing state", "List %[1]s shop listings.", "Show %[1]s listings."),
		ca("list_receipts", "from", "start date", "List %[1]s order receipts.", "Show %[1]s orders."),
	}},
	{"amazon_seller", "Amazon Seller Central", []connectorAction{
		ca("list_orders", "status", "order status", "List %[1]s orders.", "Show %[1]s orders."),
		ca("inventory_summary", "sku", "product SKU", "Read %[1]s inventory.", "Fetch %[1]s stock."),
	}},
	{"ups", "UPS", []connectorAction{
		ca("track_package", "tracking", "tracking number", "Track a %[1]s package.", "Check a %[1]s shipment."),
		ca("create_label", "address", "destination address", "Create a %[1]s shipping label.", "Print a %[1]s label."),
	}},
	{"fedex", "FedEx", []connectorAction{
		ca("track_package", "tracking", "tracking number", "Track a %[1]s package.", "Check a %[1]s shipment."),
		ca("schedule_pickup", "address", "pickup address", "Schedule a %[1]s pickup.", "Book a %[1]s pickup."),
	}},
	{"canvas_lms", "Canvas LMS", []connectorAction{
		ca("list_courses", "filter", "optional filter", "List %[1]s courses.", "Show %[1]s courses."),
		ca("list_assignments", "course", "course identifier", "List %[1]s assignments.", "Show %[1]s assignments."),
	}},
	{"duolingo", "Duolingo", []connectorAction{
		ca("streak_status", "language", "language", "Read the %[1]s streak.", "Check %[1]s progress."),
	}},
	{"peloton", "Peloton", []connectorAction{
		ca("list_workouts", "from", "start date", "List %[1]s workouts.", "Show %[1]s rides."),
	}},
	{"fitbit", "Fitbit", []connectorAction{
		ca("sleep_log", "date", "date", "Read the %[1]s sleep log.", "Fetch %[1]s sleep data."),
		ca("step_count", "date", "date", "Read %[1]s step counts.", "Fetch %[1]s steps."),
	}},
	{"myfitnesspal", "MyFitnessPal", []connectorAction{
		ca("log_food", "food", "food item", "Log food in %[1]s.", "Add a %[1]s diary entry."),
	}},
	{"goodreads", "Goodreads", []connectorAction{
		ca("add_to_shelf", "book", "book title", "Add a book to a %[1]s shelf.", "Shelve a book on %[1]s."),
	}},
	{"letterboxd", "Letterboxd", []connectorAction{
		ca("log_film", "film", "film title", "Log a film on %[1]s.", "Add a %[1]s diary entry."),
	}},
	{"steam", "Steam", []connectorAction{
		ca("list_library", "filter", "optional filter", "List the %[1]s game library.", "Show %[1]s games."),
	}},
	{"twitch", "Twitch", []connectorAction{
		ca("list_followed", "filter", "optional filter", "List followed %[1]s channels.", "Show %[1]s follows."),
	}},
	{"sonos", "Sonos", []connectorAction{
		ca("set_volume", "level", "volume level", "Set %[1]s speaker volume.", "Change %[1]s volume."),
		ca("group_rooms", "rooms", "room names", "Group %[1]s rooms.", "Link %[1]s speakers."),
	}},
	{"philips_hue", "Philips Hue", []connectorAction{
		ca("set_scene", "scene", "scene name", "Set a %[1]s lighting scene.", "Change %[1]s lights."),
	}},
	{"nest", "Nest", []connectorAction{
		ca("set_temperature", "celsius", "target temperature", "Set the %[1]s thermostat.", "Change %[1]s temperature."),
	}},
	{"ring", "Ring", []connectorAction{
		ca("list_events", "from", "start date", "List %[1]s doorbell events.", "Show %[1]s motion events."),
	}},
	{"tesla", "Tesla", []connectorAction{
		ca("vehicle_status", "vehicle", "vehicle name", "Read %[1]s vehicle status.", "Check %[1]s charge level."),
		ca("precondition", "vehicle", "vehicle name", "Precondition a %[1]s cabin.", "Warm up a %[1]s."),
	}},
	{"lyft", "Lyft", []connectorAction{
		ca("request_ride", "pickup", "pickup address", "Request a %[1]s ride.", "Book a %[1]s trip."),
	}},
	{"expedia", "Expedia", []connectorAction{
		ca("search_flights", "route", "origin and destination", "Search %[1]s flights.", "Find %[1]s fares."),
		ca("list_itineraries", "filter", "optional filter", "List %[1]s itineraries.", "Show %[1]s trips."),
	}},
	{"tripit", "TripIt", []connectorAction{
		ca("list_trips", "filter", "optional filter", "List %[1]s trips.", "Show %[1]s plans."),
	}},
	{"zapier", "Zapier", []connectorAction{
		ca("list_zaps", "filter", "optional zap filter", "List %[1]s zaps.", "Show %[1]s automations."),
		ca("toggle_zap", "zap", "zap identifier", "Turn a %[1]s zap on or off.", "Toggle a %[1]s zap."),
	}},
	{"ifttt", "IFTTT", []connectorAction{
		ca("list_applets", "filter", "optional filter", "List %[1]s applets.", "Show %[1]s applets."),
	}},
	{"mailgun", "Mailgun", []connectorAction{
		ca("list_bounces", "domain", "sending domain", "List %[1]s bounces.", "Show %[1]s bounce records."),
	}},
	{"sendgrid", "SendGrid", []connectorAction{
		ca("list_suppressions", "group", "suppression group", "List %[1]s suppressions.", "Show %[1]s unsubscribes."),
	}},
	{"bamboohr", "BambooHR", []connectorAction{
		ca("whos_out", "date", "date", "List who is out in %[1]s.", "Show %[1]s time off."),
		ca("employee_directory", "filter", "optional filter", "Read the %[1]s directory.", "Show %[1]s employees."),
	}},
	{"lever", "Lever", []connectorAction{
		ca("list_opportunities", "posting", "posting identifier", "List %[1]s opportunities.", "Show %[1]s candidates."),
	}},
	{"rippling", "Rippling", []connectorAction{
		ca("list_devices", "filter", "optional filter", "List %[1]s managed devices.", "Show %[1]s devices."),
	}},
	{"vanta", "Vanta", []connectorAction{
		ca("list_failing_tests", "filter", "optional filter", "List failing %[1]s compliance tests.", "Show %[1]s test failures."),
	}},
	{"snyk", "Snyk", []connectorAction{
		ca("list_vulnerabilities", "project", "project identifier", "List %[1]s vulnerabilities.", "Show %[1]s issues."),
	}},
	{"postman", "Postman", []connectorAction{
		ca("run_collection", "collection", "collection name", "Run a %[1]s collection.", "Execute %[1]s requests."),
	}},
	{"readwise", "Readwise", []connectorAction{
		ca("list_highlights", "book", "book title", "List %[1]s highlights.", "Show %[1]s highlights."),
	}},
	{"pocket", "Pocket", []connectorAction{
		ca("list_saved", "filter", "optional filter", "List %[1]s saved items.", "Show %[1]s reading list."),
	}},
	{"evernote", "Evernote", []connectorAction{
		ca("search_notes", "query", "note search terms", "Search %[1]s notes.", "Find %[1]s notes."),
	}},
	{"obsidian", "Obsidian Sync", []connectorAction{
		ca("list_vault_files", "folder", "vault folder", "List files in an %[1]s vault.", "Show %[1]s vault files."),
	}},
	{"coda", "Coda", []connectorAction{
		ca("list_docs", "filter", "optional filter", "List %[1]s docs.", "Show %[1]s docs."),
		ca("upsert_row", "table", "table name", "Upsert a row in a %[1]s table.", "Write a %[1]s table row."),
	}},
	{"quip", "Quip", []connectorAction{
		ca("list_threads", "folder", "folder identifier", "List %[1]s threads.", "Show %[1]s documents."),
	}},
	{"hootsuite", "Hootsuite", []connectorAction{
		ca("schedule_post", "text", "post text", "Schedule a social post in %[1]s.", "Queue a %[1]s post."),
	}},
	{"buffer", "Buffer", []connectorAction{
		ca("list_queue", "profile", "profile name", "List the %[1]s posting queue.", "Show %[1]s queued posts."),
	}},
	{"canny", "Canny", []connectorAction{
		ca("list_posts", "board", "board name", "List %[1]s feature requests.", "Show %[1]s posts."),
	}},
	{"productboard", "Productboard", []connectorAction{
		ca("list_features", "filter", "optional filter", "List %[1]s features.", "Show %[1]s roadmap features."),
	}},
}

// connectorTool is one (app, action) entry of the frozen pool.
type connectorTool struct {
	app    connectorApp
	action connectorAction
}

func (t connectorTool) name() string { return t.app.Slug + "_" + t.action.Key }

// connectorUniverse flattens the pool in declaration order.
func connectorUniverse() []connectorTool {
	out := make([]connectorTool, 0, 256)
	for _, app := range connectorPool {
		for _, action := range app.Actions {
			out = append(out, connectorTool{app: app, action: action})
		}
	}
	return out
}

// ConnectorPoolSize is the number of distinct connector tools in the frozen
// pool (>= ConnectorToolsPerSeed by contract).
func ConnectorPoolSize() int { return len(connectorUniverse()) }

// connectorSuffix is the fixed tail every connector description carries, so a
// harness can tell a linkable connector from a product tool by reading it.
const connectorSuffix = " Needs the %[1]s connector to be linked in Settings."

// connectorDefinition renders one connector tool for a seed: one of its
// description variants, plus the shared connector suffix.
func connectorDefinition(seed int64, t connectorTool) protocol.ToolDefinition {
	does := t.action.Does[v13Pick(seed, "connector-desc:"+t.name(), len(t.action.Does))]
	return protocol.ToolDefinition{
		Name:        t.name(),
		Description: fmt.Sprintf(does+connectorSuffix, t.app.Name),
		Parameters: schema([]string{t.action.Param},
			prop{name: t.action.Param, typ: "string", desc: t.action.ParamDesc}),
	}
}

// ConnectorsForSeed returns the seed's ConnectorToolsPerSeed connector tools
// in pool order. Membership is a seeded permutation of the frozen pool, so
// which apps a workspace shows changes per seed.
func ConnectorsForSeed(seed int64) []protocol.ToolDefinition {
	universe := connectorUniverse()
	n := ConnectorToolsPerSeed
	if n > len(universe) {
		n = len(universe)
	}
	perm := v13Perm(seed, "connector-pick", len(universe))
	picked := append([]int(nil), perm[:n]...)
	sort.Ints(picked)
	out := make([]protocol.ToolDefinition, 0, n)
	for _, index := range picked {
		out = append(out, connectorDefinition(seed, universe[index]))
	}
	return out
}

// IsConnectorTool reports whether name is any tool of the frozen v15
// connector pool, with the app's display name. It is seed-independent: the
// mock endpoint refuses every connector name, offered on this seed or not.
func IsConnectorTool(name string) (appName string, ok bool) {
	for _, t := range connectorUniverse() {
		if t.name() == name {
			return t.app.Name, true
		}
	}
	return "", false
}

// v15SeededCatalog is the v13/v14 seeded surface with the seed's connector
// tools spliced in at seeded positions. The v13 surface's own order is kept,
// so every product tool and decoy keeps its relative position.
func v15SeededCatalog(seed int64) []protocol.ToolDefinition {
	return spliceConnectors(seed, v13SeededCatalog(seed), ConnectorsForSeed(seed))
}

// spliceConnectors inserts connector tools into the seeded base surface at
// seeded positions (shared by v15 and v16; the slot keys are v15's).
func spliceConnectors(seed int64, base, connectors []protocol.ToolDefinition) []protocol.ToolDefinition {
	slots := make([]int, len(connectors))
	for i := range connectors {
		slots[i] = v13Pick(seed, fmt.Sprintf("connector-slot:%d", i), len(base)+1)
	}
	order := make([]int, len(connectors))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool { return slots[order[a]] < slots[order[b]] })
	out := make([]protocol.ToolDefinition, 0, len(base)+len(connectors))
	next := 0
	for pos := 0; pos <= len(base); pos++ {
		for next < len(order) && slots[order[next]] == pos {
			out = append(out, connectors[order[next]])
			next++
		}
		if pos < len(base) {
			out = append(out, base[pos])
		}
	}
	return out
}
