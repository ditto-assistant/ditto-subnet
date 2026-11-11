"""Full-size tool-catalog probe: does the harness's model request still fit?

From bench v18 every scored case offers a per-seed catalog of real toolkit
tools, 1,400 to 2,000 of them (``catalog.CatalogSizeForVersion`` in
dittobench-datagen). A harness that copies the whole catalog into each chat
request cannot fit it in the locked scoring model's context window: on the v18
rollout (DITTO-2564) such harnesses sent 1,517-1,770 tool definitions per
request and every provider refused them ("Input length (133780-164437) exceeds
model's maximum context length (131072)"). The dataset seed is fixed per
(agent, validator), so the run fails the same way on every retry. Harnesses
that select or defer tools score normally on the same cases.

The probe sends one scorer-shaped ``POST /run`` that offers a catalog of the
contract's maximum size, lets the isolated fake gateway measure each model
request the harness forwards (``fake_gateway.measure_model_request``), and
fails only when a request's provable token floor exceeds the context window.

Precision over recall. The floor counts only words every provider must render
(tool names, descriptions, parameter names and descriptions, message text),
one token per word-like run, and no schema syntax. The synthetic catalog
spends at most ``FLOOR_TOKENS_PER_TOOL`` floor tokens per tool, below what a
real full-size v18 tool costs (82.7 gpt-oss tokens per tool on the 2,000-tool
seed, measured with the harmony renderer). A harness that trips the floor here
therefore overflows on a real full-size catalog too, whether it forwards every
tool or caps them by count, bytes or estimated tokens.

Groq, one provider in the scored route, also refuses more than 128 tools. The
route always includes other providers and falls back past it, so that cap is
recorded as an observation and never fails a screen.
"""

from __future__ import annotations

import copy
import json
import random
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ditto_screener.fake_gateway import token_lower_bound

CONTRACT_FILE = Path(__file__).parent / "data" / "tool-catalog-probe-v18.json"
"""Pinned to the Go contract by tests on both sides: the bench version, the
largest catalog it serves (``catalog.MaxCatalogTools``) and the schema every
toolkit tool renders (``v18ToolkitSchema``)."""

SCORING_MODEL = "openai/gpt-oss-20b"
SCORING_CONTEXT_TOKENS = 131_072
"""gpt-oss-20b's context window, as every provider of the route states it."""

PROVIDER_TOOL_CAP = 128
"""Groq's ``'tools': maximum number of items is 128``. Observation only."""

FLOOR_TOKENS_PER_TOOL = 70
"""Floor tokens one synthetic tool costs when forwarded verbatim.

Forwarding all 2,000 floors at 140,000, 6.8% over the window, so a harness
that trims a little per tool still overflows here only if it overflows for
real. Keep it below the real per-tool cost (82.7) or the probe stops being a
lower bound of the scored request."""

OBSERVATION_CODE = "tool-catalog-request-size"
"""Recorded on every probed screen, passing or not."""

CONTEXT_OVERFLOW_CODE = "tool-catalog-context-overflow"

_SYSTEM_PROMPT = (
    "You are Ditto, a helpful assistant with access to tools. Call a tool "
    "only when it is the right action for the user's request."
)
_USER_INPUTS = (
    "Can you check whether my {object} in {kit} changed since yesterday?",
    "Please pull up the latest {object} from {kit} for me.",
    "What do I need to do to update a {object} in {kit}?",
)
_KITS = (
    "Acumen",
    "Bramble",
    "Cobalt",
    "Dovetail",
    "Ember",
    "Fathom",
    "Gable",
    "Harbor",
    "Inkwell",
    "Juniper",
    "Keystone",
    "Lattice",
    "Meridian",
    "Nimbus",
    "Orchard",
    "Pinnacle",
    "Quarry",
    "Rivet",
    "Sable",
    "Tandem",
    "Upland",
    "Vantage",
    "Willow",
    "Yardstick",
    "Zephyr",
    "Anchor",
    "Beacon",
    "Cinder",
    "Delta",
    "Fable",
    "Granite",
    "Hollow",
    "Ledger",
    "Mosaic",
    "Nectar",
    "Onyx",
    "Prism",
    "Relay",
    "Summit",
    "Timber",
)
_VERBS = (
    "list",
    "get",
    "create",
    "update",
    "delete",
    "search",
    "archive",
    "export",
    "import",
    "sync",
    "assign",
    "approve",
    "close",
    "merge",
    "publish",
    "restore",
    "share",
    "tag",
    "move",
    "count",
)
_OBJECTS = (
    "account",
    "invoice",
    "ticket",
    "project",
    "contact",
    "order",
    "report",
    "member",
    "channel",
    "document",
    "folder",
    "invite",
    "comment",
    "task",
    "release",
    "campaign",
    "segment",
    "webhook",
    "policy",
    "schedule",
    "budget",
    "customer",
    "deal",
    "asset",
    "note",
    "record",
    "survey",
    "workspace",
    "product",
    "shipment",
)
_CLAUSES = (
    "Results are returned in pages and sorted by the time they were last changed",
    "The account must be connected before this action can run",
    "Use the identifier returned by the list action for the same resource",
    "Fields that are not set keep their current values",
    "The response includes the owner, the status and the time of the change",
    "Archived items are left out unless the request asks for them",
    "This action cannot be undone once the change is saved",
    "Rate limits apply per workspace and errors are returned as plain text",
    "Only items the connected user is allowed to see are included",
    "Dates use the time zone configured for the workspace",
    "Large exports may take a few minutes before the file is ready",
    "Pass a filter to narrow the results to a single team or owner",
)


@dataclass(frozen=True)
class ToolCatalogContract:
    """The catalog shape the probe has to match, read from ``CONTRACT_FILE``."""

    bench_version: int
    max_catalog_tools: int
    toolkit_tool_parameters: Mapping[str, object]


