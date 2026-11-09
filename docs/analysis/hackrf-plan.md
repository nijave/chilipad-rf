# HackRF capture plan for the ChiliPad Cube

Goal: record raw IQ around 2450 MHz and demodulate it offline with
`scripts/iq_demod.py`. The demodulator has a synthetic test suite
(`scripts/iq_synth.py`, `scripts/test_iq.py`), so it works before the first
real capture. Run all commands from `docs/analysis/scripts/`.

Open questions this plan targets:

* a. What family B carries (`unknown-frames.md`). Each frame runs longer
  than any nRF52 or ESP window, so nobody has seen a whole frame yet.
* b. Who sends each family A packet (`poll-reply.md` section 5).
* c. Whether anything uses a channel other than 2450 MHz.
* d. Whether the dongle's burst matches the remote's on air.

Status 2026-11-07: experiment b showed that families A and B both continue
with the Cube unplugged, at carrier offsets far from the remote's, so neither
comes from the ChiliPad (`poll-reply.md` section 7). Questions a and b no
longer concern the Cube; c and d remain.

On-screen steps: `capture_timer.py` shows the elapsed capture time and the
current step full screen on the capture laptop. Start it before the capture
with `--meta-dir ../../captures` and it counts from the capture's
`start_epoch_ns`. Over SSH, set `DISPLAY=:0` and `XAUTHORITY` to the X
server's `-auth` file (`ps -o args= -C Xorg` shows it).

## Setup

```sh
sudo dnf install hackrf
hackrf_info                       # board, serial, firmware, supported rates
python3 test_iq.py                # demodulator self-test, no hardware
```

Antenna: use one rated for 2.4 GHz. The ANT500 covers 75 MHz to 1 GHz only.

Tuning: the HackRF has a DC spike at the tuned frequency.
`hackrf_capture.sh` tunes to 2446 MHz by default, so the 2450 MHz channel
sits 4 MHz up, clear of the spike. The script writes `meta.txt` beside
`iq.cs8`, and `iq_demod.py` reads the centre frequency and sample rate from
it, so `--center` and `--rate` stay off the command line. The demodulator
prints a warning when a channel overlaps the spike.

Gain: start with the amp off (`-a 0`), LNA 24 dB and VGA 20 dB, and the
radio 1 to 2 m from the Cube and the remote. Make a 2 s test capture with
one remote press:

```sh
./hackrf_capture.sh -n 2 -m "gain check"
python3 iq_demod.py ../../captures/<stamp>-hackrf/iq.cs8
```

The press must show as a `remote` line with `err=0`. Keep burst power below
about −6 dBFS: lower `-g` if bursts come out louder, raise it if `iq_demod`
finds no bursts.

Disk: `hackrf_transfer -r` writes 2 bytes per sample, 40 MB/s at 20 MS/s
(2.4 GB per minute). Check `df` before long runs and always give `-n
<seconds>`. Git ignores `docs/captures/*/*.cs8`; `meta.txt` stays committable.

The HackRF only receives in this plan. The bridge on the bridge host keeps
the nRF52
dongle in sniff mode and transmits when Home Assistant sends a command, so
any capture can include bridge commands.

Note the wall-clock time of each action. `meta.txt` holds the start time in
epoch nanoseconds, and `iq_demod` times count from the file start.

## a. Family B content

Question: does family B carry the setpoint, the on/off state or the water
temperature? Compare payload bits across states: bytes that change with the
state are data.

```sh
./hackrf_capture.sh -n 240 -m "famB: idle, presses, cube buttons, bridge"
```

During the capture:

1. 30 s with no input (Cube on, remote idle).
2. Five setpoint UP presses on the remote, 5 s apart, then five DOWN.
3. The remote's OFF button, wait 20 s, then a setpoint press to turn it on.
4. The Cube's own buttons: setpoint up and down on the unit. A frame that
   changes here but never with remote presses comes from the Cube.
5. Two setpoints from Home Assistant through the bridge, 15 s apart.

Analysis:

