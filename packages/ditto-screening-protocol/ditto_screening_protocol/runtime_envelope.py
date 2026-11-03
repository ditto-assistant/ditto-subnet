"""The published SN118 harness runtime envelope.

One machine-readable file, ``data/runtime_envelope.json``, holds every limit a
miner's harness runs inside: memory, CPU, PIDs, tmpfs, deadlines, concurrency,
image size and the inference walls. The scorer (a byte-identical Go copy with a
drift test), the screener and ``ditto practice`` read it, and
``docs/RUNTIME-ENVELOPE.md`` is its prose.

Two values are live Platform settings rather than release constants: the
harness memory cap and the case concurrency. The envelope carries the published
value; Platform stamps the live one on every lease and screening claim, and the
consumers prefer the stamped value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ENVELOPE_PATH = Path(__file__).parent / "data" / "runtime_envelope.json"
ENVELOPE_SCHEMA = "ditto.sn118.runtime_envelope.v1"
MIB = 1024 * 1024


@dataclass(frozen=True)
class RuntimeEnvelope:
    memory_limit_mib: int
    swap_mib: int
    cpus: int
    pids_limit: int
    tmpfs_mib: int
    nofile: int
    container_start_seconds: int
    healthy_within_seconds: int
    seed_call_seconds: int
    case_call_seconds: int
    run_wall_seconds: int
    lease_ttl_minutes: int
    case_concurrency: int
    screened_image_bytes: int
    source_bytes: int
    chat_request_budget: int
    chat_token_budget: int
    max_tokens_per_request: int
    oom_peak_tolerance_mib: int
    oom_host_available_floor_mib: int
    host_memory_reserve_mib: int
    single_slot_headroom_mib: int

    def memory_admitted_slots(
        self, host_memory_mib: int, sandbox_memory_limit_mib: int
    ) -> int:
        """How many harness caps fit in this host's allocatable memory.

        The sum of concurrent harness caps must not exceed host memory less
        :attr:`host_memory_reserve_mib`, or two in-cap harnesses can exhaust the
        host together and the kernel kills one of them for a fault it did not
        commit. A host with at least one cap plus
        :attr:`single_slot_headroom_mib` always keeps one slot.
        """
        return admitted_slots(
            host_memory_mib,
            sandbox_memory_limit_mib,
            reserve_mib=self.host_memory_reserve_mib,
            single_slot_headroom_mib=self.single_slot_headroom_mib,
        )


def admitted_slots(
    host_memory_mib: int,
    sandbox_memory_limit_mib: int,
    *,
    reserve_mib: int,
    single_slot_headroom_mib: int,
) -> int:
    """``(host - reserve) // cap``, floored at 1 when ``host >= cap + headroom``.

    The floor keeps every host that can physically back one harness in the
    canonical quorum. An OOM there is still classified fairly
    (``host_memory_pressure`` is no-fault), so one slot is always safe.
    """
    if sandbox_memory_limit_mib <= 0:
        return 0
    slots = max(0, (host_memory_mib - reserve_mib) // sandbox_memory_limit_mib)
    if slots == 0 and (
        host_memory_mib >= sandbox_memory_limit_mib + single_slot_headroom_mib
    ):
        return 1
    return slots


@cache
def runtime_envelope() -> RuntimeEnvelope:
    raw = json.loads(ENVELOPE_PATH.read_text(encoding="utf-8"))
    if raw.get("schema") != ENVELOPE_SCHEMA:
        raise ValueError(f"unexpected runtime envelope schema {raw.get('schema')!r}")
    harness, timing = raw["harness"], raw["timing"]
    inference, oom = raw["inference"], raw["oom"]
    return RuntimeEnvelope(
        memory_limit_mib=int(harness["memory_limit_mib"]),
        swap_mib=int(harness["swap_mib"]),
        cpus=int(harness["cpus"]),
        pids_limit=int(harness["pids_limit"]),
        tmpfs_mib=int(harness["tmpfs_mib"]),
        nofile=int(harness["nofile"]),
        container_start_seconds=int(timing["container_start_seconds"]),
        healthy_within_seconds=int(timing["healthy_within_seconds"]),
        seed_call_seconds=int(timing["seed_call_seconds"]),
        case_call_seconds=int(timing["case_call_seconds"]),
        run_wall_seconds=int(timing["run_wall_seconds"]),
        lease_ttl_minutes=int(timing["lease_ttl_minutes"]),
        case_concurrency=int(raw["concurrency"]["case_concurrency"]),
        screened_image_bytes=int(raw["image"]["screened_image_bytes"]),
        source_bytes=int(raw["image"]["source_bytes"]),
        chat_request_budget=int(inference["chat_request_budget"]),
        chat_token_budget=int(inference["chat_token_budget"]),
        max_tokens_per_request=int(inference["max_tokens_per_request"]),
        oom_peak_tolerance_mib=int(oom["peak_tolerance_mib"]),
        oom_host_available_floor_mib=int(oom["host_available_floor_mib"]),
        host_memory_reserve_mib=int(raw["host"]["memory_reserve_mib"]),
        single_slot_headroom_mib=int(raw["host"]["single_slot_headroom_mib"]),
    )
