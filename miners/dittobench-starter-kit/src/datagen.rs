//! Procedurally generates small, fresh, randomized DittoBench datasets.
//!
//! Generation is deterministic per seed (a given seed always yields the same
//! dataset) but varies widely across seeds. The practice loop rotates the seed
//! on every run so no two evaluations are identical — the anti-overfit property
//! of the off-chain practice loop. Mirrors the Go validator
//! `internal/datagen`, extended with synthetic memory cases.

use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};

use crate::protocol::{Dataset, MemoryCase, SeedMemory, ToolCase, ToolSpec};

/// Pinned "as-of" stamp for generated datasets — mirrors dittobench-api's
/// `protocol.DatasetEpoch` (2026-01-01T00:00:00Z). A wall-clock stamp would
/// break same-seed byte-identical local datasets.
pub const DATASET_EPOCH_RFC3339: &str = "2026-01-01T00:00:00Z";

/// One kind of tool-calling case and how to render it.
struct Category {
    name: &'static str,
    /// Expected tool name; empty means "no tool".
    tool: &'static str,
    templates: &'static [&'static str],
}

const SUBJECTS: &[&str] = &[
    "my dentist appointment",
    "the project deadline",
    "Sarah's birthday",
    "my car insurance",
    "the meeting notes",
    "my flight to Tokyo",
    "the grocery list",
    "my gym schedule",
    "the wifi password",
    "my passport number",
    "the book recommendation",
    "my doctor's name",
];
const TOPICS: &[&str] = &[
    "quantum computing",
    "the 2024 Olympics",
    "best espresso machines",
    "rust vs go",
    "climate policy",
    "the stock market today",
    "sourdough recipes",
    "electric vehicles",
    "the James Webb telescope",
];
const URLS: &[&str] = &[
    "https://example.com/article",
    "https://news.site/story",
    "https://blog.dev/post",
    "https://docs.io/guide",
    "https://github.com/org/repo",
];
const IMAGE_PROMPTS: &[&str] = &[
    "a sunset over mountains",
    "a robot drinking coffee",
    "a futuristic city skyline",
    "a watercolor fox",
    "an astronaut on a beach",
    "a neon cyberpunk street",
];
const ARTIFACT_KINDS: &[&str] = &[
    "a landing page",
    "a todo app",
    "a snake game",
    "a markdown resume",
    "a pomodoro timer",
    "a budget tracker",
];
const AGENT_TASKS: &[&str] = &[
    "scrape the latest headlines",
    "summarize this PDF",
    "refactor the auth module",
    "generate unit tests",
    "build a CSV report",
    "deploy the staging branch",
];
const THEMES: &[&str] = &["dark", "light", "system", "midnight", "solarized"];
const CHITCHAT: &[&str] = &[
    "hey, how's it going?",
    "thanks, that was helpful!",
    "tell me a joke",
    "what's your favorite color?",
    "good morning!",
    "you're awesome",
    "lol nice",
];
const ABSTENTIONS: &[&str] = &[
    "what's the meaning of life?",
    "should I quit my job?",
    "do you love me?",
    "what will the weather be like next year?",
    "who will win the next election?",
    "what am I thinking right now?",
];

/// Order is stable so seeding is reproducible.
const CATEGORIES: &[Category] = &[
    Category {
        name: "memory_lookup",
        tool: "search_memories",
        templates: &[
            "What did I say about %s?",
            "Remind me about %s.",
            "Do you remember %s?",
            "Look up %s from my memories.",
        ],
    },
    Category {
        name: "memory_subject",
        tool: "search_subjects",
        templates: &[
            "What subjects do I have notes on related to %s?",
            "Find the topic that covers %s.",
            "Which of my subjects mention %s?",
        ],
    },
    Category {
        name: "web_search",
        tool: "search_web",
        templates: &[
            "Search the web for %s.",
            "What's the latest on %s?",
            "Find recent news about %s.",
            "Google %s for me.",
        ],
    },
    Category {
        name: "link_read",
        tool: "read_links",
        templates: &[
            "Read %s and summarize it.",
            "What does this page say: %s",
            "Open %s and tell me the main points.",
        ],
    },
    Category {
        name: "image_create",
        tool: "create_image",
        templates: &[
            "Generate an image of %s.",
            "Create a picture of %s.",
            "Draw me %s.",
        ],
    },
    Category {
        name: "artifacts_create",
        tool: "artifacts",
        templates: &[
            "Build me %s.",
            "Make %s I can preview.",
            "Create %s as an interactive artifact.",
        ],
    },
    Category {
        name: "agent_job",
        tool: "execute_agent_job",
        templates: &[
            "Run a background job to %s.",
            "Kick off an agent to %s.",
            "Dispatch a task to %s.",
        ],
    },
    Category {
        name: "settings",
        tool: "set_theme",
        templates: &[
            "Switch to %s mode.",
            "Set my theme to %s.",
            "Change the app theme to %s.",
        ],
    },
    Category {
        name: "no_tool",
        tool: "",
        templates: &["%s"],
    },
    Category {
        name: "abstention",
        tool: "",
        templates: &["%s"],
    },
];

