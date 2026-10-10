"""The agent's tools (AGENT_PLAN 4.3). Each returns a small dict and runs in milliseconds.

Box state comes from a pluggable provider so you can feed a fake dict while testing:

    from ai.agent import tools
    tools.set_provider(lambda: {"pressure_hpa": 987.4, "alarms": [...], ...})

The provider may return the AGENT_PLAN 4.1 snapshot OR CS 1's BoxState.to_dict();
get_state() converts the second kind into the first (from_box_state).

4.1 additions (no contract change, all optional):
  wind_mph, rain_in           new sensors from PROJECT_PLAN §5
  household {"people","pets"} + supplies {"water_gallons"}   for water_calculator
  outages {"last_lost","last_restored"}                      for get_outages
"""
from datetime import datetime, timezone

from ai.agent import config, rag
from ai.ml.storm_job import runtime      # to_sea_level lives in the Colab runtime (loaded by storm_job)

_provider = None
_checkins = []                  # used when no checkin writer is set
_checkin_writer = None


def set_provider(fn):
    """fn() -> box state dict. Pass None to clear."""
    global _provider
    _provider = fn


def set_checkin_writer(fn):
    """fn(name, status) -> row id. box-service plugs in its SQLite insert."""
    global _checkin_writer
    _checkin_writer = fn


# ---------------------------------------------------------------- state
EMPTY_STATE = {"ts": None, "source": "live", "pressure_hpa": None, "temp_c": None,
               "humidity": None, "water": None, "wind_mph": None, "rain_in": None,
               "wall_power": 1, "alarms": [], "storm": {}, "battery": {}}


def _wind_to_mph(v):
    """The box's wind reading -> mph (what the agent says out loud). Unit set by config.WIND_RAW_UNIT."""
    if v is None or config.WIND_RAW_UNIT == "mph":
        return v
    return v * 2.23694                      # m/s -> mph


def from_box_state(d):
    """CS 1 BoxState.to_dict() -> AGENT_PLAN 4.1 snapshot."""
    r, p, s = d.get("readings", {}), d.get("power", {}), d.get("storm", {})
    return {
        "ts": d.get("ts"),
        "source": d.get("src", "live"),
        "pressure_hpa": r.get("p"), "temp_c": r.get("temp"), "humidity": r.get("hum"),
        "water": r.get("water"), "wind_mph": _wind_to_mph(r.get("wind")), "rain_in": r.get("rain"),
        "wall_power": 1 if p.get("wall", True) else 0,
        "alarms": [{"id": a["id"], "level": a.get("level", "alarm"), "since": a.get("since")}
                   for a in d.get("alarms", [])],
        "storm": {"phase": s.get("phase"), "risk": s.get("risk"), "alert": s.get("alert", False),
                  "reason": s.get("reason"), "model_version": s.get("model_version", s.get("model"))},
        "battery": {"hours_left": p.get("battery_hours")},
        "outages": {"last_lost": p.get("outage_since")},
        "household": d.get("household"), "supplies": d.get("supplies"),
    }


def get_state():
    """Current box state (4.1 shape). Missing keys are filled with safe defaults."""
    raw = _provider() if _provider else {}
    if "readings" in raw and "pressure_hpa" not in raw:      # CS 1's shape
        raw = from_box_state(raw)
    state = {**EMPTY_STATE, **raw}
    state["storm"] = {"phase": None, "risk": None, "alert": False, "reason": None,
                      "model_version": None, **(raw.get("storm") or {})}
    state["battery"] = {"hours_left": None, **(raw.get("battery") or {})}
    return state


# ---------------------------------------------------------------- the 8 tools
def sea_level_pressure(state):
    """Barometer reading -> sea-level pressure (what weather reports and the storm model use).
    Replay data is already sea level, so it is returned as is."""
    p = state.get("pressure_hpa")
    if p is None or state.get("source") == "replay":
        return p
    t = state.get("temp_c")
    return runtime.to_sea_level(p, config.ALTITUDE_M, t if t is not None else 25.0) + config.CAL_OFFSET_HPA


def get_sensors():
    """Raw readings plus pressure_sea_level_hpa (use that one when talking to people)."""
    s = get_state()
    out = {k: s.get(k) for k in ("pressure_hpa", "temp_c", "humidity", "water",
                                 "wind_mph", "rain_in", "wall_power", "source")}
    out["pressure_sea_level_hpa"] = sea_level_pressure(s)
    return out


def get_storm_status():
    st = get_state()["storm"]
    return {k: st.get(k) for k in ("phase", "risk", "alert", "reason", "model_version")}


def get_alarms():
    return {"alarms": list(get_state()["alarms"])}


def search_guides(q, topic=None, k=config.RAG_K):
    """Top guide chunks: title, source, url (plus text and score)."""
    return {"chunks": rag.search(q, topic, k)}


def water_calculator():
    """days = gallons / (people * 1.0 + pets * PET_GAL)   (Ready.gov ~1 gallon/person/day)."""
    s = get_state()
    hh, sup = s.get("household") or {}, s.get("supplies") or {}
    people, pets, gal = hh.get("people"), hh.get("pets") or 0, sup.get("water_gallons")
    if not people or gal is None:
        return {"ok": False, "days_left": None, "people": people, "pets": pets, "gallons": gal}
    per_day = people * config.GAL_PER_PERSON_DAY + pets * config.PET_GAL
    return {"ok": True, "days_left": gal / per_day, "people": people, "pets": pets, "gallons": gal}


def get_battery():
    s = get_state()
    return {"hours_left": s["battery"].get("hours_left"), "on_battery": not s.get("wall_power")}


def get_outages(now=None):
    """Last lost/restored times and, if on battery now, how long power has been out."""
    s = get_state()
    out = s.get("outages") or {}
    on_battery = not s.get("wall_power")
    duration = None
    if on_battery and out.get("last_lost"):
        try:
            lost = datetime.fromisoformat(out["last_lost"].replace("Z", "+00:00"))
            now = now or datetime.now(timezone.utc)
            duration = max(0, int((now - lost).total_seconds() // 60))
        except ValueError:
            pass
    return {"on_battery": on_battery, "last_lost": out.get("last_lost"),
            "last_restored": out.get("last_restored"), "duration_min": duration}


def log_checkin(name, status):
    """Save a family check-in. Returns the new row id."""
    if _checkin_writer:
        return _checkin_writer(name, status)
    _checkins.append({"name": name, "status": status})
    return len(_checkins)


# Tools the router can run with no arguments (name -> function).
NO_ARG_TOOLS = {
    "get_sensors": get_sensors, "get_storm_status": get_storm_status, "get_alarms": get_alarms,
    "water_calculator": water_calculator, "get_battery": get_battery, "get_outages": get_outages,
}
