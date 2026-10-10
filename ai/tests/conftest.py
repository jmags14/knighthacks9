"""Shared test setup. Run from the repo root:  python -m pytest ai/tests"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # so "import ai.agent" works

from ai.agent import canned, tools, trace  # noqa: E402


@pytest.fixture(autouse=True)
def clean_globals():
    """Every test starts with no fake box, no canned cache, and traces in memory only
    (so tests never write into the real data/box.db)."""
    tools.set_provider(None)
    canned._cache = None
    trace.configure(None)
    yield
    tools.set_provider(None)
    canned._cache = None
    trace.configure(None)


def make_state(**over):
    """A calm AGENT_PLAN 4.1 box state; override any key."""
    s = {"source": "live", "pressure_hpa": 1015.0, "temp_c": 29.0, "humidity": 70, "water": 20,
         "wind_mph": 5, "rain_in": 0.0, "wall_power": 1, "alarms": [],
         "storm": {"phase": "steady", "risk": 0.02, "alert": False, "reason": "Storm risk 2% because test.",
                   "model_version": "t"},
         "battery": {"hours_left": 18.0}, "household": {"people": 3, "pets": 1},
         "supplies": {"water_gallons": 6}, "outages": {}}
    s.update(over)
    return s


@pytest.fixture
def box():
    """box(**overrides) installs a fake box state and returns it."""
    def _install(**over):
        state = make_state(**over)
        tools.set_provider(lambda: state)
        return state
    return _install
