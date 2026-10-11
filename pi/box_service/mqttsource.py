"""MqttSource: the optional MQTT transport (TRANSPORT=mqtt). Same interface as SerialSource, so
BoxService does not change: readline() gives one JSON line, write() sends one JSON command.

Topics (LAPTOP_MVP.md section 3; prefix is MQTT_TOPIC_PREFIX, default "stormbox"):
    <prefix>/<readings>  box -> hub  reading JSON (QoS 0). "t":"r" is added if the box leaves it out.
    <prefix>/event       box -> hub  {"t":"alarm",...} / {"t":"btn",...} (QoS 1)
    <prefix>/status      box -> hub  "online" / "offline", retained; "offline" is the box's Last Will
    <prefix>/cmd         hub -> box  {"cmd":"hb"} {"cmd":"lcd",...} ...

Settings (environment): TRANSPORT=serial (default) | mqtt, MQTT_HOST=localhost, MQTT_PORT=1883,
MQTT_TOPIC_PREFIX=stormbox, MQTT_READINGS_SUBTOPIC=readings.
paho-mqtt is imported only when the source starts.
"""
import asyncio
import json
import os


def transport(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get("TRANSPORT") or "serial").strip().lower()


class MqttSource:
    def __init__(self, host="localhost", port=1883, prefix="stormbox", readings_subtopic="readings",
                 username=None, password=None, client=None):
        self.host, self.port, self.prefix = host, int(port), prefix.strip("/")
        self.readings_subtopic = readings_subtopic
        self.username, self.password = username, password
        self._client = client            # tests pass a fake; otherwise built on first use
        self._queue = None               # asyncio.Queue, created on the event loop
        self._loop = None
        self._broker_error = None        # broker unreachable
        self._status_error = None        # box said "offline" (Last Will)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(env.get("MQTT_HOST", "localhost"), env.get("MQTT_PORT", "1883"),
                   env.get("MQTT_TOPIC_PREFIX", "stormbox"),
                   env.get("MQTT_READINGS_SUBTOPIC", "readings"),
                   env.get("MQTT_USERNAME"), env.get("MQTT_PASSWORD"))

    def topic(self, name):
        return f"{self.prefix}/{name}"

    @property
    def last_error(self):
        """Plain-words problem for the dashboard, or None. Same idea as SerialSource.last_error."""
        return self._broker_error or self._status_error

    # ---------------------------------------------------------------- start
    def start(self):
        """Create the queue on the running loop and connect (paho reconnects by itself)."""
        if self._queue is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        if self._client is None:
            import paho.mqtt.client as mqtt
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        c = self._client
        if self.username:
            c.username_pw_set(self.username, self.password)
        c.on_connect, c.on_disconnect = self._on_connect, self._on_disconnect
        c.on_connect_fail, c.on_message = self._on_connect_fail, self._on_message
        c.reconnect_delay_set(min_delay=1, max_delay=10)
        c.connect_async(self.host, self.port)
        c.loop_start()

    def _no_broker(self):
        return f"MQTT broker not reachable at {self.host}:{self.port}. Is Mosquitto running?"

    # ---------------------------------------------------------------- paho callbacks (paho's thread)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = getattr(reason_code, "is_failure", reason_code not in (0, None))
        if failed:
            self._broker_error = self._no_broker()
            return
        self._broker_error = None
        client.subscribe([(self.topic(self.readings_subtopic), 0), (self.topic("event"), 1), (self.topic("status"), 1)])

    def _on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None):
        self._broker_error = self._no_broker()

    def _on_connect_fail(self, client, userdata):
        self._broker_error = self._no_broker()

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode(errors="replace").strip()
        if topic == self.topic("status"):
            if payload.strip('"').lower() == "offline":
                self._status_error = "Microcontroller disconnected: the box went offline."
            else:
                self._status_error = None
            return
        if topic == self.topic(self.readings_subtopic):
            try:
                data = json.loads(payload)
                data.setdefault("t", "r")
                payload = json.dumps(data)
            except (ValueError, AttributeError):
                return                       # not JSON: drop it (BoxService would ignore it anyway)
        elif topic != self.topic("event"):
            return
        if self._loop and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)

    # ---------------------------------------------------------------- SerialSource interface
    async def readline(self) -> str:
        self.start()
        return await self._queue.get()

    async def write(self, data: bytes):
        if self._client is None or self._queue is None:
            return                           # not started yet: drop, like SerialSource with a closed port
        try:
            self._client.publish(self.topic("cmd"), data.decode())
        except Exception as exc:
            print(f"[service] mqtt publish error: {exc}")

    def close(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
