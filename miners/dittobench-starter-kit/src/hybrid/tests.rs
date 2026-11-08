//! Hybrid path tests against a stubbed decisions endpoint, a scripted chat
//! model and recording tools. No network beyond loopback.

use std::sync::Mutex;

use async_trait::async_trait;
use axum::{extract::State, routing::post, Json, Router};
use ditto_harness::types::{
    ChatChunk, CostedUsage, Result as HarnessResult, ToolCall, ToolDefinition, Usage,
};

use super::*;

// ----------------------------------------------------------------- stubs

/// Decision stub: a `choice` answers the first option found in `prefer`
/// (probability 0.9), else its reference option; a `noul` answers from
/// `nouls` (default 0.1). Every request body is recorded.
#[derive(Clone, Default)]
struct JevStub {
    prefer: Vec<String>,
    nouls: HashMap<String, f64>,
    seen: Arc<Mutex<Vec<Value>>>,
}

async fn jev_handler(State(stub): State<JevStub>, Json(body): Json<Value>) -> Json<Value> {
    stub.seen.lock().unwrap().push(body.clone());
    let mut answers = serde_json::Map::new();
    for (id, q) in body["questions"].as_object().unwrap() {
        match q["type"].as_str().unwrap() {
            "noul" => {
                let p = stub.nouls.get(id).copied().unwrap_or(0.1);
                answers.insert(id.clone(), json!({"type": "noul", "noul": p}));
            }
            "choice" => {
                let names: Vec<&String> = q["criteria"].as_object().unwrap().keys().collect();
                let pick = stub
                    .prefer
                    .iter()
                    .find(|p| names.iter().any(|n| n == p))
                    .cloned()
                    .or_else(|| {
                        names
                            .iter()
                            .find(|n| {
                                [NONE_OPTION, NO_TOOL_OPTION, NO_NOTE_OPTION, "none"]
                                    .contains(&n.as_str())
                            })
                            .map(|n| (*n).clone())
                    })
                    .unwrap_or_else(|| names[0].clone());
                let rest = 0.1 / (names.len().max(2) - 1) as f64;
                let probs: serde_json::Map<String, Value> = names
                    .iter()
                    .map(|n| ((*n).clone(), json!(if **n == pick { 0.9 } else { rest })))
                    .collect();
                answers.insert(
                    id.clone(),
                    json!({"type": "choice", "choice": pick, "confidence": 0.9, "probabilities": probs}),
                );
            }
            other => panic!("unexpected question type {other}"),
        }
    }
    let input = body.to_string().len() as i64 / 4;
    Json(json!({"model": "jev-1.14.0", "answers": answers,
                "usage": {"input_tokens": input, "output_tokens": 3}}))
}

async fn serve_jev(stub: JevStub) -> String {
    let app = Router::new()
        .route("/decisions", post(jev_handler))
        .with_state(stub);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{addr}/decisions")
}

/// Scripted chat model: returns `script` in order, records each call.
struct ScriptedModel {
    script: Mutex<Vec<ChatChunk>>,
    calls: Mutex<Vec<(Vec<ChatMessage>, Vec<String>)>>,
}

