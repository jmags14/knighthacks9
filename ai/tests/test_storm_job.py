"""storm_job: cleaning, hourly averages, sea-level conversion, replay skip, warming up."""
import csv
from datetime import datetime, timedelta, timezone

from ai.agent import config
from ai.ml import storm_job
from ai.ml.storm_job import run_storm_job, runtime

T0 = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)


def rows(hours, p, start=T0, temp=25.0):
    """One reading every 10 minutes (6 per hour) at constant pressure p."""
    return [{"ts": start + timedelta(minutes=10 * i), "pressure_hpa": p, "temp_c": temp}
            for i in range(hours * 6)]


def test_warming_up_with_fewer_than_7_hours():
    r = run_storm_job(rows(6, 1015), "live")
    assert r["status"] == "warming up"
    assert r["risk"] is None and r["alert"] is False
    assert r["reason"] == "Risk: warming up"
    assert r["hours_have"] == 6


def test_no_readings_is_warming_up():
    assert run_storm_job([], "live")["status"] == "warming up"


def test_seven_hours_gives_a_risk():
    r = run_storm_job(rows(7, 1015), "live", altitude_m=0)
    assert r["status"] == "ok"
    assert r["risk"] < 0.71 and r["alert"] is False      # calm day: below the alert threshold
    assert r["model_version"] == "storm-risk-20261009"
    assert r["reason"].startswith("Storm risk")


def test_sea_level_conversion_uses_altitude():
    # A box 100 m up reads ~12 hPa low. Converted, a raw 1002 is a calm ~1014.
    r = run_storm_job(rows(8, 1002.0), "live", altitude_m=100)
    sl = r["sea_level_hourly"][-1]
    assert 11 < sl - 1002 < 13
    assert abs(sl - runtime.to_sea_level(1002.0, 100, 25.0)) < 0.1
    assert r["alert"] is False


def test_forgetting_altitude_causes_a_false_alarm():
    # Same raw readings with altitude 0 look like a storm. This is why calibration matters.
    assert run_storm_job(rows(8, 1002.0), "live", altitude_m=0)["alert"] is True


def test_calibration_offset_is_added():
    base = run_storm_job(rows(8, 1010.0), "live", altitude_m=0, cal_offset_hpa=0.0)["sea_level_hourly"][-1]
    cal = run_storm_job(rows(8, 1010.0), "live", altitude_m=0, cal_offset_hpa=1.5)["sea_level_hourly"][-1]
    assert round(cal - base, 1) == 1.5


def test_defaults_come_from_config(monkeypatch):
    monkeypatch.setattr(config, "ALTITUDE_M", 100.0)
    monkeypatch.setattr(config, "CAL_OFFSET_HPA", 2.0)
    r = run_storm_job(rows(8, 1002.0), "live")
    assert round(r["sea_level_hourly"][-1] - runtime.to_sea_level(1002.0, 100, 25.0), 1) == 2.0


def test_replay_skips_sea_level_conversion():
    # Altitude and offset are ignored for replay: the CSV values are already sea level.
    r = run_storm_job(rows(8, 1005.0), "replay", altitude_m=500, cal_offset_hpa=3.0)
    assert r["sea_level_hourly"] == [1005.0] * 7


def test_replay_of_a_real_storm_alerts():
    path = config.ROOT / "ai" / "ml" / "replay" / "nicole_2022_cdrf1.csv"
    with open(path, newline="") as f:
        data = [{"ts": r["ts"], "pressure_hpa": float(r["pressure_hpa"])} for r in csv.DictReader(f)]
    early = run_storm_job(data[:100], "replay")     # first ~17 h, pressure still ~1019
    late = run_storm_job(data[:200], "replay")     # about 33 h in, pressure near 1005
    assert early["status"] == "ok" and early["alert"] is False
    assert late["alert"] is True and late["risk"] > 0.7
    assert late["path"], "the tree path is returned for the dashboard to highlight"


def test_bad_readings_are_dropped():
    junk = [{"ts": T0 + timedelta(hours=3, minutes=5), "pressure_hpa": v}
            for v in (0.0, 849.9, 1090.1, 2000.0, None)]
    cleaned = storm_job.clean_readings(rows(8, 1015.0) + junk)
    assert len(cleaned) == 48
    assert all(850 <= p <= 1090 for _, p, _ in cleaned)


def test_readings_can_be_tuples_and_unsorted():
    data = [(r["ts"].isoformat(), r["pressure_hpa"]) for r in rows(8, 1015.0)]
    data.reverse()
    assert run_storm_job(data, "replay")["status"] == "ok"


def test_hourly_average():
    data = [{"ts": T0 + timedelta(minutes=m), "pressure_hpa": p}
            for m, p in ((0, 1000.0), (30, 1002.0), (60, 1010.0))]
    hours = storm_job.hourly_values(storm_job.clean_readings(data))
    assert [round(p, 1) for _, p, _ in hours] == [1001.0, 1010.0]


def test_one_missing_hour_is_interpolated_but_a_big_gap_restarts():
    one_gap = rows(3, 1010.0) + rows(5, 1010.0, start=T0 + timedelta(hours=4))   # hour 3 missing
    assert run_storm_job(one_gap, "replay")["status"] == "ok"
    big_gap = rows(3, 1010.0) + rows(5, 1010.0, start=T0 + timedelta(hours=8))   # 5 hours missing
    r = run_storm_job(big_gap, "replay")
    assert r["status"] == "warming up" and r["hours_have"] == 5


def test_rising_pressure_after_storm_lowers_risk():
    falling = [1020, 1018, 1016, 1013, 1010, 1007, 1004]
    # The tree only uses 'pressure rising' (d6 > 1.55) when pressure is between 1009.5 and 1012.7.
    leaving = [1000, 1002, 1004, 1006, 1008, 1010, 1012]

    def make(vals):
        return [{"ts": T0 + timedelta(hours=i, minutes=5), "pressure_hpa": float(v)} for i, v in enumerate(vals)]

    assert run_storm_job(make(falling), "replay")["risk"] > 0.7
    assert run_storm_job(make(leaving), "replay")["risk"] < 0.71
