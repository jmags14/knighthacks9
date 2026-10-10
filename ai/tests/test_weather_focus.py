"""Weather/sensor answers lead with what was asked, keep every number, and need no LLM."""
import pytest

from ai.agent import agent, router, templates

STORM = {"phase": "falling", "risk": 0.98, "alert": True, "reason": "r", "model_version": "v"}
SENSORS = {"pressure_hpa": 996.0, "temp_c": 29.0, "humidity": 70, "wind_mph": 45.0, "rain_in": 0.0}


@pytest.mark.parametrize("text,focus", [
    ("Is it windy outside?", "wind"), ("How strong is the wind?", "wind"),
    ("Is it raining?", "rain"), ("What's the pressure?", "pressure"),
    ("What's the temperature?", "temperature"), ("How humid is it?", "temperature"),
    ("What's the weather like?", None),
])
def test_router_finds_the_focus(text, focus):
    r = router.route(text)
    assert r["path"] == "tool" and r["focus"] == focus


def test_full_summary_keeps_every_number_in_natural_order():
    a = templates.weather_text(SENSORS, STORM)
    assert a.startswith("Here's what the box is reading. Outside, wind is 45 miles per hour")
    for needle in ("45 miles per hour", "tropical-storm strength", "no rain", "996 hectopascals and falling",
                   "storm is getting closer", "29 degrees Celsius, 84 Fahrenheit", "70 percent humidity",
                   "Storm risk score is 98 percent", "indoor readings"):
        assert needle in a, needle


@pytest.mark.parametrize("focus,first", [
    ("wind", "Outside, wind is 45 miles per hour"), ("rain", "The rain gauge shows no rain."),
    ("pressure", "Pressure is 996 hectopascals and falling."), ("temperature", "Inside the house it's 29 degrees"),
])
def test_focus_goes_first_and_nothing_is_dropped(focus, first):
    a = templates.weather_text(SENSORS, STORM, focus=focus)
    assert a.startswith(first) and "Here are the other readings." in a
    assert a.count("45 miles per hour") == 1 and "996 hectopascals" in a and "98 percent" in a


@pytest.mark.parametrize("mph,words", [(0, "calm"), (5, "light breeze"), (15, "steady breeze"), (25, "fairly windy"),
                                       (35, "strong wind"), (45, "stay away from windows"),
                                       (80, "safest interior room")])
def test_wind_meaning_bands(mph, words):
    assert words in templates._wind_meaning(mph)


def test_rain_amount_and_missing_sensors_and_replay():
    assert "0.8 inches of rain" in templates.weather_text({**SENSORS, "rain_in": 0.8}, STORM)
    no_wind = {k: v for k, v in SENSORS.items() if k not in ("wind_mph", "rain_in")}
    a = templates.weather_text(no_wind, STORM, focus="wind")             # asked about wind, box has none
    assert a.startswith("Here's what the box is reading.") and "Outside, wind" not in a
    assert templates.weather_text(SENSORS, STORM, "replay").startswith("Replay: ")


def test_agent_answers_focus_questions_without_the_llm(box, monkeypatch):
    import ollama
    box(wind_mph=45.0, storm=STORM)
    monkeypatch.setattr(ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
    r = agent.answer("Is it windy outside?")
    assert r["route"] == "tool" and r["answer"].startswith("Outside, wind is 45 miles per hour")


# ---------------------------------------------------------------- wind alarms say the live wind
WIND_WARN = {"t": "alarm", "id": "wind", "level": "warn", "state": "on"}
WIND_ALARM = {"t": "alarm", "id": "wind", "level": "alarm", "state": "on"}


def test_wind_warn_event_says_current_wind(box):
    box(wind_mph=45.0)
    r = agent.explain_event(WIND_WARN)
    assert r["answer"] == ("Winds are high, 39 miles per hour or more. Right now the wind is 45 miles per hour. "
                           "Stay inside and away from windows.")
    assert r["lcd"] == ("High winds", "Avoid windows")                 # LCD text is unchanged


def test_wind_alarm_event_says_current_wind_and_keeps_replay_prefix(box):
    box(wind_mph=81.0, source="replay")
    r = agent.explain_event(WIND_ALARM)
    assert r["answer"].startswith("Replay: Winds are at hurricane force, 74 miles per hour or more. "
                                  "Right now the wind is 81 miles per hour.")
    assert r["lcd"][0].startswith("REPLAY")


def test_wind_event_without_a_wind_reading_uses_the_old_text(box):
    box(wind_mph=None)
    assert agent.explain_event(WIND_WARN)["answer"] == \
        "Winds are high, 39 miles per hour or more. Stay inside and away from windows."


def test_other_events_are_unchanged_by_wind(box):
    box(wind_mph=45.0)
    assert "wind" not in agent.explain_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})["answer"]
