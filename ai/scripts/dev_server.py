"""LOCAL TESTING ONLY. A tiny FastAPI app that mounts the agent router with a fake box state.
CS 1's real app (box-service) replaces this; it is not part of the product.

Run from the repo root:
    python -m uvicorn ai.scripts.dev_server:app --port 8000
    $env:DEV_STATE = "storm"       # optional, before starting: calm|storm|flood|eye|outage|replay
"""
import copy
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))    # so "import ai.agent" works
sys.path.insert(0, str(Path(__file__).resolve().parent))        # so we can reuse ask.py's fake states

from fastapi import FastAPI

from ai.agent import agent, tools
from ai.agent.api import router
from ask import make_state                                      # the same fake boxes as ask.py


@asynccontextmanager
async def lifespan(app):
    state = make_state(os.environ.get("DEV_STATE", "calm"))
    tools.set_provider(lambda: copy.deepcopy(state))            # the agent reads this fake box
    agent.warm_up()                                             # load models once, before request 1
    yield


app = FastAPI(title="StormBox agent (dev)", lifespan=lifespan)
app.include_router(router)
