"""Bounded redaction for miner-owned screening failure feedback.

The text originates in untrusted build or runtime output.  It is only exposed
to the submission owner, but still must not carry a credential emitted by a
tool or provider into durable storage.
"""

from __future__ import annotations

import re

PRIVATE_FAILURE_LOG_TAIL_LIMIT = 16_000
PRIVATE_FAILURE_DETAIL_LIMIT = 4_000

_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SIGNED_TOKEN = re.compile(r"\bsvt_[A-Za-z0-9._-]+")


def private_failure_text(
    value: str, *, limit: int = PRIVATE_FAILURE_LOG_TAIL_LIMIT
) -> str:
    """Redact and tail-bound diagnostic text before durable owner feedback."""
    if limit < 1:
        raise ValueError("private failure limit must be positive")
    # PostgreSQL text rejects U+0000. Provider and Docker output can contain it
    # after lossy decoding, so make it visible without letting it poison storage.
    redacted = value.replace("\x00", r"\0")
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _SIGNED_TOKEN.sub("[REDACTED]", redacted)
    redacted = _SECRET.sub(r"\1\2[REDACTED]", redacted)
    if len(redacted) <= limit:
        return redacted
    prefix = "[truncated]\n"
    if limit <= len(prefix):
        return prefix[:limit]
    return prefix + redacted[-(limit - len(prefix)) :]
