"""Finding the microcontroller's serial port, and explaining serial problems in plain words.

    SERIAL_PORT=auto          (default) find the board by its USB ID
    SERIAL_PORT=COM3          an explicit port (Windows)
    SERIAL_PORT=/dev/ttyACM0  an explicit port (Linux / Pi)
    SERIAL_VIDS=2886          comma-separated USB vendor IDs (hex) that count as "our board"
                              (2886 = Seeed, the XIAO RP2040)

pyserial is imported lazily, so this module loads (and its tests run) without it.
"""
import os
from pathlib import Path

DEFAULT_VIDS = "2886"
NAME_HINTS = ("xiao", "seeed")
BY_ID_DIR = Path("/dev/serial/by-id")


class BoardNotFound(Exception):
    """SERIAL_PORT=auto and no matching USB board is plugged in."""


def configured_port(env=None) -> str:
    """The SERIAL_PORT setting. STORMBOX_SERIAL (set by `run.py --serial PORT`) wins over it."""
    env = os.environ if env is None else env
    return (env.get("STORMBOX_SERIAL") or env.get("SERIAL_PORT") or "auto").strip() or "auto"


def _vids(env=None) -> set:
    env = os.environ if env is None else env
    out = set()
    for v in env.get("SERIAL_VIDS", DEFAULT_VIDS).split(","):
        v = v.strip().lower().removeprefix("0x")
        if v:
            out.add(int(v, 16))
    return out


def list_ports():
    from serial.tools import list_ports as lp
    return list(lp.comports())


def _is_board(p, vids) -> bool:
    if getattr(p, "vid", None) in vids:
        return True
    text = " ".join(str(getattr(p, a, "") or "") for a in ("description", "manufacturer", "product")).lower()
    return any(h in text for h in NAME_HINTS)


def _by_id(device, by_id_dir=None):
    """Linux: the stable /dev/serial/by-id/... symlink that points at this device, if any."""
    d = BY_ID_DIR if by_id_dir is None else Path(by_id_dir)
    try:
        for link in sorted(d.iterdir()):
            if os.path.realpath(link) == os.path.realpath(device):
                return str(link)
    except OSError:
        pass
    return None


def find_board(ports=None, env=None, by_id_dir=None):
    """Port name of the first USB board that matches, or None. `ports` is for tests."""
    ports = list_ports() if ports is None else ports
    vids = _vids(env)
    for p in sorted(ports, key=lambda p: p.device):
        if _is_board(p, vids):
            return _by_id(p.device, by_id_dir) or p.device
    return None


def resolve_port(setting=None, ports=None, env=None, by_id_dir=None) -> str:
    """'auto' -> the board's port (raises BoardNotFound); anything else is returned as given."""
    setting = configured_port(env) if setting is None else setting
    if setting.lower() != "auto":
        return setting
    port = find_board(ports, env, by_id_dir)
    if port is None:
        raise BoardNotFound("auto")
    return port


def describe_error(exc, port=None) -> str:
    """One plain sentence for the dashboard and the log."""
    where = f" on {port}" if port else ""
    text = str(exc).lower()
    if isinstance(exc, ModuleNotFoundError):
        return "The pyserial package is not installed. Run: pip install pyserial"
    if isinstance(exc, BoardNotFound):
        return "Microcontroller disconnected: no board found. Plug in the USB cable."
    if "access is denied" in text or "busy" in text or "in use" in text or "permissionerror" in text:
        return (f"Serial port{where} is busy. Close the Arduino serial monitor "
                "(or any other program using it).")
    if "permission denied" in text:
        return (f"No permission to use serial port{where}. "
                "Add this user to the dialout group (see PI_SETUP.md), then log in again.")
    if "no such file" in text or "could not open port" in text or "filenotfound" in text or "not found" in text:
        return f"Microcontroller disconnected: nothing on {port or 'the serial port'}. Plug in the USB cable."
    return f"Microcontroller disconnected ({type(exc).__name__}). Plug in the USB cable."
