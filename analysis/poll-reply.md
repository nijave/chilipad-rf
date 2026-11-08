# Family A: poll/reply pairs

Script: `scripts/poll_reply.py`; run it from `analysis/scripts`. The output is in
`poll-reply-output.txt`. Data: 172417 (2 events), 184432 (energy run), 184855
(dongle `632F4B` l48 + ESP `632F4BBA`, 7 events) and 185111 (dongle `54C65E97`
l24 + ESP `632F4B`, 6 events). That gives 15 events and 48 sync hits
from 3 receivers; 39 of them hold the whole header and tail.
Bit positions count from the first sync bit of packet 1, so 1 bit = 1 µs.

## Short version

* **Each event has two packets.** In the 185111 run, the dongle triggered twice on
  `54C65E97`, 231 µs apart. Both captures are the same two packets: the
  trigger is `0101010` (the end of the preamble) + `632F4B` + `1` (the first bit of
  T, which is 1 in every variant). The ESP window holds packet 1 and then packet 2 at
  bit 230/231. The energy run shows two stretches per event and nothing after them.
* Both packets have the **same layout and the same 24-bit sync `632F4B`**.
  Only T (2 bits) and W (16 bits) change. W follows from T, so each event
  carries **2 bits of information**: no counter, no setpoint, no temperature.
* **No CRC or length byte** fits. W is an affine (CRC-like) function of T's 2
  bits, but no CRC-16 polynomial gives it over any plausible span.
* RSSI: packet 1 is −58…−62 dBm and packet 2 is −50/−51 dBm in every event (185111:
  6 of 6). These are two different transmitters, both weaker than the remote's
  setpoint burst (−45). The strong −31/−37 stretches in 184432 are **not
  family A** (see Timing).
* Who sends which packet is **not established**; see the hypothesis table.

## 1. Layout (established)

```
lead-in   ~5 x '1', ~8 x '0'                    (seen before packet 2, bits -42..-28)
preamble  ~28 bits 1010...                     (-28..-1; alternation ends in a 0 that
                                                runs into the sync's leading 0)
sync      632F4B                    24 bits    0..23    constant
T         1011 1 b2 b1 0            8 bits     24..31   b2,b1 vary
C1        C1                        8 bits     32..39   constant
W         16 bits                              40..55   determined by T
4D        4D                        8 bits     56..63   constant
tail      62B8 (dongle) / 6578 (ESP) 16 bits   64..79   constant up to demod errors
end       '00', then noise                     80..81
```

| T | role | W | events |
|---|---|---|---|
| `BA` (b2 b1 = 01) | packet 1 | `BEC5` | 172417 ×2, 184855 ×3, 185111 ×1 |
| `BC` (10) | packet 1 | `AB1F` | 184855 ×4, 185111 ×5 |
| `BE` (11) | packet 2 after `BA` | `A7A9` | always |
| `B8` (00) | packet 2 after `BC` | `B273` | always |

* **Packet 2 = packet 1 with T bit `0x04` inverted** (and W to match). The reply keeps b1,
  and b2 ⊕ b1 is 1 in packet 1 and 0 in packet 2. Thus b1 is a 1-bit value that the
  reply echoes, and b2 ⊕ b1 is a direction bit.
* **W is an affine function of T:** W(BA)⊕W(BC)⊕W(BE)⊕W(B8) = 0000. A random
  16-bit field matches this 1 time in 65536. The T bit `0x02` changes W by `0CB6` and the T bit
  `0x04` by `196C` = `0CB6`≪1, with no reduction step. In closed form,
  W = `B273` ⊕ clmul(v, `0CB6`) with v = b2b1. This is how a shift-register or
  CRC-type function behaves on bits sent in air order (MSB-first in the RAW view).
* All four headers repeat bit-exactly in every instance. The dongle, the ESP and
  the 172417 capture (the remote set to 73/74 °F) agree on all 64 bits after the
  sync.
* **The tail does not carry data that we can read.** At the same moment the ESP reads `6578`
  and the dongle reads `62B8` (184855 t = 8.16 and 14.96). Every variant (`62B8 6578 6570 62B0 62F8 617A 6378 62B9 62A2`) starts
  with `0110` and follows the run pattern of `62B8` (`1,2,3,1,1,1,1,3,3`) except for
  run boundaries moved by one bit or single flipped bits. The ESP reads `6578` 8 times out of 8 for packet 1. Packets 1 and 2 show it for
  every T, and so does 172417 (73/74 °F). The first byte `62` bit-reversed is
  `0x46` = 70, but this is chance: 172417 shows the same tail at a different setpoint.
  The tail is a fixed field or the transmitter's ramp-down; its true value is unknown.
