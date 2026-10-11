"""scripts/check_box.py (laptop preflight). Every check is fed fake inputs: no Ollama, board, or audio needed."""
import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pi.box_service import serialport

path = Path(__file__).resolve().parents[2] / "scripts" / "check_box.py"
spec = importlib.util.spec_from_file_location("check_box", path)
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

ALL = ["llama3.2:1b", "nomic-embed-text:latest", "qwen3:0.6b"]


def test_ollama_pass_missing_and_down():
    assert cb.check_ollama(lambda: ALL)[0] == "PASS"
    s, msg = cb.check_ollama(lambda: ["llama3.2:1b"])
    assert s == "FAIL" and "ollama pull nomic-embed-text" in msg and "qwen3:0.6b" in msg
    def down():
        raise ConnectionError()
    s, msg = cb.check_ollama(down)
    assert s == "FAIL" and "ollama serve" in msg
    assert "3b also installed" in cb.check_ollama(lambda: ALL + ["llama3.2:3b"])[1]


def test_index_count():
    assert cb.check_index(lambda: 71)[0] == "PASS"
    assert cb.check_index(lambda: 80)[0] == "WARN"
    def gone():
        raise ValueError("no collection")
    s, msg = cb.check_index(gone)
    assert s == "FAIL" and "build_index.py" in msg


def test_voice_models_missing_package(monkeypatch):
    def importable(name):
        if name == "piper":
            raise ImportError
    s, msg = cb.check_voice_models(importable)
    assert s == "FAIL" and "'piper'" in msg


def test_board_serial_pass_busy_nothing_and_no_pyserial():
    ok = cb.check_board(env={}, resolve=lambda s: "COM5", read_line=lambda p: '{"t":"r","p":1000}')
    assert ok[0] == "PASS" and "COM5" in ok[1]
    def busy(p):
        raise Exception("PermissionError(13, 'Access is denied.')")
    s, msg = cb.check_board(env={}, resolve=lambda s: "COM5", read_line=busy)
    assert s == "FAIL" and "Arduino serial monitor" in msg and "--skip-board" in msg
    s, msg = cb.check_board(env={}, resolve=lambda s: "COM5", read_line=lambda p: "")
    assert s == "FAIL" and "no reading" in msg
    def none(setting):
        raise serialport.BoardNotFound("auto")
    assert "Microcontroller disconnected" in cb.check_board(env={}, resolve=none)[1]
    def nopyserial(setting):
        raise ModuleNotFoundError("serial")
    assert "pip install pyserial" in cb.check_board(env={}, resolve=nopyserial)[1]
    assert cb.check_board(env={}, skip=True)[0] == "WARN"


def test_board_mqtt_checks_the_broker():
    env = {"TRANSPORT": "mqtt", "MQTT_HOST": "h", "MQTT_PORT": "1883"}
    assert cb.check_board(env=env, tcp=lambda h, p: True)[0] == "PASS"
    s, msg = cb.check_board(env=env, tcp=lambda h, p: False)
    assert s == "FAIL" and "Mosquitto" in msg


def test_audio_devices():
    both = [{"name": "Mic", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Spk", "max_input_channels": 0, "max_output_channels": 2}]
    assert cb.check_audio(lambda: both)[0] == "PASS"
    assert "no microphone" in cb.check_audio(lambda: both[1:])[1]
    assert "no speaker" in cb.check_audio(lambda: both[:1])[1]
    assert cb.check_audio(lambda: [])[0] == "FAIL"


@pytest.mark.parametrize("gb,status", [(739, "PASS"), (3, "WARN"), (0.5, "FAIL")])
def test_disk(gb, status):
    assert cb.check_disk(gb * 1e9)[0] == status


def test_clock():
    assert cb.check_clock(datetime(2026, 10, 11))[0] == "PASS"
    assert cb.check_clock(datetime(1970, 1, 1))[0] == "WARN"


def test_a_crashing_check_does_not_hide_the_others_and_sets_exit_code(capsys):
    def boom(a):
        raise RuntimeError("x")
    checks = [("A", lambda a: ("PASS", "ok")), ("B", boom), ("C", lambda a: ("WARN", "hm"))]
    assert cb.main([], checks) == 1
    out = capsys.readouterr().out
    assert "[PASS] A" in out and "[FAIL] B: check crashed" in out and "[WARN] C" in out
    assert cb.main([], [("A", lambda a: ("WARN", "hm"))]) == 0
