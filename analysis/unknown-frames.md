# Strong frames that are not setpoint bursts

Scripts: `scripts/unknown_frames.py` (per-frame tests, output in
`unknown-frames-output.txt`) and `scripts/unknown_search.py` (signature search over
every nRF and ESP log, output in `unknown-search-output.txt`). Run them from
`analysis/scripts`. Bit positions count from the first bit of the 3 trigger bytes,
so the RAW hex starts at bit 24.

## Short version

* **Update 2026-11-07 (HackRF):** families A and B both continue with the Cube
  unplugged, at carrier offsets about 25–30 kHz away from the remote's. Neither
  family comes from the ChiliPad; see `poll-reply.md` section 7. Family B
  payload bits come in runs of three (`000`/`111`), so its data rate is
  probably 333 kbit/s.

* Families A and B are **two different things**. They share no structure at any
  bit offset, inverted or reversed.
* Both have a clean alternating preamble of at least 24 bits at **1 µs per bit**
  and a fixed header after it, and their run lengths look like 1 Mbit data. They
  are **not** spread with the remote's codebook, not with any other 16-word
  32- or 64-chip codebook, and not Manchester. No sign of whitening.
* **A** (−45 dBm) is **two short packets 230–231 µs apart**, an exchange with
  about 140 µs turnaround. Packet 1 is about 80 bits after the preamble. Packet 2
  is about 80–90 bits. Both start with the same 29 bits (`632F4BB…`).
* **B** (−63 dBm) is **one long packet**, longer than the 2.04 ms window. It has a
  fixed 81-bit header (`03486457DA26DDCA871C…`) and a DC-heavy payload.
* The odd strong frames in 20261103-202434 are **the remote's own header,
  received off-frequency**. 202852 and 205157 used the `9388B0CA` trigger, so they
  show only setpoint bursts, as expected.
* Best guess (updated 2026-11-07): families A and B come from another 2.4 GHz
  device near the Cube, not from the remote or the Cube (see
  `poll-reply.md` section 7). Family B's data rate is probably 333 kbit/s.

## Timeline of 20261104-172417 (all on 2450 MHz)

| t (s) | RSSI | what | relation to presses |
|---|---|---|---|
| 7.982 | −45 | setpoint burst | first press |
| 13.745 | −64 | C13, unknown, long | 2.1 s before UP |
| 15.832 | −45 | setpoint burst | UP |
| 16.457 | −45 | **A1** | UP + 0.625 s |
| 18.556 | −45 | **A2** | in the failed-DOWN window (18–23 s) |
| 22.714 | −63 | **B1** | in the failed-DOWN window |
| 25.713 | −45 | setpoint burst | DOWN |
| 26.076 | −63 | **B2** | DOWN + 0.363 s |
| 39.673 | −80 | family D (see below) | – |
| 57.719 | −55 | C57, unknown | – |

## Q1. Same content or different? (established)

* A1 and A2 match exactly for 82 bits after the preamble. A2's RAW starts with one
  extra `A`: its preamble is at least 4 bits longer, so the trigger matched 4 bits
  earlier. This is not a nibble of data.
* B1 and B2 match exactly for 81 bits after the trigger, at the same offset.
* A against B: the longest exact common run at any offset is 23–30 bits, plain,
  inverted, reversed or both. Chance for two random 2064-bit strings is 19–23 bits
  per comparison, and about 26 as the best of the 60 comparisons. The best
  match of the 80-bit A header anywhere in B is 25/80 bits wrong. Chance is 23–26.
  The same holds for C13 and C57: they share nothing with A, B or each other.

## Q2. Symbol rate (established: 1 Mbit-compatible, not 250k or 500k)

Run lengths at 1 Mbit sampling (interior runs):

| region | runs | mean | len 1 | len 2 | len 3 | len 4 | longest | share with len%4==0 |
|---|---|---|---|---|---|---|---|---|
| setpoint burst (reference) | 1025 | 2.01 | 49% | 26% | 14% | 4% | 9 | 4% |
| A1 bits 24–520 | 237 | 2.07 | 56% | 21% | 8% | 3% | 12 | 4% |
| A2 bits 28–520 | 237 | 2.05 | 52% | 24% | 12% | 3% | 11 | 5% |
| B1 | 833 | 2.44 | 38% | 27% | 16% | 9% | 18 | 10% |
| B2 | 881 | 2.31 | 45% | 24% | 13% | 7% | 17 | 7% |
| random bits (geometric) | | 2.00 | 50% | 25% | 12.5% | 6% | | 7% |

* A matches the 1 Mbit codeword burst and random data. A 500 kbit signal could not
  give 1-bit runs, and a 250 kbit signal would give runs in steps of 4. Neither fits.
* B still has 38–45% 1-bit runs, so B also runs at 1 Mbit or faster. Its long runs (up to 19)
  and lag-1 autocorrelation of 0.57–0.59 point to DC-heavy payload content, not to a
  lower rate.