* Preamble: before packet 2, the dongle shows 4–7 ones from bit −42…−39, then 7–8 zeros
  (−35…−28), then 27–28 bits of alternation, with one phase slip about 13 bits before
  the sync in most windows. The 172417 frames give ≥ 24/28 alternating bits before
  packet 1. The windows begin in the preamble.

## 2. Timing (established)

* Sync to sync: 231 µs (12 windows) or 230 µs (6). In 185111 the dongle
  timer gives 231 µs in 6 of 6 events. The jitter does not follow T. The reply follows
  packet 1 to ±0.5 µs, so the turnaround is hardware or interrupt-timed.
* Packet 1 ends at bit 80 (82 with the `00`). Packet 2 RF starts at about 189–191, its
  preamble at about 203 and its sync at 230/231. **Silence ≈ 108 µs. End of packet 1 to the
  start of the packet 2 preamble ≈ 121 µs. End of packet 1 to the packet 2 sync ≈ 150 µs.**
* Energy-run lengths are 109 µs (packet 1) and 111/112 µs (packet 2). That is about
  28 preamble bits + 80 bits + ramp.
* **Repetition on a 100 ms grid.** Intervals between events: 184855 = 48, 31, 19,
  18, 25, 11 × 100 ms, error −59…+23 µs. 185111 = 2, 35, 58, 30, 34 × 100 ms,
  error −13…−121 µs (−13…−65 ppm). All 11 intervals fall within 0.12 ms of a whole number of
  100 ms. Each one does this by chance with p ≈ 0.002. Gaps are 0.2–5.8 s and
  irregular. The grid continues across the button press (184855: 3.36 → 8.17 s =
  48 ticks).
* 184432 energy run: the two weak pairs (−59/−51 at 3.858 s and ≤−62/−50 at
  6.458 s) are 26 × 100 ms −72 µs apart, so they are family A. The strong stretches
  (`4895992` 81 µs −35 + 95 µs −31, 224 µs apart; `7033508` 83 µs −37) are off
  the grid by 38 ms and 25 ms. They are 25–30 µs shorter, and their spacing is 224 µs, not
  231 µs, so they are **another transmitter**. Their shape (80 µs, then a reply
  ≈150 µs later) suggests a BLE connection event near the radios. This is not tested.
  Thus the RSSI puzzle "stronger than the remote" does not apply to family A.

## 3. CRC and length (negative)

| test | result |
|---|---|
| W = CRC-16 over [T C1] (all 65535 polys, refin/refout, byte swap; XOR differences, so any init/xorout and any constant bytes in front) | 0 polys |
| W = CRC-16 over [T] | 2 polys (`065B`, `FB92`), not standard; chance alone gives about 1 |
| 8/16-bit standard CRCs (11 + 15 polys, init 0/all-ones, xorout 0/all-ones, both reflections) over every byte span from the sync, compared with the next 8/16 bits | 0 hits |
| 4D as CRC-8 over [T C1 W] that stays constant | 2/255 polys (chance level) |
| tail as CRC-16 over [T C1 W 4D] or [C1 W 4D] that stays constant | 2 and 1 of 65535 (chance) |
| tail as a standard CRC-16 over the sync bytes or `4D` | 0 hits |
| length byte | none. C1 = `C1` and 4D = `4D` are constant, T's low bits are 24–30, and 2–6 bytes follow |

Only T and W vary, so a CRC test has 3 independent differences at most. Nothing
standard fits, and the data cannot pin down a custom polynomial.

## 4. Comparison with the setpoint burst (negative)

* The remote's setpoint burst carries `07 A5 44 SP 40 40 00 SP-60 CRC`, spread
  with codewords. Family A carries none of these bytes. The only 70 °F match (`0x46`,
  bit-reversed) is the tail byte `62`, which is the same at 73/74 °F in 172417.
* Nothing changes with the press: before and after it, both packets keep the same 64 header bits
  and the same tail (185111: 0.46 → 16.36 s). No field reads as °F or °C, raw,
  nibble-swapped or bit-reversed. The only variable is b1 (with W).
* b1 sequence: 184855 `0 1 1 0 1 0 0`, 185111 `0 0 0 0 1 0`, 172417 `1 1`. It does not
  alternate, so b1 is no ESB-style packet ID. b1 = 1 shows up only after a press, never
  before one, in all three runs. That is weak evidence: 6 events with b1 = 1 in total.
