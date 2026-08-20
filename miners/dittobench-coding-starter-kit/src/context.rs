//! Deterministic coding-context compaction with a hard byte postcondition.

use ditto_harness::{ChatMessage, Content};

const KEEP_RECENT_PAIRS: usize = 6;
const MEMORY_SHARE_DIVISOR: usize = 3;
const TRUNCATION_MARKER: &str = "\n...[context truncated]";

#[must_use]
pub fn message_bytes(messages: &[ChatMessage]) -> usize {
    messages
        .iter()
        .map(|message| serde_json::to_vec(message).map_or(usize::MAX, |bytes| bytes.len()))
        .sum()
}

/// Rebuilds context from its required task prefix, a bounded memory message,
/// an optional operational summary, and the newest complete tool pairs.
///
/// Returns `false` only when the required system prompt and current task alone
/// exceed `max_bytes`. A `true` result guarantees
/// `message_bytes(messages) <= max_bytes`.
pub fn enforce_budget(messages: &mut Vec<ChatMessage>, max_bytes: usize) -> bool {
    if message_bytes(messages) <= max_bytes {
        return true;
    }
    let first_assistant = messages
        .iter()
        .position(|message| message.role == "assistant")
        .unwrap_or(messages.len());
    let mut required = Vec::new();
    let mut memory = None;
    for (index, message) in messages[..first_assistant].iter().enumerate() {
        let text = message_text(message);
        if text.starts_with("Task-scoped retrieved memory") {
            memory = Some((index, message.clone()));
        } else if !text.starts_with("Earlier workspace activity") {
            required.push((index, message.clone()));
        }
    }
    let required_messages: Vec<_> = required
        .iter()
        .map(|(_, message)| message.clone())
        .collect();
    let required_bytes = message_bytes(&required_messages);
    if required_bytes > max_bytes {
        return false;
    }

    let mut prefix = required;
    if let Some((index, memory_message)) = memory {
        let available = max_bytes.saturating_sub(required_bytes);
        let memory_budget = available.min(max_bytes / MEMORY_SHARE_DIVISOR);
        if let Some(memory_message) = fit_text_message(&memory_message, memory_budget) {
            prefix.push((index, memory_message));
        }
    }
    prefix.sort_by_key(|(index, _)| *index);
    let mut rebuilt: Vec<_> = prefix.into_iter().map(|(_, message)| message).collect();

    let pairs = complete_pairs(&messages[first_assistant..]);
    let mut selected = Vec::new();
    for pair in pairs.iter().rev().take(KEEP_RECENT_PAIRS) {
        let used = message_bytes(&rebuilt) + message_bytes(&selected_messages(&selected));
        let available = max_bytes.saturating_sub(used);
        if let Some(pair) = fit_pair(pair, available) {
            selected.push(pair);
        }
    }
    selected.reverse();

    let dropped = pairs.len().saturating_sub(selected.len());
    if dropped > 0 {
        let names = pairs
            .iter()
            .take(dropped)
            .filter_map(|pair| pair[0].tool_calls.first())
            .map(|call| call.name.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        let summary = ChatMessage {
            role: "system".to_string(),
            content: vec![Content::text(format!(
                "Earlier workspace activity (operational summary only): dropped {dropped} completed calls ({names})"
            ))],
            ..ChatMessage::default()
        };
        let available = max_bytes
            .saturating_sub(message_bytes(&rebuilt) + message_bytes(&selected_messages(&selected)));
        if let Some(summary) = fit_text_message(&summary, available.min(1024)) {
            rebuilt.push(summary);
        }
    }
    for pair in selected {
        rebuilt.extend(pair);
    }
    if message_bytes(&rebuilt) > max_bytes {
        return false;
    }
    *messages = rebuilt;
    true
}

fn complete_pairs(messages: &[ChatMessage]) -> Vec<[ChatMessage; 2]> {
    let mut pairs = Vec::new();
    let mut index = 0;
    while index + 1 < messages.len() {
        let assistant = &messages[index];
        let tool = &messages[index + 1];
        if assistant.role == "assistant"
            && tool.role == "tool"
            && assistant
                .tool_calls
                .iter()
                .any(|call| call.id == tool.tool_call_id)
        {
            pairs.push([assistant.clone(), tool.clone()]);
            index += 2;
        } else {
            index += 1;
        }
    }
    pairs
}

fn fit_pair(pair: &[ChatMessage; 2], max_bytes: usize) -> Option<[ChatMessage; 2]> {
    if message_bytes(pair) <= max_bytes {
        return Some(pair.clone());
    }
    let mut compacted = pair.clone();
    for call in &mut compacted[0].tool_calls {
        call.args = serde_json::json!({"context_truncated": true});
    }
    compacted[0].content.clear();
    for part in &mut compacted[1].content {
        if let Some(response) = &mut part.tool_call_response {
            response.output = serde_json::json!({"context_truncated": true});
            response.error = truncate_utf8(&response.error, 512);
        }
        part.content = truncate_utf8(&part.content, 512);
    }
    (message_bytes(&compacted) <= max_bytes).then_some(compacted)
}

fn selected_messages(pairs: &[[ChatMessage; 2]]) -> Vec<ChatMessage> {
    pairs.iter().flat_map(|pair| pair.iter().cloned()).collect()
}

fn fit_text_message(message: &ChatMessage, max_bytes: usize) -> Option<ChatMessage> {
    if message_bytes(std::slice::from_ref(message)) <= max_bytes {
        return Some(message.clone());
    }
    let original = message_text(message);
    let mut empty = message.clone();
    empty.content = vec![Content::text(String::new())];
    if message_bytes(std::slice::from_ref(&empty)) + TRUNCATION_MARKER.len() > max_bytes {
        return None;
    }
    let mut low = 0;
    let mut high = original.len();
    let mut best = empty.clone();
    while low <= high {
        let midpoint = low + (high - low) / 2;
        let prefix = truncate_utf8(&original, midpoint);
        let mut candidate = empty.clone();
        candidate.content = vec![Content::text(format!("{prefix}{TRUNCATION_MARKER}"))];
        if message_bytes(std::slice::from_ref(&candidate)) <= max_bytes {
            best = candidate;
            low = midpoint.saturating_add(1);
        } else if midpoint == 0 {
            break;
        } else {
            high = midpoint - 1;
        }
    }
    Some(best)
}

fn message_text(message: &ChatMessage) -> String {
    message
        .content
        .iter()
        .map(|part| part.content.as_str())
        .collect::<Vec<_>>()
        .join("\n")
}

fn truncate_utf8(value: &str, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value.to_string();
    }
    let mut boundary = max_bytes;
    while !value.is_char_boundary(boundary) {
        boundary -= 1;
    }
    value[..boundary].to_string()
}

