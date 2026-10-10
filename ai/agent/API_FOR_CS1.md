# Agent API: notes for CS 1

The agent gives you a FastAPI **router**. You own the main app; you just include it.

## 1. Include the router

```python
from ai.agent.api import router as agent_router
app.include_router(agent_router)          # adds POST /api/ask and GET /api/traces
```

Run uvicorn from the **repo root** so `import ai.agent` works. Install deps: `pip install -r ai/requirements.txt`.

## 2. Give the agent the box state (required)

The agent never reads the sensors itself. At startup, hand it a function that returns the
current box state:

```python
from ai.agent import agent, tools

tools.set_provider(lambda: box_state.to_dict())   # called on every question: keep it fast, no I/O
agent.warm_up()                                   # once, at startup: loads models so question #1 isn't slow
```

- The provider takes no arguments and returns a dict. Your `BoxState.to_dict()` shape is
  accepted as is (`readings{p,temp,hum,water,wind,rain}`, `power{wall,battery_hours,outage_since}`,
  `alarms[{id,level,since}]`, `storm{phase,risk,alert,reason,model_version}`, `src`).
  `tools.from_box_state()` converts it.
- Also useful if you have them: `household`, `supplies`.
- Return a fresh snapshot (or a copy), not a dict that another thread is mutating.
- If no provider is set, the agent sees an empty box and answers guide questions only.

## 3. Endpoints

### POST /api/ask
Request:
```json
{"text": "Is the storm over?", "channel": "chat"}
```
`channel` is `"chat"` (default) or `"voice"`. Anything else gives 422.

Response 200:
```json
{
  "answer": "Not yet. Pressure is still falling ...",
  "sources": [{"source": "flood.md"}],
  "trace_id": 12,
  "route": "tool"
}
```
- `route` is `tool | canned | rag | no_match`. This is the one field added to the PROJECT_PLAN
  section 8 shape (`{answer, sources, trace_id}`); the frontend can ignore it.
- `sources` is `[]` for live-data answers; otherwise the guide(s) used.
- Empty or whitespace `text` gives **400** `{"detail": "text must not be empty"}`.
- Slow on the Pi (up to ~LLM_TIMEOUT_S for guide questions). Set a generous client timeout
  (30 s+). The endpoint is a plain `def`, so it runs in FastAPI's threadpool and other
  requests (WebSocket, /api/state) keep working while it thinks.

### GET /api/traces?limit=20
Newest first, `limit` 1..50 (default 20). For the engineer-mode trace panel. One item:
```json
{
  "id": 12, "ts": "2026-10-10T15:04:05+00:00", "channel": "chat",
  "question": "Is the storm over?", "route": "tool", "answer": "...",
  "tools": {"get_storm_status": {...}}, "chunks": [{"id": "...", "title": "...", "score": 0.61}],
  "sensors": {"pressure_hpa": 996.0, "...": "..."},
  "timings": {"route": 0.1, "tools": 0.3, "model": null, "notes": []}
}
```
`timings` holds ms per step plus `model` and `notes`. Traces are held in memory (last 50, lost
on restart); they are also written to the `agent_traces` table if your SQLite file has it.

## 4. Other agent entry point (proactive messages)

For alarms, phase changes and power loss, call directly (no HTTP):
```python
from ai.agent import agent
r = agent.explain_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})
# {"answer": "...", "lcd": ("line1", "line2"), "route": "proactive", "trace_id": N}
# answer == "" means nothing to say.
```
Events: `{"t":"alarm","id","level","state"}`, `{"t":"phase","phase"}`, `{"t":"power","state":"lost"|...}`.

## 5. Try it without the box

```powershell
python -m uvicorn ai.scripts.dev_server:app --port 8000
```
This is a local-only app with a fake box state; do not ship it.
