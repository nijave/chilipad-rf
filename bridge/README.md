# chilipad-bridge: Home Assistant control for the ChiliPad Cube

`chilipad_bridge.py` connects the `chilipad-sniffer` dongle (nRF52840, see
`../nrf52/README.md`) to an MQTT broker. Home Assistant then shows the Cube as
a `climate` entity. The script is one file with inline dependencies
(PEP 723: `pyserial`, `paho-mqtt>=2.0`), so `uv run` installs them.

What it does:

* A temperature from Home Assistant sends `S<degF>` to the dongle (55..93).
  `S` also turns the Cube on.
* Mode `off` sends `O<last setpoint>`. Mode `auto` sends `S<last setpoint>`.
* Sniff mode (on by default) keeps the dongle receiving with
  `c50 mb1 l64 w9388B0CA` + `p`. The bridge decodes each remote press
  (setpoint or off) and publishes it, so Home Assistant follows manual
  changes. The bridge stops capture before each send and starts it again after.
* The bridge waits up to 3 s for `[tx] sent=1 ... timeouts=0` and retries
  once. If the send fails, it publishes the previous state again.

## Install and run

```sh
cd bridge
uv run chilipad_bridge.py --selftest            # decoder test, no hardware
uv run chilipad_bridge.py --once 72             # send one setpoint, no MQTT
uv run chilipad_bridge.py --once 72 --off       # send the power-off press
CHILIPAD_MQTT_PASSWORD=... uv run chilipad_bridge.py \
    --mqtt-host broker.lan --mqtt-user chilipad
```

The bridge finds the port by USB VID `0x2fe3` or product `chilipad-sniffer`. Use
`--port /dev/ttyACM1` to set it. The bridge never opens `/dev/ttyUSB*`
(the ESP8266 sniffer). It opens the port with DTR high and RTS low, which
the Zephyr CDC ACM console needs, and reconnects every 2 s after an unplug.

Options (environment variable in brackets): `--port` (`CHILIPAD_PORT`),
`--mqtt-host` (`CHILIPAD_MQTT_HOST`), `--mqtt-port` (`CHILIPAD_MQTT_PORT`,
1883), `--mqtt-user` (`CHILIPAD_MQTT_USER`), `--mqtt-password`
(`CHILIPAD_MQTT_PASSWORD`), `--name` ("ChiliPad Cube"), `--unique-id`
(`chilipad_cube`), `--units F`, `--min 55`, `--max 93`, `--no-sniff`,
`--log-level`. Give the password through the environment: a command-line
value is visible in `ps`. The bridge never logs it.

The user that runs the bridge must be able to open the port (group
`dialout` on most distributions).

## systemd unit

`/etc/systemd/system/chilipad-bridge.service`:

```ini
[Unit]
Description=ChiliPad Cube MQTT bridge
After=network-online.target
Wants=network-online.target

[Service]
User=<user>
SupplementaryGroups=dialout
WorkingDirectory=/opt/chilipad-rf/bridge
EnvironmentFile=/etc/chilipad-bridge.env
ExecStart=/usr/bin/uv run chilipad_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/chilipad-bridge.env` (mode 0600):

```sh
CHILIPAD_MQTT_HOST=broker.lan
CHILIPAD_MQTT_USER=chilipad
CHILIPAD_MQTT_PASSWORD=change-me
```

Then `systemctl daemon-reload && systemctl enable --now chilipad-bridge`.

## Home Assistant

* Discovery: `homeassistant/climate/<unique_id>/config` (retained). The
  bridge sends it again when `homeassistant/status` is `online`.
* Topics, under `chilipad/<unique_id>/`:
  * `temperature/set`: Home Assistant writes the setpoint here.
  * `temperature/state`: last setpoint (retained).
  * `mode/set` and `mode/state`: `off` or `auto` (state retained).
  * `availability`: `online` while the bridge has a connection to the broker
    and to the dongle, otherwise `offline` (the MQTT last will also sets it).
* The entity has no current-temperature sensor. It shows only the setpoint.
* After a restart, the bridge reads its own retained state and mode back from
  the broker.

## Limits

* The Cube sends no feedback. The state is the last value the bridge sent or
  sniffed. If a burst does not reach the Cube, Home Assistant still shows the
  new value. Changes made on the Cube's own buttons are not seen.
* A remote press is only seen when the dongle decodes it cleanly. The bridge can
  miss a weak press (remote far from the dongle).
* The dongle must be within a few metres of the Cube.
* The firmware must support `S<degF>` and `O<degF>`. Older firmware answers
  `[?] unknown command`, and the bridge logs an error.
* Units are °F only.
