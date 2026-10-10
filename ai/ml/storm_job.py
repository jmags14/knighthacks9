"""Storm risk job (AGENT_PLAN 8.2). box-service calls run_storm_job() once a minute.

    from ai.ml.storm_job import run_storm_job
    result = run_storm_job(readings, source="live")      # readings = recent barometer rows

Steps:
  1. Clean: drop pressures outside 850-1090 hPa (bad sensor reads).
  2. Average each clock hour; keep the last 7 hourly values, oldest first.
  3. Convert each to sea level: to_sea_level(p, ALTITUDE_M) + CAL_OFFSET_HPA.
     Skipped when source == "replay" (replay CSVs are already sea-level NOAA data).
  4. features -> tree -> one plain-English reason (all from storm_risk_runtime.py).
Fewer than 7 hours of data -> "warming up". The job never fires alarms; the ESP32 does.
"""
import importlib.util
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # so "python ai/ml/storm_job.py" finds ai.*
from ai.agent import config

P_MIN, P_MAX = 850.0, 1090.0        # hPa: anything outside is a sensor glitch
HOURS_NEEDED = 7                    # t-6h ... now
MAX_GAP_HOURS = 2                   # fill up to this many missing hours by interpolation

# storm_risk_runtime.py is a Colab output we don't edit and ai/ml is not a package,
# so load it straight from its file.
_RUNTIME_FILE = Path(__file__).with_name("storm_risk_runtime.py")
_spec = importlib.util.spec_from_file_location("storm_risk_runtime", _RUNTIME_FILE)
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)

_model_cache = {}


def load_model(path=None):
    """Load storm_risk.json once and reuse it."""
    path = str(path or config.STORM_MODEL_FILE)
    if path not in _model_cache:
        _model_cache[path] = runtime.load(path)
    return _model_cache[path]


# ---------------------------------------------------------------- step 1: clean
def _parse_ts(ts):
    """ISO string or datetime -> aware UTC datetime."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def clean_readings(readings):
    """Accept dicts ({"ts","pressure_hpa"|"pressure"|"p", optional "temp_c"|"temp"}) or
    tuples (ts, pressure[, temp]). Returns sorted [(datetime, pressure, temp_or_None)] with
    bad values removed."""
    rows = []
    for r in readings:
        if isinstance(r, dict):
            ts = r.get("ts")
            p = next((r[k] for k in ("pressure_hpa", "pressure", "p") if r.get(k) is not None), None)
            t = next((r[k] for k in ("temp_c", "temp") if r.get(k) is not None), None)
        else:
            ts, p, t = r[0], r[1], (r[2] if len(r) > 2 else None)
        if ts is None or p is None or math.isnan(p) or not (P_MIN <= p <= P_MAX):
            continue
        rows.append((_parse_ts(ts), float(p), t))
    return sorted(rows, key=lambda x: x[0])


# ---------------------------------------------------------------- step 2: hourly averages
def hourly_values(rows):
    """Average readings per clock hour. Returns [(hour_start, avg_pressure, avg_temp)] oldest
    first, ending at the newest hour, with small gaps filled in. May be shorter than 7."""
    buckets = defaultdict(list)
    for ts, p, t in rows:
        buckets[ts.replace(minute=0, second=0, microsecond=0)].append((p, t))
    if not buckets:
        return []
    hours = sorted(buckets)
    out, step = [], timedelta(hours=1)
    for i, h in enumerate(hours):
        if i:                                            # fill a short gap before this hour
            prev, missing = hours[i - 1], int((h - hours[i - 1]) / step) - 1
            if 0 < missing <= MAX_GAP_HOURS:
                p0, p1 = out[-1][1], _avg(buckets[h])[0]
                for k in range(1, missing + 1):
                    out.append((prev + k * step, p0 + (p1 - p0) * k / (missing + 1), out[-1][2]))
            elif missing > MAX_GAP_HOURS:
                out = []                                 # big gap: restart the history
        p, t = _avg(buckets[h])
        out.append((h, p, t))
    return out


def _avg(pairs):
    ps = [p for p, _ in pairs]
    ts = [t for _, t in pairs if t is not None]
    return sum(ps) / len(ps), (sum(ts) / len(ts) if ts else None)


# ---------------------------------------------------------------- the job
def _warming_up(model, have):
    return {"status": "warming up", "risk": None, "alert": False, "path": [],
            "reason": "Risk: warming up", "model_version": model["version"],
            "hours_have": have, "hours_needed": HOURS_NEEDED}


def run_storm_job(readings, source="live", model=None, altitude_m=None, cal_offset_hpa=None):
    """Compute the storm risk from recent barometer readings.

    readings        recent rows covering the last 7+ hours (see clean_readings for formats)
    source          "live" or "replay"; replay skips the sea-level conversion
    altitude_m      override config.ALTITUDE_M (tests)
    cal_offset_hpa  override config.CAL_OFFSET_HPA (tests)

    Returns {"status": "ok"|"warming up", "risk", "alert", "reason", "path", "model_version", ...}.
    """
    model = model or load_model()
    altitude = config.ALTITUDE_M if altitude_m is None else altitude_m
    offset = config.CAL_OFFSET_HPA if cal_offset_hpa is None else cal_offset_hpa

    hours = hourly_values(clean_readings(readings))[-HOURS_NEEDED:]
    if len(hours) < HOURS_NEEDED:
        return _warming_up(model, len(hours))

    if source == "replay":                              # already sea-level: use as is
        sea_level = [p for _, p, _ in hours]
    else:
        sea_level = [runtime.to_sea_level(p, altitude, t if t is not None else 25.0) + offset
                     for _, p, t in hours]

    feats = runtime.features_from_hourly(sea_level)
    result = runtime.predict(model, feats)
    return {"status": "ok", "risk": result["risk"], "alert": result["alert"],
            "reason": runtime.explain(model, result), "path": result["path"],
            "model_version": model["version"], "features": feats,
            "sea_level_hourly": [round(p, 1) for p in sea_level]}


# ---------------------------------------------------------------- demo: python ai/ml/storm_job.py
if __name__ == "__main__":
    import csv
    name = sys.argv[1] if len(sys.argv) > 1 else "ian_2022_41009.csv"
    with open(Path(__file__).with_name("replay") / name, newline="") as f:
        rows = [{"ts": r["ts"], "pressure_hpa": float(r["pressure_hpa"])} for r in csv.DictReader(f)]
    print(f"Replaying {name}: one job run per hour of storm data (6 rows = 1 hour)")
    for i in range(0, len(rows), 6):
        res = run_storm_job(rows[:i + 1], source="replay")
        flag = "ALERT" if res["alert"] else "     "
        risk = "warming up" if res["risk"] is None else f"{res['risk']:.2f}"
        print(f"{rows[i]['ts']}  p={rows[i]['pressure_hpa']:7.1f}  risk={risk:>10}  {flag}")