impl ScriptedModel {
    fn new(script: Vec<ChatChunk>) -> ScriptedModel {
        ScriptedModel {
            script: Mutex::new(script.into_iter().rev().collect()),
            calls: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl Model for ScriptedModel {
    async fn next(&self, messages: &[ChatMessage], tools: &[ToolDefinition]) -> HarnessResult<ChatChunk> {
        self.calls.lock().unwrap().push((
            messages.to_vec(),
            tools.iter().map(|t| t.name.clone()).collect(),
        ));
        let mut chunk = self.script.lock().unwrap().pop().unwrap_or_else(|| ChatChunk {
            text: "done".into(),
            ..ChatChunk::default()
        });
        let prompt: usize = messages
            .iter()
            .flat_map(|m| m.content.iter())
            .map(|c| c.content.len())
            .sum();
        chunk.cost = Some(CostedUsage {
            usage: Usage {
                input_tokens: (prompt / 4) as i64,
                output_tokens: 20,
                ..Usage::default()
            },
            ..CostedUsage::default()
        });
        Ok(chunk)
    }
}

fn call(name: &str, args: Value) -> ChatChunk {
    let tc = ToolCall {
        id: format!("call-{name}"),
        name: name.into(),
        args,
    };
    ChatChunk {
        tool_call: Some(tc.clone()),
        tool_calls: vec![tc],
        ..ChatChunk::default()
    }
}

fn text(t: &str) -> ChatChunk {
    ChatChunk {
        text: t.into(),
        ..ChatChunk::default()
    }
}

struct RecordingTool {
    def: ToolDefinition,
    seen: Arc<Mutex<Vec<(String, Value)>>>,
}

#[async_trait]
impl Tool for RecordingTool {
    fn definition(&self) -> ToolDefinition {
        self.def.clone()
    }
    async fn execute(&self, args: Value) -> HarnessResult<Value> {
        self.seen.lock().unwrap().push((self.def.name.clone(), args));
        Ok(json!({"result": format!("{} ok: event id evt-12345", self.def.name)}))
    }
}

/// A v18-sized menu: 1,450 bulk tools in 15 toolkits plus a few first-party
/// tools, one of which (`calendar_create_event`) the request needs.
fn catalog() -> Vec<ToolDefWire> {
    let mut tools = vec![
        ToolDefWire {
            name: "calendar_create_event".into(),
            description: "Ditto: Create a calendar event with a title and a start time.".into(),
            parameters: json!({"type": "object", "required": ["title", "start"],
                "properties": {"title": {"type": "string", "description": "Event title."},
                               "start": {"type": "string", "description": "ISO start time."}}}),
        },
        ToolDefWire {
            name: "search_web".into(),
            description: "Ditto: Search the web for fresh information.".into(),
            parameters: json!({"type": "object", "required": ["query"],
                "properties": {"query": {"type": "string"}}}),
        },
        ToolDefWire {
            name: "search_memories".into(),
            description: "Search the user's memories.".into(),
            parameters: json!({"type": "object"}),
        },
    ];
    for kit in 0..15 {
        for i in 0..97 {
            tools.push(ToolDefWire {
                name: format!("KIT{kit}_ACTION_{i}"),
                description: format!(
                    "Kit{kit}: Perform bulk action {i} on records of the kit {kit} service when the user asks for it."
                ),
                parameters: json!({"type": "object", "properties": {"input": {"type": "string"}}}),
            });
        }
    }
    tools
}

fn executable(
    catalog: &[ToolDefWire],
    seen: &Arc<Mutex<Vec<(String, Value)>>>,
) -> HashMap<String, Arc<dyn Tool>> {
    catalog
        .iter()
        .map(|d| {
            let tool: Arc<dyn Tool> = Arc::new(RecordingTool {
                def: ToolDefinition {
                    name: d.name.clone(),
                    description: d.description.clone(),
                    input_schema: d.parameters.clone(),
                },
                seen: Arc::clone(seen),
            });
            (d.name.clone(), tool)
        })
        .collect()
}

fn notes() -> Vec<Note> {
    vec![
        Note { text: "[2026-10-01] The roadmap review moved to Thursday at 10:00.".into() },
        Note { text: "[2026-09-12] Priya prefers morning meetings.".into() },
        Note { text: "[2026-08-30] The cat is called Miso.".into() },
    ]
}

const REQUEST: &str = "Put the \"Roadmap review\" on my calendar for 2026-11-12T10:00.";

fn case<'a>(
    catalog: &'a [ToolDefWire],
    seen: &Arc<Mutex<Vec<(String, Value)>>>,
) -> HybridCase<'a> {
    HybridCase {
        case_id: "case-1",
        user_input: REQUEST,
        system_prompt: "You are Ditto, a helpful assistant with access to tools.",
        catalog,
        executable: executable(catalog, seen),
        memory_tools: vec!["search_memories".into()],
        notes: notes(),
    }
}

fn user_prompt(messages: &[ChatMessage]) -> String {
    messages
        .iter()
        .filter(|m| m.role == "user")
        .flat_map(|m| m.content.iter().map(|c| c.content.clone()))
        .collect::<Vec<_>>()
        .join("\n")
}

// ----------------------------------------------------------------- tests

#[test]
fn hybrid_is_off_unless_opted_in() {
    assert!(!enabled_from(None));
    assert!(!enabled_from(Some("")));
    assert!(!enabled_from(Some("0")));
    assert!(enabled_from(Some("1")));
    assert!(enabled_from(Some(" TRUE ")));
}

#[test]
fn candidates_cover_the_general_value_shapes() {
    let got = extract_candidates(
        &[
            "Email ana.ruiz@example.com the link https://ex.am/ple?x=1, quote \"Q3 plan\".",
            "Invoice INV-2041 is $4,110.67 due 2026-11-30; ask Priya Natarajan.",
        ],
        40,
    );
    for want in [
        "ana.ruiz@example.com",
        "https://ex.am/ple?x=1",
        "Q3 plan",
        "INV-2041",
        "$4,110.67",
        "2026-11-30",
        "Priya Natarajan",
    ] {
        assert!(got.iter().any(|g| g == want), "missing {want} in {got:?}");
    }
    assert_eq!(extract_candidates(&["a b c"], 40), Vec::<String>::new());
}

#[test]
fn a_clear_winner_travels_alone() {
    let ranked = vec![("a".to_string(), 90.0), ("b".to_string(), 3.0), ("c".to_string(), 1.0)];
    assert_eq!(top_relative(ranked.clone(), 3), vec!["a"]);
    let close = vec![("a".to_string(), 9.0), ("b".to_string(), 8.0), ("c".to_string(), 0.1)];
    assert_eq!(top_relative(close, 3), vec!["a", "b"]);
    assert_eq!(top_relative(ranked, 0), Vec::<String>::new());
}

#[test]
fn toolkits_group_by_name_prefix() {
    let (opts, members) = toolkit_options(&catalog(), 80);
    assert_eq!(members["kit3"].len(), 97);
    assert!(opts.len() < 255, "one grouped question covers the menu");
    assert_eq!(toolkit_of("GMAIL_SEND_EMAIL"), "gmail");
    assert_eq!(toolkit_of("gmail_send"), "gmail");
}

#[tokio::test]
async fn jev_selects_tools_and_values_and_the_model_sees_a_small_prompt() {
    let stub = JevStub {
        prefer: vec![
            "act".into(),
            "calendar_create_event".into(),
            "n0".into(),
            // Argument candidates: c0 = "Roadmap review" (quoted span first).
            "c0".into(),
        ],
        ..JevStub::default()
    };
    let seen_requests = Arc::clone(&stub.seen);
    let url = serve_jev(stub).await;
    let client = DecisionClient::new(reqwest::Client::new(), url, "typesafe/jev-1.14", None);
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![
        call(
            "calendar_create_event",
            json!({"title": "Roadmap review", "start": "2026-11-12T10:00"}),
        ),
        text("Booked the Roadmap review for 12 November at 10:00."),
    ]);

