"""Optional MQTT transport: no broker, no network. paho's client is replaced by a fake."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pi.box_service import mqttsource as ms
from pi.box_service import service as svc
from pi.box_service.state import BoxState


class FakeClient:
    def __init__(self):
        self.published, self.subscribed, self.started = [], [], False
        self.reconnect = None

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect = (min_delay, max_delay)

    def connect_async(self, host, port):
        self.addr = (host, port)

    def loop_start(self):
        self.started = True

    def loop_stop(self):
        self.started = False

    def disconnect(self):
        pass

    def subscribe(self, topics):
        self.subscribed.append(topics)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))


def msg(topic, payload):
    return NS(topic=topic, payload=payload if isinstance(payload, bytes) else payload.encode())


def source():
    return ms.MqttSource("broker", 1883, "stormbox", client=FakeClient())


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- settings
def test_transport_defaults_to_serial():
    assert ms.transport({}) == "serial"
    assert ms.transport({"TRANSPORT": "MQTT "}) == "mqtt"
    assert ms.transport({"TRANSPORT": ""}) == "serial"


def test_from_env_defaults_and_overrides():
    s = ms.MqttSource.from_env({})
    assert (s.host, s.port, s.prefix) == ("localhost", 1883, "stormbox")
    s = ms.MqttSource.from_env({"MQTT_HOST": "10.0.0.5", "MQTT_PORT": "1884", "MQTT_TOPIC_PREFIX": "/box/"})
    assert (s.host, s.port, s.topic("cmd")) == ("10.0.0.5", 1884, "box/cmd")


# ---------------------------------------------------------------- messages in
def test_readings_events_and_status_routing():
    async def go():
        s = source()
        s.start()
        s._on_connect(s._client, None, None, 0)
        assert s._client.subscribed == [[("stormbox/readings", 0), ("stormbox/event", 1), ("stormbox/status", 1)]]
        s._on_message(None, None, msg("stormbox/readings", '{"ms":1,"p":1008.4,"temp":29.1,"water":412,"wall":1}'))
        s._on_message(None, None, msg("stormbox/event", '{"t":"alarm","id":"flood","level":"alarm","state":"on"}'))
        s._on_message(None, None, msg("stormbox/status", "online"))          # not a line for BoxService
        s._on_message(None, None, msg("stormbox/other", "{}"))               # unknown topic ignored
        s._on_message(None, None, msg("stormbox/readings", "not json"))      # dropped
        lines = [await asyncio.wait_for(s.readline(), 1), await asyncio.wait_for(s.readline(), 1)]
        return s, lines
    s, lines = run(go())
    reading, event = (json.loads(x) for x in lines)
    assert reading["t"] == "r" and reading["p"] == 1008.4        # "t":"r" added when the box omits it
    assert event == {"t": "alarm", "id": "flood", "level": "alarm", "state": "on"}
    assert s._queue.empty()


def test_reading_that_already_has_t_is_unchanged():
    async def go():
        s = source()
        s.start()
        s._on_message(None, None, msg("stormbox/readings", '{"t":"r","p":1000}'))
        return json.loads(await asyncio.wait_for(s.readline(), 1))
    assert run(go()) == {"t": "r", "p": 1000}


# ---------------------------------------------------------------- commands out
def test_write_publishes_to_the_cmd_topic():
    async def go():
        s = source()
        await s.write(b'{"cmd":"hb"}')                # before start: dropped, no crash
        s.start()
        await s.write(b'{"cmd":"lcd","l1":"a","l2":"b"}')
        return s
    s = run(go())
    assert s._client.published == [("stormbox/cmd", '{"cmd":"lcd","l1":"a","l2":"b"}')]


# ---------------------------------------------------------------- health
def test_last_will_offline_sets_the_disconnected_message_and_online_clears_it():
    async def go():
        s = source()
        s.start()
        assert s.last_error is None
        s._on_message(None, None, msg("stormbox/status", "offline"))
        assert s.last_error.startswith("Microcontroller disconnected")
        s._on_message(None, None, msg("stormbox/status", "online"))
        assert s.last_error is None
    run(go())


def test_broker_down_message_and_recovery():
    async def go():
        s = source()
        s.start()
        s._on_disconnect(s._client, None)
        assert "broker not reachable at broker:1883" in s.last_error and "Mosquitto" in s.last_error
        s._on_connect_fail(s._client, None)
        assert "Mosquitto" in s.last_error
        s._on_connect(s._client, None, None, 0)
        assert s.last_error is None
        s._on_connect(s._client, None, None, NS(is_failure=True))
        assert "Mosquitto" in s.last_error
    run(go())


def test_start_connects_with_reconnect_and_only_once():
    async def go():
        s = source()
        s.start()
        s.start()
        return s
    s = run(go())
    assert s._client.addr == ("broker", 1883) and s._client.started and s._client.reconnect == (1, 10)


# ---------------------------------------------------------------- into the box state
def test_a_reading_over_mqtt_reaches_box_state_and_a_status_error_reaches_health():
    async def go():
        state = BoxState()
        src = source()
        service = svc.BoxService(state, src)
        src.start()
        src._on_message(None, None, msg("stormbox/readings", '{"ms":5,"p":1008.4,"temp":29.1,"hum":70,"water":412,"rain":16}'))
        raw = await asyncio.wait_for(src.readline(), 1)
        service._handle_line(raw)
        src._on_message(None, None, msg("stormbox/status", "offline"))
        service._running = True
        task = asyncio.create_task(service._health_loop())
        await asyncio.sleep(1.2)
        service._running = False
        await asyncio.wait_for(task, 3)
        return state
    state = run(go())
    assert state.readings["p"] == 1008.4 and state.readings["water"] == 412
    assert state.health["serial_error"].startswith("Microcontroller disconnected")


# ---------------------------------------------------------------- the fake publisher script
def test_fake_publisher_routes_topics():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fake_mqtt_box.py"
    spec = importlib.util.spec_from_file_location("fake_mqtt_box", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.topic_for('{"t":"r","p":1}', "stormbox") == "stormbox/readings"
    assert mod.topic_for('{"t":"alarm","id":"flood"}', "stormbox") == "stormbox/event"
    assert mod.topic_for('{"t":"btn","press":"short"}', "x") == "x/event"
