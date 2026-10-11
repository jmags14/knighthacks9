"""Tools, the canned matcher, and the RAG topic fallback, with embeddings faked."""
from datetime import datetime, timezone

import numpy as np
import pytest

from ai.agent import canned, rag, tools


# ---------------------------------------------------------------- tools
def test_provider_state_is_read_through_tools(box):
    box(pressure_hpa=987.4, alarms=[{"id": "flood", "level": "alarm", "since": "t"}])
    assert tools.get_sensors()["pressure_hpa"] == 987.4
    assert tools.get_alarms()["alarms"][0]["id"] == "flood"
    assert tools.get_storm_status()["phase"] == "steady"


def test_missing_state_gets_safe_defaults():
    s = tools.get_state()                      # no provider installed
    assert s["alarms"] == [] and s["storm"]["risk"] is None and s["wall_power"] == 1


def test_water_calculator(box):
    box(household={"people": 3, "pets": 1}, supplies={"water_gallons": 6})
    w = tools.water_calculator()
    assert w["ok"] and round(w["days_left"], 3) == round(6 / (3 * 1.0 + 1 * 0.25), 3)
    box(household=None, supplies=None)
    assert tools.water_calculator()["ok"] is False


def test_battery_and_outage(box):
    box(wall_power=0, battery={"hours_left": 11.5}, outages={"last_lost": "2026-10-10T01:00:00Z"})
    assert tools.get_battery() == {"hours_left": 11.5, "on_battery": True}
    now = datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc)
    assert tools.get_outages(now=now)["duration_min"] == 150
    box(wall_power=1)
    assert tools.get_outages()["duration_min"] is None


def test_checkin_default_and_plugged_writer():
    first = tools.log_checkin("Sam", "ok")
    assert tools.log_checkin("Alex", "need help") == first + 1
    tools.set_checkin_writer(lambda name, status: 99)
    try:
        assert tools.log_checkin("Z", "ok") == 99
    finally:
        tools.set_checkin_writer(None)


def test_cs1_box_state_shape_is_converted():
    cs1 = {"ts": "t", "src": "replay", "readings": {"p": 990.0, "temp": 30.0, "hum": 80.0, "water": 5,
                                                   "rain": 0.4, "wind": 18.0},      # the box sends m/s
           "power": {"wall": False, "battery_hours": 9.0, "outage_since": "2026-10-10T01:00:00+00:00"},
           "alarms": [{"id": "wind", "level": "warn", "since": "s", "acked": False}],
           "storm": {"phase": "falling", "risk": 0.9, "model": "v1"}}
    tools.set_provider(lambda: cs1)
    s = tools.get_state()
    assert s["source"] == "replay" and s["pressure_hpa"] == 990.0
    assert s["wind_mph"] == pytest.approx(18.0 * 2.23694)         # agent speaks mph, so it converts m/s
    assert s["rain_in"] is None and s["floor_pct"] is None    # "rain" is the floor percent, never inches
    assert s["wall_power"] == 0 and s["battery"]["hours_left"] == 9.0
    assert s["storm"]["model_version"] == "v1" and s["alarms"][0]["id"] == "wind"
    assert tools.get_outages()["last_lost"] == "2026-10-10T01:00:00+00:00"


def test_search_guides_wraps_rag(monkeypatch):
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: [{"id": "a", "topic": topic, "k": k}])
    assert tools.search_guides("x", topic="water", k=2) == {"chunks": [{"id": "a", "topic": "water", "k": 2}]}


# ---------------------------------------------------------------- canned matcher (fake embeddings)
def fake_cache(monkeypatch):
    """Three canned entries in a 3-D toy space; the query vector is set by the test."""
    entries = [{"id": "a", "answer": "A", "source": "S"},
               {"id": "b", "answer": "B", "source": "S"},
               {"id": "c", "answer": "C", "source": "S"}]
    mat = np.array([[1, 0, 0], [1, 0.2, 0], [0, 0, 1]], dtype="float32")
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    canned._cache = (mat, [0, 1, 2], entries)
    return lambda v: monkeypatch.setattr(rag, "embed_query", lambda text: tuple(v))


def test_canned_exact_match(monkeypatch):
    set_query = fake_cache(monkeypatch)
    set_query([0, 0, 1])
    m = canned.match("anything")
    assert m["id"] == "c" and m["score"] > 0.99


def test_canned_rejects_sibling_with_small_margin(monkeypatch):
    set_query = fake_cache(monkeypatch)
    set_query([1, 0.1, 0])               # nearly as close to b as to a -> ambiguous
    assert canned.best("q")["id"] in ("a", "b")
    assert canned.match("q") is None


def test_canned_rejects_low_similarity(monkeypatch):
    set_query = fake_cache(monkeypatch)
    set_query([0.5, 0.5, 0.7])
    assert canned.match("q") is None


# ---------------------------------------------------------------- rag topic fallback
def test_search_retries_without_topic_when_filter_returns_under_two(monkeypatch):
    monkeypatch.setattr(rag, "embed_query", lambda text: (0.1, 0.2))
    calls = []

    def fake(qv, topic, k):
        calls.append(topic)
        return [{"id": "only"}] if topic else [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    monkeypatch.setattr(rag, "_search_vec", fake)
    assert len(rag.search("q", topic="tiny")) == 3
    assert calls == ["tiny", None]


def test_search_keeps_topic_results_when_there_are_enough(monkeypatch):
    monkeypatch.setattr(rag, "embed_query", lambda text: (0.1, 0.2))
    monkeypatch.setattr(rag, "_search_vec", lambda qv, topic, k: [{"id": "x"}, {"id": "y"}])
    assert [c["id"] for c in rag.search("q", topic="flood")] == ["x", "y"]


def test_clean_source_strips_urls():
    assert rag.clean_source("Ready.gov <https://a.gov>; EPA <https://b.gov>") == "Ready.gov; EPA"
