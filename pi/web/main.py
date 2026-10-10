"""
StormBox FastAPI web app.

Configuration (environment variables):
  STORMBOX_FAKE=1          — use in-process FakeESP32
  STORMBOX_SERIAL=<port>   — use real serial (e.g. /dev/ttyUSB0 or COM3)

Start via:
  python pi/run.py --fake
  python pi/run.py --serial /dev/ttyUSB0
"""
import asyncio
import json
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Set

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure the repo root is on sys.path so `pi.*` and `ai.*` imports work when
# the file is loaded by uvicorn from any working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pi.box_service.state import BoxState
from pi.box_service.service import BoxService, SerialSource

from ai.agent.api import router as agent_router
from ai.agent import tools as agent_tools
from ai.agent import agent as agent_module

STATIC_DIR = Path(__file__).parent / "static"
_DB_PATH   = _REPO_ROOT / "data" / "box.db"


def _ensure_db():
    """Create data/box.db and the agent_traces table if they don't exist."""
    _DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS agent_traces (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT,
            channel      TEXT,
            question     TEXT,
            route        TEXT,
            tools_json   TEXT,
            chunks_json  TEXT,
            sensors_json TEXT,
            answer       TEXT,
            timings_json TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alarms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alarm_id    TEXT,
            level       TEXT,
            since       TEXT,
            explanation TEXT
        )
    """)
    con.commit()
    con.close()


# ------------------------------------------------------------------ globals

# Set in lifespan, read by route handlers.
_state:     BoxState | None  = None
_service:   BoxService | None = None
_fake:      object | None     = None   # FakeESP32 instance, fake mode only
_ws_clients: Set[WebSocket]   = set()


# ------------------------------------------------------------------ WS broadcast

async def _broadcast(snapshot: dict):
    """Push the full state JSON to all connected WebSocket clients."""
    text = json.dumps(snapshot)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ------------------------------------------------------------------ lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _state, _service, _fake

    dev_mode    = os.environ.get("STORMBOX_FAKE")   == "1"
    serial_port = os.environ.get("STORMBOX_SERIAL")

    if not dev_mode and not serial_port:
        raise RuntimeError(
            "Set STORMBOX_FAKE=1 or STORMBOX_SERIAL=<port> before starting."
        )

    _ensure_db()
    _state = BoxState(dev=dev_mode)

    # Give the agent its live state getter
    agent_tools.set_provider(lambda: _state.to_dict())

    if dev_mode:
        from pi.box_service.fake import FakeESP32
        _fake    = FakeESP32()
        source   = _fake
        fake_task = asyncio.create_task(_fake.run())
    else:
        source    = SerialSource(serial_port)
        fake_task = None

    _service = BoxService(_state, source, dev=dev_mode)
    _service.register_on_change(_broadcast)

    service_task = asyncio.create_task(_service.start())

    # Warm up the agent (loads embedding model + LLM) in a background thread
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, agent_module.warm_up)

    print(f"[web] StormBox ready  dev={dev_mode}  port=8000")

    yield   # app runs here

    _service.stop()
    service_task.cancel()
    if _fake:
        _fake.stop()
        fake_task.cancel()


# ------------------------------------------------------------------ app

app = FastAPI(lifespan=lifespan)
app.include_router(agent_router)


# ------------------------------------------------------------------ WebSocket

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        # Send the current state immediately on connect
        await ws.send_text(json.dumps(_state.to_dict()))
        # Keep the connection alive; client drives the close
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ------------------------------------------------------------------ REST API

@app.get("/api/state")
async def get_state():
    return _state.to_dict()


class AckBody(BaseModel):
    id: str

@app.post("/api/alarms/ack")
async def post_ack(body: AckBody):
    await _service.ack_alarm(body.id)
    return {"ok": True}


class TriggerBody(BaseModel):
    event: str

_VALID_EVENTS = {"flood", "wind", "storm", "heat", "power", "short", "long", "clear"}

@app.post("/api/dev/trigger")
async def post_trigger(body: TriggerBody):
    if not (_state and _state.dev):
        return JSONResponse({"error": "only available in --fake mode"}, status_code=403)
    if body.event not in _VALID_EVENTS:
        return JSONResponse(
            {"error": f"unknown event; valid: {sorted(_VALID_EVENTS)}"},
            status_code=400,
        )
    _fake.trigger(body.event)
    return {"ok": True, "event": body.event}


# ------------------------------------------------------------------ static files
# Mount LAST so API routes are matched first.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
