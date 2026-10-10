"""FastAPI router for the agent (PROJECT_PLAN section 8). CS 1 owns the main app and includes this:

    from ai.agent.api import router as agent_router
    app.include_router(agent_router)

We only make a router, never the app. The endpoints are plain `def` (not `async def`) on purpose:
FastAPI runs them in a worker thread, so a slow LLM call does not freeze the whole server.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ai.agent import agent, trace

router = APIRouter()


class AskRequest(BaseModel):
    text: str
    channel: Literal["chat", "voice"] = "chat"      # anything else -> automatic 422 error


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    trace_id: int
    route: str          # tool | canned | rag | no_match (extra field next to the section 8 three)


@router.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    r = agent.answer(text, req.channel)
    return {k: r[k] for k in ("answer", "sources", "trace_id", "route")}


@router.get("/api/traces")
def traces(limit: int = Query(20, ge=1, le=50)):
    """Newest agent traces first (the agent keeps the last 50 in memory)."""
    return trace.recent(limit)