    // Two notes: Jev's ranking decides which records reach the prompt.
    let cfg = HybridConfig {
        notes_k: 2,
        ..HybridConfig::default()
    };
    let out = run_case(&model, Some(&client), &cfg, case(&catalog, &executed))
        .await
        .unwrap();

    // The model's own text is served and its call was executed.
    assert_eq!(out.final_text, "Booked the Roadmap review for 12 November at 10:00.");
    assert_eq!(out.tool_calls.len(), 1);
    assert_eq!(executed.lock().unwrap()[0].0, "calendar_create_event");
    assert_eq!(out.route, "act");

    // Pass 1 has no menu; pass 2 is the whole menu in one request, at most
    // 255 options a question.
    let requests = seen_requests.lock().unwrap().clone();
    assert!(!requests[0]["questions"].as_object().unwrap().keys().any(|k| k.starts_with("tool")));
    let first = &requests[1];
    let mut asked = 0;
    for (id, q) in first["questions"].as_object().unwrap() {
        if let Some(criteria) = q.get("criteria").and_then(Value::as_object) {
            assert!(criteria.len() <= 255, "{id} has {} options", criteria.len());
            if id.starts_with("tool") {
                asked += criteria.len() - 1;
            }
        }
    }
    assert_eq!(asked, catalog.len(), "every tool in one request");
    assert_eq!(first["model"], "typesafe/jev-1.14");
    assert!(out.decision_calls >= 4, "route, menu, argument pass, re-plan");
    assert!(out.menu_asked);
    assert!(out.decision_input_tokens > 0);