@cache
def load_contract() -> ToolCatalogContract:
    raw = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
    return ToolCatalogContract(
        bench_version=int(raw["bench_version"]),
        max_catalog_tools=int(raw["max_catalog_tools"]),
        toolkit_tool_parameters=raw["toolkit_tool_parameters"],
    )


def applies_to(bench_version: int) -> bool:
    """Whether a submission screened for ``bench_version`` meets the catalog."""
    return bench_version >= load_contract().bench_version


def full_size_catalog(rng: random.Random) -> list[dict[str, object]]:
    """A fresh catalog of the contract's maximum size, in its toolkit shape.

    Names and descriptions are drawn per screen, so no fixed catalog can be
    recognized. Every tool costs exactly ``FLOOR_TOKENS_PER_TOOL`` floor
    tokens when forwarded as written.
    """
    contract = load_contract()
    parameters = contract.toolkit_tool_parameters
    schema_floor = _schema_floor(parameters)
    tools: list[dict[str, object]] = []
    names: set[str] = set()
    while len(tools) < contract.max_catalog_tools:
        kit = rng.choice(_KITS)
        name = "_".join((kit, rng.choice(_VERBS), rng.choice(_OBJECTS))).upper()
        if name in names:
            continue
        names.add(name)
        budget = FLOOR_TOKENS_PER_TOOL - token_lower_bound(name) - schema_floor
        tools.append(
            {
                "name": name,
                "description": _description(rng, kit, budget),
                "parameters": copy.deepcopy(dict(parameters)),
            }
        )
    return tools


def run_request(
    rng: random.Random, catalog: list[dict[str, object]], *, bench_version: int
) -> dict[str, object]:
    """A scored tool case's ``/run`` envelope around ``catalog``."""
    sample = rng.choice(catalog)
    kit, _verb, obj = str(sample["name"]).split("_", 2)
    return {
        "case_id": "c" + secrets.token_hex(8),
        "bench_version": bench_version,
        "system_prompt": _SYSTEM_PROMPT,
        "user_input": rng.choice(_USER_INPUTS).format(
            kit=kit.title(), object=obj.lower()
        ),
        "tools": catalog,
    }


@dataclass(frozen=True)
class ProbeVerdict:
    passed: bool
    code: str
    detail: str
    """Miner-facing reason; empty when the probe passed."""
    observation: str
    """Bounded operator summary recorded as ``OBSERVATION_CODE`` evidence."""


def judge(records: Iterable[Mapping[str, object]], *, run_note: str) -> ProbeVerdict:
    """Decide from the gateway's measurements of the probe's model requests.

    Only a measured request whose floor exceeds the window fails. No model
    request, an unreadable body, or a failed ``/run`` passes and is recorded:
    none of them proves the scored request would overflow.
    """
    contract = load_contract()
    measured: list[tuple[int, int, int]] = []
    unmeasured = 0
    for record in records:
        floor = _count(record.get("token_lower_bound"))
        tools = _count(record.get("tools"))
        size = _count(record.get("bytes"))
        if (
            record.get("measured", True) is True
            and floor is not None
            and tools is not None
            and size is not None
        ):
            measured.append((floor, tools, size))
        else:
            unmeasured += 1
    parts = [f"{len(measured)} model requests measured"]
    if unmeasured:
        parts.append(f"{unmeasured} unmeasured")
    if run_note:
        parts.append(run_note)
    if not measured:
        return ProbeVerdict(True, "tool-catalog-ok", "", "; ".join(parts)[:240])
    floor, tools, size = max(measured)
    largest = f"largest has {tools} tools, >= {floor} tokens, {size} bytes"
    if tools > PROVIDER_TOOL_CAP:
        largest += f" (over Groq's {PROVIDER_TOOL_CAP}-tool cap; route falls back)"
    observation = "; ".join([largest, *parts])[:240]
    if floor <= SCORING_CONTEXT_TOKENS:
        return ProbeVerdict(True, "tool-catalog-ok", "", observation)
    return ProbeVerdict(
        False,
        CONTEXT_OVERFLOW_CODE,
        (
            f"given a full-size {contract.max_catalog_tools}-tool catalog (the "
            f"bench v{contract.bench_version} maximum), the harness sent a model "
            f"request with {tools} tool definitions ({size} bytes) that holds at "
            f"least {floor} prompt tokens. The scoring model {SCORING_MODEL} has "
            f"a {SCORING_CONTEXT_TOKENS}-token context window, so every provider "
            f"rejects that request on each scored case with a large catalog. "
            f"Send the model only the tools a turn needs (search or defer the "
            f"rest) instead of the whole catalog."
        ),
        observation,
    )


def parse_request_log(raw: bytes) -> list[dict[str, object]]:
    """Decode the gateway's JSON-lines measurements, skipping torn lines."""
    records: list[dict[str, object]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _schema_floor(parameters: Mapping[str, object]) -> int:
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return 0
    floor = 0
    for name, schema in properties.items():
        floor += token_lower_bound(name)
        if isinstance(schema, Mapping) and isinstance(schema.get("description"), str):
            floor += token_lower_bound(schema["description"])
    return floor


def _description(rng: random.Random, kit: str, budget: int) -> str:
    """Plain sentences whose floor is exactly ``budget``."""
    words: list[str] = []
    while len(words) < budget:
        sentence = rng.choice(_CLAUSES).replace("workspace", kit) + "."
        words.extend(sentence.split())
    # Every word above is one floor token, so a cut at ``budget`` words is a
    # cut at ``budget`` floor tokens.
    text = " ".join(words[:budget]).rstrip(".")
    return text + "."
