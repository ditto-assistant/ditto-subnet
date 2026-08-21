"""Multi-select preview profiles and the illegal combinations.

Only the public dashboard may attach to production Platform. Backroom is an
authenticated operator control plane and therefore requires an isolated
``stack`` / ``stack-copy`` plan.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

PREVIEW_PROFILES = frozenset({"dashboard", "backroom", "stack", "stack-copy"})
_PROD_FRONTEND = frozenset({"dashboard"})


class CompositionError(ValueError):
    """The requested profile set cannot be deployed safely."""


@dataclass(frozen=True)
class PreviewPlan:
    """Resolved, legal preview.

    Attributes:
        profiles: Normalized selected profiles, including implications.
        dashboard: Serve the Platform SPA.
        backroom: Serve the Backroom worker.
        stack: Isolated Platform + one localnet validator.
        copy_database: Restore a snapshot into the preview Postgres.
        attach_prod_api: Frontends talk to production Platform (not a preview API).
        localnet_validator: Always true when ``stack`` is on.
    """

    profiles: frozenset[str]
    dashboard: bool
    backroom: bool
    stack: bool
    copy_database: bool
    attach_prod_api: bool
    localnet_validator: bool

    @property
    def isolated(self) -> bool:
        return self.stack


def compose(
    profiles: Iterable[str],
    *,
    attach_prod_api: bool = False,
) -> PreviewPlan:
    """Resolve a multi-select profile set.

    ``stack-copy`` implies ``stack``. ``stack`` implies one localnet validator
    and forbids attaching the preview Platform to production validators.
    The dashboard without ``stack`` may attach to production Platform.
    """

    selected = {item.strip().lower() for item in profiles if str(item).strip()}
    if not selected:
        raise CompositionError("select at least one profile")
    unknown = selected - PREVIEW_PROFILES
    if unknown:
        raise CompositionError("unknown profile(s): " + ", ".join(sorted(unknown)))

    copy_database = "stack-copy" in selected
    stack = copy_database or "stack" in selected
    if "backroom" in selected and not stack:
        raise CompositionError(
            "backroom is an authenticated write control plane and cannot attach "
            "to production Platform; select stack or stack-copy"
        )
    dashboard = "dashboard" in selected or stack
    backroom = "backroom" in selected or stack
    # Selecting only stack still gets dashboard+backroom URLs pointed at the
    # preview Platform so a full PR has somewhere to click.
    if stack:
        dashboard = True
        backroom = True

    if attach_prod_api and stack:
        raise CompositionError(
            "preview Platform cannot attach to production validators or "
            "issue leases into mainnet; use stack/stack-copy on localnet, "
            "or dashboard/backroom without stack against prod Platform"
        )
    if attach_prod_api and not (selected & _PROD_FRONTEND):
        raise CompositionError(
            "attach_prod_api is only valid with dashboard and/or backroom"
        )
    if attach_prod_api and selected - _PROD_FRONTEND:
        raise CompositionError(
            "attach_prod_api cannot combine with stack or stack-copy"
        )

    # Frontends-only default to prod API so a dashboard PR does not spin a
    # validator. Explicit attach_prod_api=False still talks to prod unless
    # stack is on — there is no preview Platform without stack.
    use_prod_api = (not stack) and (attach_prod_api or bool(selected & _PROD_FRONTEND))

    return PreviewPlan(
        profiles=frozenset(selected | ({"stack"} if copy_database else set())),
        dashboard=dashboard,
        backroom=backroom,
        stack=stack,
        copy_database=copy_database,
        attach_prod_api=use_prod_api,
        localnet_validator=stack,
    )
