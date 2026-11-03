# chilipad-sniffer — nRF52840 Dongle raw 2.4 GHz sniffer

Second receiver next to the ESP8266 + nRF24L01+ promiscuous sniffer in
`../src/main.cpp`. It turns a Nordic **nRF52840 Dongle (PCA10059)** into a raw
2.4 GHz receiver for the proprietary LT8920-based remote (1 Mbps GFSK,
2450 MHz) using nothing but the `NRF_RADIO` peripheral: no Bluetooth stack, no
ESB library, no SoftDevice. It talks to the laptop over the dongle's USB CDC ACM
serial port with the same line-based command style and the same `P <t> ... RAW
<hex> | REV <hex>` packet format the ESP firmware uses, so the existing GUI can
read either receiver.

## Build

Zephyr 4.4.2 + Zephyr SDK on the build host (there is no Zephyr on the laptop
this repo lives on). From the repo root:

```sh
rsync -a --delete nrf52/ user@buildhost:chilipad-rf/nrf52-src/
ssh user@buildhost 'cd ~/zephyrproject && . .venv/bin/activate && \
  west build -p always -b nrf52840dongle/nrf52840 -d ~/chilipad-rf/nrf52-build ~/chilipad-rf/nrf52-src'
```

No snippet flag is needed: `nrf52840dongle` defaults to the CDC ACM serial
backend (`BOARD_SERIAL_BACKEND_CDC_ACM`), which enables the Zephyr 4.4 USB
device stack next, the CDC ACM UART and the console. `prj.conf` spells those
options out so a board defconfig change cannot silently drop the console.

The stock `nrf52840dongle/nrf52840` target already links the image at
**0x1000** (`CONFIG_FLASH_LOAD_OFFSET`), which is what the Nordic open USB DFU
bootloader expects. Use that target, not the `.../bare` variant.

## Flash over the Nordic open USB DFU bootloader

```sh
nrfutil nrf5sdk-tools pkg generate --hw-version 52 --sd-req=0x00 \
    --application ~/chilipad-rf/nrf52-build/zephyr/zephyr.hex \
    --application-version 1 app.zip

# press the dongle's sideways RESET button (red LED pulses = bootloader), then:
nrfutil nrf5sdk-tools dfu usb-serial -pkg app.zip -p /dev/ttyACM<n>
```

Then open the application's own port (a different `/dev/ttyACM<n>`) and talk to
it with any serial terminal, 115200 baud (the CDC ACM baud rate is ignored).

## Commands

| Command | Action |
|---------|--------|
| `h` | help + current config (channel, length, address, byte order, bit reversal, BALEN, `BASE0`/`PREFIX0` as written, HFXO, capture state, packet/drop counts) |
| `p` | start/stop capture. **Capture starts OFF at boot.** |
| `c<n>` | channel 0..100 (default 50 = 2450 MHz) |
| `l<n>` | packet length 1..255 (default 64) |
| `w<hex>` | trigger address, 3..5 bytes in on-air order, e.g. `w0B5C8E03`. `w` alone resets the address to `0B5C8E03` |
| `o` | flip the address byte order (as given ↔ reversed); re-applies the address, keeps capturing |
| `r` | flip per-byte bit reversal (on by default); re-applies the address, keeps capturing |
| `s<sec>` | energy scan of ch 0..100 for `<sec>` seconds (default 2, max 60), then the top 10 channels by number of RSSI samples above -75 dBm; restores the previous channel and capture state |
| `y<list>@<ms>` | hop the receiver through a channel list, `<ms>` dwell per channel, e.g. `y10,30,50,70@200`. `y` alone stops hopping and returns to the fixed channel |
| `x<n>` | transmit the last captured buffer `n` times (default 10, 5 ms apart) on the channel it was received on, same address/length/no CRC; then restore the previous state |

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
  (`CRCCNF.LEN=0`); `TXPOWER=0 dBm`; `MODE=Nrf_1Mbit`; HFXO started before the
  radio is used.
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

Own hardware, own signal. Obey local RF law; transmitting (`x`) is limited to
replaying a frame you captured.
