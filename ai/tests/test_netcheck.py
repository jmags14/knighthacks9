"""Internet monitor (ai/system/netcheck.py) with the socket mocked, plus the agent wiring."""
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ai.agent import agent, router, templates as T, tools
from ai.system import netcheck
from ai.system.netcheck import NetMonitor


def monitor(**kw):
    events = []
    return NetMonitor(events.append, **kw), events


def feed(m, results):
    for ok in results:
        m.step(ok)


# ---------------------------------------------------------------- state machine
def test_first_result_sets_baseline_without_an_event():
    m, events = monitor()
    assert m.status()["online"] is None
    feed(m, [True])
    assert m.status()["online"] is True and events == []


def test_lost_after_exactly_three_misses():
    m, events = monitor()
    feed(m, [True, False, False])
    assert events == [] and m.status()["online"] is True
    feed(m, [False])
    assert [e["state"] for e in events] == ["lost"] and m.status()["online"] is False
    assert events[0]["t"] == "net" and events[0]["ts"] == m.status()["since"]


def test_restored_after_exactly_two_successes():
    m, events = monitor()
    feed(m, [True, False, False, False])
    feed(m, [True])
    assert [e["state"] for e in events] == ["lost"]
    feed(m, [True])
    assert [e["state"] for e in events] == ["lost", "restored"] and m.status()["online"] is True


def test_no_flapping():
    m, events = monitor()
    feed(m, [True] + [False, False, True] * 5)           # never 3 misses in a row
    assert events == [] and m.status()["online"] is True
    feed(m, [False, False, False])                       # now truly down
    feed(m, [True, False] * 5)                           # never 2 successes in a row
    assert [e["state"] for e in events] == ["lost"] and m.status()["online"] is False


def test_each_event_fires_once():
    m, events = monitor()
    feed(m, [True] + [False] * 10)
    assert len(events) == 1
    feed(m, [True] * 10)
    assert [e["state"] for e in events] == ["lost", "restored"]


def test_starting_offline_reports_lost_after_three_misses():
    m, events = monitor()
    feed(m, [False, False])
    assert events == []
    feed(m, [False])                                     # 3 misses in a row from the start
    assert [e["state"] for e in events] == ["lost"]


def test_failing_callback_does_not_break_the_monitor():
    def boom(event):
        raise RuntimeError("CS 1 bug")
    m = NetMonitor(boom)
    feed(m, [True, False, False, False])
    assert m.status()["online"] is False                 # still tracking


def test_thresholds_come_from_config_and_can_be_overridden():
    m, events = monitor(down_after=1, up_after=1)
    feed(m, [True, False, True])
    assert [e["state"] for e in events] == ["lost", "restored"]


# ---------------------------------------------------------------- probing (socket mocked)
def test_online_if_either_target_connects():
    calls = []

    def connect(host, port, timeout):
        calls.append((host, port, timeout))
        return host == "8.8.8.8"
    m = NetMonitor(connect=connect)
    assert m.probe() is True
    assert calls == [("1.1.1.1", 443, 2), ("8.8.8.8", 443, 2)]


def test_online_when_port_53_is_blocked_but_443_works():
    m = NetMonitor(connect=lambda h, p, t: p == 443)
    assert m.probe() is True


def test_first_target_ok_skips_the_second():
    calls = []
    m = NetMonitor(connect=lambda h, p, t: calls.append(h) or True)
    assert m.probe() and calls == ["1.1.1.1"]


def test_offline_when_no_target_connects():
    assert NetMonitor(connect=lambda h, p, t: False).probe() is False


def test_tcp_connect_never_raises(monkeypatch):
    def fail(*a, **k):
        raise socket.timeout("timed out")
    monkeypatch.setattr(socket, "create_connection", fail)
    assert netcheck.tcp_connect("1.1.1.1", 53, 2) is False


def test_tcp_connect_uses_the_timeout_and_closes(monkeypatch):
    seen = {}

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            seen["closed"] = True

    def create(addr, timeout=None):
        seen.update(addr=addr, timeout=timeout)
        return Sock()
    monkeypatch.setattr(socket, "create_connection", create)
    assert netcheck.tcp_connect("1.1.1.1", 53, 2) is True
    assert seen == {"addr": ("1.1.1.1", 53), "timeout": 2, "closed": True}


