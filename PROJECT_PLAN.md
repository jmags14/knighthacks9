# StormBox — Offline Disaster Box (working name)

> **One line:** When a disaster knocks out power and internet, StormBox keeps watching your home, warns you about danger, tells you what to do, and helps your supplies last. No internet, no cloud, no cell service.

> **Sensor update (2026-10-10):** BME280 confirmed (pressure + temperature + **humidity**). Air quality sensor **removed** — no air alarm. Rain level (inches) and wind speed (mph) sensors added. See updated §3.3 alarm table and §5 serial format.

This is the **single source of truth** for the team (2 EE, 2 CS/AI). If you are an AI assistant reading this: follow the decisions here. Don't add features outside the MVP unless asked, and treat the **serial protocol (§5)** and the **schemas (§7)** as contracts between teammates.

---

## 0. TL;DR

| | |
|---|---|
| **Goal** | Win the main prize, plus the NextEra Energy and Hugging Face sponsor challenges |
| **Pitch story** | Built for Florida hurricanes first; works for any long outage |
| **MVP** | One box: ESP32 safety layer + Raspberry Pi AI layer + phone/laptop dashboard, fully offline |
| **Stretch (in order)** | 1. GPS  2. LoRa radio (box-to-box SOS, needs 2 boxes)  3. Wake word  4. 3D house view |
| **Out of scope** | Camera/vision AI, cloud anything, calling 911, claiming CO detection |

**MVP is done when all five of these work on battery with no internet:**
1. Sensors → ESP32 alarms (LCD, buzzer, light) work **even if the Pi is unplugged**.
2. Push-to-talk voice Q&A answers from the offline guides.
3. Storm replay of a real NOAA hurricane triggers the whole chain.
4. The dashboard (family + engineer mode) is served by the Pi over its own Wi-Fi.
5. The storm risk model runs live and is shown in engineer mode.

---

## 1. Purpose

**Problem:** After a hurricane, power and cell service can be out for days. Phones die, towers are down, and nobody is telling you what's happening. That is exactly when people need information most.

**The box's three jobs:** **Warn** (watch the home, even while you sleep) · **Guide** (tell you what to do, answer questions) · **Help you last** (battery, water, supplies).

| Question a family has | How the box answers |
|---|---|
| Is the storm over, or is this the eye? | Pressure history → storm phase (falling / possible eye / recovering) |
| Is water coming in while we sleep? | Moisture sensor on the floor → alarm |
| Is the air getting bad (generator fumes)? | Air quality sensor → alarm |
| Is it dangerously hot with no AC? | Temperature (+ humidity if the sensor has it) → heat warning |
| When did the power go out? | Wall-power detection → outage log |
| What do I do right now? | Offline agent (voice + chat) |
| How long can we last? | Battery hours + water calculator |

**Honesty rules (say these exactly in the pitch):**
- The air sensor detects that *something* is in the air. It is **not** a carbon monoxide detector; a real product would add a certified CO sensor.
- The box does **not** replace 911 when service exists.
- Storm replay is **real NOAA data played back**, clearly labeled "REPLAY".

---

## 2. System overview

```
[Grove sensors] ─┐
[LCD+backlight] ─┤                 USB serial (JSON lines, 115200)
[Buzzer]        ─┼── ESP32 ◄──────────────────────────────► Raspberry Pi 4
[Button]        ─┤   safety layer: rules + alarms          AI layer:
[Wall-power pin]─┘   (works with Pi dead)                   box service, agent, model,
                                                            voice, web server, databases
                                                               │            │
                                                     speaker + mic     own Wi-Fi hotspot
                                                                            │
                                                              phones / laptop → dashboard
Power bank ──► Pi ──(USB)──► ESP32
```

**The loop (use this in the pitch and README):**
**Sense** (ESP32 + sensors) → **Understand** (alarm rules + storm risk model) → **Explain & act** (agent + RAG + tools) → **Show** (LCD, buzzer, voice, dashboard).

