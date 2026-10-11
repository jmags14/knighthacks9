"""The box has no rain gauge: the firmware's "rain" field is the floor moisture percent.
The agent describes it only as the floor (dry / damp / wet) and never as inches of rain."""
import pytest

from ai.agent import agent, config, prompt, router, templates as T, tools

STORM = {"phase": "steady", "risk": 0.02}


def state(**over):
    s = {"water": 20, "alarms": []}
    s.update(over)
    return s


@pytest.mark.parametrize("pct,word", [(0, "dry"), (9, "dry"), (10, "damp"), (69, "damp"), (70, "wet"), (100, "wet")])
def test_floor_word_from_percent(pct, word):
    assert tools.floor_state(state(floor_pct=pct, water=None)) == word


@pytest.mark.parametrize("raw,word", [(0, "dry"), (249, "dry"), (250, "damp"), (1700, "damp"), (1750, "wet"), (4000, "wet")])
def test_floor_word_from_raw_reading(raw, word):
    assert tools.floor_state(state(water=raw)) == word


def test_floor_percent_beats_raw_and_flood_alarm_means_wet():
    assert tools.floor_state(state(water=4000, floor_pct=0)) == "dry"
    assert tools.floor_state(state(water=0, alarms=[{"id": "flood"}])) == "wet"
    assert tools.floor_state(state(water=None)) is None


def test_firmware_rain_percent_is_never_read_as_inches():
    cs1 = {"readings": {"p": 1000.0, "temp": 29.0, "hum": 70.0, "water": 1125, "rain": 45}, "power": {},
           "alarms": [], "storm": {}}                     # exactly what the ESP32 sends: rain = 45 (percent)
    tools.set_provider(lambda: cs1)
    s = tools.get_sensors()
    assert s["rain_in"] is None and s["floor"] == "damp" and "water" not in s
    a = agent.answer("What's the weather like outside?")["answer"]
    assert "45" not in a and "inches" not in a and "rain gauge" not in a
    assert "The floor is damp." in a
    assert "1125" not in agent.answer("Show me the sensor readings")["answer"]


def test_rain_in_is_used_only_when_a_real_gauge_sends_it():
    cs1 = {"readings": {"p": 1000.0, "temp": 29.0, "water": 5, "rain_in": 0.8}, "power": {}, "alarms": [], "storm": {}}
    tools.set_provider(lambda: cs1)
    assert tools.get_sensors()["rain_in"] == 0.8
    a = T.weather_text(tools.get_sensors(), STORM)
    assert "The rain gauge shows 0.8 inches of rain." in a and "Pressure and rain tell you what is happening outside." in a
    assert "rain 0.8 inches" in T.sensors_text(tools.get_sensors())


def test_no_gauge_the_footer_does_not_mention_rain():
    a = T.weather_text({"temp_c": 29.0, "pressure_hpa": 1000.0, "wind_mph": 5, "floor": "dry"}, STORM)
    assert "Pressure and wind tell you what is happening outside." in a and "rain" not in a.lower()


def test_rain_question_without_a_gauge_says_so():
    s = {"temp_c": 29.0, "pressure_hpa": 1000.0, "floor": "dry"}
    a = T.weather_text(s, STORM, focus="rain")
    assert a.startswith("This box doesn't have a rain gauge, so I can't measure rainfall.")
    assert "Here are the other readings." in a and "The floor is dry." in a
    assert "rain gauge" not in T.weather_text(s, STORM)      # not mentioned unless asked about rain


@pytest.mark.parametrize("floor", ["dry", "damp", "wet"])
def test_floor_sentences(floor):
    s = {"temp_c": 29.0, "pressure_hpa": 1000.0, "floor": floor}
    assert f"The floor is {floor}." in T.weather_text(s, STORM)
    assert f"the floor is {floor}" in T.sensors_text(s)


def test_nothing_about_the_floor_without_a_sensor_reading():
    s = {"temp_c": 29.0, "pressure_hpa": 1000.0, "floor": None}
    assert "floor" not in T.weather_text(s, STORM) and "floor" not in T.sensors_text(s)


def test_prompt_line_has_the_floor_word():
    assert "floor: dry" in prompt.readings_line({"water": 20, "alarms": [], "storm": {}})
    assert "floor: wet" in prompt.readings_line({"water": 0, "alarms": [{"id": "flood"}], "storm": {}})
    assert "floor: not measured" in prompt.readings_line({"alarms": [], "storm": {}})


def test_calibration_matches_the_firmware():
    fw = open(config.ROOT / "esp32" / "src" / "main.cpp", encoding="utf-8").read()
    assert f"#define WATER_WET   {config.FLOOR_WET_RAW}" in fw and f"#define WATER_DRY   {config.FLOOR_DRY_RAW}" in fw
    assert f"#define WATER_ALARM_PCT  {config.FLOOR_WET_PCT}" in fw
    assert f"#define RAIN_LIGHT_PCT  {config.FLOOR_DAMP_PCT}" in fw


@pytest.mark.parametrize("q", [
    "What's the weather like outside?", "Is it raining?", "How much rain have we had?", "How hard is it raining?",
    "Show me the sensor readings", "How's it looking out?", "Is it windy?",
])
def test_no_answer_ever_gives_rain_inches_without_a_gauge(q):
    cs1 = {"readings": {"p": 1000.0, "temp": 29.0, "hum": 70.0, "water": 1125, "rain": 45}, "power": {},
           "alarms": [], "storm": {}}
    tools.set_provider(lambda: cs1)
    try:
        a = agent.answer(q)["answer"]
    finally:
        tools.set_provider(None)
    assert "inch" not in a.lower() and "45" not in a
