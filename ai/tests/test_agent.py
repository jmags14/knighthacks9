"""Agent paths with Ollama mocked: P0 proactive, P1 canned, P2 tool, P3 RAG + LLM, and fallbacks.

Nothing here talks to a real Ollama server. We replace the three things that would:
  canned.best      (needs an embedding)
  rag.search       (needs an embedding)
  agent._generate  (the LLM call)
"""
import ollama
import pytest

from ai.agent import agent, canned, config, prompt, rag, trace

CHUNKS = [
    {"id": "food-safety-04", "title": "Food Safety Without Power", "heading": "Can I refreeze thawed food?",
     "topic": "food", "source": "FoodSafety.gov <https://www.foodsafety.gov/>", "url": "https://www.foodsafety.gov/",
     "text": "Throw away ice cream and frozen yogurt that partly thawed.", "score": 0.80},
    {"id": "food-safety-02", "title": "Food Safety Without Power", "heading": "Foods to throw away",
     "topic": "food", "source": "FoodSafety.gov <https://www.foodsafety.gov/>", "url": "https://www.foodsafety.gov/",
     "text": "Throw away meat, milk, and eggs after 4 hours.", "score": 0.70},
]


def canned_hit(score=0.95, runner_up=0.60):
    return {"id": "x", "answer": "CANNED ANSWER", "source": "Ready.gov - Water", "score": score, "runner_up": runner_up}


@pytest.fixture
def no_ollama(monkeypatch):
    """Any attempt to reach Ollama fails the test. Used to prove P0 and P2 never need it."""
    def boom(*a, **k):
        raise AssertionError("Ollama was called")
    monkeypatch.setattr(ollama, "Client", boom)
    monkeypatch.setattr(canned, "best", boom)
    monkeypatch.setattr(rag, "search", boom)


@pytest.fixture
def llm(monkeypatch):
    """Fake guide search + fake LLM. Returns the list of (model, prompt) calls."""
    calls = []
    monkeypatch.setattr(canned, "best", lambda text: canned_hit(0.5, 0.45))     # no canned match
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: [dict(c) for c in CHUNKS])

    def fake_generate(model, text):
        calls.append((model, text))
        return "Throw it away."
    monkeypatch.setattr(agent, "_generate", fake_generate)
    return calls


def get_trace(result):
    return trace.get(result["trace_id"])["_trace"]


# ---------------------------------------------------------------- P0: proactive
def test_p0_flood_alarm_uses_template_not_llm(no_ollama, box):
    box()
    r = agent.explain_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})
    assert r["route"] == "proactive"
    assert r["lcd"] == ("Water detected", "Move up now")
    assert r["answer"].startswith("Water detected on the floor")
    assert isinstance(r["trace_id"], int)


def test_p0_replay_is_marked_from_box_state(no_ollama, box):
    box(source="replay")
    r = agent.explain_event({"t": "phase", "phase": "possible_eye"})
    assert r["answer"].startswith("Replay:")
    assert r["lcd"][0] == "REPLAY"


def test_p0_event_can_carry_its_own_source(no_ollama, box):
    box(source="live")
    r = agent.explain_event({"t": "alarm", "id": "heat", "level": "warn", "state": "on", "source": "replay"})
    assert r["answer"].startswith("Replay:") and r["lcd"][0].startswith("REPLAY")


def test_p0_nothing_to_say_for_cleared_alarm(no_ollama, box):
    box()
    r = agent.explain_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "off"})
    assert r["answer"] == "" and r["route"] == "proactive"


def test_p0_trace_is_saved_with_proactive_channel(no_ollama, box):
    box()
    r = agent.explain_event({"t": "power", "state": "lost"})
    t = trace.get(r["trace_id"])
    assert t["channel"] == "proactive" and t["route"] == "proactive"
    assert t["answer"] == "Wall power is out. The box is on battery."


# ---------------------------------------------------------------- P2: tools
def test_p2_storm_question_reads_live_phase(no_ollama, box):
    box(storm={"phase": "possible_eye", "risk": 0.9, "alert": True, "reason": "r", "model_version": "v"})
    r = agent.answer("Is the storm over?")
    assert r["route"] == "tool"
    assert "eye of the storm" in r["answer"] and "Stay inside" in r["answer"]
    assert get_trace(r).intent == "storm_status"
    assert "get_storm_status" in get_trace(r).tools