#[cfg(test)]
mod tests {
    use ditto_harness::{ContentType, ToolCall, ToolCallResponse};
    use serde_json::json;

    use super::*;

    fn required_prefix(memory: &str) -> Vec<ChatMessage> {
        vec![
            ChatMessage {
                role: "system".to_string(),
                content: vec![Content::text("rules")],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "system".to_string(),
                content: vec![Content::text(format!(
                    "Task-scoped retrieved memory: {memory}"
                ))],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "user".to_string(),
                content: vec![Content::text("current task")],
                ..ChatMessage::default()
            },
        ]
    }

    fn pair(index: usize, output: &str) -> [ChatMessage; 2] {
        let id = format!("call-{index}");
        [
            ChatMessage {
                role: "assistant".to_string(),
                tool_calls: vec![ToolCall {
                    id: id.clone(),
                    name: "repo_read_file".to_string(),
                    args: json!({"path": format!("file-{index}")}),
                }],
                ..ChatMessage::default()
            },
            ChatMessage {
                role: "tool".to_string(),
                tool_call_id: id.clone(),
                content: vec![Content {
                    content_type: Some(ContentType::ToolResult),
                    tool_call_response: Some(ToolCallResponse {
                        id,
                        name: "repo_read_file".to_string(),
                        output: json!({"content": output}),
                        error: String::new(),
                    }),
                    ..Content::default()
                }],
                ..ChatMessage::default()
            },
        ]
    }

    #[test]
    fn huge_seeded_memory_is_truncated_to_hard_budget() {
        let mut messages = required_prefix(&"memory ".repeat(100_000));
        assert!(enforce_budget(&mut messages, 4096));
        assert!(message_bytes(&messages) <= 4096);
        assert!(messages
            .iter()
            .any(|message| message_text(message).contains("context truncated")));
    }

    #[test]
    fn huge_tool_output_keeps_only_complete_bounded_pairs() {
        let mut messages = required_prefix("small memory");
        messages.extend(pair(0, &"x".repeat(1_000_000)));
        assert!(enforce_budget(&mut messages, 4096));
        assert!(message_bytes(&messages) <= 4096);
        let suffix = messages
            .iter()
            .position(|message| message.role == "assistant")
            .map_or(&[][..], |index| &messages[index..]);
        for pair in suffix.chunks_exact(2) {
            assert_eq!(pair[0].role, "assistant");
            assert_eq!(pair[1].role, "tool");
            assert_eq!(pair[0].tool_calls[0].id, pair[1].tool_call_id);
        }
    }

    #[test]
    fn required_task_that_exceeds_budget_fails_closed() {
        let mut messages = required_prefix("memory");
        messages[2].content = vec![Content::text("task".repeat(10_000))];
        assert!(!enforce_budget(&mut messages, 4096));
    }
}
