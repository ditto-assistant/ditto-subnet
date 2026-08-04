//! The DittoBench v8 tool catalog. It intentionally excludes the retired v7
//! workflow, automation, recipe, and job-status tools so local practice presents
//! exactly the product surface scored by the active benchmark.

use serde_json::{json, Value};

use crate::protocol::ToolDefWire;

#[derive(Clone, Copy)]
enum SchemaType {
    String,
    StringArray,
    Array,
    Integer,
    Boolean,
}

#[derive(Clone, Copy)]
struct Prop {
    name: &'static str,
    kind: SchemaType,
    description: &'static str,
}

fn prop(name: &'static str, kind: SchemaType, description: &'static str) -> Prop {
    Prop {
        name,
        kind,
        description,
    }
}

fn schema(props: &[Prop], required: &[&str]) -> Value {
    let mut properties = serde_json::Map::new();
    for prop in props {
        let value = match prop.kind {
            SchemaType::String => {
                json!({"type": "string", "description": prop.description})
            }
            SchemaType::StringArray => json!({
                "type": "array",
                "items": {"type": "string"},
                "description": prop.description,
            }),
            SchemaType::Array => {
                json!({"type": "array", "description": prop.description})
            }
            SchemaType::Integer => {
                json!({"type": "integer", "description": prop.description})
            }
            SchemaType::Boolean => {
                json!({"type": "boolean", "description": prop.description})
            }
        };
        properties.insert(prop.name.to_string(), value);
    }
    let mut out = serde_json::Map::new();
    out.insert("type".to_string(), json!("object"));
    out.insert("properties".to_string(), Value::Object(properties));
    if !required.is_empty() {
        out.insert("required".to_string(), json!(required));
    }
    Value::Object(out)
}

fn strings(props: &[(&'static str, &'static str)], required: &[&str]) -> Value {
    let props = props
        .iter()
        .map(|(name, description)| prop(name, SchemaType::String, description))
        .collect::<Vec<_>>();
    schema(&props, required)
}

fn tool(name: &str, description: &str, parameters: Value) -> ToolDefWire {
    ToolDefWire {
        name: name.to_string(),
        description: description.to_string(),
        parameters,
    }
}

