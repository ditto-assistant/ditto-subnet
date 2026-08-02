//! The Ditto tool catalog presented to harnesses. Each entry has a name, a
//! short description, and a minimal JSON-schema parameter definition. This is
//! the tool menu a harness sees on every `RunRequest` (mirrors the Go
//! validator's `internal/catalog`).

use serde_json::{json, Value};

use crate::protocol::ToolDefWire;

/// Builds a JSON-schema object for a tool's parameters. `props` is a list of
/// `(name, description)`; `required` lists required property names.
fn params(props: &[(&str, &str)], required: &[&str]) -> Value {
    let mut properties = serde_json::Map::new();
    for (name, desc) in props {
        properties.insert(
            (*name).to_string(),
            json!({ "type": "string", "description": desc }),
        );
    }
    let mut schema = serde_json::Map::new();
    schema.insert("type".to_string(), json!("object"));
    schema.insert("properties".to_string(), Value::Object(properties));
    if !required.is_empty() {
        schema.insert(
            "required".to_string(),
            Value::Array(required.iter().map(|r| json!(r)).collect()),
        );
    }
    Value::Object(schema)
}

fn tool(name: &str, description: &str, parameters: Value) -> ToolDefWire {
    ToolDefWire {
        name: name.to_string(),
        description: description.to_string(),
        parameters,
    }
}

/// Returns the full Ditto tool catalog.
pub fn catalog() -> Vec<ToolDefWire> {
    vec![
        tool(
            "create_image",
            "Generate a new image from a text prompt.",
            params(&[("prompt", "what to draw")], &["prompt"]),
        ),
        tool(
            "edit_image",
            "Edit an existing image given an instruction.",
            params(
                &[
                    ("image_url", "image to edit"),
                    ("instruction", "how to change it"),
                ],
                &["image_url", "instruction"],
            ),
        ),
        tool(
            "read_links",
            "Fetch and read the contents of one or more URLs.",
            params(&[("url", "the URL to read")], &["url"]),
        ),
        tool(
            "search_web",
            "Search the public web for current information.",
            params(&[("query", "the search query")], &["query"]),
        ),
        tool(
            "search_memories",
            "Search the user's long-term memories by semantic query.",
            params(&[("query", "what to recall")], &["query"]),
        ),
        tool(
            "search_subjects",
            "Find subject/topic clusters in the user's memories.",
            params(&[("query", "topic to find")], &["query"]),
        ),
        tool(
            "fetch_memories",
            "Fetch specific memories by ID or outline.",
            params(&[("ids", "comma-separated memory IDs")], &[]),
        ),
        tool(
            "search_memories_in_subjects",
            "Search memories scoped to specific subjects.",
            params(
                &[
                    ("query", "what to recall"),
                    ("subjects", "subjects to scope to"),
                ],
                &["query"],
            ),
        ),
        tool(
            "artifacts",
            "Create an interactive, previewable artifact (web app, doc, game).",
            params(&[("spec", "what to build")], &["spec"]),
        ),
        tool(
            "execute_agent_job",
            "Dispatch a one-off background agent job.",
            params(&[("task", "the task to run")], &["task"]),
        ),
        tool(
            "execute_agent_workflow",
            "Run a predefined multi-step agent workflow.",
            params(
                &[("workflow", "workflow name"), ("input", "workflow input")],
                &["workflow"],
            ),
        ),
        tool(
            "get_agent_job_status",
            "Check the status of a running or finished agent job.",
            params(&[("job_id", "the job ID")], &["job_id"]),
        ),
        tool(
            "list_agent_jobs",
            "List the user's recent agent jobs.",
            params(&[("limit", "max number to return")], &[]),
        ),
        tool(
            "file_feedback_for_team",
            "Send feedback or a bug report to the Ditto team.",
            params(&[("message", "the feedback message")], &["message"]),
        ),
        tool(
            "set_theme",
            "Change the app's color theme.",
            params(&[("theme", "theme name, e.g. dark or light")], &["theme"]),
        ),
        tool(
            "set_main_model",
            "Change the primary chat model.",
            params(&[("model", "model identifier")], &["model"]),
        ),
        tool(
            "set_reasoning_effort",
            "Set the reasoning effort level for responses.",
            params(&[("effort", "low, medium, or high")], &["effort"]),
        ),
        tool(
            "set_chat_tool_preferences",
            "Enable or disable specific tools for chat.",
            params(
                &[("preferences", "tool preference settings")],
                &["preferences"],
            ),
        ),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_has_all_expected_tools() {
        let c = catalog();
        assert_eq!(c.len(), 18);
        let names: Vec<&str> = c.iter().map(|t| t.name.as_str()).collect();
        for expected in [
            "create_image",
            "search_web",
            "search_memories",
            "execute_agent_job",
            "set_theme",
            "set_chat_tool_preferences",
        ] {
            assert!(names.contains(&expected), "missing tool {expected}");
        }
    }

    #[test]
    fn every_tool_has_object_schema() {
        for t in catalog() {
            assert_eq!(t.parameters["type"], "object", "tool {}", t.name);
            assert!(t.parameters.get("properties").is_some(), "tool {}", t.name);
        }
    }
}
