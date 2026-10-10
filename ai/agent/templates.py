"""Fixed sentences: P0 proactive alarms (with LCD lines) and P2 tool answers.

No LLM in here. EE 1 and CS 1 can read the exact LCD strings in P0 below.
LCD rule: every line is at most 16 characters (the Grove LCD is 16x2).
"""

LCD_WIDTH = 16

# ---------------------------------------------------------------- P0: proactive (AGENT_PLAN 5.1)
# key -> {"lcd": (line1, line2), "say": spoken text}
# Note: the air sensor was removed (PROJECT_PLAN update 2026-10-10), so there is no air alarm.
P0 = {
    "flood": {
        "lcd": ("Water detected", "Move up now"),
        "say": "Water detected on the floor. Move people and pets to higher ground and "
               "stay away from outlets and cords in water.",
    },
    "heat": {
        "lcd": ("Heat warning", "Cool down"),
        "say": "It's dangerously hot inside. Drink water, move to the coolest room, "
               "and watch kids, elders, and pets.",
    },
    "storm_warn": {
        "lcd": ("Pressure falling", "Storm nearby"),
        "say": "Pressure is falling fast. A storm may be close. Stay inside, away from windows.",
    },
    "storm_severe": {
        "lcd": ("STORM ALERT", "Stay inside"),
        "say": "Pressure is dropping very fast. Go to your safest interior room now.",
    },
    # "say_live" is used when the box reports the current wind ({w} = mph); otherwise "say".
    "wind_warn": {
        "lcd": ("High winds", "Avoid windows"),
        "say": "Winds are high, 39 miles per hour or more. Stay inside and away from windows.",
        "say_live": "Winds are high, 39 miles per hour or more. Right now the wind is {w} miles per hour. "
                    "Stay inside and away from windows.",
    },
    "wind_alarm": {
        "lcd": ("Extreme winds", "Go interior room"),
        "say": "Winds are at hurricane force, 74 miles per hour or more. Go to your safest "
               "interior room now and stay away from windows.",
        "say_live": "Winds are at hurricane force, 74 miles per hour or more. Right now the wind is {w} "
                    "miles per hour. Go to your safest interior room now and stay away from windows.",
    },
    "phase_possible_eye": {
        "lcd": ("Possible eye", "Stay inside"),
        "say": "This may be the eye of the storm. The calm is temporary and the wind will "
               "return. Stay inside.",
    },
    "phase_recovering": {
        "lcd": ("Pressure rising", "Wait for all-clr"),
        "say": "Pressure is rising. The worst may be passing, but wait for an official "
               "all-clear before going outside.",
    },
    "power_lost": {
        "lcd": ("Power out", "On battery"),
        "say": "Wall power is out. The box is on battery.",
    },
}


def event_key(event):
    """Map an event dict to a P0 key, or None if there is nothing to say.

    {"t":"alarm","id":"flood","level":"alarm","state":"on"}
    {"t":"phase","phase":"possible_eye"}
    {"t":"power","state":"lost"}      (new, additive: box-service can send it when wall power drops)
    """
    kind = event.get("t")
    if kind == "alarm":
        if event.get("state", "on") != "on":     # alarm cleared -> stay quiet
            return None
        aid, level = event.get("id"), event.get("level", "alarm")
        if aid in ("flood", "heat"):
            return aid
        if aid == "storm":
            return "storm_severe" if level == "alarm" else "storm_warn"
        if aid == "wind":
            return "wind_alarm" if level == "alarm" else "wind_warn"
    elif kind == "phase":
        key = "phase_" + str(event.get("phase"))
        return key if key in P0 else None        # only possible_eye / recovering have P0 text
    elif kind == "power":
        if event.get("state", event.get("event")) == "lost":
            return "power_lost"
    return None


def replay_lcd(lines):
    """Mark LCD lines as replay. Line 1 is 'REPLAY' + headline if it fits in 16 chars,
    otherwise line 1 = 'REPLAY' and the headline moves to line 2."""
    l1, l2 = lines
    if len(f"REPLAY {l1}") <= LCD_WIDTH:
        return (f"REPLAY {l1}", l2)
    return ("REPLAY", l1)


