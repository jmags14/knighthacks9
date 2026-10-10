"""
BoxService — reads lines from the ESP32 (real serial or in-process fake),
keeps BoxState up to date, sends heartbeats, and watches health.

Start with:  asyncio.create_task(service.start())
"""
import asyncio
import json
import socket
import time
from typing import Awaitable, Callable

from .state import BoxState


# ------------------------------------------------------------------ internet check

async def check_internet() -> bool:
    """
    Try three TCP probes in parallel; reachable if any one succeeds.
    Port 53 (DNS) is blocked on some campus/corporate networks, so we also
    try port 443 (HTTPS) on two hosts as fallbacks.
    """
    loop = asyncio.get_event_loop()

    async def _probe(host: str, port: int) -> bool:
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _tcp_probe, host, port),
                timeout=1.0,
            )
            return True
        except Exception:
            return False

    results = await asyncio.gather(
        _probe("1.1.1.1", 53),
        _probe("1.1.1.1", 443),
        _probe("8.8.8.8", 443),
    )
    return any(results)


def _tcp_probe(host: str, port: int):
    with socket.create_connection((host, port), timeout=1):
        pass


# ------------------------------------------------------------------ serial source

class SerialSource:
    """
    Wraps a pyserial port as an async line source.
    Uses run_in_executor so the blocking readline doesn't stall asyncio.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        import serial
        self._ser = serial.Serial(port, baudrate, timeout=1)

    async def readline(self) -> str:
        loop = asyncio.get_event_loop()
        while True:
            raw = await loop.run_in_executor(None, self._ser.readline)
            line = raw.decode(errors="replace").strip()
            if line:
                return line
            # readline timed out (empty bytes) — try again

    async def write(self, data: bytes):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._ser.write, data + b"\n")


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
        self._state   = state
        self._source  = source   # SerialSource or FakeESP32
        self._dev     = dev
        self._cbs:    list[OnChangeCb] = []
        self._last_rx = time.monotonic()
        self._running = False

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
                self._handle_line(raw)
                await self._notify()
            except Exception as exc:
                print(f"[service] read error: {exc}")

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
            self._state.apply_alarm_event(msg)
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

    # ------------------------------------------------------------------ lifecycle

    async def start(self):
        self._running = True
        self._state.health["internet"] = await check_internet()
        await self._notify()   # push corrected internet state before loops begin
        await asyncio.gather(
            self._read_loop(),
            self._heartbeat_loop(),
            self._health_loop(),
        )

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