* A 2 Mbit signal is unlikely for either. Both have a clean 1 µs alternating
  preamble of 24 bits or more, and two separate receptions give the same 81–82
  bits. Aliased 2 Mbit data would not repeat like that.

## Q3. Coding tests (all negative)

| test | A | B | note |
|---|---|---|---|
| remote codebook, any phase, plain/inverted/reversed | best 15–18/63 wrong | 14–16/63 | chance from random bits: 12–17 |
| same after decimation by 2, 3 or 4 | 14–15 | 14–17 | chance |
| remote codebook with frequency-offset (one-sided) errors | 0–1 of 31 blocks | 0 of 31 | the 202434 frames give 6–7 of 7 |
| repeated 64-bit blocks (≤3 errors), any phase | 0 | 0 | the setpoint burst: 1984 |
| repeated 32-bit blocks (≤1 error) | 12, all from the A packet-1/packet-2 prefix | 0 | |
| autocorrelation at lag 16/32/64 | 0.51–0.58 | 0.49–0.52 | the only peak is A at lag 230/231 (the second packet) |
| Manchester (01/10 pairs at 1 or 2 bits per chip) | 0.34–0.52 | 0.40–0.53 | Manchester gives about 1.0 |
| lowest linear complexity, 48-bit windows | 20–21 | 20–21 | whitened constant data (BLE/LT8920 7-bit, PN9) gives ≤9 |
| BLE ch 23 (2450 MHz) dewhitened header | LLID = 00 (reserved) | LLID = 00 | no empty-PDU header for any of the 64 channel seeds |
| CRC-16 (`0x1021`/`0x8005`, init 0/FFFF, xorout 0/FFFF, MSB/reversed), every start and end | 0–5 hits per packet (3–4 expected by chance) | 1–7 (4.4 expected) | none falls on a packet end in both frames |

A 16-word, 64-chip code over a 2 ms frame must repeat blocks (31 symbols from 16
words). The same holds for 32 chips. B has 0 repeats at both lengths, so B is not
any 16-word block code of those lengths. A is too short for this argument.

## Q4. Where each frame ends

Signal is about 50% ones. When a strong packet stops, the dongle output turns
0-biased (P(1) ≈ 0.3), as after the setpoint burst. Idle weak lines stay balanced.
Change points come from a likelihood step fit (`sec_end`).

| frame | start of sync | end | length after preamble | confidence |
|---|---|---|---|---|
| A1 packet 1 | 24 | ≈101 (P(1) 0.55→0.35). A1 and A2 match up to 106 | ≈ 77–82 µs | good |
| A1 packet 2 | 255 (TX ramp from ≈213, preamble ≈236) | ≈346 | ≈ 91 µs | fair |
| A2 packet 1 | 28 | content identical to A1 up to 110. After that the bits stay balanced up to the ramp | ≥ 82 µs | weak |
| A2 packet 2 | 258 (preamble from ≈236, 20 clean bits) | ≈335 | ≈ 77 µs | fair |
| B1, B2 | 24 | no end in the window | **> 2040 µs** | good |
| C13 | 24 | weak step at ≈1938 | ≈ 1.9 ms? | weak |
| C57 | 24 | balanced to ≈260, then 0-biased | ≈ 0.25 ms? | weak |

Gap in A: from the end of packet 1 (≈101) to the start of the packet-2 preamble
(≈236–242) is 135–140 µs. The ramp starts at ≈213 (≈110 µs). The spacing between
the two syncs is 231 µs in A1 and 230 µs in A2: 1 µs of jitter.

## Inside family A (established bits, guessed meaning)

```
p1  632F4BBA C1BEC54D 62B8 1220…    (A1 and A2 identical for 82 bits)
p2  632F4BBE C1A7A94D 6570 3760…    (A1 and A2 identical for 69 bits)
    ^^^^^^^ 29 equal bits, then 1 bit differs, C1 equal, 2 bytes differ, 4D equal
```

* The 29-bit common start is a sync/address. It cannot be a 32-bit sync shared by
  both directions, because bit 29 differs, and it differs the same way in both
  captures. One reading (a guess): a 24-bit sync `632F4B`, then a flags byte
  `BA`/`BE` with one direction or ACK bit, then an ID or data field.
* After the fixed part, A1 and A2 differ (A1 p1 has the 0-bias that marks the end,
  A2 does not). The payload or state changed between the two exchanges.
* No ESB/nRF24 reading fits: the 9-bit PCF would give lengths 46/47, and the
  addresses would have to match. BLE fails on LLID and on the preamble length
  (BLE has 8 bits; here there are at least 24–28 before p1 and 13–20 before p2).

## The other captures

20261103-202434 (AAAAAA for 0–20 s, then 555555):

* `AAAAAAA000000030020808…` (t = 1.753, −46) and `…0600410100238…` (t = 9.823, −39)
  are the remote's header `S B5 A4 S S B5 A4`. The test allows only 1→0 errors (a
  slicer pulled toward 0 by a large frequency offset): all 7 blocks decode with 0
  violations, and the next-best codeword has 2–4. Only long runs of 1s survive.
