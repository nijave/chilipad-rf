# chilipad-rf — ChiliPad Cube remote: sniffer, decoder and controller

Status (2026-11-04): protocol decoded; the Cube takes commands from an
nRF52840 dongle. The remote is an LT8920 at 62.5 kbit/s on
2450 MHz with the chip's default sync word; packet `07 A5 <cmd> SP 40 40 00
(cmd+SP-0x80)` with CRC-16/KERMIT, `cmd` 0x44 = set/on, 0x50 = off. Details
in `analysis/burst-structure.md`.

Status (2026-11-07): HackRF captures show the Cube never transmits; the
other 2.4 GHz traffic near it (families A and B) belongs to another device.
The remote keeps its own on/off state. See `analysis/poll-reply.md`
sections 6–7.

- `nrf52/` — Zephyr firmware for the PCA10059 dongle: sniffer, replay, and
  the `S<degF>` / `O<degF>` controller commands (`nrf52/README.md`).
- `bridge/` — serial → MQTT bridge exposing the Cube as a Home Assistant
  climate entity (`bridge/README.md`).
- `tools/capgui.py` — capture GUI with a visible clock for filmed runs.
- `analysis/` — decoder, encoder, burst generators and write-ups.
  `analysis/hackrf-plan.md` covers HackRF IQ captures and the offline
  demodulator `analysis/scripts/iq_demod.py`.
- `captures/` — raw capture logs.
- The ESP8266 + nRF24L01+ sketch below is the original sniffer; it still
  serves as a second receiver for checking the dongle's transmissions.

## Original sniffer (ESP8266 + nRF24L01+)

ESP8266 (D1 mini / NodeMCU) firmware that puts an nRF24L01+ into Travis
Goodspeed's "promiscuous mode" (illegal 2-byte address width) to sniff a
proprietary 2.4 GHz remote control built on an LT8920 radio, so you can
recover the remote's **64-bit sync word** and per-button **payloads** from the
serial log.

Target signal: 2450 MHz (nRF24 channel 50), GFSK 1 Mbps. Air frame:
3-byte preamble (0xAA/0x55 alternating) | 64-bit sync word (LSB-first on air)
| 4-bit trailer | payload (first byte = length) | CRC16. Scrambler off.
Bursts last ~2.4 ms and repeat while a button is held.

## Wiring

| nRF24L01+ pin | D1 mini / NodeMCU | Note                                     |
|---------------|-------------------|------------------------------------------|
| GND           | GND               |                                          |
| VCC           | 3V3               | **10 µF capacitor across VCC↔GND** — the nRF24 brown-outs without it, especially at PA_MAX |
| CE            | D8 (GPIO15)       |                                          |
| CSN           | D2 (GPIO4)        |                                          |
| SCK           | D5 (GPIO14)       |                                          |
| MOSI          | D7 (GPIO13)       |                                          |
| MISO          | D6 (GPIO12)       |                                          |
| IRQ           | —                 | unconnected                              |

Power the board from stable USB 3.3 V; the radio draws current spikes the
ESP8266 regulator may not like.

## Build / flash / monitor

```sh
uvx --from platformio pio run -e d1_mini          # build
uvx --from platformio pio run -e d1_mini -t upload
uvx --from platformio pio run -e d1_mini -t monitor   # or: pio device monitor -b 115200
```

(`nodemcuv2` works too: `-e nodemcuv2`.) The firmware needs no WiFi; it calls
`WiFi.mode(WIFI_OFF)` at boot. Serial is 115200 baud; `h` prints the help
menu and current config at any time.

## Commands

| Cmd      | Action |
|----------|--------|
| `h`      | help + current config (channel, trigger address, SETUP_AW + RX_ADDR_P0 readback, SPI chip check) |
| `s<n>`   | channel scan, n seconds (default 2, max 30) using `testRPD()` carrier detect; press remote buttons during it; prints per-channel hits and the top 5 |
| `c<n>`   | set channel 0..125 (default 50 = 2450 MHz) |
| `a`      | cycle address variant: `AAAA`, `5555`, `00AA`, `0055`, `AA00`, `5500` (bytes listed in register LSByte-first order; try all orientations) |
| `w<hex>` | custom RX trigger address, 3–5 bytes given in **on-air RAW order** (e.g. `w0B5C8E03` — the constant frame prefix found after the preamble). Sets SETUP_AW to 3/4/5 bytes accordingly. `w` alone returns to the 2-byte variants |
| `o`      | flip the custom trigger's register byte order (as-given ↔ reversed); the on-air byte order vs the nRF24's LSByte-first register write order is unverified, so try both |
| `p`      | start/stop promiscuous capture. Each packet prints `<millis> RAW <32 bytes hex> \| REV <bit-reversed hex>` |
| `t`      | top 10 most frequent 8-byte prefixes of REV frames (real frames share the sync word, so it dominates noise) |
| `z`      | clear the prefix table |
| `f<hex>` | print-filter: only print frames whose RAW bytes contain this hex substring (≤8 bytes); `f` alone clears it |
| `x<n>`   | replay the last captured RAW 32-byte frame n times (default 10, ~5 ms apart) as TX with the same channel/address, no CRC, no ack |

## How to find the sync word — walkthrough

1. **Find the channel.** Send `s`, then press and hold remote buttons during
   the 2-second scan. The LT8920's channel (2450 MHz → channel 50) should
   light up as one of the top 5. Lock it in with `c50` (or whatever wins).
2. **Start capture.** Send `p` and press and hold each remote button in turn,
   several seconds each. Every radio burst prints one line:
   `millis RAW <hex> | REV <hex>`. The **REV** view is the likely-useful one:
   the LT8920 sends LSB-first while the nRF24 clocks MSB-first, so
   bit-reversing each byte restores the transmitter's logical bit order.
3. **Spot the sync word.** Send `t`. Frames that started at the preamble share
   the same leading bytes in REV view — the 8-byte prefix that dominates the
   count is preamble+sync-word material. The real frames form a tight cluster;
   random noise frames scatter. Try `a` to cycle through `AAAA`, `5555`,
   `00AA`, `0055`, `AA00`, `5500` (both byte orientations of the trigger are
   included since the on-air bit order is unverified) and capture again.
4. **Inspect candidates.** Use `f<hex>` (e.g. `fAAAAAA55`) to print only the
   frames containing a candidate byte run and compare REV lines across
   different buttons: the 64-bit sync word stays constant; the payload (first
   byte = length) changes per button. With a 2-byte trigger address and fixed
   32-byte capture, a REV frame looks like
   `[trigger bytes][sync word bytes…][trailer+length][payload…][CRC16]`,
   so the sync word is the stable run right after the preamble pattern.
5. **Verify.** Send `x10` to replay the last captured RAW frame 10 times back
   at the receiver with the same channel/2-byte address, no CRC, no ack. If
   the target reacts, your captured framing is right.
6. **Refine with a custom trigger.** Capture showed the on-air bitstream after
   the preamble starts with the constant bytes `0B 5C 8E 03` (RAW order).
   Trigger on those instead of the preamble: send `w0B5C8E03` (3–5 bytes are
   accepted, given in RAW order). The mapping between on-air byte order and
   the nRF24's LSByte-first register write is unverified, so if nothing
   triggers, press `o` to flip the byte order. `w` alone (or `a`) returns to
   the 2-byte preamble variants. The `[cfg]`/`[rx]` lines print the SETUP_AW
   value and the full RX_ADDR_P0 readback so you can confirm what's actually
   on the chip.

## Notes / gotchas

- The promiscuous trick works by writing `SETUP_AW = 0x00` (2-byte address).
  RF24's `setAddressWidth()` won't allow this, so `main.cpp` subclasses RF24
  to reach the protected register primitives, re-applies the setting **after
  every** RF24 call that could rewrite it, and prints the read-back for
  verification.
- CRC is disabled and auto-ack is off on all pipes, so *any* preamble hit
  yields a 32-byte raw dump — expect mostly noise; `t` + `f` is how you dig
  the real frames out.
- This tool is for recovering access to hardware you own. Obey local RF law.
