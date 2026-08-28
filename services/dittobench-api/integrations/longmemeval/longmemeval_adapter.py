#!/usr/bin/env python3
"""Run cleaned LongMemEval-S through the public DittoBench harness protocol."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NO_TOOL_SYSTEM_PROMPT = (
    "Answer the question using the user's stored conversation memory. "
    "The current date is {question_date}. Give a concise, direct answer. "
    "If the stored conversations do not contain enough information, explicitly "
    "say that the question cannot be answered from memory. Do not use external "
    "knowledge or tools."
)

NATIVE_MEMORY_TOOL_SYSTEM_PROMPT = (
    "Answer the question using the user's stored conversation memory. "
    "The current date is {question_date}. Use the available native memory tools "
    "as needed to search, scope, and fetch the relevant stored conversations before "
    "answering. Give a concise, direct answer. If the stored conversations do not "
    "contain enough information, explicitly say that the question cannot be answered "
    "from memory. Do not use external knowledge."
)

# This is the memory-only subset of the catalog advertised to every real
# DittoBench memory case. Restricting the LongMemEval condition to these four
# tools lets each submission use its native retrieval loop without giving it
# unrelated web, settings, image, or job tools.
NATIVE_MEMORY_TOOLS = [
    {
        "name": "search_memories",
        "description": (
            "Search past conversations and return compact memory summaries. "
            "Use fetch_memories for selected IDs that need full text. For named "
            "entities/topics, prefer search_subjects -> search_memories_in_subjects."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Memory search queries",
                }
            },
            "required": ["queries"],
        },
    },
    {
        "name": "search_subjects",
        "description": (
            "Search the user's subject graph and return subject objects with id, "
            "name, description, and similarity."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subject search queries",
                }
            },
            "required": ["queries"],
        },
    },
    {
        "name": "fetch_memories",
        "description": "Fetch full conversation text for selected memory pair IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "pairIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Memory pair IDs to fetch",
                },
                "stripImages": {
                    "type": "boolean",
                    "description": "Exclude images, default true",
                },
            },
            "required": ["pairIds"],
        },
    },
    {
        "name": "search_memories_in_subjects",
        "description": (
            "Semantic memory search inside one subject. Use focused queries; "
            "fetch selected IDs for full text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject_id": {
                    "type": "string",
                    "description": "Subject ID from search_subjects or the prompt",
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Focused queries within the subject",
                },
            },
            "required": ["subject_id", "queries"],
        },
    },
]


def retrieval_condition(mode: str) -> tuple[str, str, list[dict[str, Any]]]:
    if mode == "no-tools":
        return (
            "longmemeval-s-cleaned-native-dittobench-memory-v1",
            NO_TOOL_SYSTEM_PROMPT,
            [],
        )
    if mode == "native-memory-tools":
        return (
            "longmemeval-s-cleaned-native-memory-tools-v2",
            NATIVE_MEMORY_TOOL_SYSTEM_PROMPT,
            NATIVE_MEMORY_TOOLS,
        )
    raise AdapterError(f"unsupported retrieval mode: {mode}")


def tool_call_names(response: dict[str, Any]) -> list[str]:
    raw_calls = response.get("tool_calls", [])
    if raw_calls is None:
        return []
    if not isinstance(raw_calls, list):
        raise AdapterError("harness response tool_calls must be an array")
    names: list[str] = []
    for call in raw_calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise AdapterError("harness response contains an invalid tool call")
        names.append(call["name"])
    return names


class AdapterError(RuntimeError):
    """Raised when the dataset or harness violates the adapter contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(*parts: str, size: int = 24) -> str:
    value = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:size]


LONGMEMEVAL_TIMESTAMP = re.compile(
    r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2}) "
    r"\([A-Za-z]{3}\) (?P<hour>\d{2}):(?P<minute>\d{2})$"
)


def normalize_timestamp(value: str) -> str:
    """Convert LongMemEval's display timestamp to the wire's RFC 3339 shape."""

    match = LONGMEMEVAL_TIMESTAMP.fullmatch(value)
    if match is None:
        raise AdapterError(f"unsupported LongMemEval timestamp: {value!r}")
    parts = match.groupdict()
    return (
        f"{parts['year']}-{parts['month']}-{parts['day']}T"
        f"{parts['hour']}:{parts['minute']}:00Z"
    )


