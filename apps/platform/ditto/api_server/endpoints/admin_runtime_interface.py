"""Operator view of a Subtensor runtime upgrade; reads chain metadata only."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ditto.api_models.runtime_interface import RuntimeInterfaceReport
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.chain.errors import ChainError
from ditto.chain.runtime_interface_report import BlockNotFinalized

router = APIRouter(prefix="/admin/runtime-interface", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("", response_model=RuntimeInterfaceReport)
async def get_runtime_interface_report(
    request: Request,
    response: Response,
    _admin: AdminDep,
    block: Annotated[int | None, Query(ge=1)] = None,
) -> RuntimeInterfaceReport:
    """Whether readers admit the runtime at ``block`` (default finalized head).

    Reports, for source attribution and treasury receipt reading, how the
    runtime was admitted or which consumed entries moved since the nearest
    audited runtime, and for the treasury signer which calls changed since the
    runtime its cold-signed policy binds. Changes nothing; not an approval.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        report = await request.app.state.chain.get_runtime_interface_report(block)
    except BlockNotFinalized as error:
        raise HTTPException(422, str(error)[:300]) from None
    except ValueError:
        # Unreadable chain metadata is an upstream fault, not a bad request.
        raise HTTPException(503, "runtime metadata unreadable") from None
    except (ChainError, TimeoutError):
        raise HTTPException(503, "runtime metadata unavailable") from None
    return RuntimeInterfaceReport.model_validate(asdict(report))
