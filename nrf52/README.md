# chilipad-sniffer — nRF52840 Dongle raw 2.4 GHz sniffer

Second receiver next to the ESP8266 + nRF24L01+ promiscuous sniffer in
`../esp8266/src/main.cpp`. It turns a Nordic **nRF52840 Dongle (PCA10059)** into a raw
2.4 GHz receiver for the proprietary LT8920-based remote (1 Mchip/s GFSK,
where each 4-bit nibble is one of 16 64-chip codewords, i.e. 62.5 kbit/s of
data; 2450 MHz) using nothing but the `NRF_RADIO` peripheral: no Bluetooth stack, no
ESB library, no SoftDevice. It talks to the laptop over the dongle's USB CDC ACM
serial port with the same line-based command style and the same `P <t> ... RAW
<hex> | REV <hex>` packet format the ESP firmware uses, so the existing GUI can
read either receiver.

## Build

Zephyr 4.4.2 + Zephyr SDK with a Zephyr workspace (check out this repository
somewhere and call the build directory anything you like):

```sh
cd ~/zephyrproject && . .venv/bin/activate && \
  west build -p always -b nrf52840dongle/nrf52840 -d build/nrf52 <repo>/nrf52
```

You can also rsync `nrf52/` to another machine that has the Zephyr SDK and
build it there. The firmware builds clean with Zephyr 4.4.2 for
`nrf52840dongle/nrf52840` and `raytac_mdbt50q_cx_40_dongle/nrf52840`. The
`uart0` command port (below) builds, but nobody has tested it on hardware
yet.

No snippet flag is needed: `nrf52840dongle` defaults to the CDC ACM serial
backend (`BOARD_SERIAL_BACKEND_CDC_ACM`), which enables the Zephyr 4.4 USB
device stack next, the CDC ACM UART and the console. `prj.conf` spells those
options out so a board defconfig change cannot silently drop the console.

The stock `nrf52840dongle/nrf52840` target already links the image at
**0x1000** (`CONFIG_FLASH_LOAD_OFFSET`), which is what the Nordic open USB DFU
bootloader expects. Use that target, not the `.../bare` variant.

### Raytac MDBT50Q-CX dongle

The Raytac MDBT50Q-CX-40 dongle ships the same Nordic open bootloader, so the
DFU steps below work unchanged. Build it with
`-b raytac_mdbt50q_cx_40_dongle/nrf52840`, **not** the PCA10059 target. The
PCA10059 board config enables the DC/DC regulator and the Nordic dongle has
the inductors for it; the Raytac module has none and runs in LDO mode. A
PCA10059 image programs fine ("Device programmed") and then stays silent:
no LED, no USB, no error. The dongle survives this; press RESET and the
bootloader comes back. (From a product review, 2026-11; not yet tried here.)

## Flash over the Nordic open USB DFU bootloader

```sh
nrfutil nrf5sdk-tools pkg generate --hw-version 52 --sd-req=0x00 \
    --application build/nrf52/zephyr/zephyr.hex \
    --application-version 1 app.zip

# press the dongle's sideways RESET button (red LED pulses = bootloader), then:
nrfutil nrf5sdk-tools dfu usb-serial -pkg app.zip -p /dev/ttyACM<n>
```

Then open the application's own port (a different `/dev/ttyACM<n>`) and talk to
it with any serial terminal, 115200 baud (the CDC ACM baud rate is ignored).

## Commands

