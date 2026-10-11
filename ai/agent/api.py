"""FastAPI router for the agent (PROJECT_PLAN section 8). CS 1 owns the main app and includes this:

    from ai.agent.api import router as agent_router
    app.include_router(agent_router)

We only make a router, never the app. The endpoints are plain `def` (not `async def`) on purpose:
FastAPI runs them in a worker thread, so a slow LLM call does not freeze the whole server.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ai.agent import agent, trace

router = APIRouter()


class AskRequest(BaseModel):
    text: str
    channel: Literal["chat", "voice"] = "chat"      # anything else -> automatic 422 error
    stt_ms: float | None = Field(None, ge=0)         # voice only: speech-to-text time, saved in the trace


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
    if req.stt_ms is None:
        r = agent.answer(text, req.channel)
    else:                                            # spoken question: keep the speech-to-text time in the trace
        r = agent.answer(text, req.channel, {"stt": req.stt_ms})
    return {k: r[k] for k in ("answer", "sources", "trace_id", "route")}


@router.get("/api/traces")
def traces(limit: int = Query(20, ge=1, le=50)):
    """Newest agent traces first (the agent keeps the last 50 in memory)."""
    return trace.recent(limit)
