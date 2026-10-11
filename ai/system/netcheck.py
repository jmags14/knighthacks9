"""Internet up/down detection. Standard library only. CS 1 starts it once and passes a callback.

    from ai.system import netcheck
    netcheck.start(on_event)          # on_event({"t":"net","state":"lost"|"restored","ts":"..."})
    netcheck.status()                 # {"online": True|False|None, "since": iso|None, ...}

Every NET_INTERVAL_S it tries a TCP connect to each target (IP + port, no DNS). Online if ANY
connects. NET_DOWN_AFTER misses in a row -> "lost"; NET_UP_AFTER successes in a row -> "restored".
A result that goes the other way resets the count, so a flaky link does not flap. The first result
only sets the starting state: starting online OR offline is silent. Only a drop from online to offline
says "lost"; "restored" is only said after a "lost" (coming up from a silent offline start is silent).
Runs in its own daemon thread; the callback runs on that thread, so keep it quick (enqueue and return).
"""
import socket
import threading
from datetime import datetime, timezone

from ai.agent import config


def _now():
    return datetime.now(timezone.utc).isoformat()


def tcp_connect(host, port, timeout):
    """True if a TCP connection opens. Never raises."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class NetMonitor:
    def __init__(self, on_event=None, *, interval=None, timeout=None, targets=None,
                 down_after=None, up_after=None, connect=tcp_connect):
        self.on_event = on_event
        self.interval = config.NET_INTERVAL_S if interval is None else interval
        self.timeout = config.NET_TIMEOUT_S if timeout is None else timeout
        self.targets = tuple(config.NET_TARGETS if targets is None else targets)
        self.down_after = config.NET_DOWN_AFTER if down_after is None else down_after
        self.up_after = config.NET_UP_AFTER if up_after is None else up_after
        self._connect = connect
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._online = None             # None = no result yet
        self._since = None
        self._last_check = None
        self._misses = 0
        self._hits = 0
        self._lost_sent = False         # "restored" is only sent after a "lost" was sent

    # ---------------------------------------------------------------- one check
    def probe(self):
        """True if any target connects (stops at the first success)."""
        return any(self._connect(host, port, self.timeout) for host, port in self.targets)

    def step(self, ok=None):
        """Run one check (or feed a result) and update the state. Returns the event dict if one fired."""
        ok = self.probe() if ok is None else ok
        event = None
        with self._lock:
            self._last_check = _now()
            if ok:
                self._hits, self._misses = self._hits + 1, 0
            else:
                self._misses, self._hits = self._misses + 1, 0
            if self._online is None:        # first result: set the baseline. Up or down -> say nothing.
                self._online, self._since = ok, self._last_check
            elif self._online and self._misses >= self.down_after:
                self._online, self._since, self._lost_sent = False, self._last_check, True
                event = {"t": "net", "state": "lost", "ts": self._since}
            elif not self._online and self._hits >= self.up_after:
                self._online, self._since = True, self._last_check
                if self._lost_sent:         # coming up from a silent offline start stays silent
                    self._lost_sent = False
                    event = {"t": "net", "state": "restored", "ts": self._since}
        if event and self.on_event:
            try:
                self.on_event(event)
            except Exception:           # CS 1's callback must never kill the monitor
                pass
        return event

    def status(self):
        with self._lock:
            return {"online": self._online, "since": self._since,
                    "last_check": self._last_check, "misses": self._misses}

    # ---------------------------------------------------------------- thread
    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="netcheck", daemon=True)
        self._thread.start()
        return self

    def stop(self, wait=True):
        self._stop.set()
        if wait and self._thread and self._thread is not threading.current_thread():
            self._thread.join(self.timeout * max(len(self.targets), 1) + 1)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:           # never let the thread die
                pass
            self._stop.wait(self.interval)


# ---------------------------------------------------------------- module-level default monitor
_monitor = None


def start(on_event=None, **overrides):
    """Start (or restart) the default monitor and return it. Call once at box-service startup."""
    global _monitor
    if _monitor:
        _monitor.stop()
    _monitor = NetMonitor(on_event, **overrides).start()
    return _monitor


def stop():
    global _monitor
    if _monitor:
        _monitor.stop()
        _monitor = None


def status():
    """Current status of the default monitor; online is None until the first check (or if never started)."""
    if _monitor is None:
        return {"online": None, "since": None, "last_check": None, "misses": 0}
    return _monitor.status()
