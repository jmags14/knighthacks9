"""Heat index (NWS Rothfusz regression): what the heat FEELS like, from temperature and humidity.

The box measures temperature and humidity INSIDE the house, which is what matters when the AC is
off. Source: https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml

    heat_index_f(temp_c, rh)  -> heat index in degrees F (float)
    heat_info(temp_c, rh)     -> everything the agent / dashboard needs (see below)

Alarms still come only from the ESP32 (see HEAT_FOR_EE1.md); this module only describes the heat.
"""
import math

FEELS_LIKE_MIN_F = 80       # below this the heat index is just the temperature, so we do not mention it

# (upper bound in F, exclusive, name). Matches the NWS chart colors.
CATEGORIES = [(80, "none"), (90, "Caution"), (103, "Extreme Caution"), (125, "Danger")]
TOP_CATEGORY = "Extreme Danger"


def c_to_f(temp_c):
    return temp_c * 9 / 5 + 32


def heat_index_f(temp_c, rh):
    """Heat index in F. rh = relative humidity percent; None = not measured -> temperature alone."""
    t = c_to_f(temp_c)
    if rh is None:
        return t
    rh = min(max(float(rh), 0.0), 100.0)
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)          # simple formula
    if (hi + t) / 2 < 80:
        return hi
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
          - 0.00683783 * t * t - 0.05481717 * rh * rh + 0.00122874 * t * t * rh
          + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh)
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    elif rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return hi


def category(hi_f):
    """NWS category for a heat index in F: none / Caution / Extreme Caution / Danger / Extreme Danger."""
    for upper, name in CATEGORIES:
        if hi_f < upper:
            return name
    return TOP_CATEGORY


def heat_info(temp_c, humidity=None):
    """The one helper for the agent and the dashboard (CS 1). Returns None if temp_c is None, else:

        temp_f            air temperature, rounded F
        heat_index_f      heat index, rounded F (== temp_f when humidity is not measured)
        category          "none" | "Caution" | "Extreme Caution" | "Danger" | "Extreme Danger"
        humidity_measured False when humidity is missing (temperature alone was used)
        feels_like        True when heat_index_f >= 80, i.e. worth showing
        label             "Feels like 98°F · Extreme Caution" when feels_like, else None
    """
    if temp_c is None:
        return None
    measured = humidity is not None
    hi = round(heat_index_f(temp_c, humidity))
    cat = category(hi)
    shown = hi >= FEELS_LIKE_MIN_F
    return {"temp_f": round(c_to_f(temp_c)), "heat_index_f": hi, "category": cat,
            "humidity_measured": measured, "feels_like": shown,
            "label": f"Feels like {hi}°F · {cat}" if shown else None}


def feels_like_phrase(info):
    """Spoken 'feels like 100 degrees, extreme caution level' for info from heat_info(); '' when not worth
    saying. (The dashboard label is different on purpose: 'Feels like 100°F · Extreme Caution'.)"""
    if not info or not info["feels_like"]:
        return ""
    return f"feels like {info['heat_index_f']} degrees, {info['category'].lower()} level"