**Golden rule:** Alarms come from the ESP32's fixed rules, **never** from AI. The AI explains and advises.

---

## 3. Hardware (EE)

### 3.1 Parts

| Priority | Part | Job | Notes |
|---|---|---|---|
| 1 | Raspberry Pi 4B kit | AI layer | Check RAM (4 GB works, 8 GB is better). Needs a heatsink/fan: the LLM makes it throttle |
| 1 | ESP32 dev board (+1 spare) | Safety layer | Analog sensors on **ADC1 pins (GPIO 32–39)** |
| 1 | Grove temp & barometer | Pressure, temp | Check whether it is BMP280 (no humidity) or BME280 (has humidity) |
| 1 | Grove moisture sensor | Flood | On a **long cable** to the floor; the box stays on the table |
| 1 | Grove air quality sensor | Fumes | Check its datasheet: it may need 5V |
| 1 | Grove RGB backlight LCD (16×2) | Status + light | **16 characters × 2 lines.** Check the version for 3.3V vs 5V and I2C level safety |
| 1 | Grove buzzer | Alarm sound | Alarms use the buzzer, not the speaker |
| 1 | Grove button | Talk / silence alarm | |
| 1 | Grove-to-female-jumper cables | Wiring | |
| 1 | Speaker (JBL Go 3) via 3.5 mm jack | Agent voice | Use the jack, not Bluetooth |
| 1 | Webcam (mic only) or USB mini mic | Voice input | Buy the USB mic as a backup |
| 1 | Power bank, 5V 3A, ~20,000 mAh, pass-through | Runs everything | Test the Pi under load: weak banks cause reboots |
| 1 | **Known data-capable** USB cable | ESP32 ↔ Pi | Many cables only charge |
| 1 | Project case | Splash-resistant, not waterproof | |
| 1 | USB power meter (~$10) | Measure real power draw | Feeds the battery estimate |
| 1 | DS3231 RTC module (~$5), recommended | Correct time offline | The Pi has no clock battery; see §12 |
| 2 | GPS module (NEO-6M / M8N) | Location + accurate time | Stretch |
| 3 | 2× LoRa boards | Box-to-box SOS | Stretch, needs a 2nd box |

### 3.2 Wiring notes
- Power Grove sensors from **3.3V** where they support it, so signals stay safe for the ESP32.
- **Wall-power detection:** feed the wall adapter's 5V through a voltage divider to an ESP32 input. HIGH = on wall power, LOW = on battery. Needed for the outage log and the "on battery" status.
- Document all pins in `docs/wiring.md` with photos.

### 3.3 ESP32 firmware responsibilities
- Read all sensors every 1 s and send one JSON line (§5).
- Run the **hard alarm rules** (starting values; tune with real readings):

| Alarm | Starting rule |
|---|---|
| `storm` | Pressure drop ≥ 3 hPa over 3 h (warning), ≥ 6 hPa over 3 h (severe) |
| `flood` | Moisture reading above a calibrated "wet" threshold for 3 s |
| `heat` | Heat index (temperature + humidity) above a set limit for 10 min |
| `wind` | Wind speed ≥ 39 mph → **warn**; ≥ 74 mph → **alarm**; hysteresis: clear warn below 35 mph, step alarm→warn below 70 mph |

- Use **hysteresis** so alarms don't flicker on and off at the threshold.
- Drive the LCD, buzzer, and backlight **directly**.
- Button: **short press** = talk (tell the Pi); **long press** = silence the current alarm.
- Watchdog: if no heartbeat from the Pi for 10 s, the LCD shows `Assistant off` / `Alarms active`.
- **Replay mode:** when on, use pressure values from the Pi instead of the barometer, and use the **recorded timestamps** for the 3-hour rule. Everything else stays live.

### 3.4 Lights & sound

