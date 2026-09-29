"""Verify a fixture action originated in an authenticated Backroom session.

The admin bearer authenticates Backroom as a service, not an individual actor.
Fixture registration and independent review require this separate, short-lived
body-bound HMAC; ordinary X-Admin-Actor strings have no fixture authority.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time

from fastapi import HTTPException, Request


async def require_operator_proof(request: Request) -> str:
    secret = os.environ.get("BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "fixture operator proof unavailable")
    actor = request.headers.get("X-Admin-Actor", "")
    proof = request.headers.get("X-Backroom-Operator-Proof", "")
    if not actor or actor != actor.strip().lower() or len(actor) > 320:
        raise HTTPException(403, "invalid fixture operator proof")
    match = re.fullmatch(r"([0-9]{10}):([0-9a-f]{64})", proof)
    if match is None or abs(int(match.group(1)) - int(time.time())) > 300:
        raise HTTPException(403, "invalid fixture operator proof")
    body_digest = hashlib.sha256(await request.body()).hexdigest()
    message = "\n".join(
        (match.group(1), actor, request.method, request.url.path, body_digest)
    )
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match.group(2)):
        raise HTTPException(403, "invalid fixture operator proof")
    return actor
