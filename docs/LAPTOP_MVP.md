# StormBox: Laptop Pivot MVP

> **Pivot:** The Pi is out. The **box** (ESP32 + sensors + LCD + buzzer + battery) is now a **wireless sensor node**. The **laptop** is the **edge AI hub** across the room. They talk over **MQTT** (Mosquitto on the laptop) on the box's own Wi-Fi, with no internet anywhere.
>
> This replaces the Pi parts of `PROJECT_PLAN.md`. Everything else (purpose, agent, RAG, storm risk model, demo story) stays.

---

## 1. New architecture

```
        THE BOX (battery powered)                         THE HUB (laptop)
 ┌──────────────────────────────────┐   Wi-Fi "StormBox"   ┌───────────────────────────┐
 │ Sensors → ESP32 → LCD/buzzer/RGB │◄────── MQTT ────────►│ Mosquitto (broker)        │
 │ alarm rules run HERE             │   (no internet)      │ box_service (paho-mqtt)   │
 │ ESP32 also broadcasts the Wi-Fi  │                      │ agent + RAG + risk model  │
 │ button = push-to-talk            │                      │ FastAPI + dashboard       │
 └──────────────────────────────────┘                      │ mic + speaker (voice)     │
                                                           └───────────────────────────┘
                                   judges' phones join "StormBox" → laptop dashboard
```

**Golden rule (unchanged):** alarms come from the ESP32's rules. If the hub dies, the box still alarms.

---

## 2. MVP (must work by demo, in priority order)

| # | Feature | Owner | Done when |
|---|---|---|---|
| 1 | ESP32 reads sensors, runs alarm rules, drives LCD/buzzer/RGB **with no hub** | EE 1 | Cup of water → buzzer + red flash, laptop off |
| 2 | ESP32 Wi-Fi access point `StormBox` | EE 1 | Laptop + a phone can join it |
| 3 | MQTT link: box publishes readings/events, hub sends commands | EE 1 + CS 1 | Readings show in `box_service` logs |
| 4 | Hub watchdog: LCD shows `Hub offline` when the laptop stops | EE 1 | Kill the server → LCD changes within 10 s |
| 5 | Dashboard (family + engineer) served from laptop, live over WebSocket | CS 1 | A phone on `StormBox` sees live readings |
| 6 | Agent answers from offline guides (canned → RAG → small LLM) | CS 2 | Dashboard chat answers with a source |
| 7 | **Push-to-talk on the box button** → voice answer from the speaker | CS 1 + CS 2 | Press, ask, hear the answer, LCD shows status |
| 8 | Storm replay of a real NOAA storm | CS 1 + EE 1 | Replay trips the real storm alarm on the box |
| 9 | Storm risk model live in engineer mode | CS 2 | Risk % climbs during replay |

**Cut line:** if time runs out, cut from the bottom. 1–6 is a complete project. 7–8 are what make it impressive.
**Stretch:** proactive spoken alarm explanations, decision-tree panel, GPS, LoRa.

---

## 3. MQTT contract (replaces the serial protocol)

Payloads stay the **same JSON** as `PROJECT_PLAN.md` §5. Only the transport changes.

| Topic | Direction | Payload | Notes |
|---|---|---|---|
| `stormbox/readings` | box → hub | `{"ms":..,"p":1008.4,"temp":29.1,"water":412,"air":180,"wall":1,"src":"live","alarms":[]}` | 1 per second, QoS 0 |
| `stormbox/event` | box → hub | `{"t":"alarm","id":"flood","level":"alarm","state":"on"}` / `{"t":"btn","press":"short"}` | QoS 1 |
| `stormbox/status` | box → hub | `"online"` / `"offline"` | **Retained.** Set `"offline"` as the ESP32's **Last Will** so the hub instantly knows if the box dies |
| `stormbox/cmd` | hub → box | `{"cmd":"hb"}` `{"cmd":"lcd","l1":"..","l2":".."}` `{"cmd":"light","mode":".."}` `{"cmd":"buzz","pattern":".."}` `{"cmd":"replay","on":true}` `{"cmd":"rp","p":1003.2,"ts":".."}` `{"cmd":"ack","id":"flood"}` | Heartbeat every 2 s |

**ESP32 side:** `PubSubClient` + `ArduinoJson`. Connect to the broker at the laptop's IP on the `StormBox` network. Call `setWill("stormbox/status","offline",retained)`, then publish `"online"`.

**Hub side:** `box_service` uses `paho-mqtt` instead of `pyserial`. Nothing else changes.

```python
import json, paho.mqtt.client as mqtt

def on_message(client, _, msg):
    data = json.loads(msg.payload) if msg.topic != "stormbox/status" else msg.payload.decode()
    if msg.topic == "stormbox/readings": state.update_readings(data)
    elif msg.topic == "stormbox/event":  handle_event(data)      # alarms, button presses
    elif msg.topic == "stormbox/status": state.box_online = (data == "online")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.connect("localhost", 1883)
client.subscribe([("stormbox/readings", 0), ("stormbox/event", 1), ("stormbox/status", 1)])
client.loop_start()

def send(cmd: dict):
    client.publish("stormbox/cmd", json.dumps(cmd))
```

**Fake box for CS:** `mosquitto_pub -t stormbox/readings -m '{...}'` in a loop, or a 20-line Python publisher. CS never waits on hardware.

