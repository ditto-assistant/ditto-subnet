"""Backfill historical TAO/USD rates for legacy miner payments.

This deliberately uses a two-step workflow. ``--write-plan`` reads every
unpriced payment, fetches CoinGecko's historical TAO/USD series, and writes the
exact proposed mutations to an auditable JSON file without changing Postgres.
``--apply-plan`` performs no network requests and applies only that reviewed
file. Re-running an applied plan is safe; rows already carrying the planned
rate are left unchanged, while any conflicting rate fails the whole transaction.

Usage::

    uv run python scripts/backfill_payment_tao_usd_rates.py \
      --write-plan /tmp/tao-usd-backfill.json
    uv run python scripts/backfill_payment_tao_usd_rates.py \
      --apply-plan /tmp/tao-usd-backfill.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, tuple_, update

from ditto.db import create_db_engine, create_session_maker
from ditto.db.models import EvaluationPayment

logger = logging.getLogger(__name__)

_COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/bittensor/market_chart/range"
_MAX_POINT_DISTANCE = timedelta(hours=25)
_RATE_QUANTUM = Decimal("0.00000001")
_PLAN_VERSION = 1


@dataclass(frozen=True)
class Payment:
    block_hash: str
    extrinsic_index: int
    timestamp: datetime


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    rate: Decimal


@dataclass(frozen=True)
class PlannedUpdate:
    block_hash: str
    extrinsic_index: int
    payment_timestamp: str
    tao_usd_rate: str
    source_timestamp: str
    source_distance_seconds: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a price")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid price {value!r}") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"price must be finite and positive, got {value!r}")
    return parsed


def _parse_price_points(payload: object) -> list[PricePoint]:
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise ValueError("CoinGecko response has no prices array")
    points: list[PricePoint] = []
    for raw in payload["prices"]:
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError(f"invalid CoinGecko price point {raw!r}")
        timestamp_ms = raw[0]
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, (int, float)):
            raise ValueError(f"invalid CoinGecko timestamp {timestamp_ms!r}")
        if not math.isfinite(timestamp_ms):
            raise ValueError(f"non-finite CoinGecko timestamp {timestamp_ms!r}")
        points.append(
            PricePoint(
                timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                rate=_decimal(raw[1]),
            )
        )
    points.sort(key=lambda point: point.timestamp)
    if not points:
        raise ValueError("CoinGecko returned an empty prices array")
    if len({point.timestamp for point in points}) != len(points):
        raise ValueError("CoinGecko returned duplicate price timestamps")
    return points


def _build_updates(
    payments: list[Payment], price_points: list[PricePoint]
) -> list[PlannedUpdate]:
    timestamps = [point.timestamp for point in price_points]
    updates: list[PlannedUpdate] = []
    for payment in payments:
        paid_at = _utc(payment.timestamp)
        insertion = bisect_left(timestamps, paid_at)
        candidates = price_points[max(0, insertion - 1) : insertion + 1]
        if not candidates:
            raise ValueError(f"no historical price near payment {payment.block_hash}")
        point = min(
            candidates, key=lambda candidate: abs(candidate.timestamp - paid_at)
        )
        distance = abs(point.timestamp - paid_at)
        if distance > _MAX_POINT_DISTANCE:
            raise ValueError(
                f"nearest price for payment {payment.block_hash} is "
                f"{distance.total_seconds():.0f}s away"
            )
        rate = point.rate.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
        updates.append(
            PlannedUpdate(
                block_hash=payment.block_hash,
                extrinsic_index=payment.extrinsic_index,
                payment_timestamp=paid_at.isoformat(),
                tao_usd_rate=str(rate),
                source_timestamp=point.timestamp.isoformat(),
                source_distance_seconds=round(distance.total_seconds()),
            )
        )
    return updates


async def _fetch_price_points(
    client: httpx.AsyncClient, payments: list[Payment]
) -> tuple[list[PricePoint], dict[str, str]]:
    earliest = min(_utc(payment.timestamp) for payment in payments)
    latest = max(_utc(payment.timestamp) for payment in payments)
    params = {
        "vs_currency": "usd",
        "from": str(int((earliest - timedelta(days=1)).timestamp())),
        "to": str(int((latest + timedelta(days=1)).timestamp())),
        "precision": "full",
    }
    response = await client.get(_COINGECKO_URL, params=params, timeout=30)
    response.raise_for_status()
    return _parse_price_points(response.json()), params


async def _write_plan(path: Path) -> int:
    engine = create_db_engine()
    session_maker = create_session_maker(engine)
    try:
        async with session_maker() as session:
            rows = (
                await session.execute(
                    select(
                        EvaluationPayment.block_hash,
                        EvaluationPayment.extrinsic_index,
                        EvaluationPayment.timestamp,
                    )
                    .where(EvaluationPayment.tao_usd_rate.is_(None))
                    .order_by(
                        EvaluationPayment.timestamp,
                        EvaluationPayment.block_hash,
                        EvaluationPayment.extrinsic_index,
                    )
                )
            ).all()
        payments = [Payment(*row) for row in rows]
        if not payments:
            logger.info("all miner payments are already priced; no plan written")
            return 0
        async with httpx.AsyncClient(
            headers={"User-Agent": "Ditto-SN118-payment-price-backfill/1"}
        ) as client:
            points, params = await _fetch_price_points(client, payments)
        updates = _build_updates(payments, points)
        plan = {
            "version": _PLAN_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "source": _COINGECKO_URL,
            "source_query": params,
            "updates": [asdict(item) for item in updates],
        }
        path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        logger.info(
            "wrote dry-run plan path=%s payments=%d price_points=%d",
            path,
            len(updates),
            len(points),
        )
        return 0
    finally:
        await engine.dispose()


def _load_plan(path: Path) -> list[PlannedUpdate]:
    payload: Any = json.loads(path.read_text())
    if not isinstance(payload, dict) or payload.get("version") != _PLAN_VERSION:
        raise ValueError(f"unsupported backfill plan version in {path}")
    if payload.get("source") != _COINGECKO_URL:
        raise ValueError(f"unexpected price source in {path}")
    raw_updates = payload.get("updates")
    if not isinstance(raw_updates, list) or not raw_updates:
        raise ValueError(f"backfill plan {path} has no updates")
    updates: list[PlannedUpdate] = []
    seen: set[tuple[str, int]] = set()
    for raw in raw_updates:
        if not isinstance(raw, dict):
            raise ValueError("plan update must be an object")
        try:
            item = PlannedUpdate(**raw)
        except TypeError as error:
            raise ValueError(f"invalid plan update {raw!r}") from error
        key = (item.block_hash, item.extrinsic_index)
        if key in seen:
            raise ValueError(f"duplicate payment in plan: {key}")
        seen.add(key)
        _decimal(item.tao_usd_rate)
        if item.source_distance_seconds < 0 or (
            item.source_distance_seconds > _MAX_POINT_DISTANCE.total_seconds()
        ):
            raise ValueError(f"invalid source distance for payment {key}")
        datetime.fromisoformat(item.payment_timestamp)
        datetime.fromisoformat(item.source_timestamp)
        updates.append(item)
    return updates


async def _apply_plan(path: Path) -> int:
    planned = _load_plan(path)
    keys = [(item.block_hash, item.extrinsic_index) for item in planned]
    engine = create_db_engine()
    session_maker = create_session_maker(engine)
    updated = already_applied = 0
    try:
        async with session_maker() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        EvaluationPayment.block_hash,
                        EvaluationPayment.extrinsic_index,
                        EvaluationPayment.timestamp,
                        EvaluationPayment.tao_usd_rate,
                    )
                    .where(
                        tuple_(
                            EvaluationPayment.block_hash,
                            EvaluationPayment.extrinsic_index,
                        ).in_(keys)
                    )
                    .with_for_update()
                )
            ).all()
            current = {(row[0], row[1]): row for row in rows}
            if len(current) != len(planned):
                missing = sorted(set(keys) - current.keys())
                raise ValueError(
                    f"planned payments are missing from Postgres: {missing}"
                )
            for item in planned:
                key = (item.block_hash, item.extrinsic_index)
                row = current[key]
                rate = _decimal(item.tao_usd_rate).quantize(_RATE_QUANTUM)
                if _utc(row[2]).isoformat() != item.payment_timestamp:
                    raise ValueError(f"payment timestamp changed for {key}")
                if row[3] is not None:
                    if Decimal(row[3]) != rate:
                        raise ValueError(
                            f"payment already has a conflicting rate: {key}"
                        )
                    already_applied += 1
                    continue
                await session.execute(
                    update(EvaluationPayment)
                    .where(
                        EvaluationPayment.block_hash == item.block_hash,
                        EvaluationPayment.extrinsic_index == item.extrinsic_index,
                        EvaluationPayment.tao_usd_rate.is_(None),
                    )
                    .values(tao_usd_rate=rate)
                )
                updated += 1
        logger.info(
            "applied historical TAO/USD plan path=%s updated=%d already_applied=%d",
            path,
            updated,
            already_applied,
        )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write-plan",
        type=Path,
        metavar="PATH",
        help="write a reviewed dry-run plan; Postgres remains unchanged",
    )
    mode.add_argument(
        "--apply-plan",
        type=Path,
        metavar="PATH",
        help="apply an existing plan without making provider requests",
    )
    args = parser.parse_args()
    try:
        if args.write_plan is not None:
            return asyncio.run(_write_plan(args.write_plan))
        return asyncio.run(_apply_plan(args.apply_plan))
    except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError) as error:
        logger.error("TAO/USD backfill failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