| Command | Action |
|---------|--------|
| `?` | probe: one line `chilipad-sniffer <build> ok` (see [UART command port](#uart-command-port-for-the-esp32-bridge)) |
| `h` | help + current config (channel, data rate, length, address, byte order, bit reversal, BALEN, `BASE0`/`PREFIX0` as written, HFXO, capture state, packet/drop counts) |
| `p` | start/stop capture. **Capture starts OFF at boot.** |
| `c<n>` | channel 0..100 (default 50 = 2450 MHz) |
| `l<n>` | packet length 1..255 (default 64) |
| `w<hex>` | trigger address, 3..5 bytes in on-air order, e.g. `w0B5C8E03`. `w` alone resets the address to `0B5C8E03` |
| `o` | flip the address byte order (as given ↔ reversed); re-applies the address, keeps capturing |
| `r` | flip per-byte bit reversal (on by default); re-applies the address, keeps capturing |
| `s<sec>` | energy scan of ch 0..100 for `<sec>` seconds (default 2, max 60), then the top 10 channels by number of RSSI samples above -75 dBm; restores the previous channel and capture state |
| `y<list>@<ms>` | hop the receiver through a channel list, `<ms>` dwell per channel, e.g. `y10,30,50,70@200`. `y` alone stops hopping and returns to the fixed channel |
| `x<n>` | send the last captured buffer `n` times (default 10, 5 ms apart) on its receive channel, same address/length/no CRC; then restore the previous state and print `[tx] sent=<n> segs=<n> timeouts=<k>` |
| `t<hex>` / `t1<hex>` | load TX segment 1: 1..255 bytes, two hex digits per byte (up to 510 digits). `t1` alone clears segment 1 |
| `t2<hex>` | load TX segment 2 (same rules). `t2` alone clears segment 2 |
| `t` | clear both TX segments |
| `z<n>` | send the loaded segments `n` times (default 1, max 1000, 5 ms apart, radio disabled between bursts) on the current channel `c`; segment 2 follows segment 1 back to back (see below); then restore the previous state and print `[tx] sent=<n> segs=<m> timeouts=<k>` |
| `m<rate>` | data rate for RX and TX: `m1` = `Nrf_1Mbit`, `m2` = `Nrf_2Mbit`, `mb1` = `Ble_1Mbit` (default), `mb2` = `Ble_2Mbit`, `m250` = `Nrf_250Kbit` (see below). Only `MODE` changes; address, `PCNF0`/`PCNF1`, CRC off and bit reversal stay the same. If capture is on, the firmware stops it and starts it again in the new mode |
| `k<ms>` | unmodulated carrier for `<ms>` ms (1..5000) on the current channel `c`: `MODECNF0.DTX = Center`, `TXEN`, wait for `READY`, stay in `TXIDLE`, then `DISABLE` and restore `MODECNF0`. Stops capture first and restores it after. Prints `[k] carrier <mode> <ms> ms done` |
| `k<ms>b0` / `k<ms>b1` | same, but `DTX = B0` / `B1`: the constant '0' / '1' FSK tone instead of the center frequency (`<mode>` is `b0` / `b1`) |
| `S<degF>[,<n>]` | set the Cube to `<degF>` (55..93): encode the press burst, send it `n` times (default 1, max 10, 100 ms apart), print `[S] <degF> sent`. See [Setting the Cube's temperature](#setting-the-cubes-temperature-s) |
| `O<degF>[,<n>]` | power the Cube OFF (command byte `0x50`). `<degF>` is the Cube's current setpoint, 55..93 (the dongle does not know it). Prints `[O] OFF @ <degF> sent`. Power ON is any `S<degF>` |
| `T<degF>` / `TO<degF>` | print `addr1 seg1 addr2 seg2` (hex) of the `S<degF>` / `O<degF>` burst, the same line `docs/analysis/scripts/gen_burst2.py <degF>` (or `nrf52/tools/gen_burst2_cmd.py <degF> 50`) prints. Sends nothing |
| `d` | radio register dump, ISR counters, `[d] rate=<mode> us_per_byte=<n>`, and TX totals `[d] tx ready=<n> end=<n> timeouts=<n> last_gap_us=<n>`, UART port counters `[d] uart drops=<n> rx_lost=<n> ready=<0\|1>` |

Output is one line per packet:

```
P <t_us> dt=<us> ch=<n> rssi=-<n> RAW <len bytes hex> | REV <same bytes bit-reversed>
```

`t_us` is the 1 MHz `TIMER1` capture value taken at the `ADDRESS` event
(extended to 64 bit; `dt` is the gap since the previous received packet). The
literal token ` RAW ` is present on every packet line. `REV` is the bit-reversed
view, i.e. the LT8920's LSB-first byte values — the useful one for recovering
the sync word, exactly like the ESP firmware's `REV`.

If the packet ring in the firmware fills up faster than the USB link drains it,
`[drop n]` lines are printed (rate limited) and the totals appear in `h`.
Output never blocks: when no host is reading, lines are dropped and counted in
`output drops`.

## UART command port (for the ESP32 bridge)

The firmware also accepts commands on the hardware UART (`uart0`). An ESP32 can
drive the dongle through this port instead of USB. The commands and the reply
lines are the same as on USB CDC ACM. USB keeps working at the same time.

| Setting | Value |
|---------|-------|
| Pads | TX = **P0.20**, RX = **P0.24** (pads on the PCA10059 edge) |
| Levels | 3.3 V logic (nRF52840 VDD). Do not connect 5 V signals |
| Format | 115200 baud, 8N1, no flow control |
| Line end | `\n` or `\r\n` on input; every output line ends with `\r\n` |

P0.17 and P0.22 (RTS/CTS in the board's default `uart0` pinctrl) are not
used. `app.overlay` defines its own pinctrl with only TX and RX. RX has the
internal pull-up on, so an unconnected RX stays idle.

Wiring:

```
dongle P0.20 (TX)  ->  ESP32 RX (UART2 RX, for example GPIO16)
dongle P0.24 (RX)  <-  ESP32 TX (for example GPIO17)
dongle GND         --  ESP32 GND
ESP32 5 V          ->  dongle VBUS pad (powers the dongle when USB is unplugged)
```

Do not feed 5 V to VBUS while the dongle is also plugged into a USB host.

Rules:

* Each port has its own line buffer. Input on USB and UART at the same time
  does not mix.
* Reply lines of a command go only to the port that sent the command.
  This includes long commands such as `s` (`[scan]` lines) and `e` (`E` lines),
  because they print while the command runs.
* Lines from the main loop go to **both** ports: `P ...` capture lines and
  `[drop n]`. With the UART idle, USB gets the same output as before.
  The boot line `[ok] chilipad-sniffer ready ...` also goes to both.
* An empty line is ignored. Send `\n` first after a reset to clear any
  noise bytes that the ESP32 boot put on the line.
* The UART output ring holds 4 kB. 115200 baud moves about 11.5 bytes per ms,
  so one 255-byte capture line (about 1.1 kB) takes about 100 ms. When the ring
  is full, the firmware drops the whole line and counts it. It never waits, so a
  slow UART does not stop USB output or the radio. `d` prints
  `[d] uart drops=<bytes> rx_lost=<bytes> ready=<0|1>`. `rx_lost` counts input
  bytes lost because the 1 kB UART input ring was full.
* RX and TX run from the `uart0` interrupt (EasyDMA, `CONFIG_UART_0_INTERRUPT_DRIVEN`).
  The main loop does not poll this UART, so it does not lose input bytes
  between its 1 ms passes. The RADIO interrupt has a higher priority.
* `x`, `z`, `S` and `O` lock interrupts for up to about 2 ms per burst. Do not
  send UART input while one of these commands runs. Wait for its last reply
  line (for example `[S] 74 sent`).

### Probe (`?`)

`?` prints one line:

```
chilipad-sniffer <build> ok
```

`<build>` is the UTC time of the CMake configure step, for example
`20261104-2010Z` (`CMakeLists.txt`). The build host has no `.git`, so there is
no git hash. The ESP32 can send `\n?\n` and wait for a line that starts with
`chilipad-sniffer ` and ends with ` ok`.

## Data rate (`m`)

`m` sets `MODE` for receive, `x`, `z` and `k`. The firmware scales the
TX event timeouts and the `[z] seg gap` nominal air time by the byte time
of the mode: 8 µs per byte at 1 Mbit, 4 µs at 2 Mbit, 32 µs at 250 kbit.
`PCNF0` keeps the 8-bit preamble in every mode. Nordic's ESB driver uses a
16-bit preamble at 2 Mbit, so the 2 Mbit receive sensitivity can be lower.

Nordic marks `Nrf_250Kbit` as deprecated on the nRF52840. The firmware offers `m250` only
when the MDK still defines `RADIO_MODE_MODE_Nrf_250Kbit`. The nRF52840 MDK
in Zephyr 4.4.2 (`hal_nordic`) does not define it, so this build rejects
`m250` with a message and supports `m1` and `m2` only.

## Transmitting a long burst (`t` / `z`)

One nRF52 packet carries at most 255 payload bytes after the address. The
remote's real burst is about 300 bytes, so `z` can send it as two packets
back to back. Each segment goes out as:

```
preamble (1 byte) + address (the current `w` address) + payload
```

The address uses the same byte order and bit reversal (`o`, `r`) as receive
and `x`. Payload bytes are in the RAW view (MSBit first), the same as the
`RAW` field of a `P` line. CRC is off.

Segment 2 also starts with a preamble and the `w` address. To build the two
segments from a captured burst:

1. Find an occurrence of the address bytes inside the burst, at a point that
   puts at most 255 bytes on each side.
2. Segment 1 = the bytes from the end of the leading address up to (but not
   including) that occurrence. Load it with `t<hex>`.
3. Segment 2 = the bytes after that occurrence. **Do not include the 4 address
   bytes**: the radio sends them itself. Load it with `t2<hex>`.

The only difference on air from the original burst is one extra preamble byte
(8 µs at 1 Mbit) in front of the second address, plus the gap below.

Selector rule for `t`: the payload always has an even number of hex digits,
so the firmware reads a leading `1` or `2` as a selector only when the
argument length is odd.
`t12AB` loads segment 1 = `12 AB`; `t112AB` is the same; `t212AB` loads
segment 2 = `12 AB`. The command line holds up to 600 characters; the
firmware ignores a longer line and prints `[?] command line too long`.

### Inter-segment gap

* **Equal lengths** (`len1 == len2`): `SHORTS = READY_START | END_START`.
  `PACKETPTR` is double buffered (latched by `START`, as stated in the nRF52840
  PS), so the firmware points it at segment 2 right after segment 1's `ADDRESS`
  event.
  The `END_START` short then starts segment 2 in hardware from `TXIDLE`, with
  no `DISABLE` and no CPU in the path. Expect a gap of 0 µs.
* **Different lengths**: `PCNF1.MAXLEN/STATLEN` must change between the
  segments. The PS does not say that `PCNF1` is double buffered, and
  `END_START` fires `START` on the same event, so there is no time to rewrite
  `PCNF1` between `END` and a short-triggered `START`. In this case the
  firmware locks all interrupts from `READY` to segment 1's `END` (about
  2.1 ms at most; the equal-length path does the same), polls `END`,
  writes `PCNF1` while the radio is in `TXIDLE` and triggers `START` from the
  CPU. Expect a gap of about 1 µs (a few CPU cycles). During that gap the
  radio is in `TXIDLE` and sends the `MODECNF0.DTX` default (carrier). Writing
  `PCNF1` in `TXIDLE` is not described in the PS; check the result with a
  receiver.

  To use the hardware path when segment 2 is shorter, append filler bytes to
  segment 2 so the lengths match. The filler goes on air after the burst.

After every `z` with two segments, the firmware prints
`[z] seg gap=<n> us`. This is the spacing between the two `ADDRESS` events
(captured by `TIMER1` through PPI) minus the nominal air time of segment 1's
payload plus segment 2's preamble and address. Resolution is 1 µs. `d` shows
the last value as `last_gap_us`.

### TX counters

`[tx] sent=<n> segs=<m> timeouts=<k>` follows every `x` and `z`:

* `sent`: frames (`x`) or complete bursts (`z`) that reached their last `END`;
* `segs`: `END` events of the run (for `x` it equals `sent`);
* `timeouts`: event waits (`READY`, `ADDRESS`, `END`) that timed out. A run
  stops at its first timeout.

`d` shows the totals since boot (`READY` and `END` events, timeouts).

## Address mapping (and the `o` / `r` toggles)

`w<hex>` takes the address bytes in the same on-air order as the ESP sniffer's
RAW view (`0B 5C 8E 03` is the constant prefix of the remote's frames). The
mapping to the nRF52 registers follows the nRF52840 Product Specification:

* the `ADDRESS` field goes out **least significant byte first**, and the base
  address is truncated from the least significant byte when `BALEN < 4`, so the
  first on-air byte belongs in `BASE0[7:0]`, the next in `BASE0[15:8]`, …;
* the last on-air byte is the prefix, i.e. `PREFIX0.AP0` (the most significant
  address byte) and `BALEN = bytes - 1`;
* address bits are always transmitted **LSBit first**, while the RAW view this
  tool mirrors presents each byte MSBit first, so every address byte is
  **bit-reversed** before it is written to `BASE0`/`PREFIX0`. This is the same
  bit swap Nordic's own ESB driver performs (`addr_conv()`).

Default: **byte order as given, bit reversal ON**. If a real signal is not
detected, `o` and `r` flip the two halves at runtime; all four combinations are
worth one attempt each while the remote is transmitting. Every change prints
the actual `BASE0`/`PREFIX0`/`BALEN` values.

Note that the nRF52 matches a *prefix* of the frame (the trigger address) and
the captured bytes start **after** that address, unlike the nRF24 RAW dump which
includes the trigger bytes. Use a 3-byte trigger address to see more of the
frame, or subtract the trigger length when comparing dumps from the two
receivers.

## Implementation notes

* `PCNF0`: `LFLEN=0`, `S0LEN=0`, `S1LEN=0`, 8-bit preamble; `PCNF1`:
  `STATLEN=MAXLEN=len`, `BALEN=alent-1`, `ENDIAN=Big`, whitening off; CRC off
  (`CRCCNF.LEN=0`); `TXPOWER=0 dBm`; `MODE=Ble_1Mbit` by default (`m`
  changes it); HFXO started before the radio is used.
* Two payload buffers are alternated: the `ADDRESS` interrupt retargets
  `PACKETPTR` (latched by `START`) at the other buffer, the `END` interrupt
  pushes the finished one into the ring. `SHORTS = READY_START |
  ADDRESS_RSSISTART | END_START | DISABLED_RSSISTOP`. `DISABLED` restarts the
  receiver so a packet with a non-matching address cannot stall the sniffer.
* `TIMER1` runs free at 1 MHz and is captured through PPI channel 0 on
  `RADIO EVENTS_ADDRESS` → `TASKS_CAPTURE[0]`; the ISR reads `CC[0]` and extends
  it to 64 bit. (`TIMER1` is unused by this board's Zephyr configuration: the
  kernel tick uses `RTC1`, and no nrfx timer/radio driver is enabled.)
* The ISR only copies into the ring; all formatting and USB output happens on
  the main thread with static buffers. No dynamic allocation anywhere.

## Scope

Own hardware, own signal. Obey local RF law; use `x` and `z` only to
replay frames you captured from your own device.

## Deviation and lead-in (`mb1`, `mb2`, `j`)

`mb1` selects `Ble_1Mbit` (1 Mbit/s, ±250 kHz deviation) and `mb2`
`Ble_2Mbit` (±500 kHz); `m1`/`m2` keep the Nordic modes (±170/±500 kHz).
Only MODE changes. `j<ms>[b0|b1|c]` makes every `z` burst start with a
carrier of that length (constant 1 by default, `b0` constant 0, `c`
centre) that runs straight into the preamble with no gap; `j0` turns it off.

## Setting the Cube's temperature (`S`)

`S<degF>` builds the remote's setpoint press in the firmware and sends it.
The range is 55..93 °F. Known-good radio settings are channel 50 and
`Ble_1Mbit`: `c50 mb1`. Both are the boot defaults. `S` does not change the
channel or the rate.

```
S74          send the 74 °F press once
S74,3        send it 3 times, 100 ms apart (n = 1..10)
O72          power OFF; 72 = the Cube's current setpoint
O72,3        the same, 3 times
T74          print addr1 seg1 addr2 seg2 for S74, send nothing
TO72         print addr1 seg1 addr2 seg2 for O72, send nothing
```

Output of `S74`:

```
[z] ch=50 addr=55555555 len1=154 len2=154 chain=END_START x1
[tx] sent=1 segs=2 timeouts=0
[z] seg gap=0 us beyond preamble+address (TIMER1, +/-1 us)
[S] 74 sent
```

If a burst times out, the last line is `[S] 74 FAILED (<k> of <n> sent)`.
`O72` prints the same lines, ending with `[O] OFF @ 72 sent`.

How it works:

1. `src/chili_encoder.h` encodes the packet
   `07 A5 CMD SP 40 40 00 X` + CRC-16/KERMIT (low byte first), behind the
   header `80 03 80 03 5A 5A 80 03`. `X = (CMD + SP - 0x80) & 0xFF`.
   `S` uses `CMD = 0x44` (`X = SP-60`); `O` uses `CMD = 0x50` (power OFF,
   for example `07 A5 50 48 40 40 00 18` at 72 °F). Power ON is a normal
   `0x44` press. Each nibble (low nibble first) becomes its
   64-bit codeword. This is a C port of `encode()` in
   `docs/analysis/scripts/decode.py` (see `docs/analysis/burst-structure.md`).
2. It splits the 2320-bit stream into two equal 154-byte nRF52 packets, as
   `docs/analysis/scripts/gen_burst2.py` does. The split is at stream bit 1053,
   where the stream itself contains an nRF preamble byte. That point is
   inside the codeword of packet byte `07`, so it is the same for every
   command and setpoint. The encoder checks the preamble pattern anyway and
   refuses to send if it is missing.
3. `S` loads the result as `w<addr1> v<addr2> t1<seg1> t2<seg2>` would
   (`addr1` = `55555555`), and sends it through the `z` path. The `o` and `r`
   settings apply as for `w`/`v`. A `j` lead-in also applies.
4. After the send, `S` puts back the previous `w` trigger address. Segments 1
   and 2 and the `v` address keep the burst; `t` and `v` clear them.

This is the same as the manual sequence that was checked on hardware:

```
c50 mb1 w<addr1> v<addr2> t1<seg1> t2<seg2> z1
```

### Check the encoder

Without hardware, on the laptop (needs `cc` and `python3`):

```sh
nrf52/tools/encoder_check.sh
```

It compiles `tools/encoder_check.c` against `src/chili_encoder.h` and compares
its output for every setpoint from 55 to 93 with `gen_burst2.py` (`set`) and
with `tools/gen_burst2_cmd.py` for command `0x44` (`set-cmd`) and `0x50`
(`off`). `gen_burst2_cmd.py` is `gen_burst2.py` with the command byte
patched in. Each line says `OK` or `MISMATCH`.

On the dongle, compare `T<degF>` with the Python output for the same
setpoint:

```sh
python3 docs/analysis/scripts/gen_burst2.py 74        # = T74
python3 nrf52/tools/gen_burst2_cmd.py 72 50      # = TO72
```