/// Harder, realistic tool cases with fixed prompts (no template filler). These
/// exercise the ambiguous decisions models actually flub — routing (web vs
/// read_links vs memory), over-calling on requests that need NO tool (answer
/// directly), and run-vs-read (execute_agent_job vs artifacts) — so tool
/// accuracy is no longer pinned at ~1.0. `tool` empty means the correct action
/// is to answer with no tool. Each expected tool is determinable from the tool
/// catalog descriptions, so the cases are fair, not gotchas.
/// `(category, prompt, expected_tool, expected_behavior)`.
const HARD_CASES: &[(&str, &str, &str, &str)] = &[
    // --- routing: pick the RIGHT source ---
    (
        "route_link",
        "Summarize what's on https://example.com/q3-report.html for me.",
        "read_links",
        "a specific URL was given; read that page (not a web search)",
    ),
    (
        "route_memory",
        "What did I decide about the database migration last week?",
        "search_memories",
        "a past personal decision; search the user's memories, not the web",
    ),
    (
        "route_web",
        "What's the current price of Bitcoin right now?",
        "search_web",
        "a current external fact; search the web",
    ),
    (
        "route_memory",
        "Remind me which laptop I said I was planning to buy.",
        "search_memories",
        "the user's own stated preference; search memories",
    ),
    (
        "route_web",
        "What are the latest reviews of the new Pixel phone?",
        "search_web",
        "current external info; search the web",
    ),
    // --- no tool: answer directly (over-calling is wrong) ---
    (
        "answer_direct",
        "Explain the difference between TCP and UDP.",
        "",
        "general knowledge; answer directly without calling a tool",
    ),
    (
        "answer_direct",
        "Brainstorm three name ideas for a coffee shop.",
        "",
        "a creative task; answer directly without a tool",
    ),
    (
        "answer_direct",
        "Translate \"good morning, how are you?\" into Spanish.",
        "",
        "answer directly; no tool needed",
    ),
    (
        "answer_direct",
        "Write a short haiku about autumn.",
        "",
        "answer directly; no tool needed",
    ),
    (
        "answer_direct",
        "What's 18% of 250?",
        "",
        "simple arithmetic; answer directly without a tool",
    ),
    // --- run vs read: execute_agent_job vs artifacts ---
    (
        "run_code",
        "Clone https://github.com/octocat/Hello-World and run its test suite.",
        "execute_agent_job",
        "must actually run code against a repo; dispatch the coding agent",
    ),
    (
        "build_app",
        "Build a working snake game I can play in the browser.",
        "artifacts",
        "an interactive HTML app to preview; create an artifact (not a code-run job)",
    ),
    (
        "doc_artifact",
        "Draft a one-page project proposal I can edit later.",
        "artifacts",
        "a durable document; create a markdown artifact",
    ),
];

fn pick<'a>(rng: &mut StdRng, pool: &'a [&'a str]) -> &'a str {
    pool[rng.gen_range(0..pool.len())]
}

fn filler_for(rng: &mut StdRng, category: &str) -> &'static str {
    match category {
        "memory_lookup" | "memory_subject" => pick(rng, SUBJECTS),
        "web_search" => pick(rng, TOPICS),
        "link_read" => pick(rng, URLS),
        "image_create" => pick(rng, IMAGE_PROMPTS),
        "artifacts_create" => pick(rng, ARTIFACT_KINDS),
        "agent_job" => pick(rng, AGENT_TASKS),
        "settings" => pick(rng, THEMES),
        "no_tool" => pick(rng, CHITCHAT),
        "abstention" => pick(rng, ABSTENTIONS),
        _ => pick(rng, TOPICS),
    }
}