    // The chat model saw a handful of tools, never the menu.
    let calls = model.calls.lock().unwrap();
    let (messages, offered) = &calls[0];
    assert!(offered.contains(&"calendar_create_event".to_string()));
    assert!(offered.len() <= 3 + v13::SAFE_HARBOR_TOP_K + 1, "offered {offered:?}");
    let non_memory: Vec<ToolDefWire> =
        catalog.iter().filter(|t| t.name != "search_memories").cloned().collect();
    for name in v13::semantic_top_k(REQUEST, &non_memory, 3) {
        assert!(offered.contains(&name), "safe harbor {name} missing");
    }
    // A small prompt: the request, the chosen note, the confirmed value.
    let prompt = user_prompt(messages);
    assert!(prompt.contains("Roadmap review moved to Thursday"));
    assert!(!prompt.contains("Miso"), "unchosen notes stay out: {prompt}");
    assert!(prompt.contains("calendar_create_event.title: Roadmap review"), "{prompt}");
    assert!(!prompt.contains("KIT3_ACTION"), "no menu in the prompt");
    let total: usize = messages.iter().flat_map(|m| m.content.iter()).map(|c| c.content.len()).sum();
    assert!(total < 1_200, "prompt is {total} chars");
}

#[tokio::test]
async fn grouped_menu_asks_toolkits_then_their_tools() {
    let stub = JevStub {
        prefer: vec!["act".into(), "calendar".into(), "calendar_create_event".into()],
        ..JevStub::default()
    };
    let seen_requests = Arc::clone(&stub.seen);
    let url = serve_jev(stub).await;
    let client = DecisionClient::new(reqwest::Client::new(), url, "typesafe/jev-1.14", None);
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![text("ok")]);
    let cfg = HybridConfig {
        menu: MenuMode::Grouped,
        ..HybridConfig::default()
    };
    run_case(&model, Some(&client), &cfg, case(&catalog, &executed)).await.unwrap();
    let requests = seen_requests.lock().unwrap().clone();
    let first_q = requests[1]["questions"].as_object().unwrap();
    assert!(first_q.keys().any(|k| k.starts_with("kit")));
    assert!(!first_q.keys().any(|k| k.starts_with("tool")));
    let second_q = requests[2]["questions"].as_object().unwrap();
    let tool_options: usize = second_q
        .iter()
        .filter(|(k, _)| k.starts_with("tool"))
        .map(|(_, q)| q["criteria"].as_object().unwrap().len() - 1)
        .sum();
    assert!(tool_options < 10, "stage two asks about {tool_options} tools, not the menu");
    let full: usize = requests.iter().map(|r| r.to_string().len()).sum();
    assert!(full < 60_000, "grouped mode reads a fraction of the menu ({full} bytes)");
    assert!(model.calls.lock().unwrap()[0].1.contains(&"calendar_create_event".to_string()));
}

#[tokio::test]
async fn no_action_keeps_the_safe_harbor_and_leaves_restraint_to_the_model() {
    let stub = JevStub {
        prefer: vec!["recall".into(), "n1".into()],
        nouls: HashMap::from([("no_change".to_string(), 0.9)]),
        ..JevStub::default()
    };
    let url = serve_jev(stub).await;
    let client = DecisionClient::new(reqwest::Client::new(), url, "typesafe/jev-1.14", None);
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![text("Priya prefers mornings.")]);
    let mut c = case(&catalog, &executed);
    c.user_input = "When does Priya like to meet?";
    let out = run_case(&model, Some(&client), &HybridConfig::default(), c).await.unwrap();
    let calls = model.calls.lock().unwrap();
    let offered = &calls[0].1;
    let non_memory: Vec<ToolDefWire> = catalog.iter().filter(|t| t.name != "search_memories").cloned().collect();
    for name in v13::semantic_top_k("When does Priya like to meet?", &non_memory, 3) {
        assert!(offered.contains(&name), "catalog-present safe harbor {name} missing");
    }
    assert!(offered.contains(&"search_memories".to_string()));
    assert!(user_prompt(&calls[0].0).contains("No action seems to be needed"));
    assert!(!out.menu_asked, "a case that will not act never reads the menu");
    assert_eq!(out.decision_calls, 1);
    assert!(user_prompt(&calls[0].0).contains("Priya prefers morning meetings"));
    assert_eq!(out.final_text, "Priya prefers mornings.");
    assert!(executed.lock().unwrap().is_empty());
}

