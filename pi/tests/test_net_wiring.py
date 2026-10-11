"""Phase 1: the box service uses the one internet checker (ai/system/netcheck.py).
Everything is mocked: no sockets, no audio, no serial."""
import asyncio

import pytest

from ai.system import netcheck
from pi.box_service import service as svc
from pi.box_service.state import BoxState


def make_service():
    s = svc.BoxService(BoxState(dev=True), source=None, dev=True)
    s.lcd, s.spoken, s.notified = [], [], []

    async def lcd(l1, l2):
        s.lcd.append((l1, l2))

    async def notify():
        s.notified.append(1)

    s._send_lcd_cmd, s._notify = lcd, notify
    return s


def run_worker(s, events):
    """Feed events through the real _net_worker, then stop it."""
    async def go():
        s._running = True
        s._loop = asyncio.get_running_loop()
        for e in events:
            s._on_net_event(e)
        task = asyncio.create_task(s._net_worker())
        await asyncio.sleep(0.3)
        s._running = False
        await asyncio.wait_for(task, 3)
    asyncio.run(go())


@pytest.fixture(autouse=True)
def no_real_audio(monkeypatch):
    monkeypatch.delenv("SPEAK_EVENTS", raising=False)


def test_no_second_internet_checker_in_pi():
    assert not hasattr(svc, "check_internet") and not hasattr(svc.BoxService, "check_internet")
    assert svc._netcheck is netcheck


def test_health_line_reads_netcheck_status(monkeypatch):
    s = make_service()
    monkeypatch.setattr(netcheck, "status", lambda: {"online": False, "since": "t", "last_check": "t", "misses": 3})

    async def one_tick():
        s._running = True
        task = asyncio.create_task(s._health_loop())
        await asyncio.sleep(1.2)
        s._running = False
        await asyncio.wait_for(task, 3)
    asyncio.run(one_tick())
    assert s._state.health["net"]["online"] is False


def test_net_event_goes_to_lcd_and_dashboard_but_is_silent_by_default(monkeypatch):
    s = make_service()
    monkeypatch.setattr(svc, "_speak_event", lambda e: s.spoken.append(e))
    run_worker(s, [{"t": "net", "state": "lost"}])
    assert s.lcd == [("No internet", "Works offline")] and s.notified
    assert s.spoken == []                       # SPEAK_EVENTS not set -> laptop stays quiet


def test_net_lost_then_restored_are_spoken_when_enabled(monkeypatch):
    monkeypatch.setenv("SPEAK_EVENTS", "1")
    s = make_service()
    monkeypatch.setattr(svc, "_speak_event", lambda e: s.spoken.append(e["state"]))
    run_worker(s, [{"t": "net", "state": "lost"}, {"t": "net", "state": "restored"}])
    assert s.spoken == ["lost", "restored"]


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("0", False), ("", False)])
def test_speak_events_setting(monkeypatch, value, expected):
    monkeypatch.setenv("SPEAK_EVENTS", value)
    assert svc.speak_events_enabled() is expected


def test_speaking_failure_does_not_kill_the_worker(monkeypatch):
    monkeypatch.setenv("SPEAK_EVENTS", "1")
    s = make_service()

    def boom(e):
        raise RuntimeError("no speaker")
    monkeypatch.setattr(svc, "_speak_event", boom)
    run_worker(s, [{"t": "net", "state": "lost"}])          # must finish without raising
    assert s.lcd
