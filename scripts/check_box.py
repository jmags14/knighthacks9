#!/usr/bin/env python3
"""
Laptop preflight: is everything the StormBox hub needs ready? Run before a demo.

    .venv\\Scripts\\python.exe scripts\\check_box.py
    .venv\\Scripts\\python.exe scripts\\check_box.py --skip-board    # dashboard already running (it holds the port)

Prints PASS / WARN / FAIL per check. Exit code 1 only if something FAILs.
Reads the same settings the app uses (ai/agent/config.py, ai/voice/config.py, SERIAL_PORT, TRANSPORT, MQTT_*).
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXPECTED_CHUNKS = 71            # guide chunks in the index; a different count only WARNs (guides may have changed)
MIN_FREE_GB_OK, MIN_FREE_GB_FAIL = 5, 1
SANE_YEARS = range(2024, 2036)
BOARD_WAIT_S = 5

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _installed_models(names):
    """Ollama model names as a set, with and without the ':latest' suffix."""
    out = set()
    for n in names:
        out.add(n)
        if n.endswith(":latest"):
            out.add(n[:-7])
    return out


# ---------------------------------------------------------------- checks: each returns (status, message)
def check_ollama(list_models=None):
    from ai.agent import config
    try:
        if list_models is None:
            import ollama
            list_models = lambda: [m.model for m in ollama.Client(timeout=5).list().models]
        have = _installed_models(list_models())
    except Exception as e:
        return FAIL, f"Ollama is not running ({type(e).__name__}). Start it: ollama serve"
    need = [config.LLM_MODEL, config.EMBED_MODEL, config.LLM_FALLBACK]
    missing = [m for m in need if m not in have]
    if missing:
        return FAIL, "missing model(s): " + ", ".join(f"{m} (ollama pull {m})" for m in missing)
    extra = "; llama3.2:3b also installed" if "llama3.2:3b" in have else ""
    return PASS, "running, models: " + ", ".join(need) + extra


def check_index(count=None):
    from ai.agent import config
    try:
        if count is None:
            import chromadb
            count = lambda: chromadb.PersistentClient(path=str(config.CHROMA_DIR)) \
                .get_collection(config.CHROMA_COLLECTION).count()
        n = count()
    except Exception as e:
        return FAIL, f"guide index not found ({type(e).__name__}). Run: python ai/scripts/build_index.py"
    if n == EXPECTED_CHUNKS:
        return PASS, f"{n} guide chunks"
    return WARN, f"{n} guide chunks (expected {EXPECTED_CHUNKS}). Fine if the guides changed; else rebuild the index"


def check_voice_models(importable=None):
    from ai.voice import config as vc
    problems = []
    if not Path(vc.STT_DIR).is_dir():
        problems.append(f"speech-to-text model missing ({vc.STT_DIR.name})")
    for p in (vc.PIPER_ONNX, Path(str(vc.PIPER_ONNX) + ".json")):
        if not Path(p).exists():
            problems.append(f"voice file missing ({Path(p).name})")
    for mod in ("faster_whisper", "piper", "sounddevice"):
        try:
            (importable or __import__)(mod)
        except Exception:
            problems.append(f"python package '{mod}' not installed")
    if problems:
        return FAIL, "; ".join(problems) + ". Run: python ai/scripts/download_voice_models.py"
    return PASS, f"{vc.STT_DIR.name}, {vc.PIPER_VOICE}"


def check_board(env=None, skip=False, resolve=None, read_line=None, tcp=None):
    from pi.box_service import mqttsource, serialport
    env = os.environ if env is None else env
    if skip:
        return WARN, "skipped (--skip-board)"
    if mqttsource.transport(env) == "mqtt":
        host, port = env.get("MQTT_HOST", "localhost"), int(env.get("MQTT_PORT", "1883"))
        ok = (tcp or _tcp)(host, port)
        return (PASS, f"MQTT broker reachable at {host}:{port}") if ok else \
               (FAIL, f"MQTT broker not reachable at {host}:{port}. Is Mosquitto running?")
    setting = serialport.configured_port(env)
    try:
        port = (resolve or serialport.resolve_port)(setting)
    except Exception as e:
        return FAIL, serialport.describe_error(e, setting)
    try:
        line = (read_line or _read_serial_line)(port)
    except Exception as e:
        return FAIL, serialport.describe_error(e, port) + " (use --skip-board if the dashboard is running)"
    if not line:
        return FAIL, f"board found on {port} but no reading in {BOARD_WAIT_S} s. Is the firmware running?"
    return PASS, f"board on {port}, got a reading: {line[:60]}"


def check_audio(devices=None):
    try:
        devs = devices() if devices else __import__("sounddevice").query_devices()
        devs = [dict(d) for d in devs]
    except Exception as e:
        return FAIL, f"cannot list audio devices ({type(e).__name__})"
    mic = next((d["name"] for d in devs if d.get("max_input_channels", 0) > 0), None)
    spk = next((d["name"] for d in devs if d.get("max_output_channels", 0) > 0), None)
    if not mic and not spk:
        return FAIL, "no microphone and no speaker found"
    if not mic:
        return FAIL, f"no microphone found (speaker: {spk})"
    if not spk:
        return FAIL, f"no speaker found (mic: {mic})"
    return PASS, f"mic: {mic}; speaker: {spk}"


def check_disk(free_bytes=None):
    free = (free_bytes if free_bytes is not None else shutil.disk_usage(Path(__file__).resolve().parent).free) / 1e9
    if free < MIN_FREE_GB_FAIL:
        return FAIL, f"only {free:.1f} GB free"
    if free < MIN_FREE_GB_OK:
        return WARN, f"{free:.1f} GB free (want at least {MIN_FREE_GB_OK})"
    return PASS, f"{free:.0f} GB free"


def check_clock(now=None):
    now = now or datetime.now()
    if now.year not in SANE_YEARS:
        return WARN, f"clock says {now:%Y-%m-%d}. Fix the date/time (storm history uses timestamps)"
    return PASS, f"{now:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------- helpers
def _tcp(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_serial_line(port):
    """Open the port, return the first JSON line within BOARD_WAIT_S seconds ('' if none), then close it."""
    import time

    import serial
    deadline = time.monotonic() + BOARD_WAIT_S
    with serial.Serial(port, 115200, timeout=1) as ser:
        while time.monotonic() < deadline:
            line = ser.readline().decode(errors="replace").strip()
            if line.startswith("{"):
                try:
                    json.loads(line)
                    return line
                except ValueError:
                    pass
    return ""


CHECKS = [
    ("Ollama + models", lambda a: check_ollama()),
    ("Guide index", lambda a: check_index()),
    ("Voice models", lambda a: check_voice_models()),
    ("Board / transport", lambda a: check_board(skip=a.skip_board)),
    ("Mic + speaker", lambda a: check_audio()),
    ("Free disk", lambda a: check_disk()),
    ("Clock", lambda a: check_clock()),
]


def run_checks(args, checks=None):
    results = []
    for name, fn in (checks or CHECKS):
        try:
            status, msg = fn(args)
        except Exception as e:                  # a broken check must not hide the others
            status, msg = FAIL, f"check crashed: {type(e).__name__}: {e}"
        results.append((name, status, msg))
    return results


def main(argv=None, checks=None):
    ap = argparse.ArgumentParser(description="StormBox laptop preflight")
    ap.add_argument("--skip-board", action="store_true", help="do not open the serial port (dashboard running)")
    args = ap.parse_args(argv)
    results = run_checks(args, checks)
    for name, status, msg in results:
        print(f"[{status}] {name}: {msg}")
    fails = sum(s == FAIL for _, s, _ in results)
    warns = sum(s == WARN for _, s, _ in results)
    print(f"\n{len(results) - fails - warns} passed, {warns} warning(s), {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