def replay_say(text, source):
    """Prefix spoken text with 'Replay:' when the data is recorded, not live."""
    return f"Replay: {text}" if source == "replay" else text


def p0(event, source="live", state=None):
    """Return {"say": str, "lcd": (l1, l2)} for an event, or None.
    state (optional) = the box state, so wind alarms can say the wind speed right now."""
    key = event_key(event)
    if key is None:
        return None
    entry = P0[key]
    say = entry["say"]
    wind = (state or {}).get("wind_mph")
    if "say_live" in entry and wind is not None:
        say = entry["say_live"].format(w=f"{wind:.0f}")
    lcd = replay_lcd(entry["lcd"]) if source == "replay" else entry["lcd"]
    return {"say": replay_say(say, source), "lcd": lcd, "key": key}


# ---------------------------------------------------------------- P2: tool answers
def storm_status_text(storm, alarms, source="live"):
    """'Is the storm over?' style answers from the current phase. Never says 'all clear'."""
    phase = storm.get("phase")
    if phase == "possible_eye":
        text = P0["phase_possible_eye"]["say"]
    elif phase == "recovering":
        text = P0["phase_recovering"]["say"]
    elif phase == "falling":
        text = ("Pressure is falling, so a storm may be getting closer. "
                "Stay inside and away from windows.")
    elif phase:
        text = ("Pressure looks steady right now, with no storm pattern showing. "
                "Still, wait for local officials before assuming it is safe outside.")
    else:
        text = ("I don't have enough pressure history yet to tell the storm phase. "
                "Stay inside if a storm is near, and ask again in a few hours.")
    active = [a["id"] for a in alarms]
    if active:
        text += " Active alarms: " + ", ".join(active) + "."
    return replay_say(text, source)


def risk_text(storm, source="live"):
    risk = storm.get("risk")
    if risk is None:
        return replay_say("Storm risk is still warming up. It needs about 7 hours of "
                          "pressure history.", source)
    text = storm.get("reason") or f"Storm risk score {round(100 * risk)}%."
    if storm.get("alert"):
        text += " That is a high score. Stay inside and get ready."
    return replay_say(text, source)


def water_text(w):
    if not w.get("ok"):
        return ("Add your household and water on the Supplies screen "
                "and I can tell you how long it will last.")
    pets = f" and {w['pets']} pet{'s' if w['pets'] != 1 else ''}" if w["pets"] else ""
    people = f"{w['people']} {'person' if w['people'] == 1 else 'people'}"
    return (f"You have about {w['days_left']:.1f} days of water: "
            f"{w['gallons']:g} gallons for {people}{pets}.")


def battery_text(b):
    hours = b.get("hours_left")
    if hours is None:
        return "I don't have a battery estimate yet."
    if b.get("on_battery"):
        return f"The box is on battery with about {hours:.1f} hours left."
    return f"Wall power is on. If it goes out, the battery should last about {hours:.1f} hours."


def _minutes_text(m):
    h, mins = divmod(int(m), 60)
    return f"{h} hours {mins} minutes" if h else f"{mins} minutes"


def outage_text(o):
    if o.get("on_battery"):
        if o.get("duration_min") is not None:
            return f"Wall power has been out for {_minutes_text(o['duration_min'])}. The box is on battery."
        return "Wall power is out. The box is on battery."
    if o.get("last_lost"):
        return f"Wall power is on. It last went out at {o['last_lost']}."
    return "Wall power is on and I have no outage recorded."


TREND_WORDS = {"falling": "falling", "recovering": "rising", "steady": "steady",
               "possible_eye": "flat near its lowest point"}


def pressure_trend(phase):
    """Storm phase -> plain trend word for speech and prompts. None if we don't know."""
    return TREND_WORDS.get(phase)