/// Returns the active DittoBench v8 tool catalog.
pub fn catalog() -> Vec<ToolDefWire> {
    vec![
        tool(
            "create_image",
            "Generate a new image from a text prompt.",
            strings(&[("prompt", "what to draw")], &["prompt"]),
        ),
        tool(
            "edit_image",
            "Edit an existing image given an instruction.",
            strings(
                &[
                    ("image_url", "image to edit"),
                    ("instruction", "how to change it"),
                ],
                &["image_url", "instruction"],
            ),
        ),
        tool(
            "read_links",
            "Read one or more URLs and return markdown text content.",
            schema(
                &[prop("urls", SchemaType::StringArray, "URLs to read")],
                &["urls"],
            ),
        ),
        tool(
            "search_web",
            "Search live sources for one or more queries.",
            schema(
                &[
                    prop("queries", SchemaType::StringArray, "Search queries"),
                    prop(
                        "num_results",
                        SchemaType::Integer,
                        "Number of results per query, default 10",
                    ),
                    prop(
                        "search_mode",
                        SchemaType::String,
                        "Optional source filter, default all",
                    ),
                ],
                &["queries"],
            ),
        ),
        tool(
            "search_memories",
            "Search past conversations and return compact memory summaries. Use fetch_memories for selected IDs that need full text. For named entities/topics, prefer search_subjects -> search_memories_in_subjects.",
            schema(
                &[prop(
                    "queries",
                    SchemaType::StringArray,
                    "Memory search queries",
                )],
                &["queries"],
            ),
        ),
        tool(
            "search_subjects",
            "Search the user's subject graph and return subject objects with id, name, description, and similarity.",
            schema(
                &[prop(
                    "queries",
                    SchemaType::StringArray,
                    "Subject search queries",
                )],
                &["queries"],
            ),
        ),
        tool(
            "fetch_memories",
            "Fetch full conversation text for selected memory pair IDs.",
            schema(
                &[
                    prop(
                        "pairIds",
                        SchemaType::StringArray,
                        "Memory pair IDs to fetch",
                    ),
                    prop(
                        "stripImages",
                        SchemaType::Boolean,
                        "Exclude images, default true",
                    ),
                ],
                &["pairIds"],
            ),
        ),
        tool(
            "search_memories_in_subjects",
            "Semantic memory search inside one subject. Use focused queries; fetch selected IDs for full text.",
            schema(
                &[
                    prop(
                        "subject_id",
                        SchemaType::String,
                        "Subject ID from search_subjects or the prompt",
                    ),
                    prop(
                        "queries",
                        SchemaType::StringArray,
                        "Focused queries within the subject",
                    ),
                ],
                &["subject_id", "queries"],
            ),
        ),
        tool(
            "artifacts",
            "Create an interactive, previewable artifact (web app, doc, game).",
            strings(&[("spec", "what to build")], &["spec"]),
        ),
        tool(
            "execute_agent_job",
            "Dispatch a one-off background agent job to Ditto Code — a sandboxed coding harness that can write and run code, install packages, run shell commands, and edit files in a workspace. Use for real coding/automation work that needs the file system, the network, or a repo. For a pure in-context calculation with no side effects, use run_code instead.",
            strings(&[("task", "the task to run")], &["task"]),
        ),
        tool(
            "run_code",
            "Execute JavaScript in a fast in-process sandbox to compute a result or chain tool calls (Code Mode). Use for a one-off calculation, data transformation, or orchestrating several tools over their results — it has NO file system, network, or package installs. For real coding work (writing a repo, running shell, editing files) use execute_agent_job instead.",
            strings(&[("code", "the JavaScript to execute")], &["code"]),
        ),
        tool(
            "search_tools",
            "Search the available tools by keyword and return the matching tool signatures. Use inside Code Mode to discover which tool binding to call before writing run_code.",
            strings(
                &[("query", "keywords describing the capability you need")],
                &["query"],
            ),
        ),
        tool(
            "list_agent_jobs",
            "List the user's recent agent jobs.",
            schema(
                &[
                    prop(
                        "status",
                        SchemaType::String,
                        "Optional pending, running, completed, failed, or cancelled filter",
                    ),
                    prop(
                        "limit",
                        SchemaType::Integer,
                        "Max results, default 20 and max 50",
                    ),
                ],
                &[],
            ),
        ),
        tool(
            "create_workflow",
            "Propose a reusable workflow made of one or more inspectable steps. A schedule, when requested, is part of this same proposal.",
            schema(
                &[
                    prop("name", SchemaType::String, "Short workflow name"),
                    prop("steps", SchemaType::Array, "Workflow steps"),
                    prop(
                        "schedule",
                        SchemaType::String,
                        "Optional JSON schedule for the workflow",
                    ),
                ],
                &["name", "steps"],
            ),
        ),
        tool(
            "list_workflows",
            "List saved workflows with their schedules.",
            schema(&[], &[]),
        ),
        tool(
            "list_schedules",
            "List schedules attached to the user's workflows.",
            schema(&[], &[]),
        ),
        tool(
            "run_workflow",
            "Propose running one saved workflow now.",
            schema(
                &[
                    prop(
                        "recipe_id",
                        SchemaType::String,
                        "Workflow id from list_workflows",
                    ),
                    prop(
                        "name",
                        SchemaType::String,
                        "Workflow name when its id is unknown",
                    ),
                ],
                &[],
            ),
        ),
        tool(
            "file_feedback_for_team",
            "Send feedback or a bug report to the Ditto team.",
            strings(&[("message", "the feedback message")], &["message"]),
        ),
        tool(
            "set_theme",
            "Change the app's color theme.",
            strings(&[("theme", "theme name, e.g. dark or light")], &["theme"]),
        ),
        tool(
            "set_main_model",
            "Change the primary chat model.",
            strings(&[("model", "model identifier")], &["model"]),
        ),
        tool(
            "set_reasoning_effort",
            "Set the reasoning effort level for responses.",
            strings(&[("effort", "low, medium, or high")], &["effort"]),
        ),
        tool(
            "set_chat_tool_preferences",
            "Enable or disable specific tools for chat.",
            strings(
                &[("preferences", "tool preference settings")],
                &["preferences"],
            ),
        ),
        tool(
            "discover_capabilities",
            "Look up what Ditto can do and how to use a feature. Call whenever the user asks what you or Ditto can do, where a setting lives, or how to accomplish something in the app.",
            strings(
                &[("query", "what the user is trying to do")],
                &[],
            ),
        ),
        tool(
            "save_memory",
            "Save a new fact the user states about themselves to long-term memory. Use when the user TELLS you something to remember, not when they ask a question.",
            strings(&[("content", "the fact to remember")], &["content"]),
        ),
        tool(
            "update_memory",
            "Update an existing remembered fact to a new value.",
            strings(
                &[
                    ("pair_id", "memory to update"),
                    ("content", "the new value"),
                ],
                &["pair_id", "content"],
            ),
        ),
        tool(
            "delete_memory",
            "Delete a remembered fact at the user's request.",
            strings(&[("pair_id", "memory to delete")], &["pair_id"]),
        ),
        tool(
            "calendar_create_event",
            "Create an event on the user's Google Calendar.",
            strings(
                &[("title", "event title"), ("when", "date or time")],
                &["title", "when"],
            ),
        ),
        tool(
            "calendar_search_events",
            "Search the user's Google Calendar for events.",
            strings(&[("query", "what to find")], &["query"]),
        ),
        tool(
            "gmail_send",
            "Send an email from the user's Gmail account.",
            strings(
                &[
                    ("to", "recipient"),
                    ("subject", "subject"),
                    ("body", "message body"),
                ],
                &["to", "body"],
            ),
        ),
        tool(
            "set_accent_color",
            "Change the app's accent color.",
            strings(&[("color", "accent color")], &["color"]),
        ),
        tool(
            "set_chat_font",
            "Change the font used in the chat view.",
            strings(&[("font", "font name")], &["font"]),
        ),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_matches_active_v8_surface() {
        let catalog = catalog();
        assert_eq!(catalog.len(), 31);
        let names = catalog
            .iter()
            .map(|tool| tool.name.as_str())
            .collect::<Vec<_>>();
        for expected in [
            "read_links",
            "search_web",
            "run_code",
            "search_tools",
            "create_workflow",
            "list_workflows",
            "list_schedules",
            "run_workflow",
            "discover_capabilities",
            "save_memory",
            "calendar_create_event",
            "gmail_send",
        ] {
            assert!(names.contains(&expected), "missing tool {expected}");
        }
    }

    #[test]
    fn catalog_uses_current_argument_shapes() {
        let catalog = catalog();
        let named = |name: &str| {
            catalog
                .iter()
                .find(|tool| tool.name == name)
                .unwrap_or_else(|| panic!("missing tool {name}"))
        };

        assert_eq!(
            named("read_links").parameters["properties"]["urls"]["type"],
            "array"
        );
        assert_eq!(
            named("search_web").parameters["properties"]["queries"]["type"],
            "array"
        );
        assert_eq!(
            named("fetch_memories").parameters["properties"]["pairIds"]["type"],
            "array"
        );
        assert_eq!(
            named("create_workflow").parameters["required"],
            json!(["name", "steps"])
        );
        assert_eq!(
            named("create_workflow").parameters["properties"]["steps"]["type"],
            "array"
        );
        assert_eq!(
            named("list_agent_jobs").parameters["properties"]["limit"]["type"],
            "integer"
        );
        for retired in [
            "execute_agent_workflow",
            "get_agent_job_status",
            "create_automation",
            "list_automations",
            "create_recipe",
            "apply_recipe",
        ] {
            assert!(
                catalog.iter().all(|tool| tool.name != retired),
                "v8 catalog still advertises retired tool {retired}"
            );
        }
    }

    #[test]
    fn every_tool_has_an_object_schema() {
        for tool in catalog() {
            assert_eq!(tool.parameters["type"], "object", "tool {}", tool.name);
            assert!(
                tool.parameters.get("properties").is_some(),
                "tool {}",
                tool.name
            );
        }
    }
}