| State | Backlight | Buzzer | Speaker (Pi) |
|---|---|---|---|
| All clear | Dim green | Off | — |
| Night mode | Dim red (easy on the eyes) | Off | — |
| Warning | Amber | Short chirp | Spoken explanation |
| Alarm | **Flashing red** (visual alarm for deaf/hard of hearing) | Loud pattern until silenced | Spoken instructions |
| Pi offline | Normal colors | Unchanged | — |

---

## 4. Power & battery
- The power bank has no data line, so **estimate** battery: `hours_left = (usable_Wh − used_Wh) / avg_draw_W`. Measure `avg_draw_W` with the USB meter.
- **Overnight test (EE 2):** run the full box unplugged until it dies. That gives a real "runs X hours" number for the pitch.
- Stretch: a UPS HAT that reports the exact battery level.

---

## 5. ESP32 ↔ Pi serial protocol (CONTRACT)

USB serial, 115200 baud, **one JSON object per line**.

**ESP32 → Pi, every 1 s:**
```json
{"t":"r","ms":123456,"p":1008.4,"temp":29.1,"hum":72.3,"water":412,"rain":0.0,"wind":8.2,
 "wall":1,"src":"live","alarms":["flood"]}
```
`hum` = humidity %; `rain` = Rain (in); `wind` = wind speed in mph.
**ESP32 → Pi, events:**
```json
{"t":"alarm","id":"flood","level":"alarm","state":"on"}
{"t":"btn","press":"short"}
{"t":"btn","press":"long"}
```
**Pi → ESP32:**
```json
{"cmd":"hb"}                                   // heartbeat, every 2 s
{"cmd":"lcd","l1":"Water detected","l2":"Move up now"}   // ≤16 chars per line
{"cmd":"light","mode":"normal|night|warn|alarm"}
{"cmd":"buzz","pattern":"chirp|alarm|off"}
{"cmd":"replay","on":true}
{"cmd":"rp","p":1003.2,"ts":"2022-09-28T14:00:00Z"}     // one replay sample
{"cmd":"ack","id":"flood"}                     // silence alarm from dashboard
```
**Week-one move:** CS builds a **fake ESP32** script that prints these lines, so all four people can work in parallel from hour one.

---

## 6. Pi software (CS/AI)

### 6.1 Services (all auto-start on boot with `systemd`; no laptop needed)
| Service | Job |
|---|---|
| `box-service` | Reads serial, keeps one shared **box state**, writes SQLite, sends LCD/buzz commands, runs the storm phase rules + risk model, runs replay |
| `agent` | Answers questions (voice, chat, proactive alarms) |
| `voice` | Push-to-talk → speech-to-text → agent → text-to-speech |
| `web` | FastAPI: REST API + WebSocket + serves the built React app |
| `ollama` | Runs the LLM and the embedding model |
| Hotspot | Pi broadcasts its own Wi-Fi (`StormBox`). No internet behind it |

### 6.2 The four "brains" (which part is what)

| Part | Type | Runs on | Decides | Example |
|---|---|---|---|---|
| **Alarm rules** | Fixed if/then | ESP32 | Whether an alarm fires | "Water above threshold → flood alarm" |
| **Storm phase rules** | Fixed rules over history | Pi | Falling / possible eye / recovering | "Near the lowest pressure + was falling fast + now flat → possible eye" |
| **Storm risk model** | Trained classifier (scikit-learn) | Pi | A risk % shown as early warning; **never fires alarms** | "Storm risk 78%" |
| **RAG** | Search over the guides | Pi | Which guide passages are relevant | Top 3 chunks about floods |
| **LLM** | Small open-weight model | Pi (Ollama) | How to *word* the answer | 2–3 sentences, grounded in the chunks + sensor state |

### 6.3 Agent: one central agent with tools
One agent handles voice, dashboard chat, and **proactive** messages (when an alarm fires, it explains what to do without being asked).

