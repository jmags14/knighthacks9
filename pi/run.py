"""
Entry point for the StormBox Pi server.

Usage:
  python pi/run.py --fake                    # simulated ESP32
  python pi/run.py --serial /dev/ttyUSB0     # real ESP32 on Linux/Pi
  python pi/run.py --serial COM3             # real ESP32 on Windows
  python pi/run.py --serial auto             # find the board by USB ID
  python pi/run.py                           # same, using SERIAL_PORT (default auto); what systemd runs
"""
import argparse
import os
import sys
from pathlib import Path

# Make sure repo root is importable regardless of where this is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

parser = argparse.ArgumentParser(description="StormBox Pi server")
group = parser.add_mutually_exclusive_group()
group.add_argument("--fake",   action="store_true",  help="Use in-process fake ESP32")
group.add_argument("--serial", metavar="PORT",
                   help='Serial port ("auto" finds the board by USB ID). '
                        'Without --fake/--serial, the SERIAL_PORT environment variable is used (default "auto").')
args = parser.parse_args()

if args.fake:
    os.environ["STORMBOX_FAKE"] = "1"
elif args.serial:
    os.environ["STORMBOX_SERIAL"] = args.serial

uvicorn.run(
    "pi.web.main:app",
    host="0.0.0.0",
    port=8000,
    reload=False,
    log_level="info",
)
