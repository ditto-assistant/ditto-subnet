"""Deterministic identity for the validator code installed in this process."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ditto import __version__

# v15 adds ``capabilities.scorer_benchmarks.probe``: the evidence behind the
# scorer status rather than the conclusion drawn from it. The signing bytes are
# unchanged (the v11 domain still applies and the field is omitted when absent),
# so the bump exists so the platform can tell a validator that cannot report
# probe evidence from one that reported none. The platform must ship v15
# awareness before validators send it: platforms predating forward-compatible
# JSON parsing reject the extra field before signature verification.
#
# v16 reports a slot from the moment it is claimed, with ``progress`` null until
# there is something to say. Through v15 the worker omitted such a slot from
# ``benchmark_capacity.active`` entirely, so the platform could not tell a slot
# that was seeding from a slot that was free -- and revoked live leases on the
# difference. The signing bytes are unchanged (``progress: null`` serializes
# canonically under the same v11 domain), so the bump exists purely so the
# platform can tell "this validator cannot report a bare claim" from "this
# validator reports none". Same ordering rule as v15, and it matters more here:
# a platform that has not shipped v16 awareness rejects the payload during
# request parsing, never refreshes ``seen_at``, and force-expires the very lease
# this is meant to protect.
# v17 reads the platform's authoritative lease roster off the heartbeat
# *response* and cancels any run whose lease it no longer lists. Nothing about
# the request changes -- the signing bytes are untouched again -- so the bump
# exists purely to negotiate: the platform sends a roster only to a reporter
# that declared 17, which is what keeps a v15 or v16 build on the same fleet
# behaving exactly as it does today. The ordering rule inverts relative to v15
# and v16: because the new field is on the response and the response model does
# not forbid extras, a v17 validator against a pre-v17 platform simply parses
# ``leases`` as absent -- "not answered" -- and cancels nothing. So this side
# can ship first, and the platform half can land whenever it is ready.
#
# v18 removed the validator-local inference sidecars and reports the resulting
# four-component stack identity.  The heartbeat signing domain remains v11.
#
# v19 consumes the optional ``LedgerEntry.efficiency_factor`` field: a bounded
# Bench-v9 multiplier in [0.85, 1.10]. Downside multiplies quality; upside
# closes only ``factor - 1`` of the remaining headroom to 1.0, so imperfect
# quality cannot collapse into a 1.0 tie plateau. The heartbeat request and
# signing bytes are unchanged; this is a capability-negotiation bump. Platform
# must not expose the factor until every recently-live weight-setting validator
# reports v19+, regardless of scorer capability, because an older validator
# still consumes the ledger and would otherwise disagree on KOTH and weights.
# Historical ``efficiency_bonus`` behavior is unchanged.
#
# v20 consumes ``LedgerResponse.tie_weighting_mode=pool`` and averages the
# frozen rank shares occupied by statistically tied recipients. The heartbeat
# signing bytes remain on the v11 domain. Platform must not expose the marker
# until every recently-live weight setter reports v20+, because a v19 validator
# ignores the additive field and would keep submitting the fixed rank vector.
#
# v21 changes the already-additive ``efficiency_factor`` interpretation from a
# continuous quality blend to a lexicographic order: authoritative quality is
# primary, and the adjusted efficiency projection breaks only exact quality
# ties. Platform withholds factors until every recently-live weight setter is
# v21+, preventing v19/v20 validators from applying the superseded ordering.
#
# v22 signs an independent ``confirmation_progress`` list for the bounded
# ``longmem-*`` slots. It is deliberately separate from benchmark capacity:
# LongMemEval and ablation work cannot consume or fabricate ordinary slots.
# Platform must understand v22 before validators emit it because the request
# model forbids unknown fields. The new v22 signing domain binds the complete
# privacy-safe list; v1-v21 signing bytes remain unchanged.
#
# v23 adds a signed, privacy-safe managed-updater status object. The validator
# reads only a read-only updater state mount; it never receives Docker/systemd
# access, W&B credentials, host paths, logs, environment values, or arbitrary
# error strings. The signing domain advances to v23 because the request gains a
# new signed token. Platform must ship the matching parser before validators.
#
# v24 consumes ``LedgerResponse.dethrone_band_mode=headroom_capped`` and caps the
# KOTH indifference band at ``KOTH_CEILING_HEADROOM_SHARE`` of the score the
# challenger can still gain. The heartbeat request and signing bytes are
# unchanged; this is a capability-negotiation bump. Platform must not expose the
# marker until every recently-live weight setter reports v24+, because a v23
# validator ignores the additive field and would keep defending the crown with
# the uncapped band -- a different champion, and so a different weight vector.
HEARTBEAT_PROTOCOL_VERSION = 24


@dataclass(frozen=True)
class ValidatorBuildInfo:
    software_version: str
    protocol_version: int
    code_digest: str


def source_digest(package_root: Path | None = None) -> str:
    """Hash installed Python paths and bytes in stable lexical order."""
    root = package_root or Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def validator_build_info() -> ValidatorBuildInfo:
    """Return the immutable identity reported for this running installation."""
    return ValidatorBuildInfo(
        software_version=__version__,
        protocol_version=HEARTBEAT_PROTOCOL_VERSION,
        code_digest=source_digest(),
    )