**Tools:** `get_sensors()` · `get_storm_status()` (phase + risk %) · `search_guides(q)` · `water_calculator(people, days)` · `get_battery()` · `log_checkin(name, status)` · `get_outages()`

**Flow per question:**
1. **Canned check:** if the question closely matches a pre-written answer (similarity ≥ threshold, ~0.8 to start), return it instantly.
2. **Router (plain Python, not the LLM):** keywords/similarity pick the tools. Small models are unreliable at choosing tools themselves.
3. **Run the tools** (milliseconds).
4. **Prompt the LLM** with: system rules + sensor snapshot + tool results + top 3 chunks + question. **Keep input under ~400 words** (prompt length is what makes the Pi slow).
5. **Answer** in 2–3 sentences → speak + show it with sources.
6. **Log** everything to `agent_traces` (this powers the engineer-mode trace panel).

**System prompt rules:** answer only from the provided passages and readings · say "I don't know, here's the closest guide" instead of guessing · sensor numbers are ground truth · never contradict an active alarm · short, calm, actionable · tell people to call 911 if service exists.

### 6.4 RAG (is it "too powerful"? No.)
RAG is lightweight: it's **search + pasting text into the prompt**. It is what lets a small model stay accurate. The slow part on a Pi is the LLM, not the search.
- **Sources:** Ready.gov (public), FEMA, NWS, and Red Cross guidance **paraphrased in our own words** with credit.
- **Chunking:** about 100–200 words per chunk, tagged with metadata (§7.2).
- **Embeddings:** `nomic-embed-text` via Ollama (or `all-MiniLM-L6-v2`); store in ChromaDB.
- **Retrieval:** top 3 chunks, filtered by topic when the router knows it (e.g. an active flood alarm → `topic=flood`).

### 6.5 Storm risk model: logistic regression vs decision tree
**Data:** NOAA NDBC historical station/buoy pressure (Florida stations) + HURDAT2 hurricane tracks.
**Label:** `1` if a tropical storm or hurricane center passes within **150 km** of the station in the **next 12 h**, else `0` (starting definition).
**Features (same for both models):** current pressure; change over 1 h, 3 h, 6 h; minimum over the last 6 h; rate of change of the 3 h change.
**Split by storm, not by row:** train on older storms, test on storms the model never saw (e.g. Ian 2022, Idalia 2023, Milton 2024).

**Train both:**
- **Logistic regression:** simple, fast, outputs a probability directly.
- **Decision tree (max depth 3–4):** can be **drawn**, so judges see exactly why it said "high risk".

**How to pick:**
1. For each model, choose the threshold where false alarms in normal weather stay low (target: ≤ 1 per week of data).
2. At that threshold, compare **recall** (storms caught) and **lead time** (hours of warning before closest approach).
3. Better recall + lead time wins. **If they're within ~5 points, ship the tree** (explainability).
4. Put the comparison on a slide. That *is* the ML story.

Export with `joblib`; it runs on the Pi in milliseconds. ⚠️ Check the hackathon rules on pre-event work: downloading/exploring data is usually fine, but code may need to be written at the event.

### 6.6 LLM choice
Cloud models (Claude, Gemini, Grok, GPT) **can't run offline on the box**. Use a small **open-weight** model from the Hugging Face Hub / Ollama library, ~1B parameters, 4-bit.
**Benchmark before the event:** run the same 20 test questions on the actual Pi through **Llama 3.2 1B**, **Qwen2.5 1.5B**, and a **small Gemma**. Pick by speed first, then staying on-topic. Default: Llama 3.2 1B until the benchmark says otherwise. Check for newer versions first.

### 6.7 Talking to the box
- **MVP: push-to-talk.** Press the button → speak → release (or stop after a silence). Reliable in a loud demo room, no false triggers, low CPU, and private.
- **Stretch: wake word** ("Hey StormBox") with **openWakeWord** (offline, open source). Only after the MVP works.
- **STT:** Vosk small English model. **TTS:** Piper. Both offline.
- The dashboard chat uses the same agent, so you can **type quietly at night**.

