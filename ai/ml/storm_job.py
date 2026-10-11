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


def run_storm_job(readings, source="live", model=None, altitude_m=None, cal_offset_hpa=None,
                  mode=None, now=None):
    """Compute the storm risk from recent sensor readings.

    readings        recent rows covering the last 7+ hours (see clean_readings for formats;
                    wind fields are only read in "auto" mode, see clean_wind)
    source          "live" or "replay"; replay skips the sea-level conversion
    altitude_m      override config.ALTITUDE_M (tests)
    cal_offset_hpa  override config.CAL_OFFSET_HPA (tests)
    mode            "v1" or "auto". Default: config.STORM_REPLAY_MODEL for replay,
                    config.STORM_MODEL_MODE for live. "v1" = the original pressure model, unchanged.
    now             "current time" for the live sensor-online check (tests); default = the clock

    Returns {"status": "ok"|"warming up"|"sensors offline", "risk", "alert", "reason", "path",
             "model_version", "model": "pressure"|"wind"|"both"|None, ...}.
    """
    if mode is None:
        mode = config.STORM_REPLAY_MODEL if source == "replay" else config.STORM_MODEL_MODE
    altitude = config.ALTITUDE_M if altitude_m is None else altitude_m
    offset = config.CAL_OFFSET_HPA if cal_offset_hpa is None else cal_offset_hpa
    if mode == "auto":
        return _run_auto(readings, source, model, altitude, offset, now)
    return _run_pressure(clean_readings(readings), source, model or load_model(), altitude, offset)


def _run_pressure(rows, source, model, altitude, offset):
    """The original v1 job: pressure only. (Unchanged logic; the result also says model='pressure'.)"""
    hours = hourly_values(rows)[-HOURS_NEEDED:]
    if len(hours) < HOURS_NEEDED:
        return {**_warming_up(model, len(hours)), "model": "pressure"}

    sea_level = _sea_level(hours, source, altitude, offset)
    feats = runtime.features_from_hourly(sea_level)
    result = runtime.predict(model, feats)
    return {"status": "ok", "risk": result["risk"], "alert": result["alert"],
            "reason": runtime.explain(model, result), "path": result["path"],
            "model_version": model["version"], "features": feats,
            "sea_level_hourly": [round(p, 1) for p in sea_level], "model": "pressure"}


def _sea_level(hours, source, altitude, offset):
    """[(hour, pressure, temp)] -> sea-level hPa list. Replay data is already sea level."""
    if source == "replay":
        return [p for _, p, _ in hours]
    return [runtime.to_sea_level(p, altitude, t if t is not None else 25.0) + offset for _, p, t in hours]


# ================================================================ v2: wind models ("auto" mode only)
class _LazyV2:
    """Loads ai/ml/v2/storm_risk_runtime.py on first use, so v1 mode never touches the v2 folder."""
    _mod = None

    def __getattr__(self, name):
        if _LazyV2._mod is None:
            spec = importlib.util.spec_from_file_location(
                "storm_risk_runtime_v2", config.STORM_V2_DIR / "storm_risk_runtime.py")
            _LazyV2._mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_LazyV2._mod)
        return getattr(_LazyV2._mod, name)


runtime2 = _LazyV2()

_MPH_TO_MS = 0.44704
WIND_MAX_MS = 100.0                 # faster than this is a sensor glitch (the record is ~113 m/s)
GUST_DENSE_DT_S = 1.5               # readings closer together than this count as "every second"


def load_v2(name):
    """Load the v2 model 'pressure' | 'wind' | 'both' once and reuse it."""
    path = str(config.STORM_V2_DIR / config.STORM_V2_FILES[name])
    if path not in _model_cache:
        _model_cache[path] = runtime2.load(path)
    return _model_cache[path]


def _speed_ms(r, prefix):
    """Read 'wind' or 'gust' from a row in m/s, or None. Fields: <prefix>_ms (m/s), <prefix>_mph (mph),
    or the bare <prefix> in config.WIND_RAW_UNIT. Bad values (NaN, negative, absurd) give None."""
    if r.get(f"{prefix}_ms") is not None:
        v = r[f"{prefix}_ms"]
    elif r.get(f"{prefix}_mph") is not None:
        v = r[f"{prefix}_mph"] * _MPH_TO_MS
    elif r.get(prefix) is not None:
        v = r[prefix] * (_MPH_TO_MS if config.WIND_RAW_UNIT == "mph" else 1.0)
    else:
        return None
    return float(v) if not math.isnan(v) and 0 <= v <= WIND_MAX_MS else None


def clean_wind(readings):
    """Dict rows with a timestamp and a good wind value -> sorted [(datetime, wind_ms, gust_ms_or_None)].
    A calm 0 is a valid reading. Rows without wind (e.g. pressure-only) are skipped."""
    rows = []
    for r in readings:
        if not isinstance(r, dict) or r.get("ts") is None:
            continue
        w = _speed_ms(r, "wind")
        if w is not None:
            rows.append((_parse_ts(r["ts"]), w, _speed_ms(r, "gust")))
    return sorted(rows, key=lambda x: x[0])


