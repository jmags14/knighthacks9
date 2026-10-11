# MQTT transport (optional)

USB serial is the default and the primary link (the XIAO RP2040 has no Wi-Fi). Use MQTT only if the box
becomes a Wi-Fi board (e.g. an ESP32). The JSON payloads and the box state are identical; only the transport changes.

## Run the hub on MQTT

```
set TRANSPORT=mqtt
set MQTT_HOST=localhost
python pi/run.py
```

Settings (all optional): `TRANSPORT=serial|mqtt` (default `serial`), `MQTT_HOST` (`localhost`), `MQTT_PORT` (`1883`),
`MQTT_TOPIC_PREFIX` (`stormbox`). Install `paho-mqtt` (it is listed in `requirements.txt`).

## Topics (LAPTOP_MVP.md section 3)

| Topic | Direction | Payload |
|---|---|---|
| `stormbox/readings` | box → hub | reading JSON, QoS 0 (`"t":"r"` is added if missing) |
| `stormbox/event` | box → hub | `{"t":"alarm",...}` / `{"t":"btn",...}`, QoS 1 |
| `stormbox/status` | box → hub | `online` / `offline`, retained; `offline` is the Last Will |
| `stormbox/cmd` | hub → box | `{"cmd":"hb"}`, `{"cmd":"lcd",...}`, ... |

A retained `offline` shows "Microcontroller disconnected" on the dashboard; a broker that is down shows
"MQTT broker not reachable ... Is Mosquitto running?".

## Test without hardware

1. Start a broker. Mosquitto 2.x only listens on localhost by default; for a real box add to `mosquitto.conf`:
   `listener 1883 0.0.0.0` and `allow_anonymous true`, then restart it. Allow ports 1883 and 8000 in the firewall.
2. `python scripts/fake_mqtt_box.py` (keys: f flood, w wind, s storm, h heat, b/l button, c clear, q quit).
3. In another terminal: `set TRANSPORT=mqtt` then `python pi/run.py`.

Unit tests (`pi/tests/test_mqtt.py`) need no broker.
