"""Tests for the four fixes: canned cache, weather + labeled prompt, relevance floor, unique sources."""
import json
import re

import numpy as np
import ollama
import pytest

from ai.agent import agent, canned, config, prompt, rag, router, templates, tools, trace
from ai.tests.conftest import make_state


# ================================================================= 1. canned embeddings are cached
@pytest.fixture
def canned_files(tmp_path, monkeypatch):
    """A tiny canned.json plus cache files in a temp folder, and a fake embedder that counts calls."""
    entries = [{"id": "a", "questions": ["alpha one", "alpha two", "alpha three"], "answer": "A", "source": "S"},
               {"id": "b", "questions": ["beta one", "beta two", "beta three"], "answer": "B", "source": "S"}]
    path = tmp_path / "canned.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(config, "CANNED_FILE", path)
    monkeypatch.setattr(config, "CANNED_VECTORS", tmp_path / "canned.npy")
    monkeypatch.setattr(config, "CANNED_META", tmp_path / "canned_meta.json")
    calls = []

    def fake_embed(texts, prefix=config.QUERY_PREFIX, timeout=None):
        calls.append(list(texts))
        return [[1.0, 0.0] if t.startswith("alpha") else [0.0, 1.0] for t in texts]
    monkeypatch.setattr(rag, "embed_texts", fake_embed)
    monkeypatch.setattr(rag, "embed_query", lambda text: (1.0, 0.0) if text.startswith("alpha") else (0.0, 1.0))
    return path, calls


def test_canned_embeds_questions_once_and_saves_them(canned_files):
    path, calls = canned_files
    canned.load()
    assert sum(len(c) for c in calls) == 6                      # every question embedded once...
    assert config.CANNED_VECTORS.exists() and config.CANNED_META.exists()   # ...and saved
    canned._cache = None
    calls.clear()
    canned.load()                                               # second start: read from disk
    assert calls == []
    assert canned.match("alpha please")["id"] == "a"


def test_each_canned_check_costs_one_embedding_not_the_whole_list(canned_files, monkeypatch):
    path, calls = canned_files
    canned.load()
    asked = []
    monkeypatch.setattr(rag, "embed_query", lambda text: asked.append(text) or (0.0, 1.0))
    canned.match("beta anything")
    canned.match("beta again")
    assert asked == ["beta anything", "beta again"] and len(calls) == 1   # no per-question re-embedding


def test_stale_cache_is_rebuilt_when_canned_json_changes(canned_files):
    path, calls = canned_files
    canned.load()
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries.append({"id": "c", "questions": ["gamma one", "gamma two", "gamma three"], "answer": "C", "source": "S"})
    path.write_text(json.dumps(entries), encoding="utf-8")
    canned._cache = None
    calls.clear()
    canned.load()
    assert sum(len(c) for c in calls) == 9                      # re-embedded because the file changed


def test_failed_cache_load_is_not_retried_on_every_question(canned_files, monkeypatch):
    path, calls = canned_files
    attempts = []

    def timeout(*a, **k):
        attempts.append(1)
        raise TimeoutError("ReadTimeout")
    monkeypatch.setattr(rag, "embed_texts", timeout)
    with pytest.raises(TimeoutError):
        canned.best("alpha")
    for _ in range(3):                                          # cooldown: fails fast, no new embedding attempt
        with pytest.raises(RuntimeError):
            canned.best("alpha")
    assert len(attempts) == 1


def test_agent_skips_canned_quickly_when_cache_is_broken_then_uses_rag(monkeypatch, box):
    box()
    monkeypatch.setattr(canned, "best", lambda t: (_ for _ in ()).throw(RuntimeError("canned cache unavailable")))
    chunk = {"id": "w-1", "title": "Water", "heading": "h", "topic": "water", "source": "EPA <https://epa.gov>",
             "url": "https://epa.gov", "text": "Boil water for 1 minute.", "score": 0.80}
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: [chunk])
    monkeypatch.setattr(agent, "_generate", lambda m, t: "Boil it.")
    r = agent.answer("How do I make water safe to drink?")
    assert r["route"] == "rag" and any("canned skipped" in n for n in trace.get(r["trace_id"])["_trace"].notes)


