"""Phase 3: serial port auto-detect, plain-words errors, and reconnect after the cable is pulled.
No hardware and no pyserial needed: ports and the serial module are faked."""
import asyncio
import sys
import types
from types import SimpleNamespace as NS

import pytest

from pi.box_service import serialport as sp
from pi.box_service import service as svc
from pi.box_service.state import BoxState


def port(device, vid=None, desc="", manufacturer=""):
    return NS(device=device, vid=vid, pid=0x42, description=desc, manufacturer=manufacturer, product="")


XIAO = port("COM5", vid=0x2886, desc="USB Serial Device (COM5)")
OTHER = port("COM1", vid=0x0403, desc="FTDI cable")


# ---------------------------------------------------------------- finding the board
def test_finds_the_xiao_by_vendor_id_and_ignores_others():
    assert sp.find_board([OTHER, XIAO], env={}) == "COM5"


def test_no_board_found():
    assert sp.find_board([OTHER], env={}) is None
    assert sp.find_board([], env={}) is None


def test_name_hint_matches_when_vid_is_unknown():
    assert sp.find_board([port("COM7", vid=0x1234, desc="Seeed XIAO RP2040")], env={}) == "COM7"


def test_vendor_ids_can_be_changed_by_env():
    assert sp.find_board([port("COM9", vid=0x2341)], env={"SERIAL_VIDS": "2341, 0x2886"}) == "COM9"
    assert sp.find_board([port("COM9", vid=0x2341)], env={}) is None


def test_linux_prefers_the_stable_by_id_link(tmp_path):
    dev = tmp_path / "ttyACM0"
    dev.write_text("")
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    try:
        (by_id / "usb-Seeed_XIAO-if00").symlink_to(dev)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    assert sp.find_board([port(str(dev), vid=0x2886)], env={}, by_id_dir=by_id) == str(by_id / "usb-Seeed_XIAO-if00")


def test_by_id_missing_falls_back_to_the_device(tmp_path):
    assert sp.find_board([port("/dev/ttyACM0", vid=0x2886)], env={}, by_id_dir=tmp_path / "nope") == "/dev/ttyACM0"


# ---------------------------------------------------------------- the setting
def test_setting_defaults_to_auto_and_cli_beats_env():
    assert sp.configured_port({}) == "auto"
    assert sp.configured_port({"SERIAL_PORT": "COM3"}) == "COM3"
    assert sp.configured_port({"SERIAL_PORT": "COM3", "STORMBOX_SERIAL": "COM4"}) == "COM4"
    assert sp.configured_port({"SERIAL_PORT": "  "}) == "auto"


def test_explicit_port_is_used_as_given_and_auto_is_resolved():
    assert sp.resolve_port("COM3", ports=[]) == "COM3"
    assert sp.resolve_port("auto", ports=[XIAO], env={}) == "COM5"
    assert sp.resolve_port("AUTO", ports=[XIAO], env={}) == "COM5"
    with pytest.raises(sp.BoardNotFound):
        sp.resolve_port("auto", ports=[OTHER], env={})


# ---------------------------------------------------------------- friendly messages
@pytest.mark.parametrize("text", [
    "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)",
    "[Errno 16] could not open port /dev/ttyACM0: [Errno 16] Device or resource busy",
])
def test_busy_port_message_mentions_the_serial_monitor(text):
    msg = sp.describe_error(Exception(text), "COM3")
    assert "busy" in msg and "Arduino serial monitor" in msg


def test_linux_permission_message_points_to_dialout():
    msg = sp.describe_error(Exception("[Errno 13] Permission denied: '/dev/ttyACM0'"), "/dev/ttyACM0")
    assert "dialout" in msg


@pytest.mark.parametrize("exc", [
    sp.BoardNotFound("auto"),
    Exception("could not open port 'COM3': FileNotFoundError(2, 'The system cannot find the file specified.')"),
    Exception("device reports readiness to read but returned no data"),
])
def test_unplugged_messages_say_disconnected(exc):
    assert sp.describe_error(exc, "COM3").startswith("Microcontroller disconnected")


