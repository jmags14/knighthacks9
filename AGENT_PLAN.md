# StormBox — AI / Agent Plan (CS 2)

> Companion to `PROJECT_PLAN.md`. That file is still the source of truth for the whole team; this one goes deeper on **the AI layer**: the agent, RAG, voice, and the storm risk model.
>
> **If you are an AI assistant helping CS 2:** follow the decisions here. Respect the contracts in `PROJECT_PLAN.md` (§5 serial protocol, §7 schemas, §8 API). Keep everything **offline and lightweight** (Raspberry Pi 4, 4–8 GB RAM). Don't add frameworks (no LangChain, no Google ADK) and don't let the LLM make safety decisions.

---

## Status (updated 2026-10-08)

| Piece | Status | Where |
|---|---|---|
| Storm risk model | ✅ **Done.** Trained on real NOAA data; **decision tree chosen** | `ai/ml/` (§8) |
| Replay storms | ✅ **Done.** Real 2022+ storms as 10-min CSVs | `ai/ml/replay/` |
| Knowledge pack | ✅ **Done.** 15 sourced topic files, 71 chunks | `ai/knowledge/` (§6) |
| `canned.json` (40–60 instant answers) | ⬜ Next | `ai/knowledge/canned.json` |
| `build_index.py` (embed guides → Chroma) | ⬜ | `ai/scripts/` |
| P0 templates + LCD strings | ⬜ (text drafted in §5.1) | `ai/agent/templates.py` |
| Agent code (router, tools, RAG, prompt, traces) | ⬜ | `ai/agent/` |
| Box-service storm job (sea-level conversion + calibration offset + tree every minute) | ⬜ CS 2 writes it, CS 1 calls it | `ai/ml/storm_job.py` |
| Models on the Pi + LLM benchmark | ⬜ Llama 3.2 1B primary, Qwen3 0.6B fallback | §9, §11, §15 |
| Voice (Vosk + Piper) | ⬜ CS 1, agent just gets text | §7 |

---

## 0. One-paragraph summary

The agent is **plain Python on the Pi** that turns everything the box knows (sensors, alarms, storm phase, storm risk, household supplies, offline guides) into **short, calm, spoken advice**. Code and fixed rules make every decision; a **small open-weight LLM (~1B, from the Hugging Face Hub, run by Ollama)** is used only to **word answers to open questions** using retrieved guide passages (RAG). Alarm explanations and sensor questions use **templates** (instant, no LLM).

---

## 1. Why the agent exists (the purpose)

Without the agent, the box shows numbers: `p=987 hPa`, `water=640`, `risk=0.78`. A scared family at 2 a.m. can't act on numbers. The agent's job is to **translate**:

| The box knows | The agent says |
|---|---|
| Flood alarm ON, water=640 | "Water is on the floor. Move people and pets upstairs and don't touch anything electrical while standing in water." |
| Phase = possible eye | "Pressure stopped falling near its lowest point. This may be the eye. The calm is temporary. Stay inside." |
| Risk score 78%, tree path | "Storm risk is high: pressure fell 5 hPa in 6 hours and is still falling." |
| 6 gal water, 3 people, 1 dog | "You have about 2 days of water." |
| "Is chicken safe after 6 h without power?" | Answer from the food-safety guide, with the source |

It does three things:
1. **Speaks first** when an alarm or phase change happens (proactive).
2. **Answers questions** by voice (button) or chat (dashboard).
3. **Logs every step** (`agent_traces`) so engineer mode can show exactly how it answered.

**Is it really an "agent"?** Honest answer for judges: it's a *tool-using assistant with code-based routing*. The routing is code on purpose: on a Pi, a 1B model choosing its own tools is unreliable, and this is a safety device. It still reads live state, uses tools, acts on its own when alarms fire, and grounds answers in sources.

---

## 2. Who decides what

| Decision | Made by | AI? | Where |
|---|---|---|---|
| Alarm on/off (flood, air, heat, storm) | ESP32 fixed rules | No | `esp32/` |
| Storm phase (falling / possible eye / recovering) | Fixed rules over pressure history | No | `pi/box_service/` |
| Storm risk score | Trained decision tree (or logistic regression) | ML, not LLM | `ai/ml/storm_job.py`, called by box-service every minute |
| Which tools / guide topic to use | Router (keywords + similarity) | No | `ai/agent/router.py` |
| Which guide passages are relevant | Embedding search (RAG) | Embeddings | `ai/agent/rag.py` |
| How to word an open answer | Small LLM | **Yes, only here** | Ollama |