def _memory_pair(
    question_id: str,
    session_id: str,
    timestamp: str,
    first_turn: int,
    last_turn: int,
    prompt: str,
    response: str,
) -> dict[str, str]:
    return {
        "pair_id": "lme-"
        + stable_id(question_id, session_id, str(first_turn), str(last_turn)),
        "session_id": session_id,
        "timestamp": timestamp,
        "prompt": prompt,
        "response": response,
    }


def entry_to_pairs(entry: dict[str, Any]) -> list[dict[str, str]]:
    """Losslessly encode ordered user/assistant turns in DittoBench pair records.

    LongMemEval contains assistant-first, odd-length, and occasionally
    same-role-adjacent sessions. Empty prompt/response sides preserve those
    turns without adding synthetic dialogue text.
    """

    question_id = _required_string(entry, "question_id")
    session_ids = entry.get("haystack_session_ids")
    dates = entry.get("haystack_dates")
    sessions = entry.get("haystack_sessions")
    if (
        not isinstance(session_ids, list)
        or not isinstance(dates, list)
        or not isinstance(sessions, list)
    ):
        raise AdapterError(f"{question_id}: history fields must be arrays")
    if not (len(session_ids) == len(dates) == len(sessions)):
        raise AdapterError(f"{question_id}: history arrays have different lengths")

    pairs: list[dict[str, str]] = []
    for raw_session_id, raw_date, turns in zip(
        session_ids, dates, sessions, strict=True
    ):
        session_id = str(raw_session_id)
        timestamp = normalize_timestamp(str(raw_date))
        if not isinstance(turns, list):
            raise AdapterError(f"{question_id}/{session_id}: session must be an array")

        pending_user: tuple[int, str] | None = None
        for turn_index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise AdapterError(
                    f"{question_id}/{session_id}: turn must be an object"
                )
            role = turn.get("role")
            content = turn.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise AdapterError(
                    f"{question_id}/{session_id}: unsupported role or content"
                )

            if role == "user":
                if pending_user is not None:
                    previous_index, previous_content = pending_user
                    pairs.append(
                        _memory_pair(
                            question_id,
                            session_id,
                            timestamp,
                            previous_index,
                            previous_index,
                            previous_content,
                            "",
                        )
                    )
                pending_user = (turn_index, content)
                continue

            if pending_user is None:
                pairs.append(
                    _memory_pair(
                        question_id,
                        session_id,
                        timestamp,
                        turn_index,
                        turn_index,
                        "",
                        content,
                    )
                )
            else:
                user_index, user_content = pending_user
                pairs.append(
                    _memory_pair(
                        question_id,
                        session_id,
                        timestamp,
                        user_index,
                        turn_index,
                        user_content,
                        content,
                    )
                )
                pending_user = None

        if pending_user is not None:
            user_index, user_content = pending_user
            pairs.append(
                _memory_pair(
                    question_id,
                    session_id,
                    timestamp,
                    user_index,
                    user_index,
                    user_content,
                    "",
                )
            )

    # Some cleaned LongMemEval sessions contain turns whose content is an empty
    # string. A pair with neither user nor assistant content carries no memory,
    # and native harness embedders correctly reject it as an empty query.
    pairs = [
        pair for pair in pairs if pair["prompt"].strip() or pair["response"].strip()
    ]

    # The cleaned dataset also reuses some session/pair identities. Keep the
    # last occurrence so rejecting and upserting native stores receive the same
    # logical last-write-wins history without synthetic duplicate memory.
    unique_reversed: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for pair in reversed(pairs):
        if pair["pair_id"] in seen_ids:
            continue
        seen_ids.add(pair["pair_id"])
        unique_reversed.append(pair)
    return list(reversed(unique_reversed))


