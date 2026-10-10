# Agent API: notes for CS 1

The agent gives you a FastAPI **router**. You own the main app; you just include it.

## 1. Include the router

```python
from ai.agent.api import router as agent_router
app.include_router(agent_router)          # adds POST /api/ask and GET /api/traces
```

Run uvicorn from the **repo root** so `import ai.agent` works. Install deps: `pip install -r ai/requirements.txt`.

### One-time setup on every machine (your laptop AND the Pi)
These files are git-ignored, so a fresh clone does not have them. Without them the agent answers
"I can't search the guides right now" to every guide question.
```
ollama pull llama3.2:1b
ollama pull qwen3:0.6b
ollama pull nomic-embed-text
python ai/scripts/build_index.py                  # guide search index (Ollama must be running)
python ai/scripts/download_voice_models.py        # voice only; needs internet once, then fully offline
```
Re-run `build_index.py` whenever a guide changes. Check any failure with `GET /api/traces?limit=1`:
`timings.notes` says what went wrong (e.g. `rag failed: ConnectError` = Ollama is not running).

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
- **Wind is in m/s** in `readings.wind` (what the sensor reports). The agent converts to mph when it
  speaks (setting `WIND_RAW_UNIT` in `ai/agent/config.py`). Convert to mph only on the dashboard.
- Without `readings.wind` / `readings.rain`, the "weather outside" answer has no outside readings.
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

### Heat index ("feels like")

The box measures temperature and humidity **inside** the house, so the agent now also says what the
heat feels like (NWS heat index). Nothing to wire for the voice: it is automatic.
- **Heat alarm text** (`{"t":"alarm","id":"heat",...}`) now includes the number and category when the
  box state has `temp_c` and `humidity`: "It's dangerously hot inside. It feels like 104 degrees, danger level. Drink
  water, ...". The LCD lines are unchanged. Pass the current state (the agent does this itself).
- **Sensor answers** ("What's the temperature?", "How hot is it?", "What's the heat index?") add
  "It feels like 95 degrees, extreme caution level." when the heat index is 80 F or more, and nothing below that.
- **Alarms still come only from the ESP32.** The agent never raises a heat alarm.

**For the Home screen**, call the helper with the box readings:
```python
from ai.agent.heat import heat_info
info = heat_info(temp_c=31.1, humidity=60)      # humidity may be None
# {"temp_f": 88, "heat_index_f": 95, "category": "Extreme Caution",
#  "humidity_measured": True, "feels_like": True,
#  "label": "Feels like 95°F · Extreme Caution"}
```
- Show `info["label"]` as is. It is `None` below 80 F (the heat index is just the temperature then), so
  hide the line. `heat_info` returns `None` when `temp_c` is `None` (sensors offline).
- `category` is `"none"`, `"Caution"`, `"Extreme Caution"`, `"Danger"` or `"Extreme Danger"` (use it for
  the color). If `humidity_measured` is `False`, the number is the temperature alone: show
  "humidity not measured".

## 5. Voice entry points (`ai/voice/`)

Fully offline. Two functions, and they are **not** interchangeable:

```python
from ai.voice import pipeline

# ESP32 BUTTON handler: after you have recorded the question to a 16 kHz mono WAV
r = pipeline.handle_voice_turn("/tmp/question.wav")
# -> speech to text -> agent.answer(text, "voice") -> spoken answer.
# r = {"transcript", "answer", "route", "trace_id", "timings", "wav"}
# Nothing heard / unclear -> it says "Sorry, I didn't catch that. Please try again." (route "no_speech").

# PROACTIVE alarms / phase changes / power loss (no microphone involved)
r = pipeline.speak_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})
# -> agent.explain_event(event) -> speaks the answer. answer == "" means nothing was said.
# Replay events (event["source"] == "replay") are spoken with the "Replay:" prefix, so the demo stays honest.
```

Both block until the speech has finished playing, so call them from a worker thread, not your
main loop. **One voice at a time:** if `speak_event` is called while an answer is playing, the
answer is cut off and the alarm is spoken; two alarms take turns. This is handled inside the
pipeline, so just call the functions from whichever thread you like.

**What you replace on the Pi:** only `ai/voice/audio_io.py`. It has three small functions:
`record_until_enter()` (return a WAV path), `play_wav(path)` (block until done) and
`stop_playback()` (cut the sound short; with `aplay`, kill the process). Keep the names and
behavior and the rest of the pipeline works unchanged.

**Settings** are in `ai/voice/config.py`: STT model name, Piper voice name, max record seconds.
For a faster Pi voice set `PIPER_VOICE = "en_US-lessac-low"`, then run
`python ai/scripts/download_voice_models.py` once (needs internet), and copy `ai/models/` to the Pi
(it is git-ignored). At runtime nothing touches the network.

**Pinned versions:** use `ai/requirements.txt` as is. `av` must stay below 19 or faster-whisper crashes.

## 6. Storm risk job (`ai/ml/storm_job.py`), including the wind models

```python
from ai.ml.storm_job import run_storm_job
result = run_storm_job(readings, source="live")     # call about once a minute; "replay" for replay rows
```
- `readings` = recent rows covering the last 7+ hours: dicts with `ts` (ISO or datetime) and
  `pressure_hpa` (or `p`), `temp_c`, and for wind: `wind_ms` (or bare `wind`, m/s) and optionally
  `gust_ms` (or `gust`). Rows can be pressure-only or wind-only; send what each sensor has.
- **Gust:** if you send no `gust` field, the job computes it: highest 5-second average per hour
  when readings come about every second, otherwise the highest single reading.
- Result: `status` = `"ok"` | `"warming up"` | `"sensors offline"`, plus `risk`, `alert`, `reason`,
  `path`, `model_version`, and **`model`** = `"pressure"` | `"wind"` | `"both"` (None if offline).
  Put `reason` into the box state's `storm.reason`; it already says e.g.
  "Storm risk 97% (both model) because ...", and the agent repeats it. A warming-up result also has
  `hours_have` / `hours_needed` (show "3 of 7 hours" on the dashboard).
- **Which model runs** is set in `ai/agent/config.py`: `STORM_MODEL_MODE` for live (`"v1"` = pressure
  only, `"auto"` = pick by which sensors sent a good reading in the last 30 minutes) and
  `STORM_REPLAY_MODEL` for the replay demo (`"auto"` = the wind model "both" if the replay has wind).
- **Needs 7 hours of history** before it gives a score. Save readings to SQLite and reload them at
  startup, or every reboot restarts the 7 hours. A gap of more than 2 hours also restarts it.
- **Replay:** keep a separate replay history (never mix it with live). Run the job once per simulated
  hour (every 6 rows of 10 minutes), not once per real minute. Replay files: `ai/ml/v2/replay/*.csv`
  (`ts,pressure_hpa,wind_ms,gust_ms`) listed in `ai/ml/v2/replay/index.json`. Tree JSON for the
  dashboard: `ai/ml/storm_risk.json` (pressure) or `ai/ml/v2/storm_risk_<wind|both>.json`.
- No hardware needed to see it working: `python ai/scripts/replay_check.py` (table of a whole storm)
  and `python ai/scripts/replay_check.py --at 30 -q "Is the storm over?"` (agent at hour 30).

## 7. Try it without the box

```powershell
python -m uvicorn ai.scripts.dev_server:app --port 8000      # the HTTP API
python ai/scripts/push_to_talk.py                            # the voice pipeline, laptop mic/speakers
```
Both use a fake box state and are local-only; do not ship them.
