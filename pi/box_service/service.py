"""
BoxService — reads lines from the ESP32 (real serial or in-process fake),
keeps BoxState up to date, sends heartbeats, and watches health.

Start with:  asyncio.create_task(service.start())
"""
import asyncio
import json
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .state import BoxState

from ai.agent.config import ALTITUDE_M, CAL_OFFSET_HPA, BOX_DB
from ai.agent.agent import explain_event as agent_explain
from ai.agent import templates as _alarm_templates
from ai.ml.storm_job import run_storm_job


# ------------------------------------------------------------------ helpers

def _sea_level(p: float | None, temp: float | None, alt: float, cal: float) -> float | None:
    """Convert station pressure to sea-level pressure (hPa).

    Uses the standard hypsometric formula. At alt=0 returns p+cal (calibration only).
    """
    if p is None:
        return None
    p_cal = p + cal
    if alt == 0:
        return round(p_cal, 1)
    T = (temp if temp is not None else 15.0) + 273.15
    return round(p_cal * (1 - 0.0065 * alt / (T + 0.0065 * alt)) ** -5.2558, 1)


def _save_explanation_db(alarm_id: str, level: str, since: str, text: str):
    """Insert an alarm explanation row if the alarms table exists in box.db."""
    if not BOX_DB or not BOX_DB.exists():
        return
    try:
        con = sqlite3.connect(str(BOX_DB), timeout=2)
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alarms'"
        ).fetchone()
        if has_table:
            con.execute(
                "INSERT INTO alarms (alarm_id, level, since, explanation) VALUES (?,?,?,?)",
                (alarm_id, level, since, text),
            )
            con.commit()
        con.close()
    except Exception as exc:
        print(f"[service] alarm DB error: {exc}")


def _storm_phase(hourly: list) -> str | None:
    """Derive phase label from a list of hourly sea-level pressure values."""
    if not hourly or len(hourly) < 3:
        return None
    diff_3h = hourly[-1] - hourly[-3]
    if len(hourly) >= 4:
        prev_diff = hourly[-2] - hourly[-4]
        if prev_diff <= -3 and hourly[-1] - hourly[-2] > 1.0:
            return "recovering"
        if prev_diff <= -3 and abs(hourly[-1] - hourly[-2]) < 1.0:
            return "possible eye"
    if diff_3h <= -6:
        return "falling fast"
    if diff_3h <= -3:
        return "falling"
    return None


# ------------------------------------------------------------------ internet check

async def check_internet() -> bool:
    """
    Try three TCP probes in parallel; reachable if any one succeeds.
    Uses asyncio.open_connection so asyncio.wait_for can reliably cancel on timeout
    (run_in_executor threads can't be interrupted by asyncio on Windows).
    """
    async def _probe(host: str, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=1.5,
            )
            writer.close()
            return True
        except Exception:
            return False

    results = await asyncio.gather(
        _probe("1.1.1.1", 53),
        _probe("1.1.1.1", 443),
        _probe("8.8.8.8", 443),
    )
    return any(results)


# ------------------------------------------------------------------ serial source

