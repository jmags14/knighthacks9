"""v2 wind models in storm_job.py: model choice by working sensors, replay choice, wind cleaning, gusts.
Fake sensor histories only; the real v2 model files in ai/ml/v2 are used."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from ai.agent import config
from ai.ml import storm_job
from ai.ml.storm_job import run_storm_job

T0 = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
MPH = 0.44704


def readings(hours, p=1010.0, wind_ms=12.0, with_p=True, with_w=True, step_min=10, start=T0, **extra):
    """One row per step_min for `hours` hours. p / wind_ms may be numbers or functions of the hour index."""
    rows = []
    for i in range(int(hours * 60 / step_min) + 1):
        t = start + timedelta(minutes=i * step_min)
        h = i * step_min / 60
        row = {"ts": t.isoformat()}
        if with_p:
            row["pressure_hpa"] = p(h) if callable(p) else p
        if with_w:
            row["wind_ms"] = wind_ms(h) if callable(wind_ms) else wind_ms
        rows.append({**row, **extra})
    return rows


def now_for(rows):
    return datetime.fromisoformat(rows[-1]["ts"]) + timedelta(minutes=5) if rows else T0


def run(rows, **kw):
    kw.setdefault("altitude_m", 0)
    kw.setdefault("now", now_for(rows))
    return run_storm_job(rows, **kw)


falling = lambda h: 1012 - 2.5 * h           # a storm-ish falling barometer
rising_wind = lambda h: 6 + 2.5 * h


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(config, "STORM_MODEL_MODE", "auto")


# ---------------------------------------------------------------- defaults = today
def test_defaults_are_v1_live_and_auto_replay():
    # live stays on the pressure model; the replay demo defaults to auto (v2 "both" when wind columns exist)
    assert config.STORM_MODEL_MODE == "v1" and config.STORM_REPLAY_MODEL == "auto"


def test_v1_mode_ignores_wind_and_is_unchanged():
    with_wind = run(readings(8, p=falling))
    pressure_only = run(readings(8, p=falling, with_w=False))
    assert with_wind["model"] == "pressure"
    assert {k: v for k, v in with_wind.items()} == {k: v for k, v in pressure_only.items()}
    assert with_wind["reason"].startswith("Storm risk ") and "(pressure model)" not in with_wind["reason"]


def test_v1_replay_with_wind_columns_still_uses_v1_pressure(monkeypatch):
    monkeypatch.setattr(config, "STORM_REPLAY_MODEL", "v1")
    rows = readings(8, p=falling, gust_ms=15.0)
    r = run(rows, source="replay")
    assert r["model"] == "pressure" and "model)" not in r["reason"]


# ---------------------------------------------------------------- live auto: which model
def test_auto_both_sensors_uses_v2_both(auto):
    rows = readings(8, p=falling, wind_ms=rising_wind)
    r = run(rows)
    assert r["status"] == "ok" and r["model"] == "both"
    assert r["model_version"].endswith("-both") and "(both model) because" in r["reason"]
    assert 0 <= r["risk"] <= 1
    # same answer as calling the v2 runtime by hand on the same hourly numbers
    model = storm_job.load_v2("both")
    feats = r["features"]
    assert {"p_now", "w_now", "g_max6"} <= set(feats)                  # pressure AND wind features
    assert math.isclose(feats["p_now"], r["sea_level_hourly"][-1], abs_tol=0.05)
    assert storm_job.runtime2.predict(model, feats)["risk"] == r["risk"]


def test_auto_pressure_only_keeps_original_v1_model(auto, monkeypatch):
    rows = readings(8, p=falling, with_w=False)
    r = run(rows)
    monkeypatch.setattr(config, "STORM_MODEL_MODE", "v1")
    v1 = run(rows)
    assert r["model"] == "pressure" and r["model_version"] == v1["model_version"]
    assert r["risk"] == v1["risk"] and r["path"] == v1["path"]
    assert "(pressure model) because" in r["reason"] and r["reason"].replace(" (pressure model)", "") == v1["reason"]


def test_auto_wind_only_uses_v2_wind(auto):
    r = run(readings(8, with_p=False, wind_ms=rising_wind))
    assert r["status"] == "ok" and r["model"] == "wind" and r["model_version"].endswith("-wind")
    assert "(wind model) because" in r["reason"] and r["reason"].startswith("Storm risk ")


def test_auto_neither_sensor_is_offline(auto):
    r = run(readings(8, with_p=False, with_w=False) or [{"ts": T0.isoformat()}], now=T0 + timedelta(hours=8))
    assert r["status"] == "sensors offline" and r["risk"] is None and r["alert"] is False and r["model"] is None
    assert run([], now=T0)["status"] == "sensors offline"


def test_auto_stale_sensor_counts_as_offline(auto):
    rows = readings(8, p=falling, wind_ms=rising_wind)
    def stop(rows, key, after_h):          # drop one sensor's readings after hour `after_h`
        return [{k: v for k, v in r.items()
                 if not (k == key and datetime.fromisoformat(r["ts"]) > T0 + timedelta(hours=after_h))} for r in rows]
    assert run(stop(rows, "wind_ms", 5))["model"] == "pressure"      # wind stopped 3 hours ago
    assert run(stop(rows, "pressure_hpa", 5))["model"] == "wind"     # pressure stopped, wind fresh
    assert run(rows, now=T0 + timedelta(hours=12))["status"] == "sensors offline"   # both silent for hours


def test_auto_both_but_too_little_history_is_warming_up(auto):
    r = run(readings(3, p=falling, wind_ms=rising_wind))
    assert r["status"] == "warming up" and r["risk"] is None and r["model"] == "both"
    assert r["hours_needed"] == 7


def test_auto_wind_history_shorter_than_pressure_is_warming_up(auto):
    rows = readings(8, p=falling, wind_ms=rising_wind)
    late_wind = [{k: v for k, v in r.items() if not (k == "wind_ms" and datetime.fromisoformat(r["ts"]) < T0 + timedelta(hours=5))} for r in rows]
    r = run(late_wind)
    assert r["status"] == "warming up" and r["model"] == "both"


# ---------------------------------------------------------------- replay
def test_replay_auto_with_wind_uses_both_and_no_sea_level_conversion(monkeypatch):
    monkeypatch.setattr(config, "STORM_REPLAY_MODEL", "auto")
    rows = readings(8, p=falling, wind_ms=12.0, gust_ms=20.0)
    r = run(rows, source="replay", altitude_m=500, cal_offset_hpa=3.0)    # would shift pressure if applied
    assert r["model"] == "both" and "(both model) because" in r["reason"]
    assert math.isclose(r["sea_level_hourly"][-1], falling(8), abs_tol=0.01)
    assert math.isclose(r["features"]["g_max6"], 20.0)


def test_replay_auto_without_wind_uses_v1_pressure(monkeypatch):
    monkeypatch.setattr(config, "STORM_REPLAY_MODEL", "auto")
    r = run(readings(8, p=falling, with_w=False), source="replay")
    assert r["model"] == "pressure" and "(pressure model) because" in r["reason"]


def test_live_and_replay_settings_are_independent(monkeypatch):
    rows = readings(8, p=falling, wind_ms=rising_wind)
    monkeypatch.setattr(config, "STORM_REPLAY_MODEL", "auto")       # replay auto, live still v1
    assert run(rows)["model"] == "pressure" and "model)" not in run(rows)["reason"]
    assert run(rows, source="replay")["model"] == "both"
    monkeypatch.setattr(config, "STORM_REPLAY_MODEL", "v1")
    monkeypatch.setattr(config, "STORM_MODEL_MODE", "auto")         # live auto, replay still v1
    assert run(rows)["model"] == "both"
    assert run(rows, source="replay")["model"] == "pressure"


# ---------------------------------------------------------------- wind inputs
def test_wind_units_mph_ms_and_bare_field(monkeypatch):
    base = [{"ts": T0.isoformat(), "wind_ms": 10.0}]
    mph = [{"ts": T0.isoformat(), "wind_mph": 10.0 / MPH}]
    assert storm_job.clean_wind(mph)[0][1] == pytest.approx(10.0)
    assert storm_job.clean_wind(base)[0][1] == 10.0
    bare = [{"ts": T0.isoformat(), "wind": 10.0}]
    assert config.WIND_RAW_UNIT == "ms"                                           # the sensor reports m/s
    assert storm_job.clean_wind(bare)[0][1] == 10.0
    monkeypatch.setattr(config, "WIND_RAW_UNIT", "mph")
    assert storm_job.clean_wind(bare)[0][1] == pytest.approx(10.0 * MPH)


def test_bad_wind_values_are_dropped_but_calm_zero_is_kept():
    rows = [{"ts": T0.isoformat(), "wind_ms": v} for v in (float("nan"), -1.0, 500.0, 0.0)]
    cleaned = storm_job.clean_wind(rows)
    assert [w for _, w, _ in cleaned] == [0.0]
    assert storm_job.clean_wind([{"wind_ms": 5.0}, "junk", {"ts": T0.isoformat()}]) == []


def second_rows(values, start=T0):
    return [(start + timedelta(seconds=i), v, None) for i, v in enumerate(values)]


def test_gust_one_second_readings_use_highest_5s_average_not_a_single_spike():
    calm = [5.0] * 600
    spike = calm[:300] + [40.0] + calm[301:]                         # one noisy spike
    gust = storm_job._gust_of(second_rows(spike))
    assert gust == pytest.approx((40.0 + 5 * 4) / 5) or gust < 40.0   # spike is averaged down
    assert gust < 15
    sustained = calm[:300] + [20.0] * 5 + calm[305:]                 # a real 5 s gust
    assert storm_job._gust_of(second_rows(sustained)) == pytest.approx(20.0)


def test_gust_slow_readings_use_highest_single_reading():
    rows = [(T0 + timedelta(minutes=10 * i), w, None) for i, w in enumerate([5.0, 6.0, 30.0, 7.0])]
    assert storm_job._gust_of(rows) == 30.0


def test_gust_field_from_the_box_wins():
    rows = [(T0 + timedelta(seconds=i), 5.0, 9.0 if i == 3 else None) for i in range(60)]
    assert storm_job._gust_of(rows) == 9.0


def test_hourly_wind_mean_gust_and_gap_fill():
    rows = []
    for hour, w in ((0, 4.0), (1, 6.0), (4, 12.0)):                  # hours 2 and 3 are missing
        rows += [(T0 + timedelta(hours=hour, minutes=m), w, None) for m in (0, 20, 40)]
    out = storm_job.hourly_wind(rows)
    assert [round(w, 1) for _, w, _ in out] == [4.0, 6.0, 8.0, 10.0, 12.0]   # filled in a straight line
    assert all(g >= w for _, w, g in out)