**Golden rules:** the LLM never fires, cancels, or contradicts an alarm. Sensor numbers are ground truth. The air sensor is **not** a CO detector.

---

## 3. The four paths through the agent

```
Alarm / phase event ──────────────────────────────► [P0] TEMPLATE ───────────┐
                                                                              │
Question (voice → Vosk text, or dashboard chat)                               │
   │                                                                          │
   ├─► [P1] CANNED  (similarity ≥ ~0.8 to a pre-written answer) ──────────────┤
   ├─► [P2] TOOL    (router: water / storm / battery / sensors / outage) ─────┤
   └─► [P3] RAG+LLM (everything else): search guides → top 3 → LLM ───────────┤
                                                                              ▼
                                          answer + sources → Piper speech + dashboard
                                          + agent_traces row (route, tools, chunks, timings)
```

| Path | LLM? | Typical time on Pi | Examples |
|---|---|---|---|
| **P0 Proactive template** | No | instant | Flood alarm, air alarm, heat warning, storm warning, possible eye, recovering, power lost |
| **P1 Canned** | No | < 0.2 s (one embedding) | "What do I do if the power goes out?", "How do I purify water?" |
| **P2 Tool + template** | No | instant | "Is the storm over?", "How long will our water last?", "How much battery is left?", "When did the power go out?", "What's the temperature?" |
| **P3 RAG + LLM** | **Yes** | a few seconds | "Is the chicken in the fridge still safe?", "Can I run the generator in the garage?", "Water is ankle-deep, what now?" |

**Design target:** most demo interactions hit P0–P2. P3 is the "judge asks anything" moment.

Optional upgrade (only if time and speed allow): P2 answers can be passed to the LLM to sound more natural. Default is templates.

---

## 4. Contracts the agent depends on

### 4.1 Box state snapshot (from `box-service`)
```python
{
  "ts": "2026-10-10T02:14:00Z",
  "source": "live",                 # or "replay"
  "pressure_hpa": 987.4, "temp_c": 31.2, "humidity": None,
  "water": 640, "air": 180, "wall_power": 0,
  "alarms": [{"id": "flood", "level": "alarm", "since": "..."}],
  "storm": {"phase": "falling", "risk": 0.78, "alert": True,
            "reason": "Storm risk 78% because ...", "model_version": "storm-risk-20261009"},
  "battery": {"hours_left": 11.5}
}
```

### 4.2 Agent entry points (`ai/agent/agent.py`)
```python
answer(text: str, channel: str = "chat") -> dict
#   channel: "voice" | "chat"
#   returns {"answer": str, "sources": [{"title","source","url"}], "route": "canned|tool|rag",
#            "trace_id": int}

explain_event(event: dict) -> dict
#   event: {"t":"alarm","id":"flood","level":"alarm","state":"on"} or {"t":"phase","phase":"possible_eye"}
#   returns {"answer": str, "lcd": ("Water detected", "Move up now"), "route": "proactive", "trace_id": int}
```
`POST /api/ask` (§8 of the project plan) calls `answer()`. `box-service` calls `explain_event()` when an alarm turns on or the phase changes, saves the text into `alarms.explanation`, sends the LCD lines, and hands the text to the voice service.

### 4.3 Tools (`ai/agent/tools.py`): each returns a small dict, runs in milliseconds
| Tool | Returns |
|---|---|
| `get_sensors()` | pressure, temp, humidity, water, air, wall_power, source |
| `get_storm_status()` | phase, risk, alert, reason (plain English from the model runtime) |
| `get_alarms()` | active alarms with level and since |
| `search_guides(q, topic=None, k=3)` | top chunks with title, source, url |
| `water_calculator()` | days_left, people, pets, gallons (reads `household` + `supplies`) |
| `get_battery()` | hours_left, on_battery |
| `get_outages()` | last lost/restored times, outage duration |
| `log_checkin(name, status)` | saved row id |