#[tokio::test]
async fn every_model_emitted_call_is_executed_even_when_jev_says_no_action() {
    let stub = JevStub {
        prefer: vec!["chat".into()],
        nouls: HashMap::from([("no_change".to_string(), 0.95)]),
        ..JevStub::default()
    };
    let url = serve_jev(stub).await;
    let client = DecisionClient::new(reqwest::Client::new(), url, "typesafe/jev-1.14", None);
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![call("search_web", json!({"query": "x"})), text("Here.")]);
    let out = run_case(&model, Some(&client), &HybridConfig::default(), case(&catalog, &executed))
        .await
        .unwrap();
    assert_eq!(executed.lock().unwrap().len(), 1, "no swallowed model call");
    assert_eq!(out.tool_calls[0].name, "search_web");
}

#[tokio::test]
async fn multi_call_encounters_re_plan_from_the_first_pass_pool() {
    let stub = JevStub {
        prefer: vec!["act".into(), "calendar_create_event".into(), "search_web".into()],
        ..JevStub::default()
    };
    let seen_requests = Arc::clone(&stub.seen);
    let url = serve_jev(stub).await;
    let client = DecisionClient::new(reqwest::Client::new(), url, "typesafe/jev-1.14", None);
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![
        call("calendar_create_event", json!({"title": "a", "start": "b"})),
        call("search_web", json!({"query": "evt-12345"})),
        text("Done."),
    ]);
    let out = run_case(&model, Some(&client), &HybridConfig::default(), case(&catalog, &executed))
        .await
        .unwrap();
    assert_eq!(out.tool_calls.len(), 2);
    assert_eq!(out.tool_calls[1].hop, 1);
    let requests = seen_requests.lock().unwrap().clone();
    let replans: Vec<&Value> = requests
        .iter()
        .filter(|r| r["questions"].get("next").is_some())
        .collect();
    assert_eq!(replans.len(), 2, "one re-plan per tool hop");
    let options = replans[0]["questions"]["next"]["criteria"].as_object().unwrap().len();
    assert!(options <= 25, "re-plan reads the pool, not the menu ({options})");
    // Tool results reach the model as tool messages, truncated.
    let calls = model.calls.lock().unwrap();
    assert!(calls[1].0.iter().any(|m| m.role == "tool"));
}

#[tokio::test]
async fn without_a_decision_lane_it_falls_back_to_code_only_selection() {
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![text("ok")]);
    let out = run_case(&model, None, &HybridConfig::default(), case(&catalog, &executed))
        .await
        .unwrap();
    assert_eq!(out.decision_calls, 0);
    let offered = &model.calls.lock().unwrap()[0].1;
    assert!(!offered.is_empty() && offered.len() <= 8 + 1, "{offered:?}");
    assert!(offered.contains(&"calendar_create_event".to_string()));
}

#[tokio::test]
async fn a_failing_decision_lane_degrades_instead_of_failing_the_case() {
    let client = DecisionClient::new(
        reqwest::Client::new(),
        "http://127.0.0.1:9/decisions",
        "typesafe/jev-1.14",
        None,
    );
    let catalog = catalog();
    let executed = Arc::new(Mutex::new(Vec::new()));
    let model = ScriptedModel::new(vec![text("ok")]);
    let out = run_case(&model, Some(&client), &HybridConfig::default(), case(&catalog, &executed))
        .await
        .unwrap();
    assert_eq!(out.final_text, "ok");
    assert!(out.decision_failures >= 1);
}