def test_p2_water_battery_outage_sensors(no_ollama, box):
    box(wall_power=0, battery={"hours_left": 11.5}, outages={"last_lost": "2026-10-10T01:00:00Z"})
    assert "1.8 days" in agent.answer("How long will our water last?")["answer"]
    assert "11.5 hours" in agent.answer("How much battery is left?")["answer"]
    assert "Wall power has been out" in agent.answer("When did the power go out?")["answer"]
    assert "29 C" in agent.answer("What's the temperature?")["answer"]


def test_p2_replay_storm_answer_is_marked(no_ollama, box):
    box(source="replay")
    assert agent.answer("What is the storm risk?")["answer"].startswith("Replay:")


def test_p2_flood_question_with_flood_alarm_gives_alarm_text(no_ollama, box):
    box(alarms=[{"id": "flood", "level": "alarm", "since": "x"}])
    r = agent.answer("Water is coming under the door, what should I do?")
    assert r["route"] == "tool"
    assert r["answer"].startswith("Water detected on the floor")


def test_flood_question_without_alarm_falls_through_to_canned(monkeypatch, box):
    box()
    monkeypatch.setattr(canned, "best", lambda text: canned_hit())
    r = agent.answer("Water is coming under the door, what should I do?")
    assert r["route"] == "canned" and r["answer"] == "CANNED ANSWER"


def test_empty_question(no_ollama, box):
    box()
    assert "didn't catch" in agent.answer("   ")["answer"]


# ---------------------------------------------------------------- P1: canned
def test_p1_canned_answer_returns_without_llm(monkeypatch, box):
    box()
    monkeypatch.setattr(canned, "best", lambda text: canned_hit())
    monkeypatch.setattr(agent, "_generate", lambda *a: pytest.fail("LLM must not run for canned"))
    r = agent.answer("How do I purify water?")
    assert r["route"] == "canned"
    assert r["answer"] == "CANNED ANSWER"
    assert r["sources"][0]["source"] == "Ready.gov - Water"
    assert get_trace(r).canned_score == 0.95


def test_p1_low_score_or_small_margin_is_rejected(llm, box):
    box()
    for score, runner in ((0.80, 0.50), (0.90, 0.88)):      # too low / too close to the runner-up
        canned.best = lambda text, s=score, r_=runner: canned_hit(s, r_)
        assert agent.answer("something about food")["route"] == "rag"


# ---------------------------------------------------------------- P3: RAG + LLM
def test_p3_rag_calls_primary_model_with_passages(llm, box):
    box()
    r = agent.answer("Is it safe to eat the ice cream that melted?")
    assert r["route"] == "rag" and r["answer"] == "Throw it away."
    model, text = llm[0]
    assert model == config.LLM_MODEL and len(llm) == 1
    assert "Throw away ice cream" in text and "QUESTION: Is it safe" in text
    assert [s["title"] for s in r["sources"]] == ["Food Safety Without Power"]   # de-duplicated
    assert r["sources"][0]["source"] == "FoodSafety.gov"                         # url stripped for display
    t = get_trace(r)
    assert t.model == config.LLM_MODEL and [c["id"] for c in t.chunks] == ["food-safety-04", "food-safety-02"]
    assert "llm:" + config.LLM_MODEL in t.timings


def test_p3_falls_back_to_qwen_when_primary_fails(monkeypatch, llm, box):
    box()
    calls = []

    def flaky(model, text):
        calls.append(model)
        if model == config.LLM_MODEL:
            raise TimeoutError("too slow")
        return "From the fallback."
    monkeypatch.setattr(agent, "_generate", flaky)
    r = agent.answer("Is it safe to eat the ice cream that melted?")
    assert r["answer"] == "From the fallback."
    assert calls == [config.LLM_MODEL, config.LLM_FALLBACK]
    t = get_trace(r)
    assert t.model == config.LLM_FALLBACK and any("TimeoutError" in n for n in t.notes)


def test_p3_empty_primary_answer_also_triggers_fallback(monkeypatch, llm, box):
    box()
    monkeypatch.setattr(agent, "_generate", lambda model, text: "" if model == config.LLM_MODEL else "Fallback.")
    assert agent.answer("Is it safe to eat the ice cream that melted?")["answer"] == "Fallback."


def test_p3_both_models_fail_returns_top_guide_chunk(monkeypatch, llm, box):
    box()

    def dead(model, text):
        raise ConnectionError("ollama down")
    monkeypatch.setattr(agent, "_generate", dead)
    r = agent.answer("Is it safe to eat the ice cream that melted?")
    assert r["answer"] == "Here's what the guide says: Throw away ice cream and frozen yogurt that partly thawed."
    assert r["route"] == "rag" and len(r["sources"]) == 1