/// Single `%s` substitution, matching the Go templates.
fn render(template: &str, filler: &str) -> String {
    template.replacen("%s", filler, 1)
}

// --- Memory case generation -------------------------------------------------

struct MemoryFact {
    /// Short label for the fact (used in generated case ids / debugging).
    topic: &'static str,
    question: &'static str,
    /// The expected answer the response must surface (graded deterministically, judge-free).
    answer: &'static str,
    prompt: &'static str,
    response: &'static str,
}

const MEMORY_FACTS: &[MemoryFact] = &[
    MemoryFact {
        topic: "my dog's name",
        question: "What's my dog's name?",
        answer: "Biscuit",
        prompt: "Just adopted a golden retriever puppy and named him Biscuit.",
        response: "Congrats on Biscuit! Golden retrievers are wonderful companions.",
    },
    MemoryFact {
        topic: "my favorite coffee order",
        question: "What's my usual coffee order?",
        answer: "oat milk flat white",
        prompt: "My go-to coffee is an oat milk flat white, double shot.",
        response: "Noted: oat milk flat white, double shot.",
    },
    MemoryFact {
        topic: "my anniversary",
        question: "When is my wedding anniversary?",
        answer: "June 14",
        prompt: "My wedding anniversary is June 14, don't let me forget it.",
        response: "I'll remember: your anniversary is June 14.",
    },
    MemoryFact {
        topic: "my car",
        question: "What car do I drive?",
        answer: "blue Subaru Outback",
        prompt: "I drive a blue Subaru Outback, 2021 model.",
        response: "Got it, a 2021 blue Subaru Outback.",
    },
    MemoryFact {
        topic: "my allergy",
        question: "What am I allergic to?",
        answer: "peanuts",
        prompt: "Reminder: I'm allergic to peanuts, it's serious.",
        response: "Understood, you have a serious peanut allergy.",
    },
    MemoryFact {
        topic: "my gym goal",
        question: "What's my current fitness goal?",
        answer: "run a half marathon",
        prompt: "My goal this year is to run a half marathon in under two hours.",
        response: "Great goal: a sub-two-hour half marathon.",
    },
    MemoryFact {
        topic: "my favorite author",
        question: "Who is my favorite author?",
        answer: "Ursula K. Le Guin",
        prompt: "My favorite author is Ursula K. Le Guin, hands down.",
        response: "Noted, Ursula K. Le Guin is your favorite author.",
    },
    MemoryFact {
        topic: "my work project",
        question: "What project am I working on at work?",
        answer: "billing service migration",
        prompt: "At work I'm leading the billing service migration to the new platform.",
        response: "Got it, you lead the billing service migration.",
    },
    MemoryFact {
        topic: "my hometown",
        question: "Where did I grow up?",
        answer: "Asheville",
        prompt: "I grew up in Asheville, North Carolina.",
        response: "Noted: you grew up in Asheville, NC.",
    },
    MemoryFact {
        topic: "my preferred editor",
        question: "Which code editor do I prefer?",
        answer: "Neovim",
        prompt: "I do all my coding in Neovim with a custom config.",
        response: "Understood, Neovim is your editor of choice.",
    },
];

