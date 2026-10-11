#!/usr/bin/env python3
"""
Fake box that talks MQTT (for testing TRANSPORT=mqtt without hardware).

  python scripts/fake_mqtt_box.py                       # broker at localhost:1883
  python scripts/fake_mqtt_box.py --host 192.168.4.2

Publishes the same readings/events as scripts/fake_esp32.py to stormbox/readings and stormbox/event,
"online" (retained) to stormbox/status with "offline" as the Last Will, and prints stormbox/cmd.
Keys + Enter: f flood  w wind  s storm  h heat  b short press  l long press  c clear  q quit
Needs a broker, e.g. Mosquitto with "listener 1883 0.0.0.0" and "allow_anonymous true".
"""
import argparse
import asyncio
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pi.box_service.fake import FakeESP32  # noqa: E402

KEYS = {"f": "flood", "w": "wind", "s": "storm", "h": "heat", "b": "short", "l": "long", "c": "clear"}


def topic_for(line: str, prefix: str) -> str:
    """Readings go to <prefix>/readings, alarm and button events to <prefix>/event."""
    return f"{prefix}/{'readings' if json.loads(line).get('t') == 'r' else 'event'}"


async def main(args):
    import paho.mqtt.client as mqtt

    prefix = args.prefix.strip("/")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.will_set(f"{prefix}/status", "offline", qos=1, retain=True)
    client.on_message = lambda c, u, m: print(f"[cmd] {m.payload.decode()}", file=sys.stderr, flush=True)
    client.on_connect = lambda c, u, f, rc, p=None: c.subscribe(f"{prefix}/cmd")
    client.connect(args.host, args.port)
    client.loop_start()
    client.publish(f"{prefix}/status", "online", qos=1, retain=True)

    fake = FakeESP32()
    loop = asyncio.get_running_loop()

    def read_keys():
        for raw in sys.stdin:
            key = raw.strip().lower()
            if key == "q":
                loop.call_soon_threadsafe(loop.stop)
            elif key in KEYS:
                loop.call_soon_threadsafe(fake.trigger, KEYS[key])
            elif key:
                print(f"Unknown key {key!r}: use {' '.join(KEYS)} q", file=sys.stderr)
    threading.Thread(target=read_keys, daemon=True).start()

    asyncio.create_task(fake.run())
    print(f"Publishing to {args.host}:{args.port} ({prefix}/...). Keys: {' '.join(KEYS)} q", file=sys.stderr)
    try:
        while True:
            line = await fake.readline()
            qos = 0 if topic_for(line, prefix).endswith("readings") else 1
            client.publish(topic_for(line, prefix), line, qos=qos)
    finally:
        client.publish(f"{prefix}/status", "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fake StormBox that publishes over MQTT")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="stormbox")
    try:
        asyncio.run(main(ap.parse_args()))
    except (KeyboardInterrupt, RuntimeError):
        pass