# ---------------------------------------------------------------- the thread
def test_thread_runs_checks_and_stops_quickly():
    got = threading.Event()
    m = NetMonitor(lambda e: got.set(), interval=0.01, down_after=2, up_after=1,
                   connect=lambda h, p, t: False)
    m._online = True                                     # pretend it was up, so misses produce "lost"
    m.start()
    assert got.wait(2)
    m.stop()
    assert not m._thread.is_alive()
    assert m._thread.daemon


def test_module_level_start_status_stop():
    assert netcheck.status()["online"] is None
    netcheck.start(interval=0.01, connect=lambda h, p, t: True)
    try:
        deadline = threading.Event()
        for _ in range(200):
            if netcheck.status()["online"] is not None:
                break
            deadline.wait(0.01)
        assert netcheck.status()["online"] is True
    finally:
        netcheck.stop()
    assert netcheck.status()["online"] is None


# ---------------------------------------------------------------- agent
@pytest.mark.parametrize("state,key", [("lost", "net_lost"), ("restored", "net_restored")])
def test_net_events_have_text_and_short_lcd(state, key):
    r = T.p0({"t": "net", "state": state, "ts": "2026-10-10T12:00:00+00:00"})
    assert r["key"] == key and all(len(line) <= 16 for line in r["lcd"])
    assert r["say"] == T.P0[key]["say"]


def test_net_text_matches_the_spec():
    assert T.P0["net_lost"]["say"] == (
        "Internet connection lost. StormBox keeps working offline: alarms, guides and voice still work. "
        "If your phone has service, you can still call or text 911.")
    assert T.P0["net_restored"]["say"] == (
        "Internet is back. Check official updates from the National Hurricane Center and local officials.")


def test_unknown_net_state_says_nothing():
    assert T.p0({"t": "net", "state": "weird"}) is None


def test_net_event_replay_prefix():
    r = T.p0({"t": "net", "state": "lost"}, "replay")
    assert r["say"].startswith("Replay:")


def test_explain_event_handles_net_events():
    tools.set_provider(lambda: {})
    try:
        r = agent.explain_event({"t": "net", "state": "lost"})
    finally:
        tools.set_provider(None)
    assert r["answer"] == T.P0["net_lost"]["say"] and r["lcd"] == ("No internet", "Works offline")
    assert r["route"] == "proactive"


def test_get_connectivity_reads_the_monitor():
    assert tools.get_connectivity() == {"online": None, "since": None}
    netcheck.start(interval=60, connect=lambda h, p, t: False)
    try:
        netcheck._monitor.step(False)
        assert tools.get_connectivity()["online"] is False
    finally:
        netcheck.stop()
    assert "get_connectivity" in tools.NO_ARG_TOOLS


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def test_connectivity_text():
    assert "haven't checked" in T.connectivity_text({"online": None, "since": None})
    assert T.connectivity_text({"online": True, "since": None}).startswith("Yes, the internet is working")
    since = (NOW - timedelta(minutes=95)).isoformat()
    down = T.connectivity_text({"online": False, "since": since}, now=NOW)
    assert down.startswith("No, the internet is down and has been for 1 hours 35 minutes.")
    assert "call or text 911" in down
    assert T.connectivity_text({"online": False, "since": None}).startswith("No, the internet is down.")


@pytest.mark.parametrize("text", [
    "Is the internet working?", "Are we online?", "Is the wifi working?", "Is the connection down?",
    "Do we have internet?",
])
def test_router_sends_connectivity_questions_to_the_tool(text):
    r = router.route(text)
    assert r["intent"] == "connectivity" and r["path"] == "tool" and r["tools"] == ["get_connectivity"]


@pytest.mark.parametrize("text", [
    "Can I call 911 if the internet is down?",
    "What is the wifi password?",
    "My phone has no connection, what do I do?",
    "How do I reset the router?",
    "What's the temperature?",
])
def test_router_keeps_other_questions_away_from_connectivity(text):
    assert router.route(text)["intent"] != "connectivity"


def test_starting_offline_then_recovering_early_says_nothing():
    m, events = monitor()
    feed(m, [False, False, True])                        # blip at startup, never announced as lost
    assert events == [] and m.status()["online"] is True
    feed(m, [True, False, False, False])
    assert [e["state"] for e in events] == ["lost"]


def test_starting_offline_shows_offline_status_right_away():
    m, _ = monitor()
    feed(m, [False])
    assert m.status()["online"] is False