### 6.8 Storm replay (engineer mode only)
1. The dashboard sends `POST /api/replay/start {file, speed}`.
2. The Pi sends `{"cmd":"replay","on":true}`, then streams rows from `replay/<storm>.csv` as `rp` commands at the chosen speed.
3. The ESP32 treats each value as its barometer reading and runs the **same** rules using the recorded timestamps → real LCD, buzzer, voice, and dashboard reactions.
4. At the end: `replay off`, and the barometer is back. Water and air sensors stay live the whole time.
5. Speed: 1 reading = 10 min of storm; at 4 readings/s, an 18 h storm takes ~30 s.

---

## 7. Data

### 7.1 SQLite (`data/box.db`, one file)
```sql
CREATE TABLE readings (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  pressure REAL, temp REAL, humidity REAL, water INTEGER, air INTEGER,
  wall_power INTEGER, source TEXT CHECK(source IN ('live','replay'))
);
CREATE TABLE alarms (
  id INTEGER PRIMARY KEY, type TEXT, level TEXT,           -- storm|flood|air|heat ; warn|alarm
  ts_start TEXT, ts_end TEXT, source TEXT, explanation TEXT -- explanation = agent's text
);
CREATE TABLE storm_status (
  ts TEXT PRIMARY KEY, phase TEXT, risk REAL, model_version TEXT
);
CREATE TABLE power_events (id INTEGER PRIMARY KEY, ts TEXT, event TEXT); -- lost|restored
CREATE TABLE household (id INTEGER PRIMARY KEY CHECK(id=1), people INTEGER, pets INTEGER);
CREATE TABLE supplies (id INTEGER PRIMARY KEY, item TEXT, quantity REAL, unit TEXT, updated_ts TEXT);
CREATE TABLE checkins (id INTEGER PRIMARY KEY, ts TEXT, name TEXT, status TEXT, note TEXT);
CREATE TABLE agent_traces (
  id INTEGER PRIMARY KEY, ts TEXT, channel TEXT,            -- voice|chat|proactive
  question TEXT, route TEXT,                                -- canned|rag
  tools_json TEXT, chunks_json TEXT, sensors_json TEXT,
  answer TEXT, timings_json TEXT                            -- ms per step
);
```

### 7.2 ChromaDB (`data/chroma/`), collection `guides`
```json
{"id":"flood-03","document":"<100–200 words>",
 "metadata":{"source":"Ready.gov","title":"Floods","topic":"flood","url":"https://www.ready.gov/floods"}}
```

### 7.3 Files
- `knowledge/*.md`: guide text (source of truth for the RAG; rebuild the index with `scripts/build_index.py`)
- `knowledge/canned.json`: `[{"id","questions":[...],"answer","source"}]`, 40–60 entries
- `replay/*.csv`: `ts,pressure_hpa` from NOAA
- `models/storm_risk.joblib` + `models/metrics.json`

---

## 8. API (FastAPI on the Pi, `http://192.168.4.1`)
| Method | Path | Purpose |
|---|---|---|
| WS | `/ws` | Pushes box state on every change |
| GET | `/api/state` | Current snapshot |
| GET | `/api/history?minutes=` | Readings for graphs |
| POST | `/api/ask` | `{text}` → `{answer, sources, trace_id}` |
| GET | `/api/traces` | Agent traces (engineer mode) |
| POST | `/api/replay/start` · `/api/replay/stop` | Storm replay |
| POST | `/api/alarms/ack` | Silence an alarm |
| GET/POST | `/api/supplies` · `/api/checkins` · `/api/household` | Family data |
| GET | `/api/model` | Metrics, tree structure, current path |

---

## 9. Frontend
**React + Vite**, built to static files and served by FastAPI. **Everything bundled locally: no CDNs, no Google Fonts** (offline). Live data over the WebSocket. A **QR code sticker** on the box joins the Wi-Fi and opens the page.