```sh
cap=../../captures/<stamp>-hackrf
python3 iq_demod.py $cap/iq.cs8 --jsonl $cap/demod.jsonl --bits-dir $cap/bits
```

Take the `famB` records from `demod.jsonl`, align their `bits` at `bit_off`
and diff them by state. A field that steps with the setpoint, flips with
on/off, or drifts slowly like a temperature answers the question. If the
payload never changes, family B is a beacon, or a scrambler hides the data.
In that case try the LT8920 and nRF whitening polynomials next.

## b. Family A sender by carrier offset

Question: which family A packet does the remote send, and which the Cube?
RSSI changes when someone moves the remote, but the carrier frequency offset
(CFO) depends on each transmitter's crystal. `iq_demod` reports CFO per
burst in Hz and ppm. A setpoint burst gives the remote's CFO.

```sh
./hackrf_capture.sh -n 180 -m "famA cfo: presses, polls, cube unplugged"
```

During the capture:

1. Three setpoint presses on the remote, 5 s apart, then keep its display on
   (touch a button that does not change the setpoint, or press the same
   setpoint again) so the polls continue.
2. 60 s with the display on.
3. Unplug the Cube and keep the remote awake for 30 s more.

Analysis:

```sh
python3 iq_demod.py ../../captures/<stamp>-hackrf/iq.cs8
```

Each family A pair prints as two `famA` lines with `gap=231`. Compare each
packet's `cfo=` with the CFO of the `remote` lines in the same capture. The
packet whose CFO matches the remote's comes from the remote. If the other
packet stops after the Cube loses power, the Cube sends it. Compare CFO
only inside one capture: it drifts with temperature by a few ppm.

## c. Channel survey

Question: do the remote or the Cube use channels other than 2450 MHz?

Coarse pass with `hackrf_sweep`, once with presses and once idle as a
background (Wi-Fi and BLE show up in both):

```sh
hackrf_sweep -f 2400:2484 -w 500000 > ../../captures/sweep-presses.csv
# one setpoint press every 2 s for 30 s, then Ctrl-C
hackrf_sweep -f 2400:2484 -w 500000 > ../../captures/sweep-idle.csv
```

Fine pass: one capture per band segment, scanned on every 1 MHz channel in
it. At 20 MS/s the usable span is about ±8.8 MHz around the tuned frequency.
If `hackrf_info` or the HackRF Pro documentation confirms a higher sample
rate, pass it with `-s` and widen the `--freq` list to match.

```sh
./hackrf_capture.sh -n 60 -f 2440000000 -m "survey 2432-2448, presses"
python3 iq_demod.py ../../captures/<stamp>-hackrf/iq.cs8 \
    $(for f in $(seq 2432 2438) $(seq 2442 2448); do printf -- '--freq %se6 ' $f; done)
```

Repeat with `-f 2460000000` for 2452 to 2468 MHz. The default 2446 MHz
capture already covers 2450 MHz. A burst also shows on the neighbouring
1 MHz channels, because the channel filter is 2.4 MHz wide; the line with
the highest `pwr=` and `err=0` gives the real channel. Only bursts that line up with presses
matter. If presses produce bursts on 2450 MHz alone, the system uses one
channel.

## d. Dongle burst versus remote burst

Question: does the dongle's burst match the remote's in deviation, CFO,
timing and power?

```sh
./hackrf_capture.sh -n 60 -m "dongle vs remote: bridge set then press"
```

During the capture: wait 10 s, set 72 °F from Home Assistant (the bridge
sends it), wait 10 s, press 72 °F on the remote, then wait until the end.

Analysis:

```sh
python3 iq_demod.py ../../captures/<stamp>-hackrf/iq.cs8 \
    --jsonl ../../captures/<stamp>-hackrf/d.jsonl
```

Both events print `remote` lines because the dongle sends the same sync.
Compare `dev=`, `cfo=`, `dur=` and `pwr=` between them, and diff the bits.
Matching bits with a different deviation or duration mean the dongle's
radio settings differ from the LT8920's. That matters only if the Cube ever
misses a bridge command.
