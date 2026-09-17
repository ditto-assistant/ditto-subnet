"""Additive handlers mounted inside Pylon's existing identity guard."""

# Assigned as Controller methods by the exact-source adapter.
# ruff: noqa: ARG001
from __future__ import annotations

from typing import Any

from ditto_pylon_receipts import (
    acknowledge_request,
    create_request,
    get_request,
    list_requests,
)
from litestar import get, post, put


@put(
    "/ditto/weight-receipts/{request_id:str}",
    status_code=200,
    request_max_body_size=1024 * 1024,
)
async def put_weight_receipt(
    self: Any,
    unstable_weight_service: Any,
    netuid: int,
    request_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    return await create_request(unstable_weight_service, netuid, request_id, data)


@get("/ditto/weight-receipts/{request_id:str}")
async def get_weight_receipt(
    self: Any, unstable_weight_service: Any, netuid: int, request_id: str
) -> dict[str, Any]:
    return await get_request(unstable_weight_service, netuid, request_id)


@get("/ditto/weight-receipts")
async def list_weight_receipts(
    self: Any,
    unstable_weight_service: Any,
    netuid: int,
    after_task_id: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    return await list_requests(unstable_weight_service, netuid, after_task_id, limit)


@post(
    "/ditto/weight-receipts/{request_id:str}/ack",
    status_code=200,
    request_max_body_size=4096,
)
async def acknowledge_weight_receipt(
    self: Any,
    unstable_weight_service: Any,
    netuid: int,
    request_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    return await acknowledge_request(unstable_weight_service, netuid, request_id, data)