---

## 5. Path details

### 5.1 P0: proactive templates (no LLM)
Each event maps to one spoken sentence and one 16×2 LCD message. If `source == "replay"`, prefix speech with "Replay:" and LCD line 1 with `REPLAY`.

| Event | LCD (≤16 chars/line) | Spoken (starting text; tune with guides) |
|---|---|---|
| flood alarm | `Water detected` / `Move up now` | "Water detected on the floor. Move people and pets to higher ground and stay away from outlets and cords in water." |
| air alarm | `Air alarm` / `Get fresh air` | "The air sensor detects fumes. Get everyone to fresh air. If a generator is running, it must be outside and far from windows." (Never say "carbon monoxide detected".) |
| heat warning | `Heat warning` / `Cool down` | "It's dangerously hot inside. Drink water, move to the coolest room, and watch kids, elders, and pets." |
| storm warning | `Pressure falling` / `Storm nearby` | "Pressure is falling fast. A storm may be close. Stay inside, away from windows." |
| storm severe | `STORM ALERT` / `Stay inside` | "Pressure is dropping very fast. Go to your safest interior room now." |
| phase: possible eye | `Possible eye` / `Stay inside` | "This may be the eye of the storm. The calm is temporary and the wind will return. Stay inside." |
| phase: recovering | `Pressure rising` / `Wait for all-clr` | "Pressure is rising. The worst may be passing, but wait for an official all-clear before going outside." |
| power lost | `Power out` / `On battery` | "Wall power is out. The box is on battery." |

Templates live in `ai/agent/templates.py` so EE 1 and CS 1 can see the exact LCD strings.

### 5.2 P1: canned answers
- File: `ai/knowledge/canned.json`, 40–60 entries: `{"id", "questions": [3–5 phrasings], "answer", "source"}`.
- At startup: embed every canned question once (keep in memory).
- Per question: embed the user text, cosine similarity vs all canned questions, take the best. If ≥ threshold (start at 0.80, tune with real questions), return it.
- Canned answers must not depend on live readings. If they need live data (water, storm), that's P2.

### 5.3 P2: router + tools + templates
`router.py` is a keyword table first (simple, fast, debuggable), with embedding similarity as a fallback later if needed:

| Keywords | Tools | Template / topic |
|---|---|---|
| storm over, eye, calm, passing, is it safe outside | `get_storm_status` | phase template |
| risk, how bad, storm coming | `get_storm_status` | "Storm risk N%. {reason}" |
| water last, drinking water, how many days | `water_calculator` | days-left template |
| battery, power bank, how long box | `get_battery` | hours template |
| power out, when did power | `get_outages` | outage template |
| temperature, hot, humidity, pressure, reading | `get_sensors` | readings template |
| flood, water on floor | `get_alarms`, `get_sensors` | if flood alarm on → P0 flood text; else → P3 with `topic=flood` |
| anything else | `get_sensors`, `get_alarms` | P3 (RAG + LLM), topic from keywords if any |

**Water calculator:** the family enters **people, pets, gallons on hand** once on the dashboard Supplies screen (`household` + `supplies` tables). `days = gallons / (people × 1.0 + pets × PET_GAL)` using Ready.gov's ~1 gallon per person per day; `PET_GAL` is a setting (start 0.25, check the pet guide). If no data yet: "Add your household and water on the Supplies screen and I can tell you."

### 5.4 P3: RAG + LLM
1. `rag.search(question, topic, k=3)` → 3 chunks (filter by `topic` when the router knows it; if fewer than 2 results, search without the filter).
2. Build the prompt (≤ ~400 words total):
```
SYSTEM: You are StormBox, an offline emergency assistant. Answer in 2-3 short, calm sentences.
Use ONLY the passages and readings below. If they don't answer the question, say
"I'm not sure. The closest guide is: <title>." Never contradict an active alarm.
The air sensor is not a carbon monoxide detector. If phone service works, call 911 in an emergency.

READINGS: pressure 987 hPa (falling), temp 31 C, water alarm ON, wall power OFF.
PASSAGES:
[1] (Ready.gov, Floods) ...
[2] ...
[3] ...
QUESTION: <user text>
ANSWER:
```
3. `ollama.generate(model=LLM_MODEL, prompt=..., options={"num_predict": 120, "temperature": 0.2}, keep_alive="24h")` (add `think=False` for Qwen3)
4. Return the answer + sources (titles of the chunks used).