/// Generates a deterministic-per-seed dataset of `n_tool` tool-calling cases
/// and `n_mem` memory cases. `n_tool` is clamped to [1, 200]; `n_mem` to
/// [0, 50].
pub fn generate(seed: i64, n_tool: usize, n_mem: usize) -> Dataset {
    let n_tool = n_tool.clamp(1, 200);
    let n_mem = n_mem.min(50);
    let mut rng = StdRng::seed_from_u64(seed as u64);

    let mut tool_cases = Vec::with_capacity(n_tool);
    for i in 0..n_tool {
        // ~half hard fixed cases, ~half templated — both seeded, so the mix is
        // reproducible. Hard cases keep tool accuracy realistic (no longer ~1.0).
        let (category, prompt, tool, behavior): (String, String, String, String) =
            if rng.gen_bool(0.5) {
                let h = &HARD_CASES[rng.gen_range(0..HARD_CASES.len())];
                (
                    h.0.to_string(),
                    h.1.to_string(),
                    h.2.to_string(),
                    h.3.to_string(),
                )
            } else {
                let cat = &CATEGORIES[rng.gen_range(0..CATEGORIES.len())];
                let template = cat.templates[rng.gen_range(0..cat.templates.len())];
                let filler = filler_for(&mut rng, cat.name);
                let behavior = if cat.tool.is_empty() {
                    if cat.name == "abstention" {
                        "answer or abstain without calling any tool".to_string()
                    } else {
                        "respond conversationally without calling any tool".to_string()
                    }
                } else {
                    format!("call {} exactly once", cat.tool)
                };
                (
                    cat.name.to_string(),
                    render(template, filler),
                    cat.tool.to_string(),
                    behavior,
                )
            };

        let mut tc = ToolCase {
            id: format!("{}-{}-{:04}", category, seed, i),
            category,
            prompt,
            expected_tools: Vec::new(),
            max_tool_calls: 1,
            allow_extra_tools: false,
            expected_behavior: behavior,
        };
        if tool.is_empty() {
            tc.max_tool_calls = 0;
        } else {
            tc.expected_tools = vec![ToolSpec {
                name: tool,
                ..ToolSpec::default()
            }];
        }
        tool_cases.push(tc);
    }

    let mut memory_cases = Vec::with_capacity(n_mem);
    for i in 0..n_mem {
        let fact = &MEMORY_FACTS[rng.gen_range(0..MEMORY_FACTS.len())];
        // 1-3 seed memories: the answer-bearing fact plus 0-2 distractors so
        // retrieval has to discriminate.
        let extra = rng.gen_range(0..3usize);
        let mut seeds = Vec::with_capacity(1 + extra);
        let days = rng.gen_range(1..30i64);
        seeds.push(SeedMemory {
            prompt: fact.prompt.to_string(),
            response: fact.response.to_string(),
            days_ago: days,
        });
        for _ in 0..extra {
            let d = &MEMORY_FACTS[rng.gen_range(0..MEMORY_FACTS.len())];
            seeds.push(SeedMemory {
                prompt: d.prompt.to_string(),
                response: d.response.to_string(),
                days_ago: rng.gen_range(1..60i64),
            });
        }
        let slug: String = fact
            .topic
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
            .collect();
        memory_cases.push(MemoryCase {
            id: format!("mem-{}-{}-{:04}", slug, seed, i),
            question: fact.question.to_string(),
            expected_answer: fact.answer.to_string(),
            seed_memories: seeds,
        });
    }

    Dataset {
        seed,
        generated_at: DATASET_EPOCH_RFC3339.to_string(),
        tool_cases,
        memory_cases,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn determinism_same_seed_same_dataset() {
        let a = generate(42, 30, 10);
        let b = generate(42, 30, 10);
        // Whole-dataset equality: generated_at is the pinned epoch, so two
        // same-seed datasets are byte-identical.
        assert_eq!(a, b);
        assert_eq!(a.generated_at, DATASET_EPOCH_RFC3339);
    }

    #[test]
    fn variety_different_seeds_differ() {
        let a = generate(1, 30, 10);
        let b = generate(2, 30, 10);
        // Vanishingly unlikely to be identical across 30 randomized cases.
        assert_ne!(a.tool_cases, b.tool_cases);
    }

    #[test]
    fn counts_and_clamping() {
        let ds = generate(7, 0, 0);
        assert_eq!(ds.tool_cases.len(), 1, "n_tool clamps up to 1");
        assert_eq!(ds.memory_cases.len(), 0);

        let big = generate(7, 500, 100);
        assert_eq!(big.tool_cases.len(), 200, "n_tool clamps down to 200");
        assert_eq!(big.memory_cases.len(), 50, "n_mem clamps down to 50");
    }

    #[test]
    fn memory_cases_have_answer_bearing_seed() {
        let ds = generate(99, 5, 8);
        for mc in &ds.memory_cases {
            assert!(!mc.question.is_empty());
            assert!(!mc.expected_answer.is_empty());
            assert!(
                (1..=3).contains(&mc.seed_memories.len()),
                "memory case must have 1-3 seeds, got {}",
                mc.seed_memories.len()
            );
        }
    }

    #[test]
    fn tool_cases_cover_no_tool_and_tool_categories() {
        // A reasonably sized dataset should include both kinds across seeds.
        let ds = generate(123, 60, 0);
        let has_tool = ds.tool_cases.iter().any(|c| !c.expected_tools.is_empty());
        let has_no_tool = ds.tool_cases.iter().any(|c| c.expected_tools.is_empty());
        assert!(
            has_tool && has_no_tool,
            "dataset should mix tool/no-tool cases"
        );
    }
}