### Mosquitto gotchas (check now)
- **Mosquitto 2.x only listens on localhost by default.** The ESP32 can't connect until `mosquitto.conf` has:
  ```
  listener 1883 0.0.0.0
  allow_anonymous true
  ```
  then restart Mosquitto.
- **Firewall:** allow port **1883** (MQTT) and **8000** (dashboard) on the laptop.
- **Network:** the laptop must be on `StormBox`, not campus Wi-Fi. Campus Wi-Fi usually blocks devices from talking to each other.
- **Laptop IP changes** when it rejoins. Read it once on `StormBox` (`ipconfig` / `ipconfig getifaddr en0`) and set it in the ESP32 code. Better: reserve it, or have the ESP32 use the DHCP lease it handed out.

---

## 4. "Ask the AI" with the box

Two ways in, **one agent function**: `agent.ask(text, channel) → {answer, sources, trace}`.

### A. Push-to-talk on the box (the hardware moment)
```
Box button (short press)
  → ESP32 publishes {"t":"btn","press":"short"} on stormbox/event
  → hub: send LCD "Listening..." + blue light
  → hub records from the mic until ~1.5 s of silence (max 8 s)
  → Vosk turns speech into text
  → hub: send LCD "Thinking..."
  → agent.ask(text, "voice")
  → Piper speaks the answer through the speaker
  → LCD shows a 16-char summary ("Store 28 gal"), light back to normal
  → trace logged → engineer-mode panel updates live
```
- Mic + speaker plug into the laptop. **Put the speaker next to the box** so the answer "comes from the box."
- If an alarm is active, a **long press** silences it instead of starting voice.

### B. Dashboard chat
`POST /api/ask {text}` → same `agent.ask(text, "chat")` → answer + sources shown on screen. Good for judges who'd rather type.

### C. Proactive (stretch)
Box publishes an alarm → hub calls `agent.ask("Flood alarm fired. What should the family do?", "proactive")` → speaks it automatically.

---

## 5. Presenting it as a hardware project

**Frame it as a distributed system, not "a laptop with sensors":**
> "StormBox is a **battery-powered wireless sensor node** with its own safety logic, its own Wi-Fi, and its own alarms. The laptop is the **edge AI hub**, the way a smart-home hub works. The box keeps you safe on its own. The hub makes it smart."

**Why wireless is actually better (say this):** the box can sit in the room that floods, near the floor or the garage door, while the hub stays somewhere safe and dry.

**Make the hardware visible:**
- Box on the table with the **lid open or a clear panel**, wires visible, labeled sensors.
- Every live demo moment is a **physical action on the box**: dip the sensor, spray sanitizer, press the button.
- Engineer mode shows **box health**: online/offline (from MQTT Last Will), signal strength, uptime, battery.
- A slide with the **wiring diagram** and the **ESP32 firmware's alarm state machine**.
- Mention the hardware decisions out loud: ADC1 pins (Wi-Fi conflicts with ADC2), 3.3V sensor power, hysteresis on alarms, Last Will for failure detection.

**Hardware credit list (who built what):**
| Hardware piece | Proof in demo |
|---|---|
| Sensor node (4 sensors on ESP32) | Live graphs react to physical actions |
| Local alarm logic (firmware state machine) | Alarms fire with the hub dead |
| Status display (LCD + RGB + buzzer) | Visual + sound alarms, "Hub offline" |
| Wireless networking (ESP32 access point + MQTT) | Judge's phone joins `StormBox`, sees "No internet" |
| Push-to-talk input | Button on the box starts the voice assistant |
| Power (battery-powered node) | Unplug it from the wall, still running |

---

## 6. Demo (≈3 min, updated)
| # | Moment | Proves |
|---|---|---|
| 0 | Hook: Florida cell sites down after a hurricane (cite the FCC number) | Problem |
| 1 | Judge joins `StormBox` Wi-Fi → phone says "No internet" → dashboard works | Offline |
| 2 | **Storm replay** → risk % climbs → box alarm → "possible eye, stay inside" | ML + safety + purpose |
| 3 | **Dip sensor in water** → buzzer, red flash, box speaks | Real sensing |
| 4 | **Sanitizer** near the air sensor → air alarm | Real sensing |
| 5 | **Press the box button**, judge asks a question → spoken answer; trace panel shows sources + sensors used | Hardware + AI working together |
| 6 | **Kill the hub** (stop the server or close the laptop) → LCD says `Hub offline`, dip the sensor again → **alarm still fires** | Safety doesn't depend on AI |
| — | "The AI makes it smart. The hardware makes it safe." | |

**Pitch line on the Pi (one sentence, honest):** "Our Raspberry Pi's card reader died mid-build, so we redesigned around a wireless node and an edge hub. It made the box more flexible."

---

## 7. Next steps (tonight)
| Who | Now |
|---|---|
| EE 1 | ESP32 access point `StormBox` → MQTT connect with Last Will → publish readings → subscribe to `cmd` |
| EE 2 | Fix Mosquitto config (`0.0.0.0` + anonymous), firewall, laptop on `StormBox`; mount speaker by the box; battery on the box |
| CS 1 | Swap `pyserial` for `paho-mqtt` in `box_service`; fake publisher; push-to-talk flow; dashboard over WebSocket |
| CS 2 | Agent `ask()` working in the terminal; canned answers; RAG; model on the laptop (try a 3B model now that you have more power) |
| All | **Integration check at ~5 AM:** real box → MQTT → dashboard → voice |
