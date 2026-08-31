"""Process-global snapshot of the effective required screening-policy version.

The version the screening queue requires is dynamic (a scheduled activation
raises it), but query builders are synchronous and cannot resolve it from the
database. The resolver in ``ditto.api_server.screener_policy_activation``
refreshes this snapshot whenever it loads the governing activation, so request
paths read a value at most one resolver TTL (5s) stale — the same staleness the
queue-policy settings cache already accepts.

Default is the floor version: with no resolver running (unit tests, scripts,
early startup) the platform requires the floor, matching the no-schedule
production state.
"""

from __future__ import annotations

from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

# Simple assignment is fine: swapping an int/bool is atomic under the GIL, so
# concurrent readers observe either the old or the new value, never a torn one.
_required_policy_version: int = SCREENING_FLOOR_POLICY_VERSION
_rescreen_scored: bool = False
_scored_rescreen_policy_version: int | None = None
_scored_rescreen_activation_revision: int | None = None


def update_effective_screener_policy(
    version: int,
    *,
    rescreen_scored: bool,
    scored_rescreen_policy_version: int | None = None,
    scored_rescreen_activation_revision: int | None = None,
) -> None:
    """Publish the resolver's computed policy to synchronous readers."""
    global _required_policy_version
    global _rescreen_scored
    global _scored_rescreen_policy_version
    global _scored_rescreen_activation_revision
    _required_policy_version = version
    _rescreen_scored = rescreen_scored
    _scored_rescreen_policy_version = (
        scored_rescreen_policy_version
        if scored_rescreen_policy_version is not None
        else version
        if rescreen_scored and version > SCREENING_FLOOR_POLICY_VERSION
        else None
    )
    _scored_rescreen_activation_revision = scored_rescreen_activation_revision


def effective_screening_policy_version() -> int:
    """The screening-policy version the platform currently REQUIRES."""
    return _required_policy_version


def effective_rescreen_scored() -> bool:
    """Whether stale scored/live rows may use an explicit rollout release."""
    return _rescreen_scored


def effective_scored_rescreen_policy_version() -> int | None:
    """The target version for the one explicit scored-policy release."""
    return _scored_rescreen_policy_version


def effective_scored_rescreen_activation_revision() -> int | None:
    """The due schedule revision that owns an explicit scored-policy release."""
    return _scored_rescreen_activation_revision