* t = 21.051 (−54): `…FFFD555555 BCCF8ED6…` has the same carrier and 555555 lead-in
  as a remote burst, then the word sequence X, Y, Z, X, X, Y, Z. These are the true codewords S, B5
  and A4, shifted by 2 bits. Every one of the 8–11 bit errors per word is a 0→1
  just before a 1: here the offset pulls the slicer the other way. This is the
  same remote header, received off-frequency.
* t = 5.94 and 8.15: `4E22C32B…` is a normal burst with a 2-bit-longer preamble.
* The remote thus sometimes appears strong but off-centre at 2450 MHz. Whether it
  hops channels or drifts is not known. These frames are **not** families A or B.
* Family D, weak (−70 to −73 dBm, 8 frames): after the preamble, `AF5567D7` and
  then one of two variants, `5834519CF6…` or `9836EADCF6…`. Frames come 0.43–0.93 s
  apart in places, with no clear link to presses. Family D also shows up in 172417 at t =
  39.673 (−80, 2–3 bit errors). Probably a distant, unrelated device or a far
  Cube; not A or B.
* Neither A nor B appears in 202434. That capture ran 40 s with the same
  alternating triggers and about 40 presses.

20261103-202852 and 20261103-205157 use the `9388B0CA` trigger, so every window
starts inside a codeword burst. They show nothing else; that trigger hides A and B.

`unknown_search.py`: across all nRF and ESP logs, the A, B, C13 and C57 signatures
occur only in 172417.

## Q5. Interpretation

Historical: disproved 2026-11-07, families A and B come from another device
(see `poll-reply.md` section 7).

| hypothesis | for | against |
|---|---|---|
| **A = remote query + Cube reply** (p1 remote, p2 Cube) | RSSI −45 equals the remote's. 0.625 s after UP, and inside the failed-DOWN window. Two packets with ≈140 µs turnaround, typical TX→RX→TX for nRF24/LT89xx-class radios. Same sync prefix with a 1-bit difference, so one network in two directions. p2 is not a resend of p1 | Not one of ≈40 presses in 202434 produced A. A uses unspread raw 1 Mbit framing, unlike the setpoint bursts. The dongle logs RSSI only for p1, so p2's source is unknown |
| **A = Cube talking first** | The Cube may send a status or "wake" exchange | p1 at −45 is as strong as the remote, while the Cube-like frames (B, C13) are at −63/−64 |
| **A = the remote at another data rate** | Same RSSI as the remote | Runs are plain 1 Mbit. The codebook fails at any phase, rate, inversion or with offset errors. The remote's off-frequency frames look nothing like A |
| **A = unrelated (phone BLE, other 2.4 GHz gear)** | A phone in hand would also be −45 | BLE LLID/empty-PDU test fails and the preamble is too long. Seen only near presses. The two A frames are 2.1 s apart, both within 3 s of user activity |
| **B = Cube transmission** (status/ACK/broadcast) | −63/−64 is a farther source, like C13. B2 is 0.363 s after DOWN. B1 is inside the failed-DOWN window. Fixed 81-bit header, so a fixed sender | No B after UP or the first press. None in 202434 across ≈40 presses. Not linked to the codebook layer. Over 2 ms of payload is long for an ACK |
| **B = remote query or remote at another rate** | – | RSSI is 18 dB below the remote's frames from the same position. Not codeword-based |
| **B = unrelated** (streaming device, neighbour) | Long frames over 2 ms suit streaming. Only 2 frames in 60 s | Fixed header in both frames, and both fall near presses |

Best guess: A is the strongest candidate for a two-way remote↔Cube exchange.
It may belong to waking from idle, which only the 172417 session did. B is more
likely the Cube or a nearby device. RSSI from a radio placed beside the Cube will
decide who sends what.

## Recommended next capture

Historical: disproved 2026-11-07, families A and B come from another device
(see `poll-reply.md` section 7).

1. **Dongle: 1 Mbit, channel 50 (2450 MHz), `l255`, trigger `632F4BBA`** (4 bytes,
   the bits right after A's preamble). The window then holds A packet 1 and
   packet 2 (packet 2's sync is 231 bits later), from the sync on. If the 4-byte
   trigger misses, use `632F4B`.
2. **ESP (nRF24): channel 50, 1 Mbit, trigger `03486457`** (B header, try both
   register orders). Or run the dongle with this trigger in a second session: B
   is longer than one 255-byte window, so also use a second dongle session with
   a trigger later in B once you know its first windows.
3. Repeat the 172417 script: idle for 1–2 min, then a wake press, UP and DOWN,
   with some fast presses so that one fails. Run it twice, once with the radios next
   to the remote and once next to the Cube with the remote 2 m or more away. The
   RSSI change of A p1, A p2 (the trigger gives a per-packet RSSI only for p1; use
   `5555`/`AAAA` + `632F4BBE` to trigger on p2 alone) and B shows which device sends
   each one.