**Family mode** (phone-first, dark, big text):
| Screen | Content |
|---|---|
| Home | One big status card (e.g. "Possible eye, stay inside"), four readings, battery hours, "Is the storm over?" |
| Ask | Chat with the agent, with a source line under each answer |
| Supplies | Water calculator, checklist, family check-ins |
| Guides | Browse the offline guides directly (no AI) |

**Engineer mode** (laptop-first, toggle):
| Panel | Proves |
|---|---|
| Live sensor graphs | Hardware works in real time |
| System health | ESP32 link/heartbeat, **"Internet: unreachable"** (live check), wall power, battery, Pi temperature |
| Storm replay | Play, speed, big "REPLAY" banner |
| Storm risk model | Live %, **decision tree with the current path highlighted**, test metrics, LR vs tree comparison |
| Agent trace | Tools called, chunks retrieved, sensor snapshot, timing per step |
| Alarm timeline + outage log | Every event with the agent's explanation |

**Build order:** Home (fake data) → health + graphs → Ask → replay + trace → model panel → Supplies/Guides → (stretch) 3D house view, GPS map.
**Figma:** design both modes before building (counts toward a Figma prize if there is one).

---

## 10. Tech stack (lightweight on purpose)
| Layer | Choice |
|---|---|
| ESP32 | Arduino C++, ArduinoJson, Grove/Adafruit sensor libraries |
| Pi OS | Raspberry Pi OS Lite 64-bit, systemd, NetworkManager hotspot |
| Backend | Python 3, FastAPI + uvicorn, pyserial, sqlite3 |
| AI | Ollama (LLM + `nomic-embed-text`), ChromaDB, scikit-learn + joblib |
| Voice | Vosk (STT), Piper (TTS); stretch: openWakeWord |
| Frontend | React + Vite, Chart.js or Recharts (bundled) |
| Model training | pandas + scikit-learn on a laptop (or a Databricks notebook) |
| Repo | One GitHub repo (§13) |

---

## 11. Demo (≈3 minutes, one story: "a night during a hurricane")
| # | Moment | Real or simulated | Proves |
|---|---|---|---|
| 0 | Hook: FCC stat on Florida cell sites down after a recent hurricane (cite it) | — | Problem |
| 1 | **Offline proof:** a judge joins `StormBox` Wi-Fi, and their phone says "No internet" while the dashboard works. Unplug wall power → "On battery" + outage logged | Real | Offline + power |
| 2 | **Storm replay** of a real NOAA hurricane: risk % climbs before the hard alarm → alarm → **eye: "calm is temporary, stay inside"** → recovering | Real system, replayed input | ML + safety + purpose |
| 3 | **Flood:** dip the sensor in a cup → buzzer, red flash, the box speaks on its own | Real | Sensing + proactive agent |
| 4 | **Air:** hand sanitizer vapor near the air sensor | Real | Sensing |
| 5 | **Judge asks anything** → spoken answer; the trace panel shows tools, sources, and sensor context | Real | We understand our AI |
| 6 | **Pull the Pi's USB cable** → AI offline, the ESP32 keeps the alarms running | Real | System design |
| — | Close: "The AI makes it smart. The hardware makes it safe." | | |

**Backup:** record a full clean run as a video. Pre-film the **elevator clip** (pressure drops ~1 hPa per 8 m going up) to prove the barometer is real.

---

## 12. Team roles
| Person | Owns |
|---|---|
| **EE 1** | ESP32: sensor wiring, alarm rules + hysteresis, LCD/backlight/buzzer/button, watchdog, replay mode |
| **EE 2** | Power + build: power bank, wall-power detection, battery measurement + overnight test, case, speaker/mic, RTC, long flood cable; then GPS → LoRa |
| **CS 1** | Pi core: box service, serial link (+ fake ESP32), SQLite, FastAPI/WebSocket, hotspot, voice pipeline, frontend |
| **CS 2** | AI: knowledge pack + canned answers, RAG index, agent + tools + prompts, LLM benchmark, storm risk model (data → train → compare), replay data |

