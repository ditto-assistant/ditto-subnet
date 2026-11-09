"""Estimated priced inference cost and cost factor for a local rehearsal.

From bench 16 the validator's scorer prices the whole run at the contract's
locked public per-token rates and multiplies the composite by a steep factor
of the cost per case (services/dittobench-api/PROTOCOL.md, "Bench v16 priced
inference cost factor"). A local rehearsal talks to the miner's own provider,
so the scorer cannot see that spend. This module puts a small metering proxy
in front of the harness's chat provider (and, on OpenRouter or a platform
broker, the decision lane), counts the provider-reported ``usage`` of every
completion and decision call, and applies the same published rule.

The rule here is a copy of ``ditto_screening_protocol.priced_cost`` (itself
the Python mirror of ``scoregates/priced_cost.go``); ``test_local_rehearsal``
checks it against the shared vectors
(``internal/scoregates/testdata/priced_cost_vectors.json``) so the three never
drift. Embeddings stay on the local Ollama embedder and are not metered (about
$0.001 of a v15 run). The meter also serves ``POST .../decisions``, the shape
of the validator's ``DITTOBENCH_DECISIONS_URL``: it forwards to OpenRouter's
System One route (``/systemone``, model ``typesafe/jev-1.14``) or to a platform
broker's ``/decisions``, keylessly for the harness, and prices decision input
tokens at the locked rate. ``route_harness_through`` exports that URL unless
the environment already names one.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PRICED_COST_MIN_BENCH_VERSION = 16
# Bench v17+ uses the priced-cost-v2 contract ($0.50 reference, 0.3 floor, steeper curve).
PRICED_COST_V2_MIN_BENCH_VERSION = 17
PROMPT_NANO_USD = 70
COMPLETION_NANO_USD = 150
EMBEDDING_NANO_USD = 4
DECISION_INPUT_NANO_USD = 42
DECISION_OUTPUT_NANO_USD = 0
NORMALIZATION_CASES = 350
REFERENCE_NANO_USD = 1_000_000_000
REFERENCE_NANO_USD_V2 = 500_000_000
MAX_RATIO_BPS = 100_000
CURVE: tuple[tuple[int, int], ...] = (
    (10_000, 10_000),
    (15_000, 8_000),
    (20_000, 6_000),
)
CURVE_V2: tuple[tuple[int, int], ...] = (
    (10_000, 10_000),
    (15_000, 6_500),
    (20_000, 3_000),
)
_UINT64_MAX = (1 << 64) - 1
_MAX_TOKENS = 9_007_199_254_740_991

# Bench 18+ per-run cost budget: a scored run stops admitting cases once its
# priced cost reaches the cost at which the factor hits its floor (2.0 x the
# $0.50 reference = $1.00 per 350 cases), and every unrun case scores 0.
RUN_BUDGET_MIN_BENCH_VERSION = 18


def run_budget_nano_usd(bench_version: int, cases: int) -> int | None:
    """The scorer's budget for a run of ``cases`` cases, or None below v18."""
    if bench_version < RUN_BUDGET_MIN_BENCH_VERSION or cases <= 0:
        return None
    per_run = REFERENCE_NANO_USD_V2 * CURVE_V2[-1][0] // 10_000
    # Rounded up, as the scorer does: the first whole cost whose 350-case
    # normalization reaches the floor threshold.
    return -(-per_run * cases // NORMALIZATION_CASES)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter serves TypeSafe System One here; a platform broker serves it as
# ``/decisions`` beside chat.
OPENROUTER_DECISIONS_PATH = "/systemone"
PLATFORM_DECISIONS_PATH = "/decisions"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
_BODY_LIMIT = 32 << 20


def factor_bps(ratio_bps: int) -> int:
    """The published v1 curve: 1.0 to the reference, 0.8 at 1.5x, 0.6 from 2x."""
    if ratio_bps <= CURVE[0][0]:
        return CURVE[0][1]
    for (lo_r, lo_f), (hi_r, hi_f) in zip(CURVE, CURVE[1:], strict=False):
        if ratio_bps <= hi_r:
            return lo_f - (lo_f - hi_f) * (ratio_bps - lo_r) // (hi_r - lo_r)
    return CURVE[-1][1]


def _factor_bps_for_curve(ratio_bps: int, curve: tuple[tuple[int, int], ...]) -> int:
    if ratio_bps <= curve[0][0]:
        return curve[0][1]
    for (lo_r, lo_f), (hi_r, hi_f) in zip(curve, curve[1:], strict=False):
        if ratio_bps <= hi_r:
            return lo_f - (lo_f - hi_f) * (ratio_bps - lo_r) // (hi_r - lo_r)
    return curve[-1][1]


def priced_cost(
    *,
    cases: int,
    bench_version: int = PRICED_COST_MIN_BENCH_VERSION,
    prompt_tokens: int,
    completion_tokens: int,
    embedding_tokens: int = 0,
    decision_input_tokens: int = 0,
    decision_output_tokens: int = 0,
) -> dict[str, int]:
    """Cost (nano-USD), the 350-case-normalised cost, ratio and factor."""

    def clamp(v: int) -> int:
        return min(max(v, 0), _MAX_TOKENS)

    def sat(v: int) -> int:
        return min(v, _UINT64_MAX)

    cost = sat(
        sat(
            sat(clamp(prompt_tokens) * PROMPT_NANO_USD)
            + sat(clamp(completion_tokens) * COMPLETION_NANO_USD)
        )
        + sat(
            sat(clamp(embedding_tokens) * EMBEDDING_NANO_USD)
            + sat(
                sat(clamp(decision_input_tokens) * DECISION_INPUT_NANO_USD)
                + sat(clamp(decision_output_tokens) * DECISION_OUTPUT_NANO_USD)
            )
        )
    )
    if cases <= 0:
        return {
            "cost_nano_usd": cost,
            "normalized_cost_nano_usd": 0,
            "ratio_bps": 0,
            "factor_bps": 10_000,
        }
    v2 = bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION
    reference = REFERENCE_NANO_USD_V2 if v2 else REFERENCE_NANO_USD
    curve = CURVE_V2 if v2 else CURVE
    normalized = sat(cost * NORMALIZATION_CASES) // cases
    ratio = min(sat(normalized * 10_000) // reference, MAX_RATIO_BPS)
    return {
        "cost_nano_usd": cost,
        "normalized_cost_nano_usd": normalized,
        "ratio_bps": ratio,
        "factor_bps": _factor_bps_for_curve(ratio, curve),
    }


@dataclass
class MeterTotals:
    completions: int = 0
    usage_missing: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    decision_calls: int = 0
    decision_input_tokens: int = 0
    decision_output_tokens: int = 0


def _usage_from_body(body: bytes, content_type: str) -> dict[str, Any] | None:
    """Provider usage from a JSON completion or the last SSE chunk carrying it."""
    if "text/event-stream" in content_type:
        usage = None
        for line in body.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
        return usage
    try:
        decoded = json.loads(body)
    except ValueError:
        return None
    usage = decoded.get("usage") if isinstance(decoded, dict) else None
    return usage if isinstance(usage, dict) else None


class CostMeter:
    """A loopback OpenAI-compatible chat proxy that totals provider usage.

    The harness is pointed at it with ``DITTOBENCH_PROVIDER=platform`` and
    ``DITTOBENCH_INFERENCE_BASE_URL`` -- the same variables a validator
    injects -- so the provider key stays in this process, exactly as a
    validator never hands one to a harness. The base URL carries a random
    per-meter path token (like the validator broker's per-session route), so
    another local process cannot borrow the key through the port, and only
    ``POST .../chat/completions`` and ``GET .../models`` are forwarded.
    """

    def __init__(
        self,
        upstream_base_url: str,
        api_key: str | None,
        decisions_path: str | None = None,
    ) -> None:
        self.upstream = upstream_base_url.rstrip("/")
        self._api_key = api_key
        # Upstream path for ``POST .../decisions``; None refuses the route.
        self.decisions_path = decisions_path
        self._token = secrets.token_urlsafe(24)
        self.totals = MeterTotals()
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        meter = self
        prefix = f"/m/{self._token}/v1"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:  # quiet
                return

            def _route(self) -> str | None:
                """The upstream path for an allowed request, else None."""
                if not self.path.startswith(prefix + "/"):
                    return None
                rest = self.path[len(prefix) :]
                path, separator, query = rest.partition("?")
                suffix = f"?{query}" if separator else ""
                if self.command == "POST" and path == "/chat/completions":
                    return path + suffix
                if (
                    self.command == "POST"
                    and path == "/decisions"
                    and meter.decisions_path is not None
                ):
                    return meter.decisions_path + suffix
                if self.command == "GET" and path == "/models":
                    return path + suffix
                return None

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._reply(200, b'{"status":"ok"}', "application/json")
                    return
                route = self._route()
                if route is None:
                    self._reply(404, b'{"error":"not found"}', "application/json")
                    return
                self._forward(route, None)

            def do_POST(self) -> None:  # noqa: N802
                route = self._route()
                if route is None:
                    self._reply(404, b'{"error":"not found"}', "application/json")
                    return
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    self._reply(
                        411, b'{"error":"Content-Length required"}', "application/json"
                    )
                    return
                try:
                    length = int(raw_length)
                except ValueError:
                    self._reply(
                        400, b'{"error":"bad Content-Length"}', "application/json"
                    )
                    return
                if length < 0 or length > _BODY_LIMIT:
                    self._reply(
                        413, b'{"error":"request too large"}', "application/json"
                    )
                    return
                self._forward(route, self.rfile.read(length))

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _forward(self, route: str, body: bytes | None) -> None:
                request = urllib.request.Request(
                    meter.upstream + route, data=body, method=self.command
                )
                request.add_header(
                    "Content-Type",
                    self.headers.get("Content-Type") or "application/json",
                )
                if meter._api_key:
                    request.add_header("Authorization", f"Bearer {meter._api_key}")
                elif self.headers.get("Authorization"):
                    # A platform / OpenAI-compatible upstream the harness was
                    # already configured for authenticates with the harness's
                    # own header; pass it through unchanged.
                    request.add_header("Authorization", self.headers["Authorization"])
                try:
                    with urllib.request.urlopen(request, timeout=600) as response:
                        status = response.status
                        content_type = (
                            response.headers.get("Content-Type") or "application/json"
                        )
                        payload = response.read()
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    content_type = exc.headers.get("Content-Type") or "application/json"
                    payload = exc.read()
                except (urllib.error.URLError, OSError) as exc:
                    message = json.dumps(
                        {"error": f"upstream unavailable: {type(exc).__name__}"}
                    )
                    self._reply(502, message.encode(), "application/json")
                    return
                if body is not None and route.startswith(str(meter.decisions_path)):
                    # A metered decision call is priced from its usage even
                    # when it answers an error (PROTOCOL.md, priced cost).
                    meter.record_decision(_usage_from_body(payload, content_type))
                elif body is not None and 200 <= status < 300:
                    meter.record(_usage_from_body(payload, content_type))
                self._reply(status, payload, content_type)

        return Handler

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("cost meter is not started")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/m/{self._token}/v1"

    def record(self, usage: dict[str, Any] | None) -> None:
        with self._lock:
            self.totals.completions += 1
            prompt = usage.get("prompt_tokens") if usage else None
            completion = usage.get("completion_tokens") if usage else None
            if not isinstance(prompt, int) or not isinstance(completion, int):
                self.totals.usage_missing += 1
                return
            self.totals.prompt_tokens += max(prompt, 0)
            self.totals.completion_tokens += max(completion, 0)

    def record_decision(self, usage: dict[str, Any] | None) -> None:
        with self._lock:
            self.totals.decision_calls += 1
            input_tokens = usage.get("input_tokens") if usage else None
            output_tokens = usage.get("output_tokens") if usage else None
            if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
                self.totals.usage_missing += 1
                return
            self.totals.decision_input_tokens += max(input_tokens, 0)
            self.totals.decision_output_tokens += max(output_tokens, 0)

    @property
    def decisions_url(self) -> str | None:
        """The keyless decision-lane URL the harness should use, if served."""
        if self.decisions_path is None:
            return None
        return self.base_url + "/decisions"

    def start(self) -> CostMeter:
        """Bind the loopback port and serve; nothing is bound before this."""
        if self._server is None:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving; a no-op for a meter that never started."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None


def meter_for_environment(env: dict[str, str]) -> CostMeter | None:
    """A meter in front of the chat provider the harness env selects, or None
    when that provider cannot be fronted (the caller says so and runs
    unmetered)."""
    provider = (env.get("DITTOBENCH_PROVIDER") or "openrouter").strip().lower()
    if provider == "ollama":
        base = (env.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
        return CostMeter(base + "/v1", None)
    if provider in ("openrouter", ""):
        key = env.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            return None
        return CostMeter(OPENROUTER_BASE_URL, key, OPENROUTER_DECISIONS_PATH)
    if provider in ("platform", "chutes"):
        base = env.get("DITTOBENCH_INFERENCE_BASE_URL") or env.get("CHUTES_BASE_URL")
        if not base:
            return None
        # Only the platform broker serves the decision lane beside chat.
        decisions_path = PLATFORM_DECISIONS_PATH if provider == "platform" else None
        return CostMeter(base, None, decisions_path)
    return None


def route_harness_through(meter: CostMeter, env: dict[str, str]) -> dict[str, str]:
    """The harness env that sends chat through the meter, keyless."""
    routed = dict(env)
    routed["DITTOBENCH_PROVIDER"] = "platform"
    routed["DITTOBENCH_INFERENCE_BASE_URL"] = meter.base_url
    routed.pop("OPENROUTER_API_KEY", None)
    if meter.decisions_url and not (routed.get("DITTOBENCH_DECISIONS_URL") or "").strip():
        routed["DITTOBENCH_DECISIONS_URL"] = meter.decisions_url
    return routed


def estimate(
    meter: CostMeter | None,
    report: dict[str, Any],
    bench_version: int = PRICED_COST_MIN_BENCH_VERSION,
) -> dict[str, Any] | None:
    """The estimate for a finished run: the scorer's own ``details.priced_cost``
    when it priced the run, else the meter's chat totals under the same rule
    (the rehearsed ``bench_version`` picks the v1 or v2 contract)."""
    details = report.get("details") or {}
    scored = details.get("priced_cost")
    if isinstance(scored, dict) and scored.get("result") == "priced":
        out = {"source": "scorer", **scored}
        if isinstance(details.get("run_budget"), dict):
            out["run_budget"] = details["run_budget"]
        return _with_budget(out, bench_version)
    if meter is None:
        return None
    cases = int(report.get("n") or 0)
    with meter._lock:
        totals = MeterTotals(
            completions=meter.totals.completions,
            usage_missing=meter.totals.usage_missing,
            prompt_tokens=meter.totals.prompt_tokens,
            completion_tokens=meter.totals.completion_tokens,
            decision_calls=meter.totals.decision_calls,
            decision_input_tokens=meter.totals.decision_input_tokens,
            decision_output_tokens=meter.totals.decision_output_tokens,
        )
    out: dict[str, Any] = {
        "source": "local_meter",
        "cases": cases,
        "completions": totals.completions,
        "usage_missing": totals.usage_missing,
        "prompt_tokens": totals.prompt_tokens,
        "completion_tokens": totals.completion_tokens,
        "decision_calls": totals.decision_calls,
        "decision_input_tokens": totals.decision_input_tokens,
        "decision_output_tokens": totals.decision_output_tokens,
    }
    out.update(
        priced_cost(
            cases=cases,
            bench_version=bench_version,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            decision_input_tokens=totals.decision_input_tokens,
            decision_output_tokens=totals.decision_output_tokens,
        )
    )
    # The scorer's own budget verdict wins even when it did not price the run.
    if isinstance(details.get("run_budget"), dict):
        out["run_budget"] = details["run_budget"]
    return _with_budget(out, bench_version)


def _with_budget(estimate_: dict[str, Any], bench_version: int) -> dict[str, Any]:
    """Add the bench 18+ budget and, when the run spent past it, where a scored
    run would have stopped. The scorer's own ``details.run_budget`` wins when
    the report carries one. Otherwise spend is assumed even across cases: the
    scorer admits case k while the spend before it, k x cost / cases, is below
    the budget, so ceil(budget x cases / cost) cases run, and a run that costs
    exactly its budget runs every case."""
    cases = int(estimate_.get("cases") or 0)
    scored = estimate_.get("run_budget")
    if isinstance(scored, dict):
        budget = scored.get("budget_nano_usd")
        if budget is None:
            budget = run_budget_nano_usd(bench_version, cases)
        if budget is not None:
            estimate_["budget_nano_usd"] = int(budget)
        exhausted = bool(scored.get("exhausted"))
        admitted = scored.get("admitted_cases")
        estimate_["budget_exhausted"] = exhausted
        estimate_["budget_stop_case"] = (
            int(admitted) if exhausted and admitted is not None else None
        )
        return estimate_
    budget = run_budget_nano_usd(bench_version, cases)
    if budget is None:
        return estimate_
    estimate_["budget_nano_usd"] = budget
    cost = int(estimate_.get("cost_nano_usd") or 0)
    admitted = -(-budget * cases // cost) if cost > 0 else cases
    estimate_["budget_exhausted"] = admitted < cases
    estimate_["budget_stop_case"] = admitted if admitted < cases else None
    return estimate_


def format_estimate(estimate_: dict[str, Any] | None, bench_version: int) -> str:
    if estimate_ is None:
        return "\n".join(
            [
                "",
                "=== Priced inference cost (bench 16+) ===",
                "not metered: the harness's chat provider could not be fronted by the",
                "local meter (use DITTOBENCH_PROVIDER=openrouter with",
                "OPENROUTER_API_KEY, or ollama), or --no-cost-meter was given.",
            ]
        )
    cost = estimate_["cost_nano_usd"] / 1e9
    normalized = estimate_["normalized_cost_nano_usd"] / 1e9
    factor = estimate_["factor_bps"] / 10_000
    reference_usd = (
        REFERENCE_NANO_USD_V2
        if bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION
        else REFERENCE_NANO_USD
    ) / 1e9
    applies = bench_version >= PRICED_COST_MIN_BENCH_VERSION
    lines = [
        "",
        "=== Priced inference cost (bench 16+) ===",
        f"source:              {estimate_['source']}",
        f"cases:               {estimate_.get('cases', 0)}",
        f"prompt_tokens:       {estimate_.get('prompt_tokens', 0)}",
        f"completion_tokens:   {estimate_.get('completion_tokens', 0)}",
        f"decision_calls:      {estimate_.get('decision_calls', 0)}",
        f"decision_input:      {estimate_.get('decision_input_tokens', 0)}",
        f"estimated cost:      ${cost:.4f} this run",
        f"per 350 cases:       ${normalized:.4f} (reference ${reference_usd:.2f})",
        f"cost factor:         {factor:.4f}"
        + (
            ""
            if applies
            else f" (informational: bench {bench_version} does not apply it)"
        ),
    ]
    if "budget_nano_usd" in estimate_:
        budget_usd = estimate_["budget_nano_usd"] / 1e9
        if estimate_.get("budget_exhausted"):
            stop = estimate_.get("budget_stop_case")
            if stop is None:
                where = "The scorer stopped admitting cases and scored the rest 0"
            else:
                where = (
                    f"A scored run stops about case {stop} of "
                    f"{estimate_.get('cases', 0)} and scores the rest 0"
                )
            lines.append(
                f"run budget:          ${budget_usd:.4f} (bench 18+): EXCEEDED. {where}"
            )
        else:
            lines.append(f"run budget:          ${budget_usd:.4f} (bench 18+): not reached")
    if estimate_.get("usage_missing"):
        lines.append(
            f"warning: {estimate_['usage_missing']} call(s) returned no usage; "
            "the estimate is a lower bound"
        )
    lines += [
        "Validators price prompt $0.07/M, completion (reasoning included) $0.15/M,",
        "embeddings $0.004/M and decision-lane input $0.042/M at locked rates, and",
        (
            "multiply the composite by 1.0 up to $0.50 per 350 cases, 0.65 at $0.75"
            " and 0.3 from $1.00 (bench 17+)."
            if bench_version >= PRICED_COST_V2_MIN_BENCH_VERSION
            else "multiply the composite by 1.0 up to $1.00 per 350 cases, 0.8 at"
            " $1.50 and 0.6 from $2.00 (bench 16)."
        ),
        "Fewer, shorter prompts (for example a pruned tool catalog) raise the",
        "factor. Local chat may differ from the validators' gpt-oss-20b.",
    ]
    return "\n".join(lines)