# ---------------------------------------------------------------- reconnect (fake pyserial)
class FakeSerialModule(types.ModuleType):
    """Plays a script: each open() either fails or yields a port that serves lines then 'unplugs'."""

    def __init__(self, script):
        super().__init__("serial")
        self.script = list(script)       # items: Exception to raise on open, or list of lines to serve
        self.opened = []

    class SerialException(Exception):
        pass

    def Serial(self, name, baud, timeout=1):
        self.opened.append(name)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        lines = list(step)

        class Port:
            def readline(inner):
                if lines:
                    return lines.pop(0).encode() + b"\n"
                raise FakeSerialModule.SerialException("device reports readiness to read but returned no data")

            def close(inner):
                pass

            def write(inner, data):
                pass
        return Port()


def run(coro):
    return asyncio.run(coro)


def test_pull_the_cable_then_plug_it_back_in(monkeypatch):
    fake = FakeSerialModule([
        ["a", "b"],                                              # plugged in: two lines, then cable pulled
        FakeSerialModule.SerialException("could not open port 'COM5': FileNotFoundError(2, 'x')"),
        ["c"],                                                   # plugged back in (on a NEW port name)
    ])
    monkeypatch.setitem(sys.modules, "serial", fake)
    names = iter(["COM5", "COM5", "COM6"])
    monkeypatch.setattr(sp, "resolve_port", lambda setting=None, **k: next(names))
    src = svc.SerialSource("auto")

    async def go():
        got, errors = [], []
        for _ in range(6):
            try:
                got.append(await src.readline())
                errors.append(src.last_error)
            except Exception:
                errors.append(src.last_error)
        return got, errors
    got, errors = run(go())
    assert got == ["a", "b", "c"]
    assert fake.opened == ["COM5", "COM5", "COM6"]                # re-resolved each time: followed the new port
    assert errors[0] is None and errors[1] is None
    assert errors[2].startswith("Microcontroller disconnected")   # cable pulled
    assert errors[3].startswith("Microcontroller disconnected")   # still unplugged
    assert errors[4] is None                                      # back, error cleared


def test_busy_port_sets_a_clear_error(monkeypatch):
    fake = FakeSerialModule([FakeSerialModule.SerialException("could not open port 'COM3': PermissionError(13, 'Access is denied.')")])
    monkeypatch.setitem(sys.modules, "serial", fake)
    src = svc.SerialSource("COM3")
    with pytest.raises(Exception):
        run(src.readline())
    assert "Arduino serial monitor" in src.last_error


def test_auto_with_no_board_says_disconnected(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", FakeSerialModule([]))
    monkeypatch.setattr(sp, "list_ports", lambda: [])
    src = svc.SerialSource("auto")
    with pytest.raises(sp.BoardNotFound):
        run(src.readline())
    assert src.last_error.startswith("Microcontroller disconnected")


def test_service_keeps_running_and_shows_the_error_on_the_dashboard(monkeypatch):
    state = BoxState()

    class Src:
        last_error = "Microcontroller disconnected: no board found. Plug in the USB cable."

        async def readline(self):
            raise sp.BoardNotFound("auto")

        async def write(self, data):
            pass

    s = svc.BoxService(state, Src())

    async def go():
        s._running = True
        tasks = [asyncio.create_task(s._read_loop()), asyncio.create_task(s._health_loop())]
        await asyncio.sleep(1.5)
        s._running = False
        alive = [not t.done() for t in tasks]
        for t in tasks:
            t.cancel()
        return alive
    alive = run(go())
    assert alive == [True, True]                                  # nothing crashed
    assert state.health["serial_error"].startswith("Microcontroller disconnected")
    assert state.to_dict()["health"]["serial_error"]


def test_fake_source_has_no_serial_error():
    state = BoxState(dev=True)
    s = svc.BoxService(state, object(), dev=True)
    assert getattr(s._source, "last_error", None) is None