---

## 6. The knowledge pack + vector database

**No accounts. No cloud.** ChromaDB is a Python library that saves to a folder (`ai/data/chroma/`).

### 6.1 What's in the pack (✅ done: 15 files, 71 chunks)
All facts come from official/expert sources, rewritten in plain words with exact numbers kept. Every chunk ends with a `Source:` line naming the page it came from. `ai/knowledge/_README.md` documents everything (files starting with `_` are **not** indexed).

| File | `topic` | Main sources |
|---|---|---|
| `flood.md` | `flood` | Ready.gov Floods, NWS Turn Around Don't Drown, CDC flood safety |
| `hurricane-during.md` | `storm` | NWS hurricane safety + tornado safety, Florida DEM |
| `generator-co.md` | `air` | CDC carbon monoxide, Ready.gov power outages |
| `heat.md` | `heat` | NWS heat illness, CDC heat & health |
| `power-outage.md` | `power` | Ready.gov power outages |
| `food-safety.md` | `food` | FoodSafety.gov power outage chart |
| `water.md` | `water` | Ready.gov water, EPA emergency disinfection, Florida DEM |
| `evacuation.md` | `evacuation` | Florida DEM, NWS, Ready.gov floods |
| `after-the-storm.md` | `after` | CDC after a hurricane, ESFI downed power lines, NWS |
| `first-aid.md` | `first-aid` | American Red Cross bleeding, CDC |
| `pets.md` | `pets` | Ready.gov pet guidance (via DLA summary), NWS |
| `medications.md` | `medical` | FDA insulin in emergencies, Ready.gov, Florida DEM |
| `communication.md` | `communication` | FCC/FEMA tips, FCC Text-to-911, SAMHSA helpline (via CDC) |
| `emergency-kit.md` | `kit` | Ready.gov Build a Kit, Florida DEM |
| `stormbox-readings.md` | `stormbox` | Team-written: what the box's pressure, phases, risk score, alarms mean |

**Alarm → topic filter:** `flood`→`flood`, `air`→`air`, `heat`→`heat`, `storm`→`storm`.

### 6.2 Format of one file
```markdown
---
title: Floods
topic: flood
sources:
  - name: Ready.gov (FEMA) - Floods
    url: https://www.ready.gov/floods
retrieved: 2026-10-08
---

## Never walk or drive through flood water      <- one "##" section = one chunk (~35-110 words)
Body in plain words...
Source: NWS - Turn Around Don't Drown <https://www.weather.gov/...>   <- this chunk's source
```
Front matter is simple YAML (avoid `:` inside titles). Each section is written to make sense on its own, because the agent only sees 3 chunks.

### 6.3 `ai/scripts/build_index.py` (run once; re-run when guides change)
1. Read every `ai/knowledge/*.md` except `_*.md`; parse the front matter; split by `## `.
2. For each section: heading = first line; pull out the `Source:` line into metadata; the rest is the text. Id = `<file stem>-<nn>` (e.g. `flood-02`). The parsing sketch is in `ai/knowledge/_README.md`.
3. Embed with Ollama: `ollama.embed(model="nomic-embed-text", input=["search_document: " + title + ". " + heading + ". " + text, ...])`. Embed questions as `"search_query: " + question`.
4. `chromadb.PersistentClient(path="ai/data/chroma")` → `get_or_create_collection("guides", metadata={"hnsw:space": "cosine"})` → `add(ids, documents, embeddings, metadatas={"title","heading","topic","source"})`.
5. Print the count (expect 71) and run test searches, e.g. "how much bleach per gallon" → `water-04`; "generator in garage" → `generator-co-03`; "is it over, it got calm" → `hurricane-during-02`.

Run it **on the Pi** (or on a laptop with the exact same embedding model, then copy `ai/data/chroma/`). 71 chunks embed in well under a minute; at question time only the question gets embedded.