def chunked(values: list[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    if size < 1:
        raise AdapterError("seed wave size must be positive")
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _required_string(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise AdapterError(f"{field} must be a non-empty string")
    return item


@dataclass
class HarnessClient:
    base_url: str
    timeout_seconds: float = 180.0
    attempts: int = 1
    bench_version: int = 9

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace").strip()
            raise AdapterError(
                f"{method} {path} failed: HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (
            urllib.error.URLError,
            ConnectionError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            raise AdapterError(f"{method} {path} failed: {exc}") from exc

    def health(self) -> None:
        self.request("GET", "/health")

    def seed(self, user_id: str, wave: int, pairs: list[dict[str, str]]) -> Any:
        return self.request(
            "POST",
            "/seed",
            {
                "user_id": user_id,
                "wave": wave,
                "pairs": pairs,
                "subjects": [],
                "links": [],
            },
        )

    def run(
        self, entry: dict[str, Any], user_id: str, retrieval_mode: str
    ) -> dict[str, Any]:
        question_id = _required_string(entry, "question_id")
        question = _required_string(entry, "question")
        question_date = _required_string(entry, "question_date")
        _, system_prompt, tools = retrieval_condition(retrieval_mode)
        result = self.request(
            "POST",
            "/run",
            {
                "case_id": "memory_lookup-lme-" + stable_id(question_id, size=20),
                "system_prompt": system_prompt.format(question_date=question_date),
                "user_input": question,
                "tools": tools,
                "user_id": user_id,
                "bench_version": self.bench_version,
            },
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("final_text"), str
        ):
            raise AdapterError(f"{question_id}: harness response is missing final_text")
        return result


def read_completed(path: Path) -> set[str]:
    completed: set[str] = set()
    if not path.exists():
        return completed
    with path.open() as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                completed.add(_required_string(entry, "question_id"))
            except (json.JSONDecodeError, AdapterError) as exc:
                raise AdapterError(
                    f"invalid resume output at line {line_number}: {exc}"
                ) from exc
    return completed


def iter_json_array(path: Path, chunk_size: int = 1024 * 1024) -> Iterable[Any]:
    """Stream a top-level JSON array without retaining the whole decoded dataset."""
    if chunk_size < 1:
        raise AdapterError("JSON stream chunk size must be positive")
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    with path.open(encoding="utf-8") as handle:
        while True:
            chunk = handle.read(chunk_size)
            eof = not chunk
            buffer += chunk
            position = 0
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if not started:
                    if position >= len(buffer):
                        break
                    if buffer[position] != "[":
                        raise AdapterError("dataset root must be an array")
                    started = True
                    position += 1
                    continue
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if position < len(buffer) and buffer[position] == "]":
                    trailing = buffer[position + 1 :] + handle.read()
                    if trailing.strip():
                        raise AdapterError(
                            "dataset has content after the top-level array"
                        )
                    return
                value_start = position
                try:
                    value, value_end = decoder.raw_decode(buffer, value_start)
                except json.JSONDecodeError as exc:
                    if eof:
                        raise AdapterError(
                            f"invalid dataset JSON array: {exc}"
                        ) from exc
                    position = value_start
                    break
                separator = value_end
                while separator < len(buffer) and buffer[separator].isspace():
                    separator += 1
                if separator >= len(buffer) and not eof:
                    position = value_start
                    break
                if separator >= len(buffer):
                    raise AdapterError("unterminated dataset JSON array")
                if buffer[separator] not in {",", "]"}:
                    raise AdapterError("dataset array entries must be comma-separated")
                yield value
                position = separator + 1
                if buffer[separator] == "]":
                    trailing = buffer[position:] + handle.read()
                    if trailing.strip():
                        raise AdapterError(
                            "dataset has content after the top-level array"
                        )
                    return
            buffer = buffer[position:]
            if eof:
                if not started:
                    raise AdapterError("dataset root must be an array")
                raise AdapterError("unterminated dataset JSON array")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def run(args: argparse.Namespace) -> None:
    dataset_path = Path(args.dataset)
    actual_sha = sha256_file(dataset_path)
    if actual_sha != args.dataset_sha256:
        raise AdapterError(
            f"dataset SHA-256 mismatch: got {actual_sha}, want {args.dataset_sha256}"
        )

    # Keeping all 500 decoded histories alive expands the 265 MB source file to
    # roughly 3 GB in CPython. Retain compact JSON strings and materialize only
    # the entries currently being processed by isolated harness shards.
    selected: list[tuple[int, str, str]] = []
    questions_in_file = 0
    selection_end = None if args.limit is None else args.offset + args.limit
    for absolute_index, entry in enumerate(iter_json_array(dataset_path)):
        questions_in_file += 1
        if not isinstance(entry, dict):
            raise AdapterError(f"dataset entry {absolute_index} must be an object")
        if absolute_index < args.offset or (
            selection_end is not None and absolute_index >= selection_end
        ):
            continue
        question_id = _required_string(entry, "question_id")
        selected.append(
            (
                absolute_index - args.offset,
                question_id,
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
            )
        )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = read_completed(output_path) if args.resume else set()
    if output_path.exists() and not args.resume:
        raise AdapterError(
            f"output already exists: {output_path}; use --resume or choose another path"
        )

    harness_url_template = getattr(args, "harness_url_template", None)
    explicit_harness_urls = getattr(args, "harness_urls", None)
    shards = getattr(args, "shards", 1)
    if explicit_harness_urls:
        harness_urls = [value.strip() for value in explicit_harness_urls.split(",")]
        if not all(harness_urls):
            raise AdapterError("--harness-urls contains an empty URL")
        if shards != 1:
            raise AdapterError("--shards cannot be combined with --harness-urls")
    elif harness_url_template:
        harness_urls = [
            harness_url_template.format(shard=index + 1) for index in range(shards)
        ]
    else:
        harness_urls = [args.harness_url]
    clients = [
        HarnessClient(
            url,
            args.timeout,
            args.attempts,
            getattr(args, "bench_version", 9),
        )
        for url in harness_urls
    ]
    for client in clients:
        client.health()
    retrieval_mode = getattr(args, "retrieval_mode", "native-memory-tools")
    condition, system_prompt, advertised_tools = retrieval_condition(retrieval_mode)
    started_at = utc_now()
    counters = {
        "completed": 0,
        "pairs": 0,
        "waves": 0,
        "latency_ms": 0,
        "empty_answers": 0,
        "responses_with_tool_calls": 0,
        "tool_calls": 0,
    }
    calls_by_name: dict[str, int] = {}
    output_lock = threading.Lock()
    pending = [
        (local_index, entry_json)
        for local_index, question_id, entry_json in selected
        if question_id not in completed
    ]

    with output_path.open("a", encoding="utf-8") as output:

        def process_shard(shard_index: int) -> None:
            client = clients[shard_index]
            for pending_index, (local_index, entry_json) in enumerate(pending):
                assignment_index = (
                    pending_index if args.rebalance_pending else local_index
                )
                if assignment_index % len(clients) != shard_index:
                    continue
                entry = json.loads(entry_json)
                position = args.offset + local_index
                question_id = _required_string(entry, "question_id")
                user_id_namespace = getattr(args, "user_id_namespace", "")
                user_id_parts = (
                    (question_id,)
                    if not user_id_namespace
                    else (user_id_namespace, question_id)
                )
                user_id = "lme-" + stable_id(*user_id_parts, size=24)
                pairs = entry_to_pairs(entry)
                if user_id_namespace:
                    for pair in pairs:
                        pair["pair_id"] = "lme-" + stable_id(
                            user_id_namespace, pair["pair_id"], size=24
                        )
                waves = list(chunked(pairs, args.seed_wave_pairs))
                for wave, pair_batch in enumerate(waves):
                    client.seed(user_id, wave, pair_batch)

                began = time.monotonic()
                response = client.run(entry, user_id, retrieval_mode)
                latency_ms = round((time.monotonic() - began) * 1000)
                call_names = tool_call_names(response)
                result_line = json.dumps(
                    {"question_id": question_id, "hypothesis": response["final_text"]},
                    ensure_ascii=False,
                )
                with output_lock:
                    print(result_line, file=output, flush=True)
                    counters["completed"] += 1
                    counters["pairs"] += len(pairs)
                    counters["waves"] += len(waves)
                    counters["latency_ms"] += latency_ms
                    counters["empty_answers"] += int(not response["final_text"].strip())
                    counters["responses_with_tool_calls"] += int(bool(call_names))
                    counters["tool_calls"] += len(call_names)
                    for name in call_names:
                        calls_by_name[name] = calls_by_name.get(name, 0) + 1
                    print(
                        f"[{position + 1}/{args.offset + len(selected)}] {question_id} "
                        f"shard={shard_index + 1} pairs={len(pairs)} "
                        f"waves={len(waves)} "
                        f"latency_ms={latency_ms}",
                        file=sys.stderr,
                        flush=True,
                    )

        if len(clients) == 1:
            process_shard(0)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(clients)
            ) as executor:
                futures = [
                    executor.submit(process_shard, index)
                    for index in range(len(clients))
                ]
                for future in futures:
                    future.result()

    manifest = {
        "schema_version": 1,
        "condition": condition,
        "dataset": {
            "path": dataset_path.name,
            "sha256": actual_sha,
            "questions_in_file": questions_in_file,
            "offset": args.offset,
            "limit": args.limit,
        },
        "adapter": {
            "bench_version": getattr(args, "bench_version", 8),
            "retrieval_mode": retrieval_mode,
            "system_prompt": system_prompt,
            "advertised_tools": [tool["name"] for tool in advertised_tools],
            "case_id_prefix": "memory_lookup-lme-",
            "seed_wave_pairs": args.seed_wave_pairs,
            "isolated_harness_shards": len(clients),
            "user_id_namespace": getattr(args, "user_id_namespace", ""),
            "pair_id_namespace": getattr(args, "user_id_namespace", ""),
            "rebalanced_pending_questions": args.rebalance_pending,
            "answer_fields_sent_to_harness": False,
            "question_type_sent_to_harness": False,
            "has_answer_labels_sent_to_harness": False,
        },
        "answer_model_profile": getattr(args, "answer_model_profile", ""),
        "answer_model": getattr(args, "answer_model", ""),
        "agent_label": args.agent_label,
        "started_at": started_at,
        "finished_at": utc_now(),
        "completed_this_run": counters["completed"],
        "previously_completed": len(completed),
        "total_pairs_this_run": counters["pairs"],
        "total_seed_waves_this_run": counters["waves"],
        "answer_latency_ms_this_run": counters["latency_ms"],
        "empty_answers_this_run": counters["empty_answers"],
        "responses_with_tool_calls_this_run": counters["responses_with_tool_calls"],
        "tool_calls_this_run": counters["tool_calls"],
        "tool_calls_by_name_this_run": dict(sorted(calls_by_name.items())),
        "hypotheses_sha256": sha256_file(output_path),
    }
    manifest_path = Path(args.manifest or (str(output_path) + ".manifest.json"))
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--dataset-sha256", required=True)
    harness = parser.add_mutually_exclusive_group(required=True)
    harness.add_argument("--harness-url")
    harness.add_argument(
        "--harness-urls",
        help="Comma-separated explicit URLs for isolated local harness shards",
    )
    harness.add_argument(
        "--harness-url-template",
        help=(
            "Harness URL containing {shard}; used with --shards for isolated "
            "parallelism"
        ),
    )
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--agent-label", required=True)
    parser.add_argument(
        "--bench-version",
        type=int,
        default=9,
        help="Agent benchmark generation recorded in the research manifest",
    )
    parser.add_argument(
        "--answer-model",
        default="",
        help="Upstream reader model recorded in the research manifest",
    )
    parser.add_argument(
        "--answer-model-profile",
        default="",
        help="Auditable label for the shared reader model/proxy condition",
    )
    parser.add_argument(
        "--retrieval-mode",
        choices=("native-memory-tools", "no-tools"),
        default="native-memory-tools",
        help=(
            "Advertise the native Ditto memory tool catalog, or reproduce the "
            "original no-tool ablation"
        ),
    )
    parser.add_argument(
        "--user-id-namespace",
        default="",
        help="Optional deterministic user/pair namespace for resuming partial seeds",
    )
    parser.add_argument(
        "--rebalance-pending",
        action="store_true",
        help="Redistribute only unfinished questions across shards when resuming",
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed-wave-pairs", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--attempts", type=int, choices=(1,), default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.offset < 0 or (args.limit is not None and args.limit < 1):
        parser.error("--offset must be non-negative and --limit must be positive")
    if (
        args.seed_wave_pairs < 1
        or args.attempts < 1
        or args.timeout <= 0
        or args.shards < 1
    ):
        parser.error("wave size, attempts, and timeout must be positive")
    if args.bench_version < 1:
        parser.error("--bench-version must be positive")
    if args.harness_url_template and "{shard}" not in args.harness_url_template:
        parser.error("--harness-url-template must contain {shard}")
    if args.shards != 1 and not args.harness_url_template:
        parser.error("--shards requires --harness-url-template")
    if args.rebalance_pending and (not args.resume or not args.user_id_namespace):
        parser.error("--rebalance-pending requires --resume and --user-id-namespace")
    return args


if __name__ == "__main__":
    try:
        run(parse_args())
    except (AdapterError, OSError, ValueError) as exc:
        print(f"longmemeval adapter: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
