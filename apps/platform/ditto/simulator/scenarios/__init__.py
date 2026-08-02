"""Scenario registry with pkgutil auto-discovery.

A scenario is a module in this package that defines::

    NAME: str          # unique CLI name
    DESCRIPTION: str   # one line shown by --list
    async def apply(ctx: ScenarioContext) -> None: ...

Drop a new module in this directory and it is discovered automatically —
scenario authors never edit a shared registry file. ``apply`` receives a
:class:`ScenarioContext` and owns its sessions/transactions::

    async with ctx.session_maker() as session, session.begin():
        await ctx.fabric.finalized_agent(session, index=1, composite=0.74)
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.simulator.fabric import Fabric


@dataclass(frozen=True)
class ScenarioContext:
    """Everything a scenario's ``apply(ctx)`` needs, in one immutable bundle."""

    session_maker: async_sessionmaker[AsyncSession]
    """Factory for DB sessions; scenarios open their own transactions."""

    fabric: Fabric
    """Deterministic fabrication helpers (rng seeded by ``--seed``) plus the
    run's fixed ``now`` (``ctx.fabric.now``)."""


class ScenarioApply(Protocol):
    """The shape of a scenario module's ``apply`` coroutine."""

    async def __call__(self, ctx: ScenarioContext) -> None: ...


@dataclass(frozen=True)
class Scenario:
    """One discovered scenario: its CLI name, blurb, and apply coroutine."""

    name: str
    description: str
    apply: ScenarioApply


class ScenarioDiscoveryError(Exception):
    """A module in the scenarios package does not satisfy the contract."""


def discover_scenarios() -> dict[str, Scenario]:
    """Import every module in this package and collect its scenario contract.

    Returns scenarios keyed by ``NAME``, sorted for stable --list output.

    Raises:
        ScenarioDiscoveryError: A module is missing ``NAME``/``DESCRIPTION``/
            ``apply``, or two modules claim the same ``NAME``.
    """
    scenarios: dict[str, Scenario] = {}
    for module_info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        name = getattr(module, "NAME", None)
        description = getattr(module, "DESCRIPTION", None)
        apply_fn = getattr(module, "apply", None)
        if (
            not isinstance(name, str)
            or not isinstance(description, str)
            or not callable(apply_fn)
        ):
            raise ScenarioDiscoveryError(
                f"scenario module {module.__name__!r} must define NAME: str, "
                "DESCRIPTION: str, and async apply(ctx)"
            )
        if name in scenarios:
            raise ScenarioDiscoveryError(
                f"duplicate scenario NAME {name!r} in {module.__name__!r}"
            )
        scenarios[name] = Scenario(name=name, description=description, apply=apply_fn)
    return dict(sorted(scenarios.items()))