**Lighter fallback:** if Chroma is heavy on the Pi, save the vectors to `ai/data/guides.npy` + `ai/data/guides.json` and search with numpy cosine similarity. Same results at this size.

**Sources under answers:** show the chunk's `source` metadata (e.g. "Source: EPA – Emergency disinfection of drinking water"). That's the "source line" on the dashboard Ask screen.

---

## 7. Voice (CS 1 owns the pipeline; the agent just gets text)
- **Button short press** → ESP32 sends `{"t":"btn","press":"short"}` → `voice` records from the USB mic until release / 1.5 s of silence.
- **Vosk** (`vosk-model-small-en-us`) turns audio into text, offline.
- `agent.answer(text, channel="voice")`.
- **Piper** (one English voice, e.g. `en_US-lessac-medium`: an `.onnx` + `.json` file) turns the answer into a `.wav`; play it with `aplay` through the 3.5 mm jack. Check the Piper README for the current command (`pip install piper-tts`).
- Alarms: P0 text goes straight to Piper; the buzzer is the ESP32's job.

---

## 8. Storm risk model (ML, not LLM)

**Notebook:** `ai/ml/storm_risk_colab.ipynb` (run in Google Colab: Runtime → Run all).

| Step | What happens |
|---|---|
| Data | NOAA NDBC hourly pressure for ~20 Florida buoys/coastal stations (2005–2025, June–Nov) + HURDAT2 storm tracks |
| Label | 1 if a TS/HU center comes within 150 km in the next 12 h |
| Features | `p_now`, `d1`, `d3`, `d6`, `min6`, `d3_rate` (all from the box's own barometer) |
| Split | train 2005–2018 · pick threshold on 2019–2021 · test on 2022+ (unseen storms) |
| Models | logistic regression vs decision tree (depth 3), both `class_weight="balanced"` |
| Threshold | lowest threshold with ≤ 1 false alarm per week of normal weather |
| Pick | better storm recall wins; tree wins if within 5 points |
| Output | `storm_risk.json` (~3 KB, both models + which to use), `storm_risk_runtime.py` (pure Python), `metrics.json`, figures, replay CSVs |

### 8.1 Results (✅ real NOAA run, test storms 2022+ never seen in training)

| | Logistic regression | **Decision tree (shipped)** |
|---|---|---|
| Storms caught (event recall) | 100% | **96.8%** (missed ~1 of ~31) |
| Median warning before closest approach | 24 h | **20.5 h** |
| False alarms per week (normal weather) | 1.69 (over the ≤1 target) | **0.35** (about 1 every 3 weeks) |

**Decision: ship the tree.** It's within 5 recall points, it has ~5× fewer false alarms, logistic regression broke the false-alarm target on unseen storms, and the tree can be drawn and explained. `storm_risk.json` has `"use": "tree"`; the exact alert threshold is in `metrics.json`.

**What the tree learned (depth 3):** it mostly asks how low pressure is right now (`p_now ≤ 1012.7`, `≤ 1009.5`, `≤ 1007.6`), uses `min6` on the high-pressure side, and uses `d6` once to recognize a storm that's leaving (pressure rising > 1.6 hPa in 6 h → low risk). Because it leans on **absolute** pressure, sea-level conversion + calibration (below) is essential, or a calm day at the box's altitude reads "low" and shows false risk.

**Pitch line:** "On storms it never saw, the tree catches 97% about 20 hours ahead, with about one false alarm every three weeks."

### 8.2 On the Pi (`box-service`, every minute)
Running the tree is ~3 if-checks (microseconds), so every minute is nothing for the Pi. The agent never runs it; it reads the saved result through `get_storm_status()`.
1. Clean the live barometer like the training data: drop readings outside 850–1090 hPa, average each hour, keep the last 7 hourly values (oldest first).
2. Convert each to sea level: `to_sea_level(p, ALTITUDE_M) + CAL_OFFSET_HPA`. Set `ALTITUDE_M` for where the box sits (~1 hPa per 8 m). **Calibrate once** before the event while online: compare the converted reading to the nearest NWS/airport sea-level pressure and save the difference as `CAL_OFFSET_HPA` (cheap barometers can be off ~1 hPa).
3. Sanity check: a calm Florida day should read ~1012–1018 hPa after conversion. If it reads ~1005 on a sunny day, the altitude or offset is wrong.
4. `feats = features_from_hourly(values)` → `result = predict(model, feats)` → `reason = explain(model, result)`.
5. Save to `storm_status` (`risk`, `phase`, `model_version`) and the box state. The dashboard draws the tree from `storm_risk.json` and highlights `result["path"]`.
6. Not enough history yet (< 7 h after boot)? Show "Risk: warming up" and skip. During **replay**, build the hourly values from the replay rows' recorded timestamps (6 rows = 1 hour), and **skip the sea-level conversion** because replay CSVs are already sea-level NOAA data.

### 8.3 Files from the Colab zip: where they go (all ML in one folder)
Everything ML lives in **`ai/ml/`**. Teammates read from there; nothing is copied around.

| File | Location | Who needs it |
|---|---|---|
| `storm_risk_colab.ipynb` | `ai/ml/` | CS 2 (retrain, show judges) |
| `storm_risk.json` | `ai/ml/` | **CS 1** (box-service loads it; dashboard draws the tree) |
| `storm_risk_runtime.py` | `ai/ml/` | **CS 1** (via `ai/ml/storm_job.py`) |
| `metrics.json` | `ai/ml/` | **CS 1** (engineer-mode model panel) + slides |
| `replay/*.csv` + `replay/index.json` | `ai/ml/replay/` | **CS 1** (replay streaming) + **EE 1** (one CSV to test ESP32 replay mode) |
| `figures/*.png` | `ai/ml/figures/` | **Whole team**: ML slide |
| `*.joblib` | `ai/ml/backup/` | Nobody on the box |

Don't commit the downloaded raw NOAA files (`data/ndbc/`, `hurdat2.txt`); the notebook re-downloads them.

**The risk number is a score, not a calibrated probability** (because of balanced class weights). On slides say "risk score". It never fires alarms; the ESP32's pressure-drop rule does.

**We are not uploading anything to Hugging Face.** The model, data, and notebook stay in our repo. Our Hugging Face story is the models we *use* (§15).

### 8.4 Which sensors feed what (the tree only uses the barometer)
| Sensor | Used by the storm risk tree? | What it's used for |
|---|---|---|
| **Barometer** (pressure) | **Yes, the only input.** All 6 features come from its hourly history | Tree risk score, storm phase (falling / eye / recovering), ESP32 storm alarm, dashboard graph |
| **Temperature** (same Grove module) | No | ESP32 heat alarm; heat index if humidity exists; sea-level conversion uses it (minor) |
| **Humidity** (only if the module is a BME280) | No | Heat index for the heat warning; shown on the dashboard |
| **Water / moisture** (floor sensor) | No | ESP32 flood alarm |
| **Air quality** | No | ESP32 air alarm (fumes, not certified CO) |
| **Wall power** | No | Outage log, "on battery" status |

The tree was trained only on pressure because that's the only measurement NOAA buoys share with our box over 20 years. Other sensors go to their own fixed alarm rules.

---

## 9. Models to download before the event (to the Pi)
| What | Command / source | Size (approx.) |
|---|---|---|
| **LLM (primary)** | `ollama pull llama3.2:1b` (Meta Llama 3.2 1B Instruct, 4-bit) | ~1.3 GB |
| **LLM (fallback)** | `ollama pull qwen3:0.6b` (Qwen3 0.6B). **Turn thinking off** (`think=False` in the Ollama call) or it writes long hidden reasoning and gets slow | ~0.5 GB |
| Embeddings | `ollama pull nomic-embed-text` | ~270 MB |
| Speech-to-text | Vosk small English model (alphacephei.com/vosk/models) | ~40 MB |
| Text-to-speech | One Piper voice (`.onnx` + `.json`) | ~60 MB |

Ollama can also pull GGUF models straight from the Hub: `ollama pull hf.co/<user>/<repo>`. Check the exact tags on ollama.com/library and huggingface.co before the event.

**Model choice in code:** `ai/agent/config.py` → `LLM_MODEL = "llama3.2:1b"`, `LLM_FALLBACK = "qwen3:0.6b"`, `EMBED_MODEL = "nomic-embed-text"`. Use the fallback if the primary isn't installed, or if a P3 answer takes longer than `LLM_TIMEOUT_S` (start 15 s). If both fail, answer with the top guide chunk as-is ("Here's what the guide says: …") so the user always gets something.

---

## 10. Keeping it light on the Pi
- One LLM loaded and kept warm (`keep_alive`), no reloads per question.
- Prompts ≤ ~400 words, answers ≤ 120 tokens, temperature 0.2.
- P0–P2 never call the LLM.
- Embeddings: one per question. Guide index built once.
- No frameworks: dependencies are `ollama`, `chromadb` (or numpy), `vosk`, `piper-tts`, `fastapi`.
- Log timings per step in `agent_traces.timings_json`; if P3 takes > 6 s on the Pi, try the next smaller model or trim the passages to 2.

---

## 11. LLM benchmark (confirm the choice)
Default is Llama 3.2 1B; this test confirms it's fast enough and stays on the passages, and checks the Qwen3 0.6B fallback. Run the same 20 questions on the **real Pi** through both. Record seconds to the full answer and whether it stayed on the passages.

1. Is the chicken in the fridge safe after 6 hours without power?
2. Can I run a generator in the garage with the door open?
3. Water is coming under the door, what should I do?
4. How do I make water safe to drink?
5. My phone is at 10%, how do I save battery?
6. Is it safe to walk through flood water?
7. Someone has a deep cut, what do I do?
8. How do I keep insulin cool without power?
9. What should I do with my dog during the storm?
10. When should we evacuate?
11. A power line is down in the street, what now?
12. It's 95 degrees inside, how do we stay cool?
13. Should I open windows to equalize pressure?
14. How do I know if the storm is really over?
15. The air alarm went off but I don't smell anything.
16. Can I use a gas stove to heat the house?
17. What's in a basic emergency kit?
18. How do I turn off the electricity at the breaker safely?
19. What do I do after the storm passes?
20. What's the capital of France? (should say it can only help with emergency topics)

---

## 12. Step-by-step to-do (CS 2)

### Done ✅
- [x] Run `storm_risk_colab.ipynb` on real NOAA data; tree chosen (§8.1)
- [x] Knowledge pack: 15 sourced files, 71 chunks (§6.1)

### Next (this week)
- [ ] Put the zip contents into the repo and send files to teammates (§8.3).
- [ ] Tell CS 1 about the sea-level conversion + calibration offset (§8.2); agree on `ALTITUDE_M`.
- [ ] Add the ML slide: tree picture, comparison chart, Nicole example, pitch line (§8.1).
- [ ] Confirm hackathon rules on pre-written code.

### Before the event
- [ ] Write `ai/knowledge/canned.json` (40–60 entries, built from the knowledge pack).
- [ ] Write `ai/scripts/build_index.py`; build the index (expect 71 chunks); test 10 searches by hand (do the right chunks come back?).
- [ ] Write `templates.py` (P0 table, §5.1) and share LCD strings with EE 1.
- [ ] Pull the models onto the Pi (§9). Run the benchmark (§11) to confirm Llama 3.2 1B (fallback Qwen3 0.6B).
- [ ] Write `rag.py`, `canned.py`, then test in a terminal.

### At the event
- [ ] `tools.py` against the fake ESP32 / box state.
- [ ] `router.py` + `prompt.py` + `agent.py`: `answer()` working in a terminal for all 4 paths.
- [ ] `explain_event()` wired into box-service (proactive alarms + phase changes).
- [ ] `trace.py`: every answer writes an `agent_traces` row (route, tools, chunks, sensors, timings).
- [ ] Hook into `/api/ask` and the voice pipeline (with CS 1).
- [ ] Replay test: storm risk climbs before the ESP32 storm alarm → proactive storm text → "possible eye" → "recovering".
- [ ] Rehearse: 3 questions judges might ask; check the trace panel shows the route.

---

## 13. Repo files for the AI layer (everything CS 2 owns is in `ai/`)
```
stormbox/
  CLAUDE.md  PROJECT_PLAN.md  AGENT_PLAN.md
  ai/
    ml/                       storm risk model, all in one place
      storm_risk_colab.ipynb
      storm_risk.json  metrics.json
      storm_risk_runtime.py   pure-Python predict + explain
      storm_job.py            §8.2: clean → hourly → sea level → predict (box-service calls it)
      replay/  *.csv  index.json
      figures/ decision_tree.png  model_comparison.png  test_storm_example.png
      backup/  *.joblib
    knowledge/                *.md (15 guides)  _README.md  canned.json
    agent/
      config.py     model names, timeouts, ALTITUDE_M, CAL_OFFSET_HPA
      agent.py      answer(), explain_event()
      router.py     keyword table → tools/topic/path
      tools.py      the 8 tools
      canned.py     canned matching
      rag.py        guide search (Chroma or numpy)
      prompt.py     builds the ≤400-word prompt
      templates.py  P0 + P2 sentences, LCD strings
      trace.py      writes agent_traces
    scripts/        build_index.py  ask.py  benchmark_llm.py
    tests/
    data/           chroma/ (generated by build_index.py; don't commit)
    requirements.txt
  pi/  dashboard/  esp32/     (CS 1 and EE, unchanged)
```

---

## 14. Changes vs `PROJECT_PLAN.md` (update it when the team agrees)
- §6.3: proactive alarm messages use **templates**, not the LLM (faster, safer). Routes in `agent_traces` become `proactive|canned|tool|rag`.
- §6.5 / §7.3: the model ships as `storm_risk.json` + a pure-Python runtime (joblib files kept as backup only); the box converts barometer readings to sea-level pressure first.
- §6.5: the decision tree won (96.8% recall, 20.5 h lead, 0.35 false alarms/week vs logistic regression's 100% / 24 h / 1.69). Say "risk score", not "% chance".
- §6.4 / §7.2: knowledge chunks carry a per-chunk `source` (from each section's `Source:` line) and `heading` in Chroma metadata; topics are `flood, storm, air, heat, power, food, water, evacuation, after, first-aid, pets, medical, communication, kit, stormbox`.
- §13: all CS 2 files live in `ai/` (model, replays, figures in `ai/ml/`; agent in `ai/agent/`; guides in `ai/knowledge/`) instead of `models/`, `replay/`, `knowledge/`, `pi/agent/`.
- §6.6: LLM = **Llama 3.2 1B** (primary), **Qwen3 0.6B** (fallback, thinking off).
- §17: **No uploads to Hugging Face.** The Hugging Face story is the open models we run on the box (§15).

---

## 15. Hugging Face: what we use (no uploads)

Judged on "meaningful use of the Hugging Face ecosystem". Our answer: **every AI model on the box is an open model from the Hub, running fully offline.**

| Job | Model | Hub page |
|---|---|---|
| Answer wording (LLM) | Llama 3.2 1B Instruct | `meta-llama/Llama-3.2-1B-Instruct` |
| LLM fallback | Qwen3 0.6B | `Qwen/Qwen3-0.6B` |
| RAG embeddings | nomic-embed-text v1.5 | `nomic-ai/nomic-embed-text-v1.5` |
| Text-to-speech | Piper voice | `rhasspy/piper-voices` |
| Speech-to-text (stretch) | Whisper tiny/base via faster-whisper | `Systran/faster-whisper-tiny` / `-base` |

(Ollama's `llama3.2:1b`, `qwen3:0.6b`, and `nomic-embed-text` are packaged versions of these same models.)

**Stretch ideas, in order:**
1. **Spanish:** Llama 3.2 officially supports Spanish, Piper has Spanish voices, and Whisper understands Spanish. Add one rule to the prompt ("answer in the language of the question"). Only switch STT from Vosk to Whisper if Whisper is fast enough on the Pi; otherwise Spanish works through dashboard chat only.
2. **Fine-tune (uses the $20 credits, stays local):** LoRA fine-tune Qwen3 0.6B on Q&A generated from the knowledge pack, for **style** (2–3 calm sentences, cite the source, say "I don't know"). Keep RAG for facts. Export GGUF and load it in Ollama from a local file. Only after the main agent works.

**Pitch line:** "Four open models from the Hugging Face Hub, a 1-billion-parameter LLM, embeddings, and speech, all running on a Raspberry Pi with no internet."
