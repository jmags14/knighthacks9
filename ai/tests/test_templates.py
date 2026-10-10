"""P0 / P2 templates: LCD width, replay marking, and honesty rules."""
import pytest

from ai.agent import templates as T


def test_every_lcd_line_fits_16_chars():
    for key, entry in T.P0.items():
        for line in entry["lcd"]:
            assert len(line) <= 16, (key, line)


def test_every_lcd_line_fits_in_replay_mode_too():
    for key, entry in T.P0.items():
        replay = T.replay_lcd(entry["lcd"])
        assert all(len(line) <= 16 for line in replay), (key, replay)
        assert replay[0].startswith("REPLAY")


def test_replay_lcd_prefixes_when_it_fits():
    assert T.replay_lcd(("Power out", "On battery")) == ("REPLAY Power out", "On battery")


def test_replay_lcd_moves_headline_down_when_too_long():
    assert T.replay_lcd(("Water detected", "Move up now")) == ("REPLAY", "Water detected")


def test_spoken_text_gets_replay_prefix_only_for_replay():
    event = {"t": "alarm", "id": "flood", "level": "alarm", "state": "on"}
    live, replay = T.p0(event, "live"), T.p0(event, "replay")
    assert not live["say"].startswith("Replay:")
    assert replay["say"] == "Replay: " + live["say"]
    assert live["lcd"] == ("Water detected", "Move up now")


@pytest.mark.parametrize("event,key", [
    ({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"}, "flood"),
    ({"t": "alarm", "id": "heat", "level": "warn", "state": "on"}, "heat"),
    ({"t": "alarm", "id": "storm", "level": "warn", "state": "on"}, "storm_warn"),
    ({"t": "alarm", "id": "storm", "level": "alarm", "state": "on"}, "storm_severe"),
    ({"t": "alarm", "id": "wind", "level": "warn", "state": "on"}, "wind_warn"),
    ({"t": "alarm", "id": "wind", "level": "alarm", "state": "on"}, "wind_alarm"),
    ({"t": "phase", "phase": "possible_eye"}, "phase_possible_eye"),
    ({"t": "phase", "phase": "recovering"}, "phase_recovering"),
    ({"t": "power", "state": "lost"}, "power_lost"),
])
def test_event_keys(event, key):
    assert T.event_key(event) == key


@pytest.mark.parametrize("event", [
    {"t": "alarm", "id": "flood", "level": "alarm", "state": "off"},   # cleared: stay quiet
    {"t": "phase", "phase": "falling"},                                # no P0 text for this phase
    {"t": "alarm", "id": "air", "level": "alarm", "state": "on"},      # air sensor was removed
    {"t": "btn", "press": "short"},
    {},
])
def test_events_with_nothing_to_say(event):
    assert T.event_key(event) is None
    assert T.p0(event) is None


def test_no_air_template_and_never_claims_co_detection():
    assert not any(k == "air" or k.startswith("air_") for k in T.P0)
    for entry in T.P0.values():
        assert "carbon monoxide detected" not in entry["say"].lower()


def test_phase_text_always_says_to_stay_or_wait():
    for phase in ("falling", "possible_eye", "recovering", "steady", None):
        text = T.storm_status_text({"phase": phase}, [], "live").lower()
        assert "stay" in text or "wait" in text or "officials" in text or "forecast" in text, phase


def test_storm_status_lists_active_alarms_and_marks_replay():
    text = T.storm_status_text({"phase": "falling"}, [{"id": "flood"}], "replay")
    assert text.startswith("Replay:") and "flood" in text


def test_risk_text_warming_up_and_alert():
    assert "warming up" in T.risk_text({"risk": None})
    assert "high score" in T.risk_text({"risk": 0.9, "alert": True, "reason": "Storm risk 90% because x."})


def test_water_text():
    ok = {"ok": True, "days_left": 1.846, "people": 3, "pets": 1, "gallons": 6}
    assert T.water_text(ok) == "You have about 1.8 days of water: 6 gallons for 3 people and 1 pet."
    assert "Supplies screen" in T.water_text({"ok": False})


def test_battery_and_outage_text():
    assert "11.5 hours" in T.battery_text({"hours_left": 11.5, "on_battery": True})
    assert "don't have" in T.battery_text({"hours_left": None, "on_battery": False})
    assert "2 hours 15 minutes" in T.outage_text({"on_battery": True, "duration_min": 135})
    assert "no outage" in T.outage_text({"on_battery": False})


def test_sensors_text_has_celsius_and_fahrenheit():
    text = T.sensors_text({"pressure_hpa": 1013.2, "temp_c": 30.0, "wind_mph": 12.0})
    assert "30 C, 86 F" in text and "1013 hectopascals" in text and "12 miles per hour" in text
