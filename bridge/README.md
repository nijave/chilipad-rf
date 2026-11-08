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
(`chilipad_cube`), `--units F`, `--min 55`, `--max 93`,
`--default-setpoint` (`CHILIPAD_DEFAULT_SETPOINT`, 72), `--no-sniff`,
`--log-level`. Give the password through the environment: a command-line
value is visible in `ps`. The bridge never logs it.

The Cube sends no feedback, so the bridge only knows a setpoint it sent,
sniffed from the remote, or restored from the broker. Until one exists it
shows the default setpoint in mode off, because Home Assistant hides the
temperature control while the target is unknown, and a mode change with no
known setpoint uses the default.

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

## Container (Podman quadlet)

Files:

* `Containerfile`: `python:3.12.16-slim-trixie`, pip installs `pyserial`
  and `paho-mqtt>=2.0`, copies only `chilipad_bridge.py` to `/app`. The image
  runs as the non-root user `chilipad` (UID 10001). `--selftest` does not work
  in the image (it needs `captures/` from the repo). Run it from a checkout.
* `build.sh`: builds a manifest list for `linux/amd64` and `linux/arm64`.
* `chilipad-bridge.container`: rootless quadlet (user unit) for the Pine A64.
* `chilipad-bridge.env.example`: example env file, no secrets.

### Build and push

On the build machine (x86_64, with `qemu-user-static` for arm64):

```sh
cd bridge
./build.sh                    # tag YYYYMMDD-<script sha> and latest, no push
./build.sh --tag 1.0.0        # explicit tag
podman run --rm registry.example.com/chilipad-rf:latest --help
podman run --rm --platform linux/arm64 registry.example.com/chilipad-rf:latest --help
```

Push is manual:

```sh
./build.sh --push             # pushes :<git sha> and :latest (both architectures)
```

`AutoUpdate=registry` compares the digest behind the tag named in the
quadlet, so the quadlet pins `:latest` and every push moves it.

### Install on the Pine

The quadlet is a user unit. The container runs rootless, the env file stays
in your home, and auto-update runs from your own timer.

1. Let your user open the dongle, and keep user units running without a login:

   ```sh
   sudo usermod -aG dialout "$USER"     # log out and in again
   sudo loginctl enable-linger "$USER"
   ```

2. Find the dongle's stable path. Quadlet `AddDevice=` does not expand globs,
   so you must use the exact name:

   ```sh
   ls -l /dev/serial/by-id/
   # usb-chilipad-rf_chilipad-sniffer_<serial>-if00 -> ../../ttyACM0
   ```

3. Install the unit and the env file:

   ```sh
   mkdir -p ~/.config/containers/systemd
   cp chilipad-bridge.container ~/.config/containers/systemd/
   cp chilipad-bridge.env.example ~/.config/chilipad-bridge.env
   chmod 600 ~/.config/chilipad-bridge.env
   ```

   Edit `~/.config/containers/systemd/chilipad-bridge.container`. Replace the
   `REPLACE_ME` path in `AddDevice=` with the name from step 2. Keep the
   `:/dev/chilipad` suffix. Edit `~/.config/chilipad-bridge.env` and set
   `CHILIPAD_MQTT_HOST`, `CHILIPAD_MQTT_USER` and `CHILIPAD_MQTT_PASSWORD`.
   Podman reads it with `--env-file`: `KEY=value`, no quotes, no `export`.

4. Start it:

   ```sh
   podman pull registry.example.com/chilipad-rf:latest
   systemctl --user daemon-reload
   systemctl --user start chilipad-bridge
   systemctl --user status chilipad-bridge
   journalctl --user -u chilipad-bridge -f
   ```

   Quadlet generates the service, so `systemctl --user enable` is not used.
   The `[Install] WantedBy=default.target` line starts it at boot (with
   lingering on).

How the device works: `AddDevice=` puts the dongle in the container as
`/dev/chilipad`, and `Environment=CHILIPAD_PORT=/dev/chilipad` points the
bridge at it. The bridge does not auto-detect the port in the container: the
container's `/sys` shows host names such as `ttyACM0`, not `/dev/chilipad`.
`GroupAdd=keep-groups` keeps your host groups (`dialout`) for the non-root
container user, so the device (`root:dialout`, mode 0660) opens without root.
It needs the `crun` runtime (the default on current Podman). Podman older than
5.0 has no `GroupAdd=` key. Use `PodmanArgs=--group-add keep-groups` there.
For a rootful system unit (`/etc/containers/systemd/`), use
`GroupAdd=<dialout GID>` (`getent group dialout`) instead.

The default rootless network (pasta) is enough for an outgoing MQTT
connection. Set `Network=host` only if the broker runs on the Pine and
listens on `127.0.0.1` only.

### Auto-update

The unit has `AutoUpdate=registry`. Turn on the user timer once:

```sh
systemctl --user enable --now podman-auto-update.timer
podman auto-update --dry-run          # show what would update
podman auto-update                    # update now
```

The timer (daily) pulls `:latest`. If the digest changed, it restarts the
unit, and rolls back if the new container does not start.

### Troubleshooting

* `Permission denied: '/dev/chilipad'`: check `ls -l /dev/ttyACM*` on the
  host (expect `root dialout ... 0660`) and `id` (expect `dialout`). After
  `usermod`, log out and in, then restart the user manager
  (`sudo systemctl restart user@$(id -u)`) or reboot, so that the units get
  the new group. Check `podman info --format '{{.Host.OCIRuntime.Name}}'`
  prints `crun`.
* `no such file or directory` for the `AddDevice` path: the by-id name is
  wrong or the dongle is not in the USB port. Run `ls -l /dev/serial/by-id/` again.
  The name includes the board's serial number, so a different dongle has a
  different name.
* Dongle unplugged and plugged in again: the container keeps the old device
  node, and the bridge retries forever. Run
  `systemctl --user restart chilipad-bridge`.
* SELinux hosts: if `ausearch -m avc` shows denials for the tty, run
  `sudo setsebool -P container_use_devices=1`.
* `--mqtt-host (or CHILIPAD_MQTT_HOST) is required`: the env file is missing
  or has the wrong path (`%h/.config/chilipad-bridge.env`).
* Send one setpoint by hand (stop the service first, the port is exclusive):

  ```sh
  systemctl --user stop chilipad-bridge
  podman run --rm --group-add keep-groups \
      --device /dev/serial/by-id/usb-chilipad-rf_chilipad-sniffer_<serial>-if00:/dev/chilipad \
      registry.example.com/chilipad-rf:latest --port /dev/chilipad --once 72
  systemctl --user start chilipad-bridge
  ```

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
  new value. Changes made on the Cube's own buttons are not seen. HackRF
  captures and the FCC filings confirm that the Cube only receives
  (`../analysis/poll-reply.md` section 7).
* After a power cut the Cube starts in the off state, but the bridge restores
  its last retained state. Home Assistant shows the old state until the next
  command or sniffed press. A set command (temperature or mode `auto`) turns
  the Cube on again. An energy-monitoring outlet gives Home Assistant the real
  on/off state.
* The remote keeps its own on/off state. After you press OFF on the remote,
  it ignores UP/DOWN until its power button, even if Home Assistant turned
  the Cube on in the meantime.
* A remote press is only seen when the dongle decodes it cleanly. The bridge can
  miss a weak press (remote far from the dongle).
* The dongle must be within a few metres of the Cube.
* The firmware must support `S<degF>` and `O<degF>`. Older firmware answers
  `[?] unknown command`, and the bridge logs an error.
* Units are °F only.