def _wind_meaning(mph):
    """Plain words for a wind speed. 39 and 74 mph match the box's wind warn / alarm levels."""
    if mph < 1:
        return "so it's calm"
    if mph < 8:
        return "a light breeze"
    if mph < 19:
        return "a steady breeze"
    if mph < 32:
        return "fairly windy"
    if mph < 39:
        return "strong wind"
    if mph < 74:
        return "tropical-storm strength, so stay away from windows"
    return "hurricane strength, so get to your safest interior room"


PRESSURE_MEANING = {
    "falling": "A steady drop like this can mean a storm is getting closer.",
    "steady": "That is steady, so there is no storm pattern right now.",
    "recovering": "It is coming back up, so the worst may be passing. Stay inside until officials say it is safe.",
    "possible_eye": "That could be the eye of a storm, so stay inside.",
}


def weather_text(s, storm, source="live", focus=None):
    """'What's the weather like outside?' / 'Is it windy?' Answered fully offline, never by sending people
    to a forecast. Every number is kept. With a focus (wind, rain, pressure, temperature) that part comes
    first. Outside clues: wind, rain and pressure (air pressure is the same inside and out); temperature
    and humidity are measured INSIDE the house, so they are labeled that way."""
    p = s.get("pressure_sea_level_hpa", s.get("pressure_hpa"))
    if s.get("temp_c") is None and p is None:
        return "I don't have sensor readings yet."
    sections = {}                                               # key -> sentence(s), in the default order
    if s.get("wind_mph") is not None:
        w = s["wind_mph"]
        sections["wind"] = f"Outside, wind is {w:.0f} miles per hour, {_wind_meaning(w)}."
    if s.get("rain_in") is not None:
        sections["rain"] = ("The rain gauge shows no rain." if s["rain_in"] <= 0
                            else f"The rain gauge shows {s['rain_in']:.1f} inches of rain.")
    if p is not None:
        trend = pressure_trend(storm.get("phase"))
        text = f"Pressure is {p:.0f} hectopascals" + (f" and {trend}." if trend else ".")
        sections["pressure"] = (text + " " + PRESSURE_MEANING[storm.get("phase")]
                                if storm.get("phase") in PRESSURE_MEANING else text)
    if s.get("temp_c") is not None:
        inside = f"Inside the house it's {s['temp_c']:.0f} degrees Celsius, {s['temp_c'] * 9 / 5 + 32:.0f} Fahrenheit"
        if s.get("humidity") is not None:
            inside += f", with {s['humidity']:.0f} percent humidity"
        sections["temperature"] = inside + "."
    risk = storm.get("risk")
    sections["risk"] = ("Storm risk is still warming up." if risk is None
                        else f"Storm risk score is {round(100 * risk)} percent.")

    if focus in sections:                                       # asked about one thing: answer it first
        rest = [sections[k] for k in sections if k != focus]
        parts = [sections[focus], "Here are the other readings."] + rest
    else:
        parts = ["Here's what the box is reading."] + list(sections.values())
    parts.append("Temperature and humidity are indoor readings. Pressure, wind and rain tell you what "
                 "is happening outside.")
    return replay_say(" ".join(parts), source)


def sensors_text(s, source="live"):
    parts = []
    p = s.get("pressure_sea_level_hpa", s.get("pressure_hpa"))
    if p is not None:
        parts.append(f"pressure {p:.0f} hectopascals")
    if s.get("temp_c") is not None:
        parts.append(f"temperature {s['temp_c']:.0f} C, {s['temp_c'] * 9 / 5 + 32:.0f} F")
    if s.get("humidity") is not None:
        parts.append(f"humidity {s['humidity']:.0f} percent")
    if s.get("wind_mph") is not None:
        parts.append(f"wind {s['wind_mph']:.0f} miles per hour")
    if s.get("rain_in") is not None:
        parts.append(f"rain {s['rain_in']:.1f} inches")
    if not parts:
        return "I don't have sensor readings yet."
    return replay_say("Right now: " + ", ".join(parts) + ".", source)