def test_p3_off_topic_skips_llm(monkeypatch, llm, box):
    box()
    low = [dict(CHUNKS[0], score=0.30)]
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: low)
    r = agent.answer("What's the capital of France?")
    assert r["answer"] == agent.OFF_TOPIC and llm == []


def test_p3_weak_match_says_not_sure_with_closest_guide(monkeypatch, llm, box):
    box()
    weak = [dict(CHUNKS[0], score=0.55)]
    monkeypatch.setattr(rag, "search", lambda q, topic=None, k=3: weak)
    r = agent.answer("Should I open windows to equalize pressure?")
    assert r["answer"] == "I'm not sure. The closest guide is: Food Safety Without Power."
    assert llm == [] and r["sources"][0]["title"] == "Food Safety Without Power"


def test_p3_passes_topic_to_search(monkeypatch, llm, box):
    box()
    seen = {}

    def spy(q, topic=None, k=3):
        seen["topic"], seen["k"] = topic, k
        return [dict(c) for c in CHUNKS]
    monkeypatch.setattr(rag, "search", spy)
    agent.answer("Someone has a deep cut, what do I do?")
    assert seen == {"topic": "first-aid", "k": 3}


def test_ollama_down_for_canned_and_rag_gives_safe_message(monkeypatch, box):
    box()

    def down(*a, **k):
        raise ConnectionError("no ollama")
    monkeypatch.setattr(canned, "best", down)
    monkeypatch.setattr(rag, "search", down)
    r = agent.answer("How do I purify water?")
    assert r["answer"] == agent.NO_SEARCH and "911" in r["answer"] and r["route"] == "rag"
    assert any("canned skipped" in n for n in get_trace(r).notes)


def test_qwen_is_called_with_thinking_off(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def generate(self, **kw):
            seen.update(kw)
            return {"response": "<think>hmm</think>Stay inside."}
    monkeypatch.setattr(ollama, "Client", FakeClient)
    assert agent._generate(config.LLM_FALLBACK, "p") == "Stay inside."     # <think> block stripped
    assert seen["think"] is False and seen["keep_alive"] == config.LLM_KEEP_ALIVE
    assert seen["options"] == {"num_predict": 120, "temperature": 0.2}
    seen.clear()
    agent._generate(config.LLM_MODEL, "p")
    assert "think" not in seen                                              # llama gets no think flag


# ---------------------------------------------------------------- safety around the LLM
def test_prompt_mentions_active_alarm_so_llm_cannot_contradict_it(llm, box):
    box(alarms=[{"id": "flood", "level": "alarm", "since": "x"}])
    agent.answer("Is it safe to eat the ice cream that melted?")
    text = llm[0][1]
    assert "flood alarm ON" in text and prompt.RULE_ALARM in text


def test_prompt_stays_under_400_words_even_with_long_passages():
    long_chunks = [dict(CHUNKS[0], text="word " * 200) for _ in range(3)]
    state = {"pressure_hpa": 987, "temp_c": 31, "alarms": [{"id": "flood"}], "wall_power": 0, "storm": {}}
    assert len(prompt.build_prompt("Is it safe?", state, long_chunks, "air").split()) <= 400


def test_co_rule_only_appears_for_air_questions():
    state = {"alarms": []}
    assert prompt.RULE_AIR in prompt.system_prompt("Can I run a generator inside?", state)
    assert prompt.RULE_AIR not in prompt.system_prompt("How do I purify water?", state, "water")
    assert prompt.RULE_911 in prompt.system_prompt("Someone is bleeding", state, "first-aid")


# ---------------------------------------------------------------- traces
def test_trace_row_is_written_to_sqlite_when_the_table_exists(tmp_path, no_ollama, box):
    import sqlite3
    db = tmp_path / "box.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE agent_traces (id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, question TEXT,
                   route TEXT, tools_json TEXT, chunks_json TEXT, sensors_json TEXT, answer TEXT, timings_json TEXT)""")
    con.commit()
    con.close()
    trace.configure(db)
    box()
    r = agent.answer("How long will our water last?", channel="voice")
    row = sqlite3.connect(db).execute("SELECT channel, question, route, answer FROM agent_traces").fetchall()
    assert row == [("voice", "How long will our water last?", "tool", r["answer"])]
    assert r["trace_id"] == 1


def test_trace_falls_back_to_memory_when_there_is_no_database(tmp_path, no_ollama, box):
    trace.configure(tmp_path / "missing.db")
    box()
    r = agent.answer("How much battery is left?")
    assert trace.get(r["trace_id"])["route"] == "tool"
