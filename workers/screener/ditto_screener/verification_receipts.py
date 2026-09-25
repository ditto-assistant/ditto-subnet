"""Digest-only mechanical verification receipts for the active v13 attempt.

These hashes attest only that the trusted gate observed the named mechanical
step. They do not attest policy completion or private metamorphic verification.
"""

from __future__ import annotations

from ditto_screening_protocol.mechanical_verification import mechanical_evidence_sha256

__all__ = ["mechanical_evidence_sha256"]