class SerialSource:
    """
    Wraps a pyserial port as an async line source.
    The port is opened lazily on the first readline() call and re-opened
    automatically after a disconnect (caller sees an exception and retries).
    Uses run_in_executor so blocking calls don't stall asyncio.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        self._port     = port
        self._baudrate = baudrate
        self._ser      = None   # opened on first use

    def _open_sync(self):
        """Open (or reopen) the port. Raises serial.SerialException on failure."""
        import serial
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = serial.Serial(self._port, self._baudrate, timeout=1)

    def _readline_sync(self) -> str:
        """Read one non-empty line. Raises on port error."""
        while True:
            raw = self._ser.readline()
            line = raw.decode(errors="replace").strip()
            if line:
                return line

    async def readline(self) -> str:
        loop = asyncio.get_event_loop()
        if self._ser is None:
            await loop.run_in_executor(None, self._open_sync)
        try:
            return await loop.run_in_executor(None, self._readline_sync)
        except Exception:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
            raise

    async def write(self, data: bytes):
        if self._ser is None:
            return   # port not open; drop the write silently
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._ser.write, data + b"\n")
        except Exception as exc:
            print(f"[service] serial write error: {exc}")

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


# ------------------------------------------------------------------ box service

OnChangeCb = Callable[[dict], Awaitable[None]]


class BoxService:
    """
    Orchestrates:
      - serial/fake read loop  → BoxState → change notifications
      - heartbeat (every 2 s) → ESP32
      - health ticker (every 1 s) → esp32_link, internet (every 30 s)
    """

    def __init__(self, state: BoxState, source, dev: bool = False):
        self._state        = state
        self._source       = source   # SerialSource or FakeESP32
        self._dev          = dev
        self._cbs:         list[OnChangeCb] = []
        self._last_rx      = time.monotonic()
        self._running      = False
        # Rolling pressure buffer fed to the storm risk model (8 h @ 1 Hz)
        self._pressure_buf:    deque = deque(maxlen=28_800)
        self._prev_storm_phase: str | None = None
        self._cur_storm_level:  str | None = None  # Pi-managed storm alarm level

    def register_on_change(self, cb: OnChangeCb):
        """Register a coroutine called with the full state dict on every change."""
        self._cbs.append(cb)

    async def _notify(self):
        snapshot = self._state.to_dict()
        for cb in self._cbs:
            try:
                await cb(snapshot)
            except Exception as exc:
                print(f"[service] notify error: {exc}")

    # ------------------------------------------------------------------ read loop

    async def _read_loop(self):
        while self._running:
            try:
                raw = await self._source.readline()
                self._last_rx = time.monotonic()
                # Use the recording's own timestamp if present (replay rows carry it);
                # fall back to wall clock for live ESP32 and fake data.
                try:
                    msg_ts = json.loads(raw).get("ts") or datetime.now(timezone.utc).isoformat()
                except Exception:
                    msg_ts = datetime.now(timezone.utc).isoformat()
                self._handle_line(raw)
                p    = self._state.readings["p"]
                temp = self._state.readings["temp"]
                if p is not None:
                    self._state.readings["p_sl"] = _sea_level(p, temp, ALTITUDE_M, CAL_OFFSET_HPA)
                    self._pressure_buf.append({"ts": msg_ts, "p": p, "temp": temp})
                self._apply_wind_alarm(self._state.readings["wind"])
                await self._notify()
            except Exception as exc:
                print(f"[service] read error: {exc}")
                await asyncio.sleep(2)

    def _handle_line(self, raw: str):
        """Parse one §5 JSON line and apply it to BoxState. Ignore malformed lines."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        t = msg.get("t")
        if t == "r":
            self._state.apply_reading(msg)
        elif t == "alarm":
            aid = msg.get("id")
            if aid in ("wind", "storm"):
                return  # Pi computes these; ignore any alarm events for them
            is_new = (msg.get("state") == "on"
                      and self._state._find_alarm(aid) is None)
            self._state.apply_alarm_event(msg)
            if is_new:
                asyncio.create_task(self._explain_alarm_bg(msg))
        elif t == "btn":
            # Button events are handled by the voice layer (CS 2); just log for now.
            print(f"[service] button: {msg.get('press')}")
        # Unknown message types are silently ignored as per the spec.

    # ------------------------------------------------------------------ heartbeat

    async def _heartbeat_loop(self):
        """Send {"cmd":"hb"} to the ESP32 every 2 s."""
        while self._running:
            await asyncio.sleep(2)
            cmd = json.dumps({"cmd": "hb"}).encode()
            if self._dev:
                print("[service] -> hb", flush=True)
            else:
                try:
                    await self._source.write(cmd)
                except Exception as exc:
                    print(f"[service] hb error: {exc}")

    # ------------------------------------------------------------------ health

    async def _health_loop(self):
        """Update esp32_link every second; check internet every 30 s."""
        internet_tick = 0
        while self._running:
            await asyncio.sleep(1)
            age_ms = int((time.monotonic() - self._last_rx) * 1000)
            self._state.health["last_msg_age_ms"] = age_ms
            self._state.health["esp32_link"] = age_ms < 5000
            internet_tick += 1
            if internet_tick >= 30:
                internet_tick = 0
                self._state.health["internet"] = await check_internet()
            await self._notify()

    # ------------------------------------------------------------------ LCD + explanations

    async def _send_lcd_cmd(self, l1: str, l2: str):
        cmd = json.dumps({"cmd": "lcd", "l1": l1, "l2": l2}).encode()
        if self._dev:
            print(f"[service] -> lcd: '{l1}' / '{l2}'")
        else:
            try:
                await self._source.write(cmd)
            except Exception as exc:
                print(f"[service] lcd error: {exc}")

    async def _explain_alarm_bg(self, event: dict):
        """Run explain_event for a new alarm in a thread; update alarm, LCD, and DB."""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, lambda: agent_explain(event))
            answer = result.get("answer", "")
            if not answer:
                return
            lcd = result.get("lcd", ("", ""))
            alarm = self._state._find_alarm(event.get("id"))
            if alarm:
                alarm["explanation"] = answer
                _save_explanation_db(
                    alarm["id"], alarm["level"], alarm["since"], answer
                )
            await self._send_lcd_cmd(lcd[0], lcd[1])
            await self._notify()
        except Exception as exc:
            print(f"[service] explain_alarm error: {exc}")

    async def _explain_phase_bg(self, phase: str):
        """Run explain_event for a storm phase change; update storm.explanation and LCD."""
        event = {"t": "phase", "phase": phase}
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, lambda: agent_explain(event))
            answer = result.get("answer", "")
            if not answer:
                return
            lcd = result.get("lcd", ("", ""))
            self._state.storm["explanation"] = answer
            await self._send_lcd_cmd(lcd[0], lcd[1])
            await self._notify()
        except Exception as exc:
            print(f"[service] explain_phase error: {exc}")

    # ------------------------------------------------------------------ Pi-managed alarms

    def _apply_wind_alarm(self, wind: float | None):
        """Pi-managed wind alarm with hysteresis.

        Derive target level from the current alarm state (not a separate tracking
        variable) so there is no drift between the two.

        Warn ≥ 39 mph, alarm ≥ 74 mph.
        Hysteresis: clear below 35 mph, step alarm→warn below 70 mph.
        """
        if wind is None:
            return

        existing  = self._state._find_alarm("wind")
        cur_level = existing["level"] if existing else None

        # Compute target level
        if cur_level is None:
            if wind >= 74.0:
                target = "alarm"
            elif wind >= 39.0:
                target = "warn"
            else:
                return  # below threshold, nothing to do
        elif cur_level == "alarm":
            if wind < 35.0:
                target = None
            elif wind < 70.0:
                target = "warn"       # step down
            else:
                return  # still in alarm range, no change
        else:  # cur_level == "warn"
            if wind < 35.0:
                target = None
            elif wind >= 74.0:
                target = "alarm"      # step up
            else:
                return  # still in warn range, no change

        if target is None:
            self._state.apply_alarm_event(
                {"t": "alarm", "id": "wind", "level": cur_level, "state": "off"}
            )
        else:
            event = {"t": "alarm", "id": "wind", "level": target, "state": "on"}
            self._state.apply_alarm_event(event)
            # Set explanation synchronously from the template so _notify() below
            # pushes the correct text immediately, without waiting for the bg task.
            tpl = _alarm_templates.p0(event)
            alarm = self._state._find_alarm("wind")
            if alarm and tpl:
                alarm["explanation"] = tpl["say"]
            asyncio.create_task(self._explain_alarm_bg(event))

    def _compute_storm_level(self, sea_level_hourly: list) -> str | None:
        """Two-signal storm alarm: whichever signal is worse wins.

        Signal 1 — 3-hour recorded-time window (handles replay):
          Uses sea_level_hourly from run_storm_job, which buckets readings by their
          stored 'ts' field.  Replay rows carry recorded storm timestamps so the
          hourly buckets span real 3-hour windows.

        Signal 2 — rapid recent raw-pressure drop (fake-mode shortcut):
          Scans the last 120 buffer entries (~2 real minutes).  A 6 hPa drop in
          2 minutes is meteorologically impossible, so this only fires on fake data.
          The fake's storm button drops ~0.2 hPa/s → 6 hPa in ~30 s.
        """
        # Signal 1
        if len(sea_level_hourly) >= 3:
            diff_3h = sea_level_hourly[-1] - sea_level_hourly[-3]
            if diff_3h <= -6:
                return "alarm"
            if diff_3h <= -3:
                return "warn"

        # Signal 2
        recent = list(self._pressure_buf)[-120:]
        if len(recent) >= 5:
            vals = [r["p"] for r in recent if r.get("p") is not None]
            if vals:
                drop = max(vals) - vals[-1]
                if drop >= 6.0:
                    return "alarm"
                if drop >= 3.0:
                    return "warn"

        return None

    def _apply_storm_alarm(self, new_level: str | None):
        """Sync the storm alarm in state to match the computed level."""
        if new_level == self._cur_storm_level:
            return
        old_level = self._cur_storm_level
        self._cur_storm_level = new_level

        if new_level is None:
            if old_level:
                self._state.apply_alarm_event(
                    {"t": "alarm", "id": "storm", "level": old_level, "state": "off"}
                )
        else:
            event = {"t": "alarm", "id": "storm", "level": new_level, "state": "on"}
            self._state.apply_alarm_event(event)
            tpl = _alarm_templates.p0(event)
            alarm = self._state._find_alarm("storm")
            if alarm and tpl:
                alarm["explanation"] = tpl["say"]
            asyncio.create_task(self._explain_alarm_bg(event))

    # ------------------------------------------------------------------ storm risk

    async def _storm_loop(self):
        """Call run_storm_job every 60 s in a thread and push the result into state.storm."""
        while self._running:
            await asyncio.sleep(60)
            if not self._pressure_buf:
                continue
            readings = list(self._pressure_buf)
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(
                    None, lambda: run_storm_job(readings, source="live")
                )
                if result.get("status") == "ok":
                    hourly   = result.get("sea_level_hourly") or []
                    new_phase = _storm_phase(hourly)
                    self._state.storm.update({
                        "phase":         new_phase,
                        "risk":          result["risk"],
                        "alert":         result.get("alert"),
                        "reason":        result.get("reason"),
                        "path":          result.get("path"),
                        "model_version": result.get("model_version"),
                    })
                    if new_phase and new_phase != self._prev_storm_phase:
                        asyncio.create_task(self._explain_phase_bg(new_phase))
                    self._prev_storm_phase = new_phase
                else:
                    hourly = []
                    # "warming up" — not enough data yet, leave risk as None
                    self._state.storm["phase"] = None
                    self._prev_storm_phase = None
                self._apply_storm_alarm(self._compute_storm_level(hourly))
            except Exception as exc:
                print(f"[service] storm job error: {exc}")
            await self._notify()

    # ------------------------------------------------------------------ lifecycle

    async def start(self):
        self._running = True
        asyncio.create_task(self._init_internet())
        await asyncio.gather(
            self._read_loop(),
            self._heartbeat_loop(),
            self._health_loop(),
            self._storm_loop(),
        )

    async def _init_internet(self):
        """Fire-and-forget: check internet once at startup, then push the result."""
        self._state.health["internet"] = await check_internet()
        await self._notify()

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------ API actions

    async def ack_alarm(self, alarm_id: str):
        """Mark an alarm acked in state and tell the ESP32."""
        self._state.ack_alarm(alarm_id)
        cmd = json.dumps({"cmd": "ack", "id": alarm_id}).encode()
        if self._dev:
            print(f"[service] -> ack {alarm_id}")
        else:
            try:
                await self._source.write(cmd)
            except Exception as exc:
                print(f"[service] ack error: {exc}")
        await self._notify()
