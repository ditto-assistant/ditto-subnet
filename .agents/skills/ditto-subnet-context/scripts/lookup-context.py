#!/usr/bin/env python3
"""Return a bounded, task-specific context packet for the monorepo."""

from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[2]
RESOLVED_REPO_ROOT = REPO_ROOT.resolve()
INDEX_PATH = SKILL_ROOT / "references" / "context-index.json"
# Design-only skill; not a monorepo ownership topic.
UNROUTED_SKILLS = {"impeccable"}
TOKEN_RE = re.compile(r"[a-z0-9]+")
MIN_TOPIC_SCORE = 10
FUZZY_TOKEN_MIN_LENGTH = 6
FUZZY_TOKEN_RATIO = 0.8
STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
    "without",
}


def load_index() -> dict[str, object]:
    with INDEX_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != 1 or not isinstance(data.get("topics"), list):
        raise ValueError("unsupported or malformed context index")
    return data


def token_sequence(value: str) -> tuple[str, ...]:
    return tuple(
        token for token in TOKEN_RE.findall(value.lower()) if token not in STOPWORDS
    )


def tokens(value: str) -> set[str]:
    return set(token_sequence(value))


def fuzzy_token_match(left: str, right: str) -> bool:
    """Match a long token with a small pluralization or typo, never a substring."""

    if left == right:
        return True
    if min(len(left), len(right)) < FUZZY_TOKEN_MIN_LENGTH:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if longer in {shorter + "s", shorter + "es"}:
        return True
    if shorter.endswith("y") and longer == shorter[:-1] + "ies":
        return True
    if shorter in longer:
        return False
    return SequenceMatcher(None, left, right).ratio() >= FUZZY_TOKEN_RATIO


def phrase_match(
    query_tokens: tuple[str, ...], keyword_tokens: tuple[str, ...]
) -> tuple[bool, bool]:
    """Return (matched, exact) for a token-boundary phrase match."""

    width = len(keyword_tokens)
    if not width or width > len(query_tokens):
        return False, False
    for start in range(len(query_tokens) - width + 1):
        candidate = query_tokens[start : start + width]
        if candidate == keyword_tokens:
            return True, True
        if all(
            fuzzy_token_match(query_token, keyword_token)
            for query_token, keyword_token in zip(
                candidate, keyword_tokens, strict=True
            )
        ):
            return True, False
    return False, False


def keyword_overlap_score(
    query_tokens: set[str], keyword_sequence: tuple[str, ...]
) -> int:
    """Partial credit only when a multi-word keyword shares two or more tokens.

    A single shared token such as ``review`` or ``version`` must not promote
    an unrelated topic. Exact and fuzzy phrase matches are scored separately.
    """

    if len(keyword_sequence) < 2:
        return 0
    overlap = query_tokens & set(keyword_sequence)
    if len(overlap) < 2:
        return 0
    return 3 * len(overlap)


def topic_score(topic: dict[str, object], query: str) -> int:
    query_sequence = token_sequence(query)
    query_tokens = set(query_sequence)
    keywords = [str(value) for value in topic.get("keywords", [])]
    score = 0
    for keyword in keywords:
        keyword_sequence = token_sequence(keyword)
        matched, exact = phrase_match(query_sequence, keyword_sequence)
        if matched:
            score += (12 if exact else 10) + len(keyword_sequence)
            continue
        score += keyword_overlap_score(query_tokens, keyword_sequence)
    if score < MIN_TOPIC_SCORE:
        return 0
    score += len(query_tokens & tokens(str(topic.get("title", ""))))
    score += len(query_tokens & tokens(str(topic.get("summary", ""))))
    return score


