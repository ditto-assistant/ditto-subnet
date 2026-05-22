"""``GET /metrics`` - Prometheus exposition endpoint.

Empty registry at MVP: ``prometheus_client.generate_latest()`` still emits
process collectors (``process_cpu_seconds_total`` etc.) which is enough
to validate scrape configuration. Per-feature counters land with the
endpoints they cover.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["ops"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Return Prometheus exposition for the default registry.

    ``generate_latest`` is synchronous but completes in microseconds for
    this cardinality - no ``asyncio.to_thread`` wrap required at MVP.
    Revisit when histograms with high-cardinality labels appear.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
