"""Lightweight causal proof for decisive static-malicious findings.

The legacy detector intentionally remains a broad co-occurrence detector.  V2
uses its matches only as candidates, then requires a bounded value-flow proof
and a proven build/runtime path.  Ambiguity is returned as an advisory lead;
it is never converted into a 100%-confidence finding.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from ditto_screener.rust_test_items import is_rust_test_only_attribute
from ditto_screener.source_reachability import (
    ReachabilityEvidence,
    ReachabilityState,
)
from ditto_screener.source_signals import find_decisive_malicious_source, mask_comments


class CausalState(StrEnum):
    PROVEN = "proven"
    ABSENT = "absent"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class V2StaticAnalysis:
    decisive: tuple[dict[str, object], ...]
    advisory: tuple[dict[str, object], ...]


_ASSIGNMENT = re.compile(
    r"(?:^|[;{]\s*)(?:let(?:\s+mut)?|const|var|final)?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?!=)(.+?)(?:;|$)"
)
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_URL = re.compile(r"https?://[^\s'\"),;]+", re.IGNORECASE)
_OUTBOUND = re.compile(
    r"\b(?:post|put|send|upload|webhook|callback|curl|wget|request)\w*\s*(?:\.|\()",
    re.IGNORECASE,
)
_READ = re.compile(
    r"\b(?:read|open|scan|walk|glob|copy|collect|upload)\w*\s*\(",
    re.IGNORECASE,
)
_CONTROL = re.compile(
    r"(?:\b(?:connect|request|post|create|start|exec|build|mount|run|socket|client|daemon)\w*\s*(?:\.|\()|"
    r"(?:^|\s)(?:docker|podman|mount)\s+)",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?:/proc/(?:1|self)/environ|(?:^|[/\\])\.env\b|"
    r"(?:^|[/\\])\.(?:ssh|aws|azure)(?:[/\\]|\b)|"
    r"\.config[/\\]gcloud|\.bittensor[/\\]wallets|"
    r"std::env::vars|env::vars|os\.environ|GetEnvironmentVariables|"
    r"private[-_ ]?key|mnemonic)",
    re.IGNORECASE,
)
_CROSS_USER = re.compile(r"(?:/root(?:/|\b)|/home/|/Users/|/proc/1/root|/host(?:/|\b))")
_DOCKER_TARGET = re.compile(
    r"(?:/var/run/docker\.sock|/run/(?:user/[0-9]+/)?docker\.sock|"
    r"\bDOCKER_HOST\b|tcp://[^\s]+:237[56]|/v1\.[0-9]+/(?:containers|images|volumes))",
    re.IGNORECASE,
)
_HOST_BOUNDARY = re.compile(
    r"(?:--privileged\b|--(?:pid|ipc|network)(?:=|\s+)host\b|"
    r"(?:source|src)(?:=|:)\s*/(?:\s|[,\"'])|"
    r"(?:-v|--volume)\s+/(?:\s*):|/proc/1/root)",
    re.IGNORECASE,
)


def test_only_line_numbers(path: str, text: str) -> frozenset[int]:
    """Return deterministic language-native test-only source lines."""
    normalized = path.casefold().removeprefix("./")
    if normalized.endswith("_test.go"):
        return frozenset(range(1, len(text.splitlines()) + 1))
    if not normalized.endswith(".rs"):
        return frozenset()
    lines = mask_comments(text).splitlines()
    marked: set[int] = set()
    for index, line in enumerate(lines):
        if not is_rust_test_only_attribute(line):
            continue
        depth = 0
        opened = False
        # Include attributes between cfg/test and the item itself.
        for cursor in range(index, len(lines)):
            marked.add(cursor + 1)
            depth += lines[cursor].count("{")
            depth -= lines[cursor].count("}")
            if "{" in lines[cursor]:
                opened = True
            if opened and depth <= 0:
                break
            if not opened and cursor > index + 8:
                break
    return frozenset(marked)


def mask_test_only_source(path: str, text: str) -> str:
    marked = test_only_line_numbers(path, text)
    if not marked:
        return text
    return "\n".join(
        "" if line_number in marked else line
        for line_number, line in enumerate(text.splitlines(), 1)
    )


def _external_url_state(line: str) -> CausalState:
    urls = _URL.findall(line)
    if not urls:
        return CausalState.UNRESOLVED
    for url in urls:
        try:
            host = urlsplit(url).hostname
            if host is None:
                continue
            if host.casefold() == "localhost":
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return CausalState.PROVEN
            if not (address.is_loopback or address.is_unspecified):
                return CausalState.PROVEN
        except ValueError:
            continue
    return CausalState.ABSENT


def _taint_by_line(
    lines: list[str], source_pattern: re.Pattern[str]
) -> tuple[list[dict[str, int]], bool]:
    tainted: dict[str, int] = {}
    snapshots: list[dict[str, int]] = []
    unresolved = False
    for line_number, line in enumerate(lines, 1):
        for match in _ASSIGNMENT.finditer(line):
            variable, expression = match.groups()
            identifiers = set(_IDENTIFIER.findall(expression))
            if source_pattern.search(expression):
                tainted[variable] = line_number
            elif identifiers.intersection(tainted):
                tainted[variable] = min(
                    tainted[name] for name in identifiers if name in tainted
                )
            else:
                tainted.pop(variable, None)
            if any(
                token in expression for token in ("eval(", "getattr(", "macro_rules!")
            ):
                unresolved = True
        snapshots.append(dict(tainted))
    return snapshots, unresolved


def _line_carries_source(
    line: str, variables: Mapping[str, int], source_pattern: re.Pattern[str]
) -> tuple[bool, int | None]:
    if source_pattern.search(line):
        return True, None
    used = [
        line_number
        for name, line_number in variables.items()
        if re.search(rf"\b{re.escape(name)}\b", line)
    ]
    return (bool(used), min(used) if used else None)


def _proof_for_category(
    category: str, path: str, text: str
) -> tuple[CausalState, tuple[dict[str, object], ...], str]:
    # Candidate extraction already requires executable effect roles after
    # comment/string masking. Preserve raw quoted URLs here: the shared Rust-
    # oriented masker treats `//` inside a Python single-quoted URL as a line
    # comment, which would turn a proven external endpoint into uncertainty.
    lines = mask_test_only_source(path, text).splitlines()
    if category == "data_exfiltration":
        taint, unresolved = _taint_by_line(lines, _SENSITIVE)
        saw_sink = False
        saw_loopback = False
        for line_number, line in enumerate(lines, 1):
            if not _OUTBOUND.search(line):
                continue
            saw_sink = True
            carries, source_line = _line_carries_source(
                line, taint[line_number - 1], _SENSITIVE
            )
            if not carries:
                # The bounded proof cannot establish that this additional
                # outbound effect is independent from the sensitive value.
                # In particular, a proven loopback flow elsewhere in the file
                # must not turn an untraced external or dynamic sink into an
                # affirmative-safe clearance.
                unresolved = True
                continue
            external = _external_url_state(line)
            if external == CausalState.PROVEN:
                return (
                    CausalState.PROVEN,
                    (
                        {
                            "path": path,
                            "line": source_line or line_number,
                            "role": "sensitive_source",
                        },
                        {"path": path, "line": line_number, "role": "external_sink"},
                    ),
                    "sensitive-value-to-external-sink",
                )
            if external == CausalState.ABSENT:
                saw_loopback = True
            else:
                unresolved = True
        if saw_loopback and not unresolved:
            return CausalState.ABSENT, (), "loopback-only-sink"
        if saw_sink or unresolved:
            return CausalState.UNRESOLVED, (), "unresolved-source-to-sink-flow"
        return CausalState.UNRESOLVED, (), "no-outbound-sink"

    if category in {"cross_user_access", "credential_access"}:
        pattern = _CROSS_USER if category == "cross_user_access" else _SENSITIVE
        taint, unresolved = _taint_by_line(lines, pattern)
        for line_number, line in enumerate(lines, 1):
            if not _READ.search(line):
                continue
            carries, source_line = _line_carries_source(
                line, taint[line_number - 1], pattern
            )
            if carries:
                return (
                    CausalState.PROVEN,
                    (
                        {
                            "path": path,
                            "line": source_line or line_number,
                            "role": "sensitive_path",
                        },
                        {"path": path, "line": line_number, "role": "access_sink"},
                    ),
                    "sensitive-path-to-access-sink",
                )
        return (
            CausalState.UNRESOLVED,
            (),
            "unresolved-access-flow" if unresolved else "no-path-to-access-flow",
        )

    if category == "malicious_build":
        for pattern, source_role in (
            (_DOCKER_TARGET, "docker_target"),
            (_HOST_BOUNDARY, "host_boundary"),
        ):
            taint, unresolved = _taint_by_line(lines, pattern)
            for line_number, line in enumerate(lines, 1):
                if not _CONTROL.search(line):
                    continue
                carries, source_line = _line_carries_source(
                    line, taint[line_number - 1], pattern
                )
                if carries:
                    return (
                        CausalState.PROVEN,
                        (
                            {
                                "path": path,
                                "line": source_line or line_number,
                                "role": source_role,
                            },
                            {"path": path, "line": line_number, "role": "control_sink"},
                        ),
                        "dangerous-target-to-control-effect",
                    )
            if unresolved:
                return CausalState.UNRESOLVED, (), "unresolved-control-flow"
        return CausalState.UNRESOLVED, (), "no-target-to-control-flow"

    return CausalState.UNRESOLVED, (), "unsupported-static-rule"


def analyze_static_candidates_v2(
    files: Iterable[tuple[str, str]],
    reachability: Mapping[str, ReachabilityEvidence],
) -> V2StaticAnalysis:
    """Require both proven reachability and rule-specific causal flow."""
    material = list(files)
    decisive_candidates = find_decisive_malicious_source(
        [(path, mask_test_only_source(path, text)) for path, text in material],
        explicitly_executable_paths=frozenset(path for path, _ in material),
    )
    raw_candidates = find_decisive_malicious_source(
        material,
        explicitly_executable_paths=frozenset(path for path, _ in material),
    )
    by_path = dict(material)
    decisive: list[dict[str, object]] = []
    advisory: list[dict[str, object]] = []
    resolved_signatures: set[tuple[str, str, str]] = set()
    for candidate in decisive_candidates:
        locations = candidate.get("locations")
        if not isinstance(locations, list) or not locations:
            continue
        path = str(locations[0]["path"])
        category = str(candidate["category"])
        path_evidence = reachability.get(
            path, ReachabilityEvidence(ReachabilityState.UNRESOLVED)
        )
        causal_state, causal_path, reason = _proof_for_category(
            category, path, by_path.get(path, "")
        )
        enriched = {
            **candidate,
            "reachability_state": path_evidence.state.value,
            "reachability_bases": list(path_evidence.bases),
            "causal_state": causal_state.value,
            "causal_path": list(causal_path),
            "resolution_basis": reason,
        }
        signature = (path, category, str(candidate["kind"]))
        if (
            path_evidence.state == ReachabilityState.PROVEN_REACHABLE
            and causal_state == CausalState.PROVEN
        ):
            decisive.append(enriched)
        else:
            advisory.append(enriched)
        resolved_signatures.add(signature)
    for candidate in raw_candidates:
        locations = candidate.get("locations")
        if not isinstance(locations, list) or not locations:
            continue
        path = str(locations[0]["path"])
        signature = (path, str(candidate["category"]), str(candidate["kind"]))
        if signature in resolved_signatures:
            continue
        path_evidence = reachability.get(
            path, ReachabilityEvidence(ReachabilityState.UNRESOLVED)
        )
        advisory.append(
            {
                **candidate,
                "reachability_state": path_evidence.state.value,
                "reachability_bases": list(path_evidence.bases),
                "causal_state": CausalState.UNRESOLVED.value,
                "causal_path": [],
                "resolution_basis": "test-only-or-incomplete-candidate",
            }
        )
    return V2StaticAnalysis(tuple(decisive), tuple(advisory))


__all__ = [
    "CausalState",
    "V2StaticAnalysis",
    "analyze_static_candidates_v2",
    "mask_test_only_source",
    "test_only_line_numbers",
]
