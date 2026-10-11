"""
FakeESP32 — simulates the ESP32's serial output (§5 protocol).

Used two ways:
  1. In-process: import FakeESP32, call fake.run() as an asyncio task,
     then use fake.readline() / fake.write() just like SerialSource.
  2. Standalone: run scripts/fake_esp32.py, which wraps run_standalone().
"""
import asyncio
import json
import random
import sys
import time


class FakeESP32:
    """
    Simulates the ESP32. Puts §5 JSON lines into an asyncio.Queue every second.
    Alarm events are pushed immediately when trigger() is called.
    """

    # Realistic starting values
    _BASE_P     = 1012.0   # hPa
    _BASE_TEMP  = 29.0     # °C
    _BASE_HUM   = 78.0     # % (mid-range 70-90)
    _BASE_WATER = 100      # moisture ADC count (dry)
    _BASE_WIND  = 7.0      # mph (calm)

    def __init__(self):
        self._p     = self._BASE_P
        self._temp  = self._BASE_TEMP
        self._hum   = self._BASE_HUM
        self._water = self._BASE_WATER
        self._wind  = self._BASE_WIND
        self._ms    = 0        # simulated uptime ms

        self._alarms: set[str] = set()

        self._flood_on = False
        self._heat_on  = False
        self._storm_dropping  = False
        self._storm_drop_rate = 0.0   # hPa per tick (1 s)
        self._storm_dropped   = 0.0   # cumulative drop so far

        # Wind ramp state
        self._wind_ramping     = False
        self._wind_ramp_target = 0.0
        self._wind_alarm_level = ""   # "warn" or "alarm" while wind alarm is active

        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    # ------------------------------------------------------------------ queue helpers

    def _put(self, obj: dict):
        """Put a JSON line into the queue (safe to call synchronously in the event loop)."""
        self._queue.put_nowait(json.dumps(obj))

    def _make_reading(self) -> dict:
        return {
            "t":      "r",
            "ms":     self._ms,
            "p":      round(self._p, 1),
            "temp":   round(self._temp, 1),
            "hum":    round(self._hum, 1),
            "water":  self._water,
            "wind":   round(self._wind / 2.237, 2),   # internal mph → m/s (matches real ESP32)
            "src":    "live",
            "alarms": sorted(self._alarms),
        }

    def _alarm_event(self, alarm_id: str, level: str, state: str) -> dict:
        return {"t": "alarm", "id": alarm_id, "level": level, "state": state}

    # ------------------------------------------------------------------ trigger API

    def trigger(self, event: str):
        """
        Drive an event from outside (HTTP handler, stdin, etc.).
        Safe to call from within the asyncio event loop.
        """
        if event == "flood":
            self._flood_on = not self._flood_on
            if self._flood_on:
                self._water = 900
                self._alarms.add("flood")
                self._put(self._alarm_event("flood", "alarm", "on"))
            else:
                self._water = self._BASE_WATER + random.randint(-20, 20)
                self._alarms.discard("flood")
                self._put(self._alarm_event("flood", "alarm", "off"))

        elif event == "wind":
            # Ramp wind to ~130 mph over ~25 s (5 mph/s).
            # warn fires at 39 mph (~6 s in), alarm at 74 mph (~13 s in).
            if not self._wind_ramping:
                self._wind_ramping     = True
                self._wind_ramp_target = 130.0

        elif event == "storm":
            # Pressure drops at 0.2 hPa/s; storm alarm fires after ~30 s (≥ 6 hPa drop).
            # Wind, rain, and humidity also rise to simulate realistic storm conditions.
            if not self._storm_dropping:
                self._storm_dropping  = True
                self._storm_drop_rate = 0.2
                self._storm_dropped   = 0.0

        elif event == "heat":
            self._heat_on = not self._heat_on
            if self._heat_on:
                self._temp = 42.0
                self._hum  = 85.0   # high humidity → heat index well above threshold
                self._alarms.add("heat")
                self._put(self._alarm_event("heat", "alarm", "on"))
            else:
                self._temp = self._BASE_TEMP + random.uniform(-1, 1)
                self._hum  = self._BASE_HUM  + random.uniform(-2, 2)
                self._alarms.discard("heat")
                self._put(self._alarm_event("heat", "alarm", "off"))

        elif event == "short":
            self._put({"t": "btn", "press": "short"})

        elif event == "long":
            self._put({"t": "btn", "press": "long"})

        elif event == "clear":
            # Clear everything back to idle
            for aid in list(self._alarms):
                self._put(self._alarm_event(aid, self._wind_alarm_level or "alarm", "off"))
            self._alarms.clear()
            self._flood_on = self._heat_on = self._storm_dropping = self._wind_ramping = False
            self._p     = self._BASE_P
            self._temp  = self._BASE_TEMP
            self._hum   = self._BASE_HUM
            self._water = self._BASE_WATER
            self._wind  = self._BASE_WIND
            self._wind_alarm_level = ""

    # ------------------------------------------------------------------ async interface

    async def readline(self) -> str:
        """Block until the next §5 line is ready and return it."""
        return await self._queue.get()

    async def write(self, data: bytes):
        """Receive a Pi→ESP32 command (just log it in fake mode)."""
        try:
            cmd = json.loads(data.decode().strip())
            print(f"[fake] <- {cmd}", file=sys.stderr, flush=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ tick loop

    async def _tick(self):
        """Advance simulated time by 1 s, drift sensor values, push a reading."""
        self._ms += 1000

        # --- pressure ---
        if self._storm_dropping:
            self._p             -= self._storm_drop_rate
            self._storm_dropped += self._storm_drop_rate
            if self._storm_dropped >= 6.0 and "storm" not in self._alarms:
                self._alarms.add("storm")
                self._put(self._alarm_event("storm", "alarm", "on"))
        else:
            self._p += random.uniform(-0.3, 0.3)

        # --- temperature ---
        if not self._heat_on:
            self._temp = max(20.0, min(45.0, self._temp + random.uniform(-0.2, 0.2)))

        # --- humidity (70-90% normally; rises toward 95% during storm) ---
        if not self._heat_on:
            if self._storm_dropping:
                self._hum = min(95.0, self._hum + 0.3)
            else:
                self._hum = max(70.0, min(90.0, self._hum + random.uniform(-0.2, 0.2)))

        # --- moisture ---
        if not self._flood_on:
            self._water = max(50, min(200, self._water + random.randint(-5, 5)))

        # --- wind ---
        if self._wind_ramping:
            # min() caps at target; leave _wind_ramping True so the else branch
            # never runs and wind holds at target until Clear is pressed.
            self._wind = min(self._wind_ramp_target, self._wind + 5.0)
        elif self._storm_dropping:
            # Storm: gradually reach ~55 mph
            if self._wind < 55.0:
                self._wind = min(55.0, self._wind + 0.4)
        else:
            self._wind = max(0.0, min(15.0, self._wind + random.uniform(-0.5, 0.5)))

        # --- wind alarm with hysteresis ---
        # Thresholds: warn ≥ 39 mph, alarm ≥ 74 mph
        # Hysteresis:  clear warn below 35 mph, step alarm→warn below 70 mph
        if "wind" not in self._alarms:
            if self._wind >= 74.0:
                self._wind_alarm_level = "alarm"
                self._alarms.add("wind")
                self._put(self._alarm_event("wind", "alarm", "on"))
            elif self._wind >= 39.0:
                self._wind_alarm_level = "warn"
                self._alarms.add("wind")
                self._put(self._alarm_event("wind", "warn", "on"))
        else:
            lvl = self._wind_alarm_level
            if self._wind < 35.0:
                # Wind died down — clear alarm entirely
                self._alarms.discard("wind")
                self._wind_alarm_level = ""
                self._put(self._alarm_event("wind", lvl or "warn", "off"))
            elif self._wind < 70.0 and lvl == "alarm":
                # Step down from alarm to warn
                self._wind_alarm_level = "warn"
                self._put(self._alarm_event("wind", "warn", "on"))
            elif self._wind >= 74.0 and lvl == "warn":
                # Step up from warn to alarm
                self._wind_alarm_level = "alarm"
                self._put(self._alarm_event("wind", "alarm", "on"))

        self._put(self._make_reading())

    async def run(self):
        """Main tick loop. Run as an asyncio task."""
        self._running = True
        while self._running:
            await self._tick()
            await asyncio.sleep(1)

    def stop(self):
        self._running = False


# ------------------------------------------------------------------ standalone runner

async def run_standalone():
    """
    Run the fake ESP32 as a standalone process.
    Prints §5 lines to stdout; reads single-char commands from stdin.
    """
    import threading

    fake = FakeESP32()
    loop = asyncio.get_event_loop()

    event_map = {
        "f": "flood", "w": "wind",  "s": "storm",
        "h": "heat",  "b": "short", "l": "long",
        "c": "clear",
    }

    print(
        "Controls: f=flood  w=wind  s=storm  h=heat  b=short  l=long  c=clear",
        file=sys.stderr, flush=True,
    )

    def _read_stdin():
        for raw in sys.stdin:
            key = raw.strip().lower()
            ev  = event_map.get(key)
            if ev:
                loop.call_soon_threadsafe(fake.trigger, ev)
            elif key:
                print(f"Unknown key {key!r} — use: {' '.join(event_map)}", file=sys.stderr)

    threading.Thread(target=_read_stdin, daemon=True).start()

    asyncio.create_task(fake.run())

    while True:
        line = await fake.readline()
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
