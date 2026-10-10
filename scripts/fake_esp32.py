#!/usr/bin/env python3
"""
Standalone fake ESP32.

Run from the repo root:
  python scripts/fake_esp32.py

Prints one §5 reading line per second to stdout.
Type a command key + Enter to trigger an event:
  f  flood        w  wind (~20 s)  s  storm (~30 s)
  h  heat         p  wall power    b  short press
  l  long press   c  clear all
"""
import asyncio
import sys
from pathlib import Path

# Allow running as a script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pi.box_service.fake import FakeESP32, run_standalone  # noqa: E402

if __name__ == "__main__":
    asyncio.run(run_standalone())
