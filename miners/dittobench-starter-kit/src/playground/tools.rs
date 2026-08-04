//! Fake action tools for the playground: every non-memory catalog tool returns
//! a plausible canned result instead of executing, so the agent loop runs
//! realistically (tool selection + multi-hop) without real integrations.

use async_trait::async_trait;
use ditto_harness::types::{Result as HarnessResult, Tool, ToolDefinition};
use serde_json::{json, Value};

/// A tool that returns a plausible canned result instead of executing. Lets the
/// agent loop run realistically so you can watch tool selection + multi-hop use.
/// (The call + result are recovered from the run transcript, alongside the real
/// memory-tool calls — see `playground_turn`.)
pub(super) struct FakeTool {
    pub(super) def: ToolDefinition,
}

#[async_trait]
impl Tool for FakeTool {
    fn definition(&self) -> ToolDefinition {
        self.def.clone()
    }

    async fn execute(&self, args: Value) -> HarnessResult<Value> {
        Ok(fake_response(&self.def.name, &args))
    }
}

/// Plausible canned result per tool name (matches prod semantics: proposals are
/// not applied, agent jobs return approval envelopes, etc.).
fn fake_response(name: &str, args: &Value) -> Value {
    let s = |k: &str| {
        args.get(k)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    // The v8 catalog uses plural arrays for batched search and link reads; keep
    // accepting the singular form in the playground for creative model output.
    let list = |singular: &str, plural: &str| -> Vec<String> {
        if let Some(one) = args.get(singular).and_then(Value::as_str) {
            return vec![one.to_string()];
        }
        args.get(plural)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default()
    };
    match name {
        "search_web" => {
            let queries = list("query", "queries");
            let groups: Vec<Value> = queries
                .iter()
                .map(|q| {
                    json!({
                        "query": q,
                        "results": [
                            {"title": format!("{q}: the definitive 2026 guide"), "url": "https://example.com/guide",
                             "snippet": format!("Authoritative overview of {q}. Top recommendation: the \"Aurora Trailblazer Pro\" — widely rated #1 for {q}, praised for comfort, grip, and durability.")},
                            {"title": format!("Best of {q} — expert roundup"), "url": "https://reviews.example.com/roundup",
                             "snippet": format!("Runner-up picks for {q}: the \"Summit Glide\" (budget) and \"Vector Edge\" (performance). All three are well-reviewed in 2026.")}
                        ]
                    })
                })
                .collect();
            json!({
                "queries": groups,
                "sufficient": true,
                "guidance": "These results fully answer the query. Do NOT search again — write your final answer now, citing the result links.",
                "note": "FAKE search results (playground)"
            })
        }
        "read_links" => {
            let pages: Vec<Value> = list("url", "urls")
                .iter()
                .map(|u| json!({"url": u, "markdown": format!("# Page: {u}\n\nKey points: this page provides a complete, authoritative answer for your query. Use it directly.")}))
                .collect();
            json!({
                "pages": pages,
                "sufficient": true,
                "guidance": "Page content retrieved. Do NOT read more links — answer now using this content.",
                "note": "FAKE page content (playground)"
            })
        }
        "create_image" => json!({
            "status": "created", "artifact_id": "img_fake_001",
            "image_url": "ditto://artifact/playground/img_fake_001",
            "prompt": s("prompt"), "note": "FAKE image (playground)"
        }),
        "edit_image" => json!({
            "status": "created", "artifact_id": "img_fake_002",
            "image_url": "ditto://artifact/playground/img_fake_002",
            "note": "FAKE edited image (playground)"
        }),
        "execute_agent_job" => json!({
            "job_approval_proposal": {
                "status": "awaiting_approval", "agent": "Ditto Code",
                "task": s("task"), "estimated_tokens": 12000,
                "note": "FAKE proposal — the user would click Apply to run it (playground)"
            }
        }),
        "create_workflow" => json!({
            "workflow_approval_proposal": {
                "status": "awaiting_approval", "agent": "Ditto Code",
                "name": s("name"), "steps": args.get("steps").cloned().unwrap_or_default(),
                "schedule": s("schedule"),
                "note": "FAKE workflow proposal (playground)"
            }
        }),
        "list_agent_jobs" => json!({
            "jobs": [
                {"job_id": "job_fake_1", "status": "completed", "prompt": "build a scraper"},
                {"job_id": "job_fake_2", "status": "running", "prompt": "refactor module"}
            ], "note": "FAKE job list (playground)"
        }),
        "list_workflows" => json!({
            "workflows": [
                {"recipe_id": "workflow_fake_1", "name": "Weekly research", "schedule": "0 9 * * 1"}
            ], "note": "FAKE workflow list (playground)"
        }),
        "list_schedules" => json!({
            "schedules": [
                {"recipe_id": "workflow_fake_1", "schedule": "0 9 * * 1"}
            ], "note": "FAKE schedule list (playground)"
        }),
        "run_workflow" => json!({
            "workflow_run_proposal": {
                "status": "awaiting_approval",
                "recipe_id": s("recipe_id"),
                "name": s("name"),
                "note": "FAKE workflow run proposal (playground)"
            }
        }),
        "file_feedback_for_team" => json!({
            "status": "filed", "ticket_id": "FB-FAKE-123",
            "message": s("message"),
            "note": "FAKE: feedback recorded; team can follow up (playground)"
        }),
        // Settings tools all return a proposal the user would Apply.
        n if n.starts_with("set_") => json!({
            "status": "proposed", "tool": n, "args": args,
            "note": "FAKE proposal — not applied; the user confirms with Apply (playground)"
        }),
        // Artifacts: pretend the operation succeeded.
        "artifacts" => json!({
            "status": "ok", "spec": s("spec"),
            "artifact_id": "art_fake_001",
            "note": "FAKE artifact op (playground)"
        }),
        _ => json!({ "status": "ok", "note": format!("FAKE result for {name} (playground)") }),
    }
}
