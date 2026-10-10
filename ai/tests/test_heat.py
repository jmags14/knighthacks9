"""Heat index: NWS Rothfusz regression checked against the NWS heat index chart, plus the agent wiring."""
import pytest

from ai.agent import heat, router, templates as T


def f_to_c(f):
    return (f - 32) * 5 / 9


# (temp F, RH %, heat index F from the NWS chart). The chart rounds, so allow 1 F.
CHART = [
    (80, 40, 80), (85, 40, 85), (90, 40, 91), (95, 40, 98), (100, 40, 109), (110, 40, 136),
    (90, 50, 95), (90, 60, 100), (90, 70, 106), (90, 80, 113), (95, 50, 105), (100, 50, 118),
    (88, 60, 95),
]


@pytest.mark.parametrize("temp_f,rh,expected", CHART)
def test_matches_nws_chart(temp_f, rh, expected):
    assert abs(heat.heat_index_f(f_to_c(temp_f), rh) - expected) <= 1.0


def test_cool_air_uses_the_simple_formula():
    # (HI + T) / 2 < 80 -> no regression; heat index stays close to the temperature
    assert abs(heat.heat_index_f(f_to_c(75), 50) - 74.5) < 0.2


def test_low_humidity_adjustment_lowers_it():
    # RH < 13 and 80 <= T <= 112: the NWS subtracts a correction from the regression
    c = f_to_c(96)
    assert heat.heat_index_f(c, 10) < heat.heat_index_f(c, 13)
    assert abs(heat.heat_index_f(c, 10) - 90.4) < 0.2


def test_high_humidity_adjustment_raises_it():
    # RH > 85 and 80 <= T <= 87: the NWS adds a correction
    assert abs(heat.heat_index_f(f_to_c(86), 90) - 105.4) < 0.2


@pytest.mark.parametrize("hi,name", [
    (79, "none"), (80, "Caution"), (89, "Caution"), (90, "Extreme Caution"), (102, "Extreme Caution"),
    (103, "Danger"), (124, "Danger"), (125, "Extreme Danger"), (140, "Extreme Danger"),
])
def test_categories(hi, name):
    assert heat.category(hi) == name


def test_info_90f_60pct():
    info = heat.heat_info(f_to_c(90), 60)
    assert info["temp_f"] == 90 and info["heat_index_f"] == 100
    assert info["category"] == "Extreme Caution" and info["humidity_measured"]
    assert info["label"] == "Feels like 100°F · Extreme Caution"


def test_info_below_80_has_no_label():
    info = heat.heat_info(f_to_c(75), 50)
    assert info["feels_like"] is False and info["label"] is None and info["category"] == "none"


def test_missing_humidity_uses_temperature_alone():
    info = heat.heat_info(f_to_c(95), None)
    assert info["heat_index_f"] == 95 and info["humidity_measured"] is False
    assert heat.heat_info(None, 60) is None


# ---------------------------------------------------------------- agent text
HOT = {"temp_c": f_to_c(88), "humidity": 60}       # chart: about 95 F
COOL = {"temp_c": 22.0, "humidity": 50}


def test_sensor_answers_add_feels_like_only_when_hot():
    hot = T.weather_text(HOT, {"phase": "steady", "risk": 0.02}, focus="temperature")
    assert "It feels like 95 degrees, extreme caution level." in hot
    assert "feels like" not in T.weather_text(COOL, {"phase": "steady", "risk": 0.02})
    assert "feels like 95 degrees, extreme caution level" in T.sensors_text(HOT)
    assert "feels like" not in T.sensors_text(COOL)


def test_weather_text_says_humidity_not_measured():
    s = {"temp_c": f_to_c(95), "humidity": None}
    assert "(humidity not measured)" in T.weather_text(s, {"phase": "steady", "risk": 0.02})


def test_heat_index_text():
    assert "88 degrees Fahrenheit but feels like 95 degrees, extreme caution level" in T.heat_index_text(HOT)
    assert "not a concern" in T.heat_index_text(COOL)
    assert "Humidity not measured" in T.heat_index_text({"temp_c": f_to_c(95), "humidity": None})
    assert T.heat_index_text({}) == "I don't have a temperature reading yet."


def test_heat_alarm_gets_feels_like_but_lcd_unchanged():
    event = {"t": "alarm", "id": "heat", "level": "alarm", "state": "on"}
    plain = T.p0(event)
    live = T.p0(event, state={"temp_c": f_to_c(100), "humidity": 50})
    assert "It feels like 118 degrees, danger level." in live["say"]
    assert live["lcd"] == plain["lcd"] and all(len(x) <= 16 for x in live["lcd"])
    assert "feels like" not in plain["say"]
    assert "feels like" not in T.p0(event, state=COOL)["say"]


@pytest.mark.parametrize("text", [
    "How hot is it?", "How hot does it feel inside?", "What does it feel like inside?",
    "What's the heat index?", "What is the heat index right now",
])
def test_router_sends_heat_questions_to_the_heat_index(text):
    r = router.route(text)
    assert r["intent"] == "heat_index" and r["path"] == "tool" and r["tools"] == ["get_sensors"]


@pytest.mark.parametrize("text", [
    "What does heat exhaustion feel like?",
    "How hot is too hot for my dog?",
    "How hot should the oven be?",
    "Someone feels like they will faint from the heat",
])
def test_router_keeps_heat_illness_questions_as_guide_questions(text):
    assert router.route(text)["intent"] != "heat_index"
