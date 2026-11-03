"""No automation can start a bounty payment (#2943).

Money moves only when an operator dispatches an approved round. These tests
pin the three things that keep it so: the app wires only the simulated fee
verifier and transfer executor, the platform-role deadline sweeper never
creates a transfer, and nothing but the admin dispatch endpoint reaches the
dispatch or the executor. A background caller added later fails here.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select

from ditto.api_server import create_api_server
from ditto.api_server.bounty_claims import SimulatedBountyFeeVerifier
from ditto.api_server.bounty_deadlines import BountyDeadlineSweeper
from ditto.api_server.bounty_settlement import SimulatedTransferExecutor
from ditto.api_server.contributor_identity import admin_actor
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import BountyTransfer
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.endpoints.test_bounty_claims import ALICE, BOB, World
from ditto.tests.api_server.endpoints.test_bounty_reviews import claimed
from ditto.tests.api_server.endpoints.test_bounty_settlement import (
    accepted,
    approve,
    claim_of,
    obligation,
    policy,
)

PLATFORM = Path(__file__).resolve().parents[4]
DISPATCH_ROUTE = "/api/v1/admin/bounties/obligations/{obligation_id}/dispatch"
RECONCILE_ROUTE = "/api/v1/admin/bounties/transfers/{transfer_id}/reconcile"


def test_the_app_wires_only_simulated_bounty_money():
    app = create_api_server(make_api_server_config())
    assert type(app.state.bounty_fee_verifier) is SimulatedBountyFeeVerifier
    executor = app.state.bounty_transfer_executor
    assert type(executor) is SimulatedTransferExecutor
    assert (executor.name, executor.simulated) == ("simulated", True)


@pytest.mark.asyncio
async def test_the_deadline_sweeper_never_dispatches(app, client, session_maker):
    world = World(app, client, session_maker)
    await world.start(ALICE, BOB)
    won = await accepted(world, ALICE)
    late = await claimed(world, BOB)
    assert (await policy(world)).status_code == 200
    approved = await approve(world, obligation(won, "fee_refund"))
    assert approved.json()["state"] == "approved", approved.text
    sweeper = BountyDeadlineSweeper(session_maker=session_maker)
    # The sweep settles this bounty (Bob's missed deadline) and nothing else.
    assert await sweeper.sweep(now=datetime.now(UTC) + timedelta(days=30)) == 1
    assert (await claim_of(world, late))["slot_status"] == "expired"
    async with session_maker() as session:
        transfers = await session.scalar(
            select(func.count()).select_from(BountyTransfer)
        )
    assert transfers == 0
    assert obligation(await claim_of(world, won), "fee_refund")["state"] == "approved"


def _scopes(node: ast.AST, name: str, scope: str = "<module>") -> list[str]:
    """The functions under ``node`` that name ``name``, once per mention."""
    found: list[str] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name == name:
            found.append(f"def {name}")
        scope = node.name
    if (
        (isinstance(node, ast.Name) and node.id == name)
        or (isinstance(node, ast.Attribute) and node.attr == name)
        or (isinstance(node, ast.alias) and name in (node.name, node.asname))
        or (isinstance(node, ast.Constant) and node.value == name)
    ):
        found.append(scope)
    for child in ast.iter_child_nodes(node):
        found.extend(_scopes(child, name, scope))
    return found


def references(name: str) -> dict[str, list[str]]:
    """Every Platform module naming ``name``, with the functions that name it.

    Tests are left out. A definition, an import, an attribute, a bare name and
    a string spelling the name all count, so a getattr or a scheduled call by
    name is caught too.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(PLATFORM.joinpath("ditto").rglob("*.py")) + sorted(
        PLATFORM.joinpath("scripts").rglob("*.py")
    ):
        module = path.relative_to(PLATFORM).as_posix()
        if module.startswith("ditto/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if scopes := sorted(set(_scopes(tree, name))):
            found[module] = scopes
    return found


def admin_route(endpoint: str) -> APIRoute:
    """The one route served by the bounty endpoint function ``endpoint``."""
    [route] = [
        route
        for route in create_api_server(make_api_server_config()).routes
        if isinstance(route, APIRoute)
        and route.endpoint.__module__ == "ditto.api_server.endpoints.bounties"
        and route.endpoint.__name__ == endpoint
    ]
    # An operator write: the admin token and a named actor.
    called = {dependency.call for dependency in route.dependant.dependencies}
    assert {require_admin, admin_actor} <= called, endpoint
    return route


def test_only_the_admin_dispatch_endpoint_dispatches():
    assert references("dispatch_obligation") == {
        "ditto/api_server/bounty_settlement.py": ["def dispatch_obligation"],
        "ditto/api_server/endpoints/bounties.py": ["dispatch"],
    }
    route = admin_route("dispatch")
    assert (route.path, route.methods) == (DISPATCH_ROUTE, {"POST"})


def test_only_the_dispatch_and_reconcile_endpoints_reach_the_executor():
    # The app wires it; only an operator's dispatch or reconcile uses it.
    assert references("bounty_transfer_executor") == {
        "ditto/api_server/endpoints/bounties.py": ["dispatch", "reconcile"],
        "ditto/api_server/factory.py": ["create_api_server"],
    }
    assert admin_route("reconcile").path == RECONCILE_ROUTE
