"""edge/v1 endpoint paths on Platform, relative to the API origin.

A node signs the exact path it requests, so both node implementations and
Platform take paths from here rather than spelling them out.
"""

from __future__ import annotations

from typing import Final

PREFIX: Final = "/api/v1/edge"

ENROLL: Final = f"{PREFIX}/nodes"
STATUS: Final = f"{PREFIX}/nodes/me"
LEAVE: Final = f"{PREFIX}/nodes/me/leave"
POLL: Final = f"{PREFIX}/poll"
ACK: Final = f"{PREFIX}/ack"


def object_path(digest: str) -> str:
    return f"{PREFIX}/objects/{digest}"


def answer_path(challenge_id: str) -> str:
    return f"{PREFIX}/challenges/{challenge_id}"
