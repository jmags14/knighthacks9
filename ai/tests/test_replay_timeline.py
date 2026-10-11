"""replay_timeline: the whole risk curve of a recorded storm, computed without side effects.
Uses the real replay CSVs and v2 model files."""
import pytest

from ai.agent import agent, tools
from ai.ml import storm_job
from ai.ml.storm_job import replay_timeline

NICOLE = "nicole_2022_cdrf1.csv"


@pytest.fixture(scope="module")
def nicole():
    return replay_timeline(NICOLE)


def test_one_entry_per_simulated_hour(nicole):
    tl = nicole["timeline"]
    assert len(tl) == 54                                   # 325 rows of 10 minutes, 6 rows per hour
    assert tl[0]["sim_time"] == "2022-11-09T07:50:00Z"     # the last row of the first hour
    assert set(tl[0]) == {"sim_time", "pressure_hpa", "wind_ms", "gust_ms", "risk", "alert", "model",
                          "path", "status", "hours_ready", "hours_needed"}


def test_first_hours_are_warming_up(nicole):
    tl = nicole["timeline"]
    assert [e["status"] for e in tl[:6]] == ["warming up"] * 6
    assert [e["hours_ready"] for e in tl[:6]] == [1, 2, 3, 4, 5, 6]
    assert all(e["hours_needed"] == 7 and e["risk"] is None and e["alert"] is False for e in tl[:6])
    assert tl[6]["status"] == "ok" and tl[6]["hours_ready"] == 7 and tl[6]["risk"] is not None


def test_uses_the_wind_and_pressure_model_and_its_threshold(nicole):
    assert nicole["model"] == "both" and nicole["threshold"] == 0.64
    assert {e["model"] for e in nicole["timeline"]} == {"both"}
    assert nicole["hours_needed"] == 7


def test_alert_matches_the_threshold(nicole):
    for e in nicole["timeline"]:
        if e["status"] == "ok":
            assert e["alert"] == (e["risk"] >= nicole["threshold"])
            assert e["path"]                                # the tree path the dashboard draws


def test_risk_crosses_threshold_before_closest_approach(nicole):
    assert nicole["closest_time"] == "2022-11-10T19:00:00Z"
    tl = nicole["timeline"]
    first_alert = next(e for e in tl if e["alert"])
    assert first_alert["risk"] >= nicole["threshold"]
    assert first_alert["sim_time"] < nicole["closest_time"]       # same ISO format, so strings compare
    before = [e for e in tl if e["status"] == "ok" and e["sim_time"] < first_alert["sim_time"]]
    assert before and all(e["risk"] < nicole["threshold"] for e in before)   # a real crossing, not always high


def test_readings_come_from_the_csv(nicole):
    e = nicole["timeline"][0]
    assert (e["pressure_hpa"], e["wind_ms"], e["gust_ms"]) == (1019.6, 6.1, 8.2)
    assert nicole["storm"] == "Nicole" and nicole["year"] == 2022 and nicole["station"] == "cdrf1"


def test_pressure_only_file_uses_the_v1_model():
    r = replay_timeline(storm_job._V1_REPLAY_DIR / "ian_2022_41009.csv")
    assert r["model"] == "pressure" and r["threshold"] == storm_job.load_model()["threshold"]
    assert all(e["wind_ms"] is None and e["gust_ms"] is None for e in r["timeline"])
    assert r["closest_time"] is None or isinstance(r["closest_time"], str)


def test_mode_v1_forces_the_pressure_model():
    r = replay_timeline(NICOLE, mode="v1")
    assert r["model"] == "pressure" and r["threshold"] == storm_job.load_model()["threshold"]


def test_bad_files_are_rejected():
    with pytest.raises(FileNotFoundError):
        replay_timeline("nope.csv")
    with pytest.raises(ValueError):
        replay_timeline("index.json")
    with pytest.raises(ValueError):
        replay_timeline("../../agent/config.py")


def test_repeat_calls_are_equal_but_independent():
    a, b = replay_timeline(NICOLE), replay_timeline(NICOLE)
    assert a == b
    a["timeline"].clear()
    assert len(replay_timeline(NICOLE)["timeline"]) == 54      # the cache was not changed


def test_it_is_pure_no_agent_no_state(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("replay_timeline must not touch the agent or the box state")
    monkeypatch.setattr(agent, "explain_event", boom)
    monkeypatch.setattr(agent, "answer", boom)
    monkeypatch.setattr(tools, "get_state", boom)
    storm_job._timeline_cache.clear()
    assert len(replay_timeline(NICOLE)["timeline"]) == 54