def _gust_of(samples):
    """Hourly gust (m/s) from one hour of [(datetime, wind_ms, gust_or_None)] sorted by time.
    1) a gust field from the box wins (max of it); 2) readings about every second: highest
    N-second average (ignores single noisy spikes); 3) slower readings: highest single reading."""
    given = [g for _, _, g in samples if g is not None]
    if given:
        return max(given)
    times = [t for t, _, _ in samples]
    winds = [w for _, w, _ in samples]
    dts = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]))
    if dts and dts[len(dts) // 2] <= GUST_DENSE_DT_S:               # median spacing ~ 1 second
        best, lo, total = None, 0, 0.0
        for hi, w in enumerate(winds):
            total += w
            while (times[hi] - times[lo]).total_seconds() >= config.GUST_WINDOW_S:
                total -= winds[lo]
                lo += 1
            if hi - lo + 1 >= 3:                                   # a lone spike is not a window
                avg = total / (hi - lo + 1)
                best = avg if best is None else max(best, avg)
        if best is not None:
            return best
    return max(winds)


def hourly_wind(rows):
    """[(datetime, wind_ms, gust_ms_or_None)] -> [(hour_start, mean_wind, gust)] oldest first, small gaps
    filled by interpolation (same rules as pressure). Gust is never below the mean."""
    buckets = defaultdict(list)
    for row in rows:
        buckets[row[0].replace(minute=0, second=0, microsecond=0)].append(row)
    out, step = [], timedelta(hours=1)
    hours = sorted(buckets)
    for i, h in enumerate(hours):
        w = sum(s[1] for s in buckets[h]) / len(buckets[h])
        g = max(_gust_of(buckets[h]), w)
        if i:
            prev, missing = hours[i - 1], int((h - hours[i - 1]) / step) - 1
            if 0 < missing <= MAX_GAP_HOURS:
                _, w0, g0 = out[-1]
                for k in range(1, missing + 1):
                    f = k / (missing + 1)
                    out.append((prev + k * step, w0 + (w - w0) * f, g0 + (g - g0) * f))
            elif missing > MAX_GAP_HOURS:
                out = []
        out.append((h, w, g))
    return out


def _is_online(rows, now):
    """A sensor works if its newest good reading is recent."""
    return bool(rows) and rows[-1][0] >= now - timedelta(minutes=config.SENSOR_ONLINE_MIN)


def _tag(reason, name):
    """'Storm risk 82% because ...' / 'Storm risk score 82 because ...' -> 'Storm risk 82% (wind model) because ...'"""
    pct, _, why = reason.partition(" because ")
    pct = pct.replace("Storm risk score ", "Storm risk ").rstrip("%") + "%"
    return f"{pct} ({name} model) because {why}"


def _run_auto(readings, source, v1_model, altitude, offset, now):
    """Pick the model by which sensors are working (live) or by whether the replay has wind (replay)."""
    p_rows, w_rows = clean_readings(readings), clean_wind(readings)
    if source == "replay":                       # recorded data: no clock, just "does it have wind columns?"
        name = "both" if (w_rows and p_rows) else "pressure"
    else:
        now = _parse_ts(now) if now is not None else datetime.now(timezone.utc)
        p_ok, w_ok = _is_online(p_rows, now), _is_online(w_rows, now)
        name = {(True, True): "both", (True, False): "pressure", (False, True): "wind"}.get((p_ok, w_ok))
    if name is None:
        return {"status": "sensors offline", "risk": None, "alert": False, "path": [],
                "reason": "Risk: sensors offline", "model_version": None, "model": None}
    if name == "pressure":                       # keep the original v1 model for pressure-only
        res = _run_pressure(p_rows, source, v1_model or load_model(), altitude, offset)
    else:
        res = _run_v2(name, p_rows, w_rows, source, altitude, offset)
    if res["status"] == "ok":
        res["reason"] = _tag(res["reason"], name)
    return res


def _run_v2(name, p_rows, w_rows, source, altitude, offset):
    """Run the v2 'wind' or 'both' model on the last 7 clock hours that every needed sensor covers."""
    model = load_v2(name)
    series = {}                                  # sensor letter -> {hour: value(s)}
    if "p" in runtime2.needs(model):
        series["p"] = {h: (p, t) for h, p, t in hourly_values(p_rows)}
    series["w"] = {h: (w, g) for h, w, g in hourly_wind(w_rows)}
    end = min(max(s) for s in series.values() if s)
    wanted = [end - timedelta(hours=k) for k in range(HOURS_NEEDED - 1, -1, -1)]
    have = max(sum(1 for h in wanted if h in s) for s in series.values())
    if any(h not in s for s in series.values() for h in wanted):
        return {**_warming_up(model, have), "model": name}

    hist = {"w": [series["w"][h][0] for h in wanted], "g": [series["w"][h][1] for h in wanted]}
    if "p" in series:
        hist["p"] = _sea_level([(h, *series["p"][h]) for h in wanted], source, altitude, offset)
    feats = runtime2.features_from_history(model, hist)
    result = runtime2.predict(model, feats)
    return {"status": "ok", "risk": result["risk"], "alert": result["alert"],
            "reason": runtime2.explain(model, result), "path": result.get("path", []),
            "model_version": model["version"], "features": feats, "model": name,
            "sea_level_hourly": [round(p, 1) for p in hist.get("p", [])]}


# ================================================================ replay timeline (for the dashboard chart)
_V2_REPLAY_DIR = config.STORM_V2_DIR / "replay"
_V1_REPLAY_DIR = Path(__file__).with_name("replay")
ROWS_PER_HOUR = 6                   # replay files have one row per 10 minutes
_timeline_cache = {}


def _resolve_replay_csv(csv_path):
    """A path, or a bare file name from the replay menu (index.json), -> existing .csv Path."""
    p = Path(csv_path)
    if p.name == str(csv_path):                           # bare name: only the two replay folders are searched
        p = next((d / p.name for d in (_V2_REPLAY_DIR, _V1_REPLAY_DIR) if (d / p.name).is_file()), p)
    if p.suffix.lower() != ".csv":
        raise ValueError(f"not a replay csv: {csv_path!r}")
    if not p.is_file():
        raise FileNotFoundError(f"replay file not found: {csv_path!r}")
    return p.resolve()


def _replay_index_entry(path):
    """index.json row for this file name, or {} (old v1 files are not listed)."""
    try:
        import json
        with open(_V2_REPLAY_DIR / "index.json", encoding="utf-8") as f:
            return next((e for e in json.load(f) if e.get("file") == path.name), {})
    except (OSError, ValueError):
        return {}


def _num(s):
    return float(s) if s not in (None, "") else None


def replay_timeline(csv_path, mode=None):
    """The whole risk curve of a recorded storm, one entry per simulated hour. Pure computation: no events,
    no speech, no ESP32, no live state, so a dashboard can scrub through it without triggering anything.

    csv_path   path, or a bare file name from the replay menu (e.g. "nicole_2022_cdrf1.csv")
    mode       "v1" | "auto"; default config.STORM_REPLAY_MODEL (the same choice live replay makes)

    Returns {"file", "storm", "year", "station", "closest_time", "closest_km",   # None if not in index.json
             "model", "threshold", "hours_needed", "timeline": [...]}
    Each timeline entry: {"sim_time", "pressure_hpa", "wind_ms", "gust_ms",       # readings at the hour's last row
                          "risk", "alert", "model", "path", "status", "hours_ready", "hours_needed"}
    status is "ok" or "warming up" (risk None; hours_ready counts up to hours_needed). Each entry is the
    result run_storm_job gives after that hour of a live replay, so the chart matches what the box shows.
    The result is cached per file, so repeat calls are instant (every call returns its own copy).
    """
    import copy
    path = _resolve_replay_csv(csv_path)
    mode = mode or config.STORM_REPLAY_MODEL
    key = (str(path), mode, path.stat().st_mtime_ns)
    if key not in _timeline_cache:
        _timeline_cache[key] = _build_timeline(path, mode)
    return copy.deepcopy(_timeline_cache[key])


def _build_timeline(path, mode):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    rows = []
    for r in raw:
        row = {"ts": r["ts"], "pressure_hpa": float(r["pressure_hpa"])}
        wind, gust = _num(r.get("wind_ms")), _num(r.get("gust_ms"))
        if wind is not None:
            row["wind_ms"] = wind
        if gust is not None:
            row["gust_ms"] = gust
        rows.append(row)

    timeline = []
    for end in range(ROWS_PER_HOUR, len(rows) + 1, ROWS_PER_HOUR):
        res = run_storm_job(rows[:end], source="replay", mode=mode)
        last = rows[end - 1]
        ok = res["status"] == "ok"
        timeline.append({
            "sim_time": last["ts"], "pressure_hpa": last["pressure_hpa"],
            "wind_ms": last.get("wind_ms"), "gust_ms": last.get("gust_ms"),
            "risk": res["risk"], "alert": bool(res["alert"]), "model": res.get("model"),
            "path": res.get("path", []), "status": res["status"],
            "hours_ready": HOURS_NEEDED if ok else res.get("hours_have", 0), "hours_needed": HOURS_NEEDED})

    model = next((e["model"] for e in timeline if e["model"]), None)
    if model in ("wind", "both"):                         # v2 models carry their own alert line
        threshold = load_v2(model)["threshold"]
    elif model == "pressure":                             # pressure-only runs the original v1 model
        threshold = load_model()["threshold"]
    else:
        threshold = None
    meta = _replay_index_entry(path)
    closest = meta.get("closest_time")                    # same format as sim_time so the chart can compare
    closest = _parse_ts(closest).strftime("%Y-%m-%dT%H:%M:%SZ") if closest else None
    return {"file": path.name, "storm": meta.get("storm"), "year": meta.get("year"),
            "station": meta.get("station"), "closest_time": closest,
            "closest_km": meta.get("closest_km"), "model": model, "threshold": threshold,
            "hours_needed": HOURS_NEEDED, "timeline": timeline}


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
