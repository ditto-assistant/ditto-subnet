#!/usr/bin/env python3
"""Replay frozen V13 L1-L3 ledgers through two report-only L4 courts.

The default is validation only. --execute requires a separately capped,
metered OpenRouter key; this script never calls Platform or Backroom writers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import httpx

from ditto_screener.adjudicator import (
    ADJUDICATOR_PROMPT_REVISION,
    SourceReviewAdjudicator,
)
from ditto_screening_protocol import SCREENING_POLICY_VERSION
from ditto_screening_protocol.models import SourceReviewFinding, SourceReviewInvariant

MODELS = ("z-ai/glm-5.3-flash", "openai/gpt-5.6-sol")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
RELEASE_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
BASELINE_LAYERS = ("l1", "l2", "l3", "l4")
HOST_EVIDENCE_FIELDS = (
    "agent_id",
    "attempt_id",
    "artifact_sha256",
    "image_identity_digest",
    "runtime_receipt_digest",
    "private_verification_or_nonapplicability_digest",
    "i1_i8_s1_s3_sweep_digest",
)
MAX_STEPS = 128
MAX_COMPLETION_TOKENS = 16_000
TIMEOUT_SECONDS = 600.0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--two-layer",
        action="store_true",
        help="replay frozen Sol investigator handoffs through a GLM verifier",
    )
    parser.add_argument("--max-reported-cost-usd", type=float)
    parser.add_argument("--external-route-cap-usd", type=float)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, value: object) -> None:
    _ensure_private_result_directory(path)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _ensure_private_result_directory(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("results directory must be private (mode 0700)")


def _uuid(value: object, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except ValueError as error:
        raise ValueError(f"{field} must be a UUID") from error
    return str(parsed)


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a full lowercase SHA-256 digest")
    return value


def _json_sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sol_handoff(
    raw: object, identity: Mapping[str, object]
) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("sol_investigator must be an object")
    for field in (
        "agent_id",
        "attempt_id",
        "artifact_sha256",
        "policy_version",
        "manifest_digest",
        "review_settings_revision",
    ):
        if raw.get(field) != identity[field]:
            raise ValueError(f"sol_investigator {field} differs from exact case")
    if raw.get("model") != "openai/gpt-5.6-sol":
        raise ValueError("sol_investigator model must be exact Sol")
    prompt_revision = raw.get("prompt_revision")
    if not isinstance(prompt_revision, str) or not prompt_revision:
        raise ValueError("sol_investigator needs a prompt revision")
    notes = raw.get("notes")
    if (
        not isinstance(notes, list)
        or len(notes) > 48
        or not all(isinstance(note, dict) for note in notes)
    ):
        raise ValueError("sol_investigator notes must be 0-48 objects")
    notes_sha = _sha(raw.get("notes_payload_sha256"), "sol notes_payload_sha256")
    if _json_sha256(notes) != notes_sha:
        raise ValueError("sol_investigator notes digest mismatch")
    finding = raw.get("finding")
    finding_digest = raw.get("finding_digest")
    if finding is None:
        if finding_digest is not None:
            raise ValueError("sol_investigator finding digest without finding")
    else:
        if not isinstance(finding, dict):
            raise ValueError("sol_investigator finding must be an object")
        try:
            parsed_finding = SourceReviewFinding.model_validate(finding)
        except ValueError as error:
            raise ValueError("sol_investigator finding is invalid") from error
        if parsed_finding.canonical_digest() != _sha(
            finding_digest, "sol finding_digest"
        ):
            raise ValueError("sol_investigator finding digest mismatch")
    error_code = raw.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise ValueError("sol_investigator error_code must be a string or null")
    latency_ms = raw.get("latency_ms")
    if (
        not isinstance(latency_ms, int)
        or isinstance(latency_ms, bool)
        or latency_ms < 0
    ):
        raise ValueError("sol_investigator needs nonnegative latency_ms")
    cost = raw.get("reported_cost_usd")
    if cost is not None and (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("sol_investigator cost must be nonnegative or null")
    compactions = raw.get("compactions")
    if (
        not isinstance(compactions, int)
        or isinstance(compactions, bool)
        or compactions < 0
    ):
        raise ValueError("sol_investigator needs nonnegative compactions")
    return {
        "model": raw["model"],
        "prompt_revision": prompt_revision,
        "notes": notes,
        "notes_payload_sha256": notes_sha,
        "finding": finding,
        "finding_digest": finding_digest,
        "error_code": error_code,
        "latency_ms": latency_ms,
        "reported_cost_usd": cost,
        "compactions": compactions,
    }


def _host_evidence(
    raw: object, identity: Mapping[str, object]
) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("mandatory_host_evidence must be an object")
    for field in ("agent_id", "attempt_id", "artifact_sha256"):
        if field in raw and raw[field] != identity[field]:
            raise ValueError(f"mandatory host {field} differs from exact case")
    for field in HOST_EVIDENCE_FIELDS[3:]:
        if field in raw and raw[field] is not None:
            _sha(raw[field], f"mandatory host {field}")
    return dict(raw)


def _case(raw: object, root: Path, policy_version: int) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("each case must be an object")
    agent_id = _uuid(raw.get("agent_id"), "agent_id")
    attempt_id = _uuid(raw.get("attempt_id"), "attempt_id")
    artifact_sha = _sha(raw.get("artifact_sha256"), "artifact_sha256")
    manifest_digest = _sha(raw.get("manifest_digest"), "manifest_digest")
    notes_digest = _sha(raw.get("review_notes_digest"), "review_notes_digest")
    if raw.get("policy_version") != policy_version:
        raise ValueError("case policy_version differs from manifest")
    settings_revision = raw.get("review_settings_revision")
    if (
        not isinstance(settings_revision, int)
        or isinstance(settings_revision, bool)
        or settings_revision < 1
    ):
        raise ValueError("case needs an exact review settings revision")
    baseline_release = raw.get("baseline_worker_release")
    if (
        not isinstance(baseline_release, str)
        or RELEASE_RE.fullmatch(baseline_release) is None
    ):
        raise ValueError("case needs an exact baseline worker release")
    baseline_prompts = raw.get("baseline_prompt_revisions")
    if (
        not isinstance(baseline_prompts, dict)
        or set(baseline_prompts) != set(BASELINE_LAYERS)
        or any(
            not isinstance(baseline_prompts[layer], str) or not baseline_prompts[layer]
            for layer in BASELINE_LAYERS
        )
    ):
        raise ValueError("case needs exact L1-L4 baseline prompt revisions")
    archive_name = raw.get("archive")
    if not isinstance(archive_name, str) or not archive_name:
        raise ValueError("archive must be a relative path")
    archive = (root / archive_name).resolve()
    if not archive.is_relative_to(root) or not archive.is_file():
        raise ValueError("archive must be a file inside artifact-root")
    if _sha256(archive) != artifact_sha:
        raise ValueError("artifact digest mismatch")
    notes = raw.get("notes")
    if not isinstance(notes, list) or not 1 <= len(notes) <= 48:
        raise ValueError("case needs 1-48 frozen review notes")
    if not all(isinstance(note, dict) for note in notes):
        raise ValueError("review notes must be objects")
    notes_payload_sha = _sha(raw.get("notes_payload_sha256"), "notes_payload_sha256")
    if _json_sha256(notes) != notes_payload_sha:
        raise ValueError("frozen notes payload digest mismatch")
    finding = raw.get("finding")
    if finding is not None and not isinstance(finding, dict):
        raise ValueError("finding must be an object or null")
    error_code = raw.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise ValueError("error_code must be a string or null")
    label = raw.get("label")
    if not isinstance(label, dict) or label.get("decision") not in {"clear", "reject"}:
        raise ValueError("independent label must be clear or reject")
    if label.get("provenance") != "independent-v13-source-review":
        raise ValueError("label needs independent-v13-source-review provenance")
    references = label.get("evidence_references")
    if (
        not isinstance(references, list)
        or not references
        or not all(isinstance(reference, str) and reference for reference in references)
    ):
        raise ValueError("label needs nonempty evidence references")
    expected_invariants = label.get("accepted_reject_invariants")
    if label["decision"] == "reject":
        known = {invariant.value for invariant in SourceReviewInvariant}
        if (
            not isinstance(expected_invariants, list)
            or not expected_invariants
            or any(invariant not in known for invariant in expected_invariants)
        ):
            raise ValueError("reject label needs accepted V13 invariant names")
    elif expected_invariants not in (None, []):
        raise ValueError("clear label cannot name breached invariants")
    cohort = raw.get("cohort")
    if not isinstance(cohort, str) or not cohort:
        raise ValueError("case needs a named cohort")
    identity = {
        "agent_id": agent_id,
        "attempt_id": attempt_id,
        "artifact_sha256": artifact_sha,
        "policy_version": policy_version,
        "manifest_digest": manifest_digest,
        "review_settings_revision": settings_revision,
    }
    return {
        "agent_id": agent_id,
        "attempt_id": attempt_id,
        "artifact_sha256": artifact_sha,
        "manifest_digest": manifest_digest,
        "review_settings_revision": settings_revision,
        "baseline_worker_release": baseline_release,
        "baseline_prompt_revisions": dict(baseline_prompts),
        "review_notes_digest": notes_digest,
        "notes_payload_sha256": notes_payload_sha,
        "archive": archive,
        "notes": notes,
        "finding": finding,
        "error_code": error_code,
        "label": label,
        "cohort": cohort,
        "sol_investigator": _sol_handoff(raw.get("sol_investigator"), identity),
        "mandatory_host_evidence": _host_evidence(
            raw.get("mandatory_host_evidence"), identity
        ),
    }


def _load_manifest(
    path: Path, root: Path
) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest = json.loads(path.read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("policy_version") != SCREENING_POLICY_VERSION
    ):
        raise ValueError("manifest must use current screening policy")
    if not isinstance(manifest.get("revision"), str) or not manifest["revision"]:
        raise ValueError("manifest needs a revision")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("manifest needs cases")
    cases = [_case(raw, root, SCREENING_POLICY_VERSION) for raw in raw_cases]
    identities = [(case["agent_id"], case["attempt_id"]) for case in cases]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate agent/attempt pair")
    return manifest, cases


def _summary(
    rows: list[dict[str, object]], models: tuple[str, ...] = MODELS
) -> dict[str, object]:
    by_model: dict[str, dict[str, object]] = {}
    for model in models:
        subset = [row for row in rows if row["requested_model"] == model]
        complete = [row for row in subset if row["complete"]]
        by_model[model] = {
            "completed": len(complete),
            "incomplete": len(subset) - len(complete),
            "cost_coverage_incomplete": sum(
                row["reported_cost_lower_bound"] is True for row in subset
            ),
            "matches": sum(row["label_match"] is True for row in complete),
            "false_clears": sum(
                row["label_decision"] == "reject" and row["decision"] == "clear"
                for row in complete
            ),
            "false_rejects": sum(
                row["label_decision"] == "clear" and row["decision"] == "reject"
                for row in complete
            ),
            "reject_invariant_matches": sum(
                row["reject_invariant_match"] is True for row in complete
            ),
            "reported_cost_usd": round(
                sum(float(row["reported_cost_usd"]) for row in subset), 6
            ),
        }
    paired: list[dict[str, object]] = []
    by_case: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for row in rows:
        key = (str(row["agent_id"]), str(row["attempt_id"]))
        by_case.setdefault(key, {})[str(row["requested_model"])] = row
    for pair in by_case.values():
        if (
            len(models) != 2
            or len(pair) != 2
            or not all(pair[m]["complete"] for m in models)
        ):
            continue
        paired.append(pair[models[1]])
    baseline_releases = sorted({str(row["baseline_worker_release"]) for row in rows})
    baseline_prompt_sets = {
        json.dumps(row["baseline_prompt_revisions"], sort_keys=True) for row in rows
    }
    return {
        "models": by_model,
        "fully_paired_cases": len(paired),
        "baseline_releases": baseline_releases,
        "mixed_baseline_releases": len(baseline_releases) > 1,
        "mixed_baseline_prompt_sets": len(baseline_prompt_sets) > 1,
    }


def _exception_code(error: Exception) -> str:
    """Return a bounded failure class without persisting exception text."""
    if isinstance(error, TimeoutError):
        return "call-timeout"
    if isinstance(error, httpx.HTTPError):
        return "provider-http-error"
    if isinstance(error, OSError):
        return "transport-error"
    if isinstance(error, ValueError):
        return "call-invalid"
    return "call-exception"


class _Meter:
    def __init__(self, model: str, max_cost: float, spent: list[float]) -> None:
        self.model = model
        self.max_cost = max_cost
        self.spent = spent
        self.responses = 0
        self.cost = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self.upstreams: set[str] = set()
        self.error: str | None = None

    def observe(self, metadata: Mapping[str, object]) -> None:
        self.responses += 1
        cost = metadata.get("cost_usd")
        prompt = metadata.get("prompt_tokens")
        completion = metadata.get("completion_tokens")
        if (
            metadata.get("model") != self.model
            or not isinstance(cost, (int, float))
            or isinstance(cost, bool)
            or not math.isfinite(cost)
            or cost < 0
            or not isinstance(prompt, int)
            or isinstance(prompt, bool)
            or prompt < 0
            or not isinstance(completion, int)
            or isinstance(completion, bool)
            or completion < 0
        ):
            self.error = "unmetered-or-route-mismatch"
            raise ValueError("calibration response lacked exact metering or model")
        self.cost += float(cost)
        self.spent[0] += float(cost)
        self.tokens_in += prompt
        self.tokens_out += completion
        upstream = metadata.get("upstream")
        if isinstance(upstream, str):
            self.upstreams.add(upstream)
        if self.spent[0] > self.max_cost:
            self.error = "reported-cost-cap-exceeded"
            raise ValueError("calibration reported cost cap exceeded")


async def _execute(
    args: argparse.Namespace,
    manifest: dict[str, object],
    cases: list[dict[str, object]],
) -> dict[str, object]:
    if args.api_key_file is None or not args.api_key_file.is_file():
        raise ValueError("--execute needs a dedicated route --api-key-file")
    if args.base_url != "https://openrouter.ai/api/v1":
        raise ValueError("only the metered OpenRouter route is supported")
    if (
        args.max_reported_cost_usd is None
        or not math.isfinite(args.max_reported_cost_usd)
        or args.max_reported_cost_usd <= 0
    ):
        raise ValueError("--execute needs --max-reported-cost-usd")
    if (
        args.external_route_cap_usd is None
        or not math.isfinite(args.external_route_cap_usd)
        or args.external_route_cap_usd <= 0
    ):
        raise ValueError("--execute needs an independently configured route cap")
    if args.max_reported_cost_usd > args.external_route_cap_usd:
        raise ValueError("reported cap cannot exceed external route cap")
    _ensure_private_result_directory(args.results_file)
    two_layer = bool(getattr(args, "two_layer", False))
    models = (MODELS[0],) if two_layer else MODELS
    rows: list[dict[str, object]] = []
    spent = [0.0]
    metadata = {
        "manifest_revision": manifest["revision"],
        "policy_version": SCREENING_POLICY_VERSION,
        "prompt_revision": ADJUDICATOR_PROMPT_REVISION,
        "models": models,
        "arm": "sol-investigator-glm-verifier"
        if two_layer
        else "frozen-ledger-l4-pair",
        "base_url": args.base_url,
        "max_steps": MAX_STEPS,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_reported_cost_usd": args.max_reported_cost_usd,
        "external_route_cap_usd": args.external_route_cap_usd,
        "external_route_cap_attested_not_verified": True,
    }
    for case in cases:
        handoff = case["sol_investigator"] if two_layer else None
        host_evidence = case["mandatory_host_evidence"] if two_layer else None
        missing_host_fields = (
            [
                field
                for field in HOST_EVIDENCE_FIELDS
                if not isinstance(host_evidence, dict)
                or host_evidence.get(field) is None
            ]
            if two_layer
            else []
        )
        skip_code = None
        if two_layer:
            if handoff is None:
                skip_code = "investigator-handoff-missing"
            elif missing_host_fields:
                skip_code = "mandatory-host-evidence-missing"
            elif not handoff["notes"]:
                skip_code = "investigator-no-evidence"
            elif handoff["reported_cost_usd"] is None:
                skip_code = "investigator-unmetered"
        for model in models:
            if spent[0] >= args.max_reported_cost_usd:
                raise ValueError("reported cost cap reached before all cases")
            meter = _Meter(model, args.max_reported_cost_usd, spent)
            started = time.monotonic()
            verdict = None
            call_error: Exception | None = None
            if skip_code is None:
                court = SourceReviewAdjudicator(
                    api_key_file=str(args.api_key_file),
                    base_url=args.base_url,
                    model=model,
                    timeout_seconds=TIMEOUT_SECONDS,
                    max_steps=MAX_STEPS,
                    max_completion_tokens=MAX_COMPLETION_TOKENS,
                    completion_observer=meter.observe,
                )
                try:
                    verdict = await court.adjudicate(
                        str(case["archive"]),
                        notes=handoff["notes"] if two_layer else case["notes"],
                        finding=handoff["finding"] if two_layer else case["finding"],
                        error_code=(
                            handoff["error_code"] if two_layer else case["error_code"]
                        ),
                        deadline=asyncio.get_running_loop().time() + TIMEOUT_SECONDS,
                        policy_version=SCREENING_POLICY_VERSION,
                        ledger_final=True,
                    )
                except Exception as error:
                    # A failed model arm is a coverage result, not a reason to
                    # silently omit it. Cancellation/SystemExit still propagate.
                    call_error = error
            verifier_latency_ms = round((time.monotonic() - started) * 1_000)
            decision = verdict.decision if verdict is not None else None
            complete = decision in {"clear", "reject"} and meter.error is None
            label = case["label"]
            accepted_invariants = label.get("accepted_reject_invariants") or []
            reject_invariant = (
                verdict.reject_invariant.value
                if verdict is not None and verdict.reject_invariant
                else None
            )
            citations = verdict.citations if verdict is not None else []
            cited_locations = {f"{cite.path}:{cite.line}" for cite in citations}
            diagnostic = verdict.run_diagnostic if verdict is not None else None
            error_class = (
                type(call_error).__name__
                if call_error is not None
                and re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9]{0,63}", type(call_error).__name__
                )
                else diagnostic.error_class
                if diagnostic is not None
                else None
            )
            error_code = (
                skip_code
                if skip_code is not None
                else _exception_code(call_error)
                if call_error is not None
                else diagnostic.failure_code
                if diagnostic is not None
                else None
            )
            investigator_cost = (
                float(handoff["reported_cost_usd"])
                if handoff is not None and handoff["reported_cost_usd"] is not None
                else 0.0
            )
            investigator_latency = int(handoff["latency_ms"]) if handoff else 0
            row: dict[str, object] = {
                "arm": metadata["arm"],
                "agent_id": case["agent_id"],
                "attempt_id": case["attempt_id"],
                "artifact_sha256": case["artifact_sha256"],
                "manifest_digest": case["manifest_digest"],
                "review_settings_revision": case["review_settings_revision"],
                "baseline_worker_release": case["baseline_worker_release"],
                "baseline_prompt_revisions": case["baseline_prompt_revisions"],
                "review_notes_digest": case["review_notes_digest"],
                "baseline_notes_payload_sha256": case["notes_payload_sha256"],
                "notes_payload_sha256": (
                    handoff["notes_payload_sha256"]
                    if handoff
                    else case["notes_payload_sha256"]
                ),
                "investigator_prompt_revision": (
                    handoff["prompt_revision"] if handoff else None
                ),
                "investigator_finding_digest": (
                    handoff["finding_digest"] if handoff else None
                ),
                "investigator_compactions": handoff["compactions"] if handoff else None,
                "host_evidence_status": (
                    "references_supplied_unverified"
                    if two_layer and not missing_host_fields
                    else "missing"
                    if two_layer
                    else "not_evaluated"
                ),
                "missing_host_evidence_fields": missing_host_fields,
                "host_evidence_reference_digests": (
                    {
                        field: host_evidence.get(field)
                        for field in HOST_EVIDENCE_FIELDS[3:]
                        if host_evidence.get(field) is not None
                    }
                    if isinstance(host_evidence, dict)
                    else None
                ),
                "policy_ready": False,
                "cohort": case["cohort"],
                "requested_model": model,
                "decision": decision,
                "label_decision": label["decision"],
                "label_match": decision == label["decision"] if complete else None,
                "complete": complete,
                "escalation_code": (
                    verdict.escalation_code
                    if verdict is not None
                    else skip_code or "calibration-call-failed"
                ),
                "error_class": error_class,
                "error_code": error_code,
                "reject_invariant": reject_invariant,
                "reject_invariant_match": (
                    reject_invariant in accepted_invariants
                    if complete
                    and decision == "reject"
                    and label["decision"] == "reject"
                    else None
                ),
                "cites_label_evidence": (
                    bool(cited_locations.intersection(label["evidence_references"]))
                    if complete
                    and decision == "reject"
                    and label["decision"] == "reject"
                    else None
                ),
                "clear_clause": verdict.clear_clause.value
                if verdict is not None and verdict.clear_clause
                else None,
                "citations": [
                    {"path": cite.path, "line": cite.line} for cite in citations
                ],
                "reason_sha256": (
                    hashlib.sha256(verdict.reason.encode()).hexdigest()
                    if verdict is not None
                    else None
                ),
                "latency_ms": investigator_latency + verifier_latency_ms,
                "investigator_latency_ms": investigator_latency if two_layer else None,
                "verifier_latency_ms": verifier_latency_ms,
                "responses": meter.responses,
                "reported_cost_usd": round(investigator_cost + meter.cost, 6),
                "investigator_reported_cost_usd": (
                    round(investigator_cost, 6) if two_layer and handoff else None
                ),
                "verifier_reported_cost_usd": round(meter.cost, 6),
                "prompt_tokens": meter.tokens_in,
                "completion_tokens": meter.tokens_out,
                "upstreams": sorted(meter.upstreams),
                "meter_error": meter.error,
                "reported_cost_lower_bound": not complete,
            }
            rows.append(row)
            _write_private_json(
                args.results_file,
                {"run": metadata, "summary": _summary(rows, models), "items": rows},
            )
            if meter.error is not None:
                raise ValueError(f"stopped after {meter.error}; partial report saved")
    return {"run": metadata, "summary": _summary(rows, models), "items": rows}


async def _main() -> None:
    args = _arguments()
    root = args.artifact_root.resolve()
    manifest, cases = _load_manifest(args.manifest, root)
    if not args.execute:
        models = (MODELS[0],) if args.two_layer else MODELS
        print(
            json.dumps(
                {
                    "validated_cases": len(cases),
                    "models": models,
                    "arm": (
                        "sol-investigator-glm-verifier"
                        if args.two_layer
                        else "frozen-ledger-l4-pair"
                    ),
                    "sol_handoffs_present": (
                        sum(case["sol_investigator"] is not None for case in cases)
                        if args.two_layer
                        else None
                    ),
                    "mandatory_host_evidence_references_complete": (
                        sum(
                            isinstance(case["mandatory_host_evidence"], dict)
                            and all(
                                case["mandatory_host_evidence"].get(field) is not None
                                for field in HOST_EVIDENCE_FIELDS
                            )
                            for case in cases
                        )
                        if args.two_layer
                        else None
                    ),
                }
            )
        )
        return
    report = await _execute(args, manifest, cases)
    print(
        json.dumps(
            {"summary": report["summary"], "results_file": str(args.results_file)}
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