def test_embedding_calls_ask_ollama_to_keep_the_model_loaded(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

        def embed(self, **kw):
            seen.update(kw)
            return {"embeddings": [[0.1, 0.2]]}
    monkeypatch.setattr(ollama, "Client", FakeClient)
    rag.embed_texts(["q"])
    assert seen["keep_alive"] == config.LLM_KEEP_ALIVE and seen["timeout"] == config.EMBED_TIMEOUT_S
    rag.embed_texts(["q"], timeout=config.EMBED_LOAD_TIMEOUT_S)
    assert seen["timeout"] == config.EMBED_LOAD_TIMEOUT_S


# ================================================================= 2a. weather question -> tool path
@pytest.mark.parametrize("text", [
    "What's the weather like outside?", "How's the weather?", "What are the conditions?",
    "How's it looking out there?", "What's it like outside?", "How is it outside?",
])
def test_weather_questions_route_to_tools(text):
    r = router.route(text)
    assert r["intent"] == "weather" and r["path"] == "tool"
    assert r["tools"] == ["get_sensors", "get_storm_status"]


@pytest.mark.parametrize("text", [
    "How do I run a generator outside?", "Can I use a grill outside?", "Is it safe to go outside?",
])
def test_outside_in_other_questions_is_not_weather(text):
    assert router.route(text)["intent"] != "weather"


def test_weather_answer_says_inside_and_uses_template_no_llm(monkeypatch, box):
    box(temp_c=29.0, humidity=70, pressure_hpa=1015.0,
        storm={"phase": "steady", "risk": 0.02, "alert": False, "reason": None, "model_version": "v"})

    def boom(*a, **k):
        raise AssertionError("Ollama was called")
    monkeypatch.setattr(ollama, "Client", boom)
    r = agent.answer("What's the weather like outside?")
    assert r["route"] == "tool"
    a = r["answer"]
    assert "Inside the house it's 29 degrees Celsius" in a
    assert "Pressure is 1015 hectopascals and steady" in a
    assert "Storm risk score is 2 percent" in a
    assert "Outside, the box reads wind 5 miles per hour and no rain measured." in a   # box() defaults
    assert "indoor readings" in a and "forecast" not in a      # works offline: never points to a forecast


@pytest.mark.parametrize("phase,word", [("falling", "falling"), ("recovering", "rising"), ("steady", "steady")])
def test_weather_trend_words(box, phase, word):
    box(storm={"phase": phase, "risk": 0.5, "alert": False, "reason": None, "model_version": "v"})
    assert f"hectopascals and {word}." in agent.answer("What's the weather like?")["answer"]


def test_weather_warming_up_and_replay_and_missing_sensors(box):
    box(storm={"phase": None, "risk": None, "alert": False, "reason": None, "model_version": None})
    assert "warming up" in agent.answer("How's the weather?")["answer"]
    box(source="replay")
    assert agent.answer("How's the weather?")["answer"].startswith("Replay:")
    box(temp_c=None, pressure_hpa=None)
    assert agent.answer("How's the weather?")["answer"] == "I don't have sensor readings yet."


# ================================================================= 2b. labeled readings in the prompt
def test_every_reading_in_the_prompt_has_a_label_and_unit():
    state = make_state(pressure_hpa=1015.0, humidity=None, wind_mph=5,
                       alarms=[{"id": "flood", "level": "alarm", "since": "x"}])
    line = prompt.readings_line(state)
    assert "pressure: 1015 hPa (sea level)" in line
    assert "temperature: 29 C (inside the house)" in line
    assert "humidity: not measured" in line
    assert "wind: 5 mph" in line
    assert "active alarms: flood" in line and "wall power: ON" in line and "data: live" in line
    for item in line.split(", "):                           # no unlabeled number anywhere
        assert re.match(r"^[a-z ]+: ", item), item


def test_prompt_shows_trend_and_replay_and_no_alarms():
    state = make_state(source="replay", storm={"phase": "falling"})
    line = prompt.readings_line(state)
    assert "pressure trend: falling" in line and "data: REPLAY of a past storm" in line
    assert "active alarms: none" in line


def test_prompt_pressure_is_converted_to_sea_level(monkeypatch):
    monkeypatch.setattr(config, "ALTITUDE_M", 100.0)
    line = prompt.readings_line(make_state(pressure_hpa=1002.0, temp_c=25.0))
    value = int(re.search(r"pressure: (\d+) hPa", line).group(1))
    assert 1012 <= value <= 1016                             # ~12 hPa higher than the raw reading


def test_full_prompt_contains_the_labeled_line(box):
    chunk = {"id": "w", "title": "T", "heading": "H", "source": "S", "text": "text here", "score": 0.9}
    text = prompt.build_prompt("Q?", make_state(), [chunk])
    assert "READINGS: pressure: 1015 hPa (sea level)" in text
    assert "temp:" not in text and "humidity level" not in text


# ================================================================= 3. relevance floor (see test_agent.py too)
def test_off_topic_france_gets_the_no_match_reply(monkeypatch, box):
    box()
    monkeypatch.setattr(canned, "best", lambda t: {"id": "x", "answer": "A", "source": "S", "score": 0.5, "runner_up": 0.4})
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: [{"id": "c", "title": "T", "score": 0.485}])
    monkeypatch.setattr(agent, "_generate", lambda *a: pytest.fail("LLM must not run below the floor"))
    r = agent.answer("What's the capital of France?")
    assert r["route"] == "no_match" and r["answer"].startswith("I'm not sure about that.")