Shared: everyone writes their part of the README; everyone rehearses the demo.

---

## 13. Repo layout
```
stormbox/
  esp32/         firmware (EE 1)
  pi/            box_service/, agent/, voice/, web/ (CS)
  dashboard/     React app (CS 1)
  knowledge/     guides + canned.json (CS 2)
  ml/            notebooks, training script, metrics (CS 2)
  models/        storm_risk.joblib
  replay/        NOAA storm CSVs
  scripts/       build_index.py, fake_esp32.py, setup_pi.sh
  docs/          block diagram, wiring.md + photos, demo video, slides
  README.md
```

---

## 14. Timeline (at the event)
| When | Milestone |
|---|---|
| Hour 1 | Repo up, Pi booting with SSH, fake ESP32 streaming, everyone building against it |
| First quarter | Each layer works alone: firmware rules, box service, agent answering in a terminal, model trained, dashboard Home |
| **Midpoint** | **Integration checkpoint:** real sensors → Pi → dashboard → voice. Fix this before adding anything |
| Third quarter | Replay, proactive alarms, trace panel, model panel |
| Final stretch | **Feature freeze.** Rehearse the demo repeatedly, record the backup video, write Devpost |

---

## 15. Before the event checklist
- [ ] Confirm hackathon rules (pre-written code? hardware rules?) and sponsor challenge prompts
- [ ] Lock: one-liner, MVP, roles, **serial protocol (§5)**
- [ ] Order: spare ESP32, jumper cables, data USB cable, USB mic, USB power meter, RTC, case (GPS if confident)
- [ ] Flash the Pi SD card (64-bit Lite), confirm SSH; test the power bank under load
- [ ] Download onto the Pi: candidate LLMs, the embedding model, Vosk model, Piper voice
- [ ] Run the LLM benchmark on the Pi; pick the model
- [ ] Download NOAA data (station pressure + HURDAT2) and 1–2 replay storms
- [ ] Collect guide sources (Ready.gov, NWS, FEMA, Red Cross) + the FCC outage stat for the hook
- [ ] Figma wireframes for both modes

---

## 16. Risks & gotchas
| Risk | Mitigation |
|---|---|
| The Pi 4 is slow for the LLM | Canned answers first, short prompts, 2–3 sentence answers, cooling |
| **The Pi has no clock offline** (wrong timestamps after reboot) | DS3231 RTC; fallback: the dashboard sends the phone's time to the Pi on connect; GPS time (stretch) |
| Grove voltage vs the ESP32's 3.3V | Check every module's datasheet; level-shift where needed |
| Charge-only USB cable | Label the known-good cable |
| CDN assets break offline | Bundle everything in the React build |
| Integration hell | Contract-first protocol, fake ESP32, midpoint integration checkpoint |
| Demo hardware failure | Backup video, spare ESP32, rehearse the cable pull |
| Overclaiming | Follow the honesty rules in §1 |

---

## 17. Sponsors to target
- **NextEra Energy (primary):** they own FPL. Pitch emergency preparedness and recovery during outages; the **outage log** speaks their language.
- **Hugging Face:** models come from the Hub; publish our storm-risk model + cleaned NOAA dataset there.
- **Maybe:** Databricks (if the prompt is data/ML: train the model there), Figma (if there's a design prize).
- **Skip:** forcing in sponsors that don't fit.

---

## 18. Pitch lines
- "Keeps working when the internet and power don't."
- "Other offline AI tools answer questions. Ours notices the danger first, then tells you what to do."
- "The AI makes it smart. The hardware makes it safe."
- Answer to *"Why not just use your phone?"*: your phone dies in a day, the towers are down, and it can't sense your house.