* Possible link to the remote's radio: the remote's burst header `80 03 80 03 5A 5A 80 03`
  is the LT8920 default sync word. The 64 bits after the family A preamble split into
  four 16-bit words (`632F 4BBA C1BE C54D`), like the LT8920's 4×16-bit
  sync registers. One reading: T+W select one of four sync words. Treat this as a
  hypothesis only.

## 5. Who sends what (hypothesis)

Established: two transmitters at steady, different levels. 185111 (devices
stationary, 16 s): packet 1 −60/−61 and packet 2 −50/−51 every time, for both T types.
Packet 2 follows packet 1 within ±0.5 µs. The first sender keeps a 100 ms tick and
polls every 0.2–5.8 s while the remote's display is on.

| hypothesis | for | against |
|---|---|---|
| **packet 1 = remote (poll), packet 2 = Cube (reply)** | In 172417, packet 1 = −45, the same as that run's remote bursts (remote at normal distance). With the radios near the Cube, the Cube is the stronger of the two (−50 > −60). Polling while the display is on fits a remote that shows the Cube's state. The reply echoes b1 | In 184432 the remote's burst is −45, while packet 1 is −59 at 0.6 s before and ≤−62 at 2.0 s after the burst (the remote may have moved). In 185111, packet 1 at 4.16 s (around the press) is still −60. −50 is weak for a Cube "next to" the radios |
| **packet 1 = Cube, packet 2 = remote** | Packet 2 (−50) is closer to the remote's burst (−45) than packet 1 (−60) | 172417 packet 1 = −45 = the remote's level. It needs a Cube that polls every few seconds while the remote's display is on. Packet 1 is weaker than packet 2 even though the Cube sits by the radios |
| **a third device** (e.g. a second unit) | – | Both packets use the same sync and fields, and timing depends on presses or the display |

Purpose (hypothesis): a short presence/status exchange. It carries 2 bits (a direction bit plus an echoed
flag b1) and no setpoint or temperature. It looks like a keep-alive or
"anything new?" poll. b1 = 1 may flag a recent change. Pairing is unlikely:
the IDs never change and it runs during normal use. If the remote shows the Cube's
temperature, the data does not travel in this exchange.

## Next capture

1. **Dongle `w54C65E97 l24`** (gives the RSSI of both packets), radios fixed. Keep the remote
   awake: (1) remote 0.3 m from the radios for 20 s, then (2) remote 3 m away for 20 s, then
   (3) **Cube unplugged**, remote awake. The packet whose RSSI tracks the
   remote is the remote's. If packet 2 disappears in step 3 and packet 1 continues
   (maybe more often, or as retries), packet 1 = remote and packet 2 = Cube.
2. A 90 s run with one press at 5 s, to check whether the polls stop when the
   remote's display goes off (~30 s), and whether b1 follows the press.
3. ESP on `632F4B` with the RSSI of packet 2 alone (`AAAAAA` + `B8C1`/`BEC1` trigger), and
   with setpoint and power changes (Cube off, F/C toggle), to see whether T/b1 or the tail
   ever change.

## 6. First HackRF capture (20261107-194951)

A 3 s smoke test from a laptop (HackRF Pro, 2446 MHz centre, 20 MS/s, LNA
24 / VGA 20, amp off). The laptop sat away from the Cube, and nobody touched
the remote. `iq_demod.py` found one family A pair at t = 2.345 s; the rest of
the 256 bursts are weak unknowns (Wi-Fi/BLE). Output:
`captures/20261107-194951-hackrf/demod.txt`.

| packet | T C1 W 4D tail | power | CFO | deviation |
|---|---|---|---|---|
| 1 | `BC C1 AB1F 4D 6778` | −88.0 dBFS | +39.8 kHz (+16.2 ppm) | 222 kHz |
| 2 | `B8 C1 B273 4D 67B9` | −81.9 dBFS | −0.8 kHz (−0.3 ppm) | 215 kHz |

* Both packets decode with 0 errors on the sync, in the same polarity
  (higher frequency = `1`). T and W match the table in section 1.
* The packets start 229 µs apart (231 µs in the dongle data).
* **The two carrier offsets differ by 40.6 kHz (16.6 ppm).** This supports the
  two-transmitter reading with a measurement that does not depend on distance.
  A remote setpoint burst in the same capture will show which offset is the
  remote's (`hackrf-plan.md`, experiment b).
* Packet 2 is again the stronger one (6 dB), as in every earlier capture.
* The tails read `6778` and `67B9` from clean IQ, which differ from the
  dongle's `62B8` and the ESP's `6578`. One capture is not enough to fix the
  tail value.
* Deviation is about ±220 kHz, so the synthetic tests' 250 kHz is close.
