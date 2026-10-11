"""Phase 2: one moisture sensor = floor water only. No rain tile on either dashboard page."""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"


@pytest.mark.parametrize("page", ["index.html", "dev.html"])
def test_floor_water_tile_and_no_rain_tile(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    labels = re.findall(r'tile\(\s*"([^"]+)"', html)
    assert "Floor water" in labels
    assert not any("rain" in label.lower() for label in labels)
    assert "Rain (in)" not in html


def test_home_card_says_microcontroller_disconnected_when_unplugged():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '"Microcontroller disconnected"' in html and "health.serial_error" in html
    assert "Sensors offline" in html                       # still the title for other offline causes
