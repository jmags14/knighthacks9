"""
Shared box state — the single source of truth for everything the web layer
and the box service need to read or push to WebSocket clients.

One instance lives in memory; only touch it from the asyncio event loop.
"""
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BoxState:
    def __init__(self, dev: bool = False):
        self.ts = _now()
        self.src = "live"
        self.dev = dev  # True only when running with --fake

        self.readings = {"p": None, "p_sl": None, "temp": None, "hum": None, "water": None, "rain": None, "wind": None}

        self.power = {
            "wall": True,
            "battery_hours": None,
            "outage_since": None,  # ISO timestamp set when wall power is lost
        }

        # Each alarm: {"id": str, "level": "warn"|"alarm", "since": ISO, "acked": bool}
        self.alarms: list[dict] = []

        self.storm = {"phase": None, "risk": None, "alert": None, "reason": None, "path": None, "model_version": None, "explanation": None}

        self.status_card = {"title": "All clear", "action": "Keep this box nearby"}

        self.health = {
            "esp32_link": False,
            "last_msg_age_ms": 0,
            "internet": False,
        }

        self.replay = {"on": False, "file": None, "progress": 0}

    # ------------------------------------------------------------------ helpers

    def _find_alarm(self, alarm_id: str) -> dict | None:
        for a in self.alarms:
            if a["id"] == alarm_id:
                return a
        return None

    def _active_ids(self) -> set[str]:
        return {a["id"] for a in self.alarms if not a["acked"]}

    # Priority: alarm-level entries first, then warn; within same severity: flood > wind > storm > heat
    _CARD_PRIORITY = [
        ("flood", "alarm"), ("wind",  "alarm"), ("storm", "alarm"), ("heat",  "alarm"),
        ("flood", "warn"),  ("wind",  "warn"),  ("storm", "warn"),  ("heat",  "warn"),
    ]

    def _refresh_status_card(self):
        """Pick status card text based on highest-priority active alarm."""
        active = [a for a in self.alarms if not a["acked"]]
        if not active:
            self.status_card = {"title": "All clear", "action": "Keep this box nearby"}
            return

        order = {key: i for i, key in enumerate(self._CARD_PRIORITY)}
        best  = min(active, key=lambda a: order.get((a["id"], a["level"]), 99))
        aid, lvl = best["id"], best["level"]

        if aid == "flood":
            self.status_card = {"title": "Water detected", "action": "Move to higher ground"}
        elif aid == "wind":
            if lvl == "alarm":
                self.status_card = {"title": "Hurricane-force winds", "action": "Go to an interior room now"}
            else:
                self.status_card = {"title": "High winds", "action": "Stay away from windows"}
        elif aid == "storm":
            self.status_card = {"title": "Storm warning", "action": "Stay indoors, monitor pressure"}
        elif aid == "heat":
            self.status_card = {"title": "Heat warning", "action": "Stay hydrated, find shade"}
        else:
            self.status_card = {"title": "Alert active", "action": "Check sensor readings"}

    # ------------------------------------------------------------------ message handlers

    def apply_reading(self, msg: dict):
        """Handle a §5 't':'r' reading line."""
        self.ts = _now()
        self.src = msg.get("src", "live")

        r = self.readings
        r["p"]     = msg.get("p")
        r["temp"]  = msg.get("temp")
        r["hum"]   = msg.get("hum")
        r["water"] = msg.get("water")
        r["rain"]  = msg.get("rain")
        r["wind"]  = msg.get("wind")

        wall = bool(msg.get("wall", 1))
        if wall != self.power["wall"]:
            # Transition: wall power just came back or just went out
            self.power["outage_since"] = None if wall else _now()
        self.power["wall"] = wall

        # Add any alarms the ESP32 lists that we haven't seen an event for yet
        for aid in msg.get("alarms", []):
            if not self._find_alarm(aid):
                self.alarms.append({"id": aid, "level": "alarm", "since": _now(), "acked": False, "explanation": None})

        self._refresh_status_card()

    def apply_alarm_event(self, msg: dict):
        """Handle a §5 't':'alarm' event line."""
        aid   = msg.get("id")
        level = msg.get("level", "alarm")
        state = msg.get("state")

        if state == "on":
            existing = self._find_alarm(aid)
            if not existing:
                self.alarms.append({"id": aid, "level": level, "since": _now(), "acked": False, "explanation": None})
            else:
                existing["level"] = level
        elif state == "off":
            self.alarms = [a for a in self.alarms if a["id"] != aid]

        self._refresh_status_card()

    def ack_alarm(self, alarm_id: str):
        a = self._find_alarm(alarm_id)
        if a:
            a["acked"] = True
        self._refresh_status_card()

    # ------------------------------------------------------------------ serialisation

    def to_dict(self) -> dict:
        return {
            "ts":          self.ts,
            "src":         self.src,
            "dev":         self.dev,
            "readings":    dict(self.readings),
            "power":       dict(self.power),
            "alarms":      list(self.alarms),   # dicts are already JSON-safe
            "storm":       dict(self.storm),
            "status_card": dict(self.status_card),
            "health":      dict(self.health),
            "replay":      dict(self.replay),
        }