def select_topics(
    data: dict[str, object], query: str, maximum: int
) -> list[dict[str, object]]:
    topics = list(data["topics"])
    if not query.strip():
        return [topic for topic in topics if topic.get("id") == "overview"]
    ranked = sorted(
        (
            (topic_score(topic, query), index, topic)
            for index, topic in enumerate(topics)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    selected = [topic for score, _, topic in ranked if score >= MIN_TOPIC_SCORE][
        :maximum
    ]
    if selected:
        return selected
    return [topic for topic in topics if topic.get("id") == "overview"]


def render_topic(topic: dict[str, object]) -> str:
    lines = [f"[{topic['id']}] {topic['title']}", str(topic["summary"])]
    labels = (
        ("Skills", "skills"),
        ("Owns", "owns"),
        ("Read", "read"),
        ("Search", "search"),
        ("Validate", "validate"),
        ("Invariants", "invariants"),
        ("Related", "related"),
    )
    for label, key in labels:
        values = topic.get(key, [])
        if values:
            lines.append(f"{label}:")
            lines.extend(f"- {value}" for value in values)
    return "\n".join(lines)


def check_index(
    data: dict[str, object], *, cover_installed_skills: bool = False
) -> int:
    failures: list[str] = []
    seen: set[str] = set()
    topic_ids = {
        str(topic.get("id", "")) for topic in data["topics"] if topic.get("id")
    }
    for topic in data["topics"]:
        topic_id = str(topic.get("id", ""))
        if not topic_id or topic_id in seen:
            failures.append(f"duplicate or empty topic id: {topic_id!r}")
        seen.add(topic_id)
        for related in topic.get("related", []):
            if str(related) not in topic_ids:
                failures.append(f"{topic_id}.related: unknown topic id {related!r}")
        for key in ("owns", "read"):
            for relative in topic.get(key, []):
                candidate = (REPO_ROOT / str(relative)).resolve()
                try:
                    candidate.relative_to(RESOLVED_REPO_ROOT)
                except ValueError:
                    failures.append(
                        f"{topic_id}.{key}: path escapes repository: {relative}"
                    )
                    continue
                if not candidate.exists():
                    failures.append(f"{topic_id}.{key}: missing {relative}")
        for skill_name in topic.get("skills", []):
            skill_path = (
                REPO_ROOT / ".agents" / "skills" / str(skill_name) / "SKILL.md"
            ).resolve()
            try:
                skill_path.relative_to(RESOLVED_REPO_ROOT)
            except ValueError:
                failures.append(
                    f"{topic_id}.skills: path escapes repository: {skill_name}"
                )
                continue
            if not skill_path.is_file():
                failures.append(
                    f"{topic_id}.skills: missing .agents/skills/{skill_name}/SKILL.md"
                )
    if cover_installed_skills:
        indexed_skills = {
            str(name) for topic in data["topics"] for name in topic.get("skills", [])
        }
        skills_root = REPO_ROOT / ".agents" / "skills"
        if skills_root.is_dir():
            for child in sorted(skills_root.iterdir()):
                if not (child / "SKILL.md").is_file():
                    continue
                if child.name in UNROUTED_SKILLS or child.name in indexed_skills:
                    continue
                failures.append(
                    f"skill {child.name!r} is not referenced by any topic.skills"
                )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"context index ok: {len(seen)} topics, all owned/read paths exist")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="*", help="task description or topic words")
    parser.add_argument("--max-topics", type=int, default=3)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--list", action="store_true", dest="list_topics")
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print every topic score for the query, including zeros",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.max_topics <= 8:
        raise SystemExit("--max-topics must be between 1 and 8")
    data = load_index()
    if args.check:
        return check_index(data, cover_installed_skills=True)
    if args.list_topics:
        for topic in data["topics"]:
            print(f"{topic['id']}\t{topic['title']}")
        return 0
    query = " ".join(args.query)
    if args.explain:
        ranked = sorted(
            (
                (topic_score(topic, query), index, topic)
                for index, topic in enumerate(data["topics"])
            ),
            key=lambda item: (-item[0], item[1]),
        )
        for score, _, topic in ranked:
            print(f"{score:3d}  {topic['id']}")
        return 0
    selected = select_topics(data, query, args.max_topics)
    if args.as_json:
        json.dump(selected, sys.stdout, indent=2)
        print()
    else:
        print("\n\n".join(render_topic(topic) for topic in selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
