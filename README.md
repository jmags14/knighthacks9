# StormBox

**When a hurricane knocks out power and internet, StormBox keeps watching your home, warns you about
danger, and tells you what to do. No internet, no cloud, no cell service.**

Built for Knight Hacks 9. Florida hurricanes first; it works for any long outage.

The idea in one loop: **Sense** (box sensors) → **Understand** (alarm rules + storm risk model) →
**Explain and act** (offline AI agent) → **Show** (buzzer, dashboard, voice).

**Golden rule: alarms come from the box's fixed rules, never from AI.** The AI explains and advises.
If the laptop dies, the box still sounds its alarms.

---

## How it is built

```
   THE BOX (battery powered)                            THE HUB (laptop, offline)
 ┌───────────────────────────────┐   USB serial     ┌─────────────────────────────────┐
 │ Seeed XIAO RP2040             │   (default)      │ box service: state, storm phase,│
 │  barometer, temp + humidity,  │◄────────────────►│   wind / storm alarms, replay   │
 │  floor moisture, wind, power  │   or MQTT        │ storm risk model (decision tree)│
 │  buzzer; flood + heat alarms  │   (optional)     │ AI agent: canned → guides → LLM │
 │  run ON the board             │                  │ voice: speech in, speech out    │
 └───────────────────────────────┘                  │ web dashboard (phones can join) │
                                                    └─────────────────────────────────┘
```

The original plan used a Raspberry Pi as the hub. We moved to a **laptop hub** (see
`docs/LAPTOP_MVP.md`), and the board connects over **USB serial** because the XIAO RP2040 has no Wi-Fi.
**MQTT** is an optional second transport for a Wi-Fi board later (`docs/MQTT_SETUP.md`). The messages
are the same JSON either way, so nothing else changes.

---

## What it can do

**On the box (works without the laptop)**
- Reads pressure, temperature, humidity, floor moisture, wind speed and wall power.
- **Flood alarm** when the floor sensor reads wet, and **heat alarm** when the room is too hot. Both run
  on the board with a buzzer.

**On the hub**
- **Storm phase and storm risk:** tracks pressure over time (falling, possible eye, recovering) and
  runs a decision-tree model trained on real NOAA hurricane records. It uses pressure, wind, or both,
  and shows the risk score plus the tree path that produced it. The model never sets off alarms itself.
- **Wind and storm alarms:** worked out on the hub from pressure and wind (39 mph warn, 74 mph alarm).
- **Storm replay:** plays back real recorded hurricanes (Nicole, Idalia, Helene, Debby, Ian, Milton)
  clearly labeled **REPLAY**, with speed, pause and resume, a timeline chart and the risk curve.
  Flood and heat stay live during a replay.
- **Offline AI agent:** answers questions from vetted emergency guides (Ready.gov, NWS, CDC, FEMA) in
  three steps: canned answers, then guide search (RAG), then a small local language model. Alarm and
  sensor questions are answered by fixed templates, no model needed.
  - **Heat index ("feels like"):** from the *indoor* temperature and humidity, which is what matters
    when the AC is off. Uses the NWS formula.
  - **Floor moisture:** described as the floor being dry, damp or wet. The box has no rain gauge.
  - **Internet check:** notices when the internet drops or returns, and says the box keeps working.
  - **Proactive messages:** explains new alarms and storm phase changes in plain words.
- **Voice:** push-to-talk with speech-to-text and text-to-speech, all offline.
- **Dashboard:** family view and engineer view, served over the hub's own network. Engineer mode shows
  readings, alarm timeline, the replay chart, the storm risk tree and box health.

**What it does not do:** it does not replace 911, it is not a certified carbon monoxide or smoke
detector, and it has no camera, cloud or forecast feed. There is no air-quality sensor and no rain gauge.

---

## Where things stand

| Piece | Status |
|---|---|
| Board firmware: sensors, flood + heat alarms, buzzer, serial output | Working |
| Hub: box service, storm risk, replay, dashboard, agent, voice | Working, tested without the board |
| Board ↔ hub over USB serial | Written; being connected to the real board |
| Board LCD, lights, receiving commands (heartbeat, replay pressure) | **Not in the firmware yet**; the hub already sends them |
| MQTT / Wi-Fi link | Written and tested with a fake box; needs a Wi-Fi board |
| Push-to-talk from the box button | Laptop push-to-talk works; the button is not wired in yet |

---

## Quick start (laptop, Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -r ai/requirements.txt
```

You also need [Ollama](https://ollama.com) with the models pulled once while online:
`ollama pull llama3.2:1b`, `ollama pull qwen3:0.6b`, `ollama pull nomic-embed-text`. Then, once:

```powershell
.\.venv\Scripts\python ai/scripts/download_voice_models.py   # speech models (needs internet)
.\.venv\Scripts\python ai/scripts/build_index.py             # guide search index (needs Ollama running)
```

**Run the dashboard with no hardware** (simulated box):
```powershell
.\.venv\Scripts\python pi/run.py --fake         # then open http://localhost:8000
```

**Run with the real board:**
```powershell
.\.venv\Scripts\python pi/run.py --serial COM3  # or --serial auto to find the board by its USB ID
```
Close any serial monitor first: only one program can use the port.

**MQTT instead of serial:** see `docs/MQTT_SETUP.md`.

**Check everything before a demo:**
```powershell
.\.venv\Scripts\python scripts/check_box.py
```

**Talk to the agent in a terminal** (fake box, shows how it got each answer):
```powershell
.\.venv\Scripts\python ai/scripts/ask.py
.\.venv\Scripts\python ai/scripts/ask.py --state hot -q "How hot does it feel?"
.\.venv\Scripts\python ai/scripts/replay_check.py --at 30 -q "Is the storm over?"
```

**Tests** (no hardware, no Ollama needed):
```powershell
.\.venv\Scripts\python -m pytest ai/tests pi/tests -q
```

---

## Repo map

| Folder | What is in it |
|---|---|
| `esp32/` | Board firmware (PlatformIO, Seeed XIAO RP2040) |
| `pi/` | Hub: `run.py`, box service (serial / MQTT / fake), FastAPI + WebSocket, dashboard pages. Named `pi` from the original plan; it runs on the laptop |
| `ai/agent/` | The agent: router, templates, tools, prompts, guide search. `API_FOR_CS1.md` is how the hub calls it |
| `ai/knowledge/` | Emergency guides and canned answers |
| `ai/ml/` | Storm risk models, replay data and the replay timeline |
| `ai/system/` | Internet up/down check |
| `ai/voice/` | Speech in and out |
| `ai/scripts/`, `scripts/` | Terminal chat, replay check, index builder, fake boxes, demo preflight |
| `docs/` | `PROJECT_PLAN.md` (original plan), `LAPTOP_MVP.md` (the current plan), `MQTT_SETUP.md` |

For the contracts between teammates, see `docs/PROJECT_PLAN.md` (serial messages, data shapes) and
`ai/agent/API_FOR_CS1.md` (how the hub uses the agent, storm job, replay timeline and internet check).

---

## Honesty notes

- Storm replay is **real recorded NOAA data**, always labeled REPLAY.
- The storm risk score is an early warning, not a forecast. Official guidance from the National
  Hurricane Center and local officials always comes first.
- Temperature and humidity are measured **inside** the house; wind and pressure tell you about outside.
- If your phone has service, you can still call or text 911.
