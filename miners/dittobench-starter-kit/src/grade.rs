//! Deterministic memory grading — the local mirror of the validator's
//! judge-free value check (dittobench-datagen `grade.Hit`). No LLM: a memory
//! answer is correct iff the expected value appears in the response by
//! normalized bounded containment (or an exact number token for numeric
//! answers). Local `evaluate`/`practice` scores therefore match on-chain
//! grading semantics for the value-kind cases the local generator emits.

/// Whether the expected answer is present in the response: normalized bounded
/// containment, or an exact number-token match for purely numeric answers (so
/// "5" cannot match inside "500").
pub fn hit(expected: &str, response: &str) -> bool {
    let e = normalize(expected);
    if e.is_empty() {
        return false;
    }
    let r = normalize(response);
    if is_pure_number(&e) {
        return contains_number_token(&r, &e);
    }
    if !e.contains(' ') && COMMON_WORDS.contains(&e.as_str()) {
        return false;
    }
    contains_bounded_phrase(&r, &e)
}

/// Grades a memory response like the validator: the `answer` slot is
/// authoritative when set, the prose is the fallback, and abstaining on an
/// answerable case scores zero.
pub fn memory_correct(
    expected: &str,
    answer: Option<&str>,
    final_text: &str,
    abstain: bool,
) -> bool {
    if abstain {
        return false; // every local case is answerable
    }
    if let Some(slot) = answer {
        if hit(expected, slot) {
            return true;
        }
    }
    hit(expected, final_text)
}

/// Lowercases, trims surrounding punctuation/quotes, collapses whitespace.
fn normalize(s: &str) -> String {
    let s = s.trim().to_lowercase();
    let s = s.trim_matches(|c: char| "\"'.,!?;:".contains(c));
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn is_alnum(b: u8) -> bool {
    b.is_ascii_alphanumeric()
}

/// '-' counts as attached so "-5" never matches "5" and "order-42-x" never
/// matches "42".
fn num_attached(b: u8) -> bool {
    b.is_ascii_digit() || b == b'.' || b == b',' || b == b'-'
}

fn is_pure_number(s: &str) -> bool {
    if s.is_empty() {
        return false;
    }
    let bytes = s.as_bytes();
    let mut seen_sep = false;
    for (i, &c) in bytes.iter().enumerate() {
        if c.is_ascii_digit() {
            continue;
        }
        if (c == b'.' || c == b',') && !seen_sep && i > 0 && i < bytes.len() - 1 {
            seen_sep = true;
            continue;
        }
        return false;
    }
    true
}

fn contains_bounded_phrase(text: &str, phrase: &str) -> bool {
    find_bounded(text, phrase, is_alnum).is_some()
}

fn contains_number_token(text: &str, num: &str) -> bool {
    find_bounded(text, num, num_attached).is_some()
}

/// First occurrence of `needle` in `text` whose neighbors fail `attached`.
fn find_bounded(text: &str, needle: &str, attached: fn(u8) -> bool) -> Option<usize> {
    if needle.is_empty() {
        return None;
    }
    let (t, n) = (text.as_bytes(), needle.len());
    let mut from = 0;
    while let Some(j) = text[from..].find(needle).map(|j| j + from) {
        let before = j == 0 || !attached(t[j - 1]);
        let after = j + n >= t.len() || !attached(t[j + n]);
        if before && after {
            return Some(j);
        }
        from = j + 1;
    }
    None
}

const COMMON_WORDS: &[&str] = &[
    "no", "yes", "may", "can", "will", "is", "are", "was", "were", "be", "do", "did", "has", "had",
    "not", "the", "and", "or", "one", "two", "it", "to", "of", "in", "on", "at",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hit_edge_cases() {
        // Ported from the validator's matcher tests (grade.TestHitEdgeCases).
        let cases: &[(&str, &str, bool)] = &[
            ("Sarah", "your friend Sarah called yesterday", true),
            ("Sarah", "no one called", false),
            ("Tokyo", "you flew to tokyo in may", true),
            ("5", "you have 5 cats", true),
            ("5", "you have 500 dollars", false),
            ("3.5", "about 3.5 miles", true),
            ("", "anything at all", false),
            ("no", "I know nothing about your plans", false),
            ("may", "you may not have that information", false),
            ("Ann", "your planner is annoyingly complex", false),
            ("blue", "it was blue, actually", true),
            ("James Webb", "the james webb telescope", true),
            ("5", "about 3.5 miles", false),
            ("5", "temperature dropped to -5 today", false),
            ("100", "you owe -100 dollars", false),
            ("42", "see ticket order-42-x for details", false),
        ];
        for (exp, resp, want) in cases {
            assert_eq!(hit(exp, resp), *want, "hit({exp:?}, {resp:?})");
        }
    }

    #[test]
    fn slot_authoritative_and_abstain_zeroes() {
        assert!(memory_correct(
            "Lisbon",
            Some("Lisbon"),
            "long prose",
            false
        ));
        assert!(memory_correct("Lisbon", None, "you live in lisbon", false));
        assert!(!memory_correct(
            "Lisbon",
            Some("Lisbon"),
            "you live in lisbon",
            true
        ));
        assert!(!memory_correct(
            "Lisbon",
            Some("Porto"),
            "no city here",
            false
        ));
    }
}
