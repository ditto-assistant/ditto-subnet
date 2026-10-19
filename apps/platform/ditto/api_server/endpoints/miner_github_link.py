from fastapi import APIRouter, Depends, Request
from ditto.api_server.endpoints.miner_auth import resolve_miner_session
from ditto.api_server.dependencies import get_session

router = APIRouter(tags=["miner-github-link"])

@router.get("/me/github-link")
async def get_github_link(request: Request, session: Depends(get_session)):
    row, _ = await resolve_miner_session(request, session)
    # Logic to fetch active GitHub link from DB
    return {"link": None}