# ================================================================= 4. each source listed once
def chunk(title, source, url="https://x"):
    return {"title": title, "source": source, "url": url}


def test_same_source_from_many_chunks_is_listed_once():
    chunks = [chunk("Floods", "Ready.gov (FEMA) - Floods <https://ready.gov/floods>"),
              chunk("Floods", "Ready.gov (FEMA) - Floods <https://ready.gov/floods>"),
              chunk("Hurricane", "Ready.gov (FEMA) - Floods <https://ready.gov/floods>")]
    out = rag.sources_of(chunks)
    assert [s["source"] for s in out] == ["Ready.gov (FEMA) - Floods"]
    assert out[0]["url"] == "https://ready.gov/floods"


def test_combined_source_strings_are_split_and_deduplicated():
    chunks = [chunk("A", "Ready.gov <https://r.gov>; CDC - Flood safety <https://cdc.gov/f>"),
              chunk("B", "CDC - Flood safety <https://cdc.gov/f>; NWS <https://nws.gov>"),
              chunk("C", "StormBox team")]
    names = [s["source"] for s in rag.sources_of(chunks)]
    assert names == ["Ready.gov", "CDC - Flood safety", "NWS", "StormBox team"]
    assert len(names) == len(set(names))


def test_canned_sources_split_and_unique(monkeypatch, box):
    box()
    monkeypatch.setattr(canned, "best", lambda t: {"id": "x", "answer": "A", "score": 0.95, "runner_up": 0.5,
                                                   "source": "Ready.gov - Water; Florida DEM; Ready.gov - Water"})
    r = agent.answer("How much water per person?")
    assert [s["source"] for s in r["sources"]] == ["Ready.gov - Water", "Florida DEM"]


def test_rag_answer_lists_a_shared_source_once(monkeypatch, box):
    box()
    monkeypatch.setattr(canned, "best", lambda t: {"id": "x", "answer": "A", "source": "S", "score": 0.4, "runner_up": 0.3})
    same = "CDC - After a hurricane <https://cdc.gov/h>"
    chunks = [{"id": f"c{i}", "title": f"Guide {i}", "heading": "h", "topic": "after", "source": same,
               "url": "https://cdc.gov/h", "text": "text", "score": 0.8} for i in range(3)]
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: chunks)
    monkeypatch.setattr(agent, "_generate", lambda m, t: "Answer.")
    r = agent.answer("What should I do after the storm?")
    assert [s["source"] for s in r["sources"]] == ["CDC - After a hurricane"]


# ================================================================= sea-level helper used by tools
def test_get_sensors_adds_sea_level_pressure(monkeypatch, box):
    monkeypatch.setattr(config, "ALTITUDE_M", 100.0)
    monkeypatch.setattr(config, "CAL_OFFSET_HPA", 1.0)
    box(pressure_hpa=1002.0, temp_c=25.0)
    s = tools.get_sensors()
    assert s["pressure_hpa"] == 1002.0 and 1013 <= s["pressure_sea_level_hpa"] <= 1017
    box(pressure_hpa=1002.0, source="replay")                  # replay is already sea level
    assert tools.get_sensors()["pressure_sea_level_hpa"] == 1002.0
    assert templates.sensors_text(tools.get_sensors(), "replay").startswith("Replay: Right now: pressure 1002")
