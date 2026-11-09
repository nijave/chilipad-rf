# capgui.py — capture clock GUI

A one-file Tkinter GUI that shows a huge capture clock while the ESP8266 and nRF52840
sniffers log RF packets. Film the laptop screen and the remote with a phone; the on-screen
clock lets you line up button presses in the video with logged packets to within ~0.1 s.

## Run

```sh
uv run tools/capgui.py --esp /dev/ttyUSB0 --nrf /dev/ttyACM1
```

Flags:

| Flag | Default | Meaning |
|------|---------|---------|
| `--esp PORT` | `/dev/ttyUSB0` | ESP8266 sniffer port; `none` to disable |
| `--nrf PORT` | `/dev/ttyACM1` | nRF52840 USB CDC sniffer port; `none` to disable |
| `--out DIR` | repo's `docs/captures/` | directory that gets one sub-directory per session |

Both devices are optional — the GUI works if either or both are missing or unplugged
(a device shows `missing` and its thread retries the port every 2 s). The ESP port is
opened with DTR/RTS low so opening it does not reset the board.

## The window

- **Big clock** (top, ~1/3 of window height): during the pre-roll countdown it shows
  `-3` … `-1`; once recording it shows capture time `+12.3` (seconds since countdown
  zero, one decimal); after stop it keeps the last value.
- **Remaining line**: `27.6 s left`.
- **Window background**: grey = idle, amber = pre-roll, green = recording, red flash
  for 0.5 s when a capture ends.
- **Device panels**: one per sniffer — a big square that flashes cyan for 150 ms on
  every packet line (useful for checking video/log alignment), a packet counter, and
  `connected` / `missing` / `disabled` status.
- **Controls**: duration (s, default 30), pre-roll (s, default 3), per-device start and
  stop commands (space-separated, default `p`; each sent as its own line ~100 ms apart),
  a free-text note saved in the session header, and one Start/Stop button.
- **Keys**: `Space` = start/stop, `M` = add a mark (event only), `Escape` = stop.

## Capture flow

Pressing Start creates `<out>/YYYYmmdd-HHMMSS/`, writes `meta.txt`, counts down the
pre-roll, then at zero records `t0` (monotonic clock) and sends each device's start
commands — so the first packets land at `t >= 0`. Recording stops at the duration, or
via the button / `Space` / `Escape`: stop commands are sent, logging continues for
300 ms, files close, and the window flashes red.

## Log format

Each session directory contains:

- `meta.txt` — wall-clock start (ISO with ms), note, duration, pre-roll, ports, start
  and stop commands, plus the wall-clock `t0` moment.
- `<device>.log` (`esp.log`, `nrf.log`) — one line per line received from that device:
  `+12.345678 <line>` (time in seconds relative to `t0`, 6 decimals).
- `all.log` — same lines from both devices interleaved: `+12.345678 <device> <line>`.
- `events.log` — marks and state changes: `+0.000000 start`, `+12.345678 mark 3`,
  `+30.000000 stop`.

A received line containing ` RAW ` counts as a packet (it flashes the device square and
increments the counter). Lines received while idle are not written to disk.

## Filming tip

Frame the shot so both the big clock **and** the remote control are visible for the whole
capture. Start the recording on the phone before pressing Start in the GUI (the pre-roll
countdown gives you time), and press `M` at any moment you want to be able to find later
— marks land in `events.log` with their capture time.
