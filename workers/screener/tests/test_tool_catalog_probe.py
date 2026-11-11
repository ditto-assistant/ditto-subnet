"""The full-size tool-catalog probe: gateway measurement, catalog, verdict."""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from ditto_screener import tool_catalog_probe
from ditto_screener.fake_gateway import (
    FakeModelGateway,
    measure_model_request,
    token_lower_bound,
)
from ditto_screener.tool_catalog_probe import (
    FLOOR_TOKENS_PER_TOOL,
    SCORING_CONTEXT_TOKENS,
    full_size_catalog,
    judge,
    load_contract,
    run_request,
)

_REPO = Path(__file__).resolve().parents[3]
_GO_CATALOG = _REPO / "research/dittobench-datagen/catalog/v18.go"


def _chat_body(tools: list[dict[str, object]], *, user: str = "hi") -> bytes:
    return json.dumps(
        {
            "model": "openai/gpt-oss-20b",
            "messages": [
                {"role": "system", "content": "You are Ditto."},
                {"role": "user", "content": user},
            ],
            "tools": [{"type": "function", "function": tool} for tool in tools],
        }
    ).encode()


@pytest.mark.parametrize(
    ("text", "floor"),
    [
        ("", 0),
        ("hello world", 2),
        # Underscores split o200k letter chunks ("ACME", "_LIST", ...).
        ("ACME_LIST_WIDGETS", 3),
        # A contraction suffix stays inside its word's chunk.
        ("it's fine", 2),
        # Digits and punctuation are never counted: their chunks may merge.
        ("2026-11-11, 10:30 {}[]", 0),
        # Non-ASCII joins the run it touches, so it cannot add a count.
        ("naïve café", 2),
        ("漢字 only", 1),
    ],
)
def test_token_floor_counts_only_word_runs(text: str, floor: int) -> None:
    assert token_lower_bound(text) == floor


def test_measurement_counts_rendered_text_not_json_syntax() -> None:
    tool = {
        "name": "ACME_GET_ORDER",
        "description": "Fetch one order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "The id"}},
        },
    }
    measured = measure_model_request(_chat_body([tool], user="where is it"))

    assert measured["tools"] == 1
    # ACME GET ORDER (3) + Fetch one order (3) + order id (2) + The id (2)
    # + You are Ditto (3) + where is it (3). No "type", "function", "object"
    # or "properties": a renderer may drop or reshape the schema syntax.
    assert measured["token_lower_bound"] == 16
    assert measured["bytes"] == len(_chat_body([tool], user="where is it"))


def test_measurement_reads_the_responses_api_shape() -> None:
    body = json.dumps(
        {
            "model": "openai/gpt-oss-20b",
            "instructions": "Be brief",
            "input": [{"role": "user", "content": [{"text": "list my deals"}]}],
            "tools": [
                {"type": "function", "name": "CRM_LIST_DEALS", "description": "List."}
            ],
        }
    ).encode()

    measured = measure_model_request(body)

    assert measured["tools"] == 1
    assert measured["token_lower_bound"] == 2 + 3 + 3 + 1


def test_measurement_of_an_unparseable_body_is_zero_not_large() -> None:
    measured = measure_model_request(b"not json at all")

    assert measured == {"bytes": 15, "tools": 0, "token_lower_bound": 0}


async def test_gateway_logs_one_measurement_per_forwarded_model_request(
    tmp_path: Path,
) -> None:
    log = tmp_path / "model-requests"
    catalog = full_size_catalog(random.Random(1))
    whole = _chat_body(catalog)
    assert len(whole) > 1024 * 1024  # past the gateway's former 1 MiB cap
    async with FakeModelGateway(request_log_file=str(log)) as gateway:
        url = gateway.gateway_url.replace("host.docker.internal", "127.0.0.1")
        async with httpx.AsyncClient(timeout=30) as client:
            assert (
                await client.post(f"{url}/v1/chat/completions", content=whole)
            ).status_code == 200
            small = _chat_body(catalog[:5])
            assert (
                await client.post(f"{url}/v1/chat/completions", content=small)
            ).status_code == 200
            embed = await client.post(f"{url}/v1/embeddings", json={"input": "x"})
            assert embed.status_code == 200

    records = [json.loads(line) for line in log.read_text().splitlines()]
    # Embeddings are not model requests.
    assert len(records) == 2
    assert records[0]["tools"] == len(catalog)
    assert records[0]["bytes"] == len(whole)
    assert records[0]["token_lower_bound"] > SCORING_CONTEXT_TOKENS
    assert records[1]["tools"] == 5
    assert records[1]["token_lower_bound"] < 5 * FLOOR_TOKENS_PER_TOOL + 20


async def test_gateway_records_an_unreadable_body_as_unmeasured(
    tmp_path: Path,
) -> None:
    log = tmp_path / "model-requests"
    async with FakeModelGateway(request_log_file=str(log)) as gateway:
        port = urlsplit(gateway.gateway_url).port
        assert port is not None
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # A declared body past the cap is refused before it is read.
        writer.write(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {4 * 1024 * 1024 + 1}\r\n\r\n".encode()
        )
        await writer.drain()
        raw_response = await reader.read()
        writer.close()
        await writer.wait_closed()
    # The harness still gets an ordinary answer; only the measurement is lost.
    assert raw_response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {"measured": False}
    ]


