"""Shadow router-track source screening for one submission.

The single production seam that turns the pure, ``ditto``-free
:func:`screen_router_submission` entry point into a signed, content-addressed
evidence record the screener can emit. It is **shadow-only and verdict-neutral**:
the evidence is ``weight_eligible=False`` by construction, carries no task text or
held-out identities (only digests), and is produced additively next to the memory
verdict — it can never deny a submission or change its signed screening result.

``sample is None`` is the opt-in / yes-and default: a submission that advertised
no router project (``/router/health`` -> ``unsupported``), or one for which no
paired held-out arm has been produced yet, maps to the benign ``INFRASTRUCTURE``
outcome with no findings. When a held-out arm producer feeds a real
:class:`RouterGeneralizationSample`, the same call grades it with no change here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ditto_screener.signing import sign_router_source_screen
from ditto_screening_protocol import (
    RouterSourceScreenEvidence,
    screen_router_submission,
)

if TYPE_CHECKING:
    from ditto_screening_protocol import RouterGeneralizationSample

# A rule-slug (``^[a-z][a-z0-9-]{0,63}$``) naming the shadow router analyzer, so
# ``analyzer_version`` validates against the evidence contract. It is distinct
# from the screener's dotted package ``__version__`` (which the pattern rejects)
# and is bumped when the router generalization rules change.
ROUTER_SOURCE_ANALYZER_VERSION = "router-shadow-v1"


def build_signed_router_source_screen(
    *,
    keypair: Any,
    screener_hotkey: str,
    agent_artifact_sha256: str,
    screened_image_sha256: str,
    policy_version: int,
    sample: RouterGeneralizationSample | None,
    analyzer_version: str = ROUTER_SOURCE_ANALYZER_VERSION,
) -> tuple[RouterSourceScreenEvidence, str]:
    """Screen one submission's router dimension and sign the result.

    Pure orchestration plus one signature: no network, no scoring. Returns the
    canonical evidence and its hex sr25519 signature bound to ``screener_hotkey``.
    ``sample is None`` yields the benign ``INFRASTRUCTURE`` evidence (no findings,
    never a deny); an included sample is graded by ``screen_router_submission``.
    """
    evidence = screen_router_submission(
        agent_artifact_sha256=agent_artifact_sha256,
        screened_image_sha256=screened_image_sha256,
        analyzer_version=analyzer_version,
        policy_version=policy_version,
        sample=sample,
    )
    signature = sign_router_source_screen(
        keypair, screener_hotkey=screener_hotkey, evidence=evidence
    )
    return evidence, signature