def test_full_size_catalog_matches_the_contract_and_its_floor() -> None:
    contract = load_contract()
    catalog = full_size_catalog(random.Random(7))

    assert len(catalog) == contract.max_catalog_tools
    assert len({tool["name"] for tool in catalog}) == len(catalog)
    for tool in catalog:
        assert tool["parameters"] == contract.toolkit_tool_parameters
        floor = measure_model_request(json.dumps({"tools": [tool]}).encode())[
            "token_lower_bound"
        ]
        assert floor == FLOOR_TOKENS_PER_TOOL
    # Forwarding the whole catalog is the overflow the probe exists to catch.
    whole = measure_model_request(_chat_body(catalog))["token_lower_bound"]
    assert whole > SCORING_CONTEXT_TOKENS
    # And a harness that forwards the same share of a real full-size catalog
    # overflows too: real v18 tools cost more than the synthetic floor.
    assert FLOOR_TOKENS_PER_TOOL < 82.7


def test_each_screen_draws_a_different_catalog() -> None:
    first = full_size_catalog(random.Random(1))
    second = full_size_catalog(random.Random(2))

    assert [tool["name"] for tool in first] != [tool["name"] for tool in second]


def test_run_request_is_a_scored_tool_case_envelope() -> None:
    rng = random.Random(3)
    catalog = full_size_catalog(rng)
    request = run_request(rng, catalog, bench_version=18)

    assert re.fullmatch(r"c[0-9a-f]{16}", str(request["case_id"]))
    assert request["bench_version"] == 18
    assert request["tools"] is catalog
    assert "tool_endpoint" not in request  # the gate fills the scorer's route


def test_contract_is_pinned_to_the_go_catalog_source() -> None:
    """The Go contract owns the catalog; this file must move with it.

    ``catalog/screener_probe_contract_test.go`` checks the same file from the
    Go side against ``MaxCatalogTools`` and ``v18ToolkitSchema``.
    """
    source = _GO_CATALOG.read_text(encoding="utf-8")
    sizes = re.findall(
        r"\{protocol\.BenchVersionV(\d+), CatalogSize\{Min: (\d+), Max: (\d+)\}\}",
        source,
    )
    assert sizes, "catalogSizes entries not found in v18.go"
    newest_version, _min, newest_max = max(sizes, key=lambda entry: int(entry[0]))
    contract = load_contract()
    assert contract.bench_version == int(newest_version)
    assert contract.max_catalog_tools == max(int(entry[2]) for entry in sizes)
    assert contract.max_catalog_tools == int(newest_max)
    schema = re.search(
        r'v18ToolkitSchema = schema\(nil, prop\{name: "(\w+)", typ: "(\w+)", '
        r'desc: "([^"]+)"\}\)',
        source,
    )
    assert schema is not None
    name, typ, desc = schema.groups()
    assert contract.toolkit_tool_parameters == {
        "type": "object",
        "properties": {name: {"type": typ, "description": desc}},
    }


def test_judge_passes_a_request_that_fits() -> None:
    verdict = judge(
        [{"bytes": 9000, "tools": 12, "token_lower_bound": 900}], run_note=""
    )

    assert verdict.passed
    assert verdict.detail == ""
    assert "12 tools" in verdict.observation
    assert "Groq" not in verdict.observation


def test_judge_fails_only_past_the_context_window() -> None:
    at_limit = judge(
        [{"bytes": 1, "tools": 2000, "token_lower_bound": SCORING_CONTEXT_TOKENS}],
        run_note="",
    )
    over = judge(
        [
            {"bytes": 10, "tools": 3, "token_lower_bound": 50},
            {
                "bytes": 1_100_000,
                "tools": 2000,
                "token_lower_bound": SCORING_CONTEXT_TOKENS + 1,
            },
        ],
        run_note="",
    )

    assert at_limit.passed
    assert not over.passed
    assert over.code == "tool-catalog-context-overflow"
    assert "131072" in over.detail and "2000 tool definitions" in over.detail
    # More than 128 tools is only noted: the route falls back past Groq.
    assert "Groq" in over.observation and "Groq" in at_limit.observation


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"measured": False}],
        [{"bytes": 1, "tools": 1, "token_lower_bound": "999999"}],
        [{"bytes": 1, "tools": -1, "token_lower_bound": 999_999}],
    ],
)
def test_judge_never_fails_without_a_valid_measurement(
    records: list[dict[str, object]],
) -> None:
    verdict = judge(records, run_note="/run failed: HTTP 500")

    assert verdict.passed
    assert "/run failed" in verdict.observation


def test_request_log_parsing_skips_torn_lines() -> None:
    raw = b'{"bytes":1,"tools":1,"token_lower_bound":2}\n{"bytes":\n[1]\n'

    assert tool_catalog_probe.parse_request_log(raw) == [
        {"bytes": 1, "tools": 1, "token_lower_bound": 2}
    ]
