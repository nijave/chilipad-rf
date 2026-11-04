# ChiliPad Cube remote: structure of one press burst

All offsets count from the first bit of the first `9388B0CA` (the dongle
trigger). A byte offset `x.5` means the block starts half a byte late in the
RAW (MSB-first) byte view. Scripts are in `analysis/scripts/`. Run
`python3 decode.py` to rebuild everything in `analysis/bursts/`. Its log is
`analysis/decode-output.txt`.

## Short version

A press is a small LT8920-style packet. Each 4-bit nibble of the packet goes
on air as one of 16 fixed 64-bit codewords, 64 chips per nibble.

```
carrier (>=52 us of '1') | preamble 3 bytes 0x55 (24 bits)
| 16 codewords: header 80 03 80 03 5A 5A 80 03      (bytes 0 .. 128)
| 4 raw bits "1010"  (trailer, not spread)           (128 .. 128.5)
| 20 codewords: 07 | A5 44 SP 40 40 00 SP-60 | CRC   (128.5 .. 288.5)
| 14 bits "0010011 0000000", then the carrier stops
```

* `SP` is the setpoint in °F. `SP-60` is a signed byte. Both bytes go low
  nibble first.
* CRC = CRC-16/KERMIT (poly `0x1021`, init `0x0000`, reflected in and out,
  xorout 0) over `07 … SP-60`. The low byte goes first.
* Every press has the same length, whatever the setpoint.

## Established (reproduced bit-for-bit) vs. hypothesis

Established. The encoder in `decode.py` regenerates every capture:

* the stitched 74 °F and 75 °F bursts, all 2323 bits that are the same in
  every tail capture;
* all six 255-byte head windows (75, 76, 77, 76, 75, 74 °F);
* 1748 blocks and end markers from the 20261103-203934 sweep (dongle windows
  plus ESP frame 4), with 0 mismatches;
* every press in 202852, 203934, 205157, 162759, 163310 and 163708. Each press
  matches exactly one setpoint with 0–15 bit errors. The errors are RSSI noise
  (`analysis/identify-output.txt`). 162759 alternates 74 °F and 73 °F.

The CRC model fits 8 presses with all 4 CRC nibbles seen (57, 58, 60, 61, 64,
74, 75, 87 °F) and the first CRC byte of 29 more (`crc_search.py`). A search
over all 65536 polynomials, both reflections and both byte orders finds only
this model.

Hypothesis or unknown:

* What the constant bytes `A5 44 … 40 40 00` and the header mean. They may be
  a pairing ID, a command or a mode. Other buttons were not decoded.
* The role of the header (preamble or sync). The receiver logic is unknown.
* Whether the Cube needs the full 3-byte preamble and the carrier lead-in.
* 76 °F and 77 °F after byte 259 (the last 3.5 codewords, CRC byte 2) are
  predicted from the CRC model, not captured. The sweep captured CRC byte 1 of
  both, and it matches.
* The 14 trailing bits are the first 7 chips of a nibble-0 codeword, then
  zeros. They may be a TX ramp-down artifact. They are in every capture, so the
  files keep the first 11 of them.

## Bit framing: why `1388B0CA` vs `9388B0CA`

The true codeword boundary is **1 bit after** the `9388B0CA` byte grid. Bit 0
(the leading `1` of `0x93`) is the last preamble bit. Evidence: with a `555555`
trigger (capture 20261103-202434), RAW starts `27116195A033E5CD…`, which is
`9388B0CAD019F2E6` shifted left by 1. One weak capture shows
`…FFFFFFFFFFFFF555555 27116195…`: carrier, 24 preamble bits, then the first
codeword.

As a result, in the `9388B0CA` view, the first bit of each block is the **last bit of
the block before it**. Four codewords end in 0: A4 (nibble 3), CC (7), C5 (B)
and A2 (2). Whatever follows one of these shows its first nibble with the top
bit cleared:

* `9388…` becomes `1388…`
* `A7E0…` becomes `27E0…`
* `8A27…` becomes `0A27…`
* `CC18…` becomes `4C18…`
* `C4F3…` becomes `44F3…`
* `F020…` becomes `7020…`

The rule holds for all 69 S-followers and every other pair seen. The bit
carries no data. The "nibble shift" after `D4C18FE2A9564525` is the 4-bit raw trailer at
byte 128 (`1101` in the RAW view, `1010` in the true framing). After it, every
block sits at an `x.5` offset.

## Codebook (task 3)

| nibble | RAW-view block (first bit 1) | true codeword |
|---|---|---|
| 0 | `9388B0CAD019F2E6` ("sync") | `27116195A033E5CD` |
| 1 | `C4F30304A5ECD55C` | `89E606094BD9AAB9` |
| 2 | `A2E1F402A42EF39A` | `45C3E805485DE734` |
| 3 | `A4717A73025C4516` | `48E2F4E604B88A2C` |
| 4 | `A7E03171410A772C` | `4FC062E28214EE59` |
| 5 | `8A27C039D2C6C844` | `144F8073A58D9089` |
| 6 | `81C0514D66CA15F8` | `0380A29ACD942BF1` |
| 7 | `CC18FE2A95645252` | `9831FC552AC8A4A4` |
| 8 | `B52BE101838EE2C9` | `6A57C203071DC593` |
| 9 | `A00C1ED3888E902E` | `40183DA7111D205D` |
| A | `F020764CA1565672` | `E040EC9942ACACE5` |
| B | `C53AD4112D89E197` | `8A75A8225B13C32E` |
| C | `E33124028D105F97` | `C66248051A20BF2F` |
| D | `93035E1B4395C402` | `2606BC36872B8805` |
| E | `8B18AA986C3302DE` | `16315530D86605BD` |
| F | `8CB0A48A7E244638` | `19614914FC488C71` |

How the nibble values follow from the data: the block at 240.5 cycles with period 16
over the 56–93 °F sweep. The block at 248.5 steps up exactly when the 240.5
block wraps (between 59/60, 75/76 and 91/92). That fixes the order and the
offset: the pair is the byte `SP-60`. The pair at 176.5/184.5 then decodes,
with the same table, to exactly `SP` (`0x40`, `0x4A`–`0x4D`, `0x50` for 64,
74–77, 80 °F). The CRC check confirms both.

Structure tests on the 16 true codewords (`codebook_stats.py`):

* Pairwise Hamming distances are 22–41 (mean 30.7). Random 64-bit words give
  a mean of 32.
* No codeword is a rotation of another (any shift, ≤6 errors, complement
  included). No bit reversals. All 120 pairwise XORs are distinct, so there is
  no common XOR constant and no complement pairing.
* The GF(2) rank is 16 (15 after the XOR with codeword 0), so the set is not a
  linear or affine code.
* Linear complexity is 30–36, as for random bits. The codewords look like an
  arbitrary or pseudo-random table, not a structured spreading family.

## Burst map (75 °F; the bytes in **bold** depend on the setpoint)

| byte offset | RAW-view block | nibble | logical byte |
|---|---|---|---|
| 0, 8, 16, 24 | `9388…` `B52B…` `A471…` `1388…` | 0 8 3 0 | 80 03 |
| 32, 40, 48, 56 | `9388…` `B52B…` `A471…` `1388…` | 0 8 3 0 | 80 03 |
| 64, 72, 80, 88 | `F020…` `8A27…` `F020…` `8A27…` | A 5 A 5 | 5A 5A |
| 96, 104, 112, 120 | `9388…` `B52B…` `A471…` `1388…` | 0 8 3 0 | 80 03 |
| 128.0 | 4 raw bits `1101` (`1010` in the true framing) | trailer | |
| 128.5, 136.5 | `4C18…` `1388…` | 7 0 | 07 (length) |
| 144.5, 152.5 | `8A27…` `F020…` | 5 A | A5 |
| 160.5, 168.5 | `A7E0…` `A7E0…` | 4 4 | 44 |
| **176.5, 184.5** | `C53A…` `27E0…` | B 4 | **4B = SP** |
| 192.5 … 224.5 | `9388` `A7E0` `9388` `A7E0` `9388` | 0 4 0 4 0 | 40 40 0· |
| 232.5 | `9388…` | 0 | ·0 → 00 |
| **240.5, 248.5** | `8CB0…` `9388…` | F 0 | **0F = SP-60** |
| **256.5 … 280.5** | `81C0` `C53A` `0A27` `8CB0` | 6 B 5 F | **B6 F5 = CRC** |
| 288.5 | `1 0010011 0000000` then noise | end | |

Block counts in one burst: S×12, A7×5, then the rest vary. 74 °F has F0×4,
B5×3, A4×3, 8A×3, CC×2, 8B×2, C5×1, 8C×1. 75 °F has 8A×4, B5×3, A4×3, F0×3,
C5×2, 8C×2, CC×1, 81×1.

## Burst length (task 1)

* Data: 2309 bits (288.625 bytes, **2309 µs** at 1 Mbit/s). This runs from the
  first bit of `9388B0CA` to the last chip of the last CRC codeword. Add 14
  trailing bits that are the same in every capture: 2323 bits. Noise follows.
* Before the data: a 24-bit preamble and at least 52 µs of carrier. Total air
  time is about 2.4 ms.
* Files: `analysis/bursts/{74,75,76,77}F.hex` are **290 bytes (2320 µs)** each,
  from `9388B0CA` to `…930` or `…130` (the data plus 11 of the trailing bits).
  74 and 75 come from captures (head stitched to tail at the first `A7E03171`, byte
  160.5; the 788 overlap bits match). 76 and 77 are the captured bytes 0–259
  plus the predicted CRC tail.

## Setpoint dependence (task 3)

Only 4 bytes change: SP at 176.5/184.5, SP-60 at 240.5/248.5, and the CRC at
256.5–280.5. The "16 °F apart" claim holds and has a simple cause: the 240.5
block is the low nibble of SP-60. Thus 65/81 share `8A27C039` (nibble 5) and
71/87 share `C53AD411` (nibble B). The unit is °F, not °C, and the value is not an
index from 56: the offset is 60, so the carry falls at 60/76/92. The "at-min" presses
logged as 56 actually carry 55 and 46. All their CRCs match. 46 may be a
"low/off" code.

Cells show the first 4 bytes (RAW view, first bit 1) of each block. `*` means
predicted and not captured; every other cell comes from a capture and agrees
with the model. The full table, regenerated by `setpoint_table.py`, is in
`analysis/setpoint-table.md`.

| SP | set | SP-60 | CRC | @176.5 | @184.5 | @240.5 | @248.5 | @256.5 | @264.5 | @272.5 | @280.5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 46 | 2E | F2 | 3B 5E | 8B18* | A2E1* | A2E1 | 8CB0 | C53A | A471 | 8B18 | 8A27* |
| 55 | 37 | FB | DE 26 | CC18* | A471* | C53A | 8CB0 | 8B18 | 9303 | 81C0 | A2E1* |
| 56 | 38 | FC | 9D 38 | B52B* | A471* | E331 | 8CB0 | 9303 | A00C | B52B | A471* |
| 57 | 39 | FD | 50 22 | A00C* | A471* | 9303 | 8CB0 | 9388 | 8A27 | A2E1 | A2E1 |
| 58 | 3A | FE | 07 0D | F020* | A471* | 8B18 | 8CB0 | CC18 | 9388 | 9303 | 9388 |
| 59 | 3B | FF | CA 17 | C53A* | A471* | 8CB0 | 8CB0 | F020 | E331 | CC18 | C4F3* |
| 60 | 3C | 00 | 6E 28 | E331* | A471* | 9388 | 9388 | 8B18 | 81C0 | B52B | A2E1 |
| 61 | 3D | 01 | A3 32 | 9303* | A471* | C4F3 | 9388 | A471 | F020 | A2E1 | A471 |
| 62 | 3E | 02 | F4 1D | 8B18* | A471* | A2E1 | 9388 | A7E0 | 8CB0 | 9303 | C4F3* |
| 63 | 3F | 03 | 39 07 | 8CB0* | A471* | A471 | 9388 | A00C | A471 | CC18 | 9388* |
| 64 | 40 | 04 | 89 0C | 9388 | A7E0 | A7E0 | 9388 | A00C | B52B | E331 | 9388 |
| 65 | 41 | 05 | 44 16 | C4F3* | A7E0* | 8A27 | 9388 | A7E0 | A7E0 | 81C0* | C4F3* |
| 66 | 42 | 06 | 13 39 | A2E1* | A7E0* | 81C0 | 9388 | A471 | C4F3 | A00C* | A471* |
| 67 | 43 | 07 | DE 23 | A471* | A7E0* | CC18 | 9388 | 8B18 | 9303 | A471* | A2E1* |
| 68 | 44 | 08 | F5 EB | A7E0* | A7E0* | B52B | 9388 | 8A27 | 8CB0 | C53A* | 8B18* |
| 69 | 45 | 09 | 38 F1 | 8A27* | A7E0* | A00C | 9388 | B52B | A471 | C4F3* | 8CB0* |
| 70 | 46 | 0A | 6F DE | 81C0* | A7E0* | F020 | 9388 | 8CB0 | 81C0 | 8B18* | 9303* |
| 71 | 47 | 0B | A2 C4 | CC18* | A7E0* | C53A | 9388 | A2E1 | F020 | A7E0* | E331* |
| 72 | 48 | 0C | E1 DA | B52B* | A7E0* | E331 | 9388 | C4F3 | 8B18 | F020* | 9303* |
| 73 | 49 | 0D | 2C C0 | A00C* | A7E0* | 9303 | 9388 | E331 | A2E1 | 9388* | E331* |
| 74 | 4A | 0E | 7B EF | F020 | A7E0 | 8B18 | 9388 | C53A | CC18 | 8CB0 | 8B18 |
| 75 | 4B | 0F | B6 F5 | C53A | A7E0 | 8CB0 | 9388 | 81C0 | C53A | 8A27 | 8CB0 |
| 76 | 4C | 10 | 1C 2D | E331 | A7E0 | 9388 | C4F3 | E331 | C4F3 | 9303* | A2E1* |
| 77 | 4D | 11 | D1 37 | 9303 | A7E0 | C4F3 | C4F3 | C4F3 | 9303 | CC18* | A471* |
| 78 | 4E | 12 | 86 18 | 8B18* | A7E0* | A2E1 | C4F3 | 81C0 | B52B | B52B* | C4F3* |
| 79 | 4F | 13 | 4B 02 | 8CB0* | A7E0* | A471 | C4F3 | C53A | A7E0 | A2E1* | 9388* |
| 80 | 50 | 14 | 48 A8 | 9388 | 8A27 | A7E0 | C4F3 | B52B | A7E0* | B52B* | F020* |
| 81 | 51 | 15 | 85 B2 | C4F3* | 8A27* | 8A27 | C4F3 | 8A27 | B52B | A2E1* | C53A* |
| 82 | 52 | 16 | D2 9D | A2E1* | 8A27* | 81C0 | C4F3 | A2E1 | 9303 | 9303* | A00C* |
| 83 | 53 | 17 | 1F 87 | A471* | 8A27* | CC18 | C4F3 | 8CB0 | C4F3 | CC18* | B52B* |
| 84 | 54 | 18 | 34 4F | A7E0* | 8A27* | B52B | C4F3 | A7E0 | A471 | 8CB0* | A7E0* |
| 85 | 55 | 19 | F9 55 | 8A27* | 8A27* | A00C | C4F3 | A00C | 8CB0 | 8A27* | 8A27* |
| 86 | 56 | 1A | AE 7A | 81C0* | 8A27* | F020 | C4F3 | 8B18 | F020 | F020* | CC18* |
| 87 | 57 | 1B | 63 60 | CC18* | 8A27* | C53A | C4F3 | A471 | 81C0 | 9388 | 81C0 |
| 88 | 58 | 1C | 20 7E | B52B* | 8A27* | E331 | C4F3 | 9388 | A2E1 | 8B18* | CC18* |
| 89 | 59 | 1D | ED 64 | A00C* | 8A27* | 9303 | C4F3 | 9303 | 8B18 | A7E0* | 81C0* |
| 90 | 5A | 1E | BA 4B | F020* | 8A27* | 8B18 | C4F3 | F020 | C53A | C53A* | A7E0* |
| 91 | 5B | 1F | 77 51 | C53A* | 8A27* | 8CB0 | C4F3 | CC18 | CC18 | C4F3* | 8A27* |
| 92 | 5C | 20 | DF A8 | E331* | 8A27* | 9388 | A2E1 | 8CB0 | 9303 | B52B* | F020* |
| 93 | 5D | 21 | 12 B2 | 9303* | 8A27* | C4F3 | A2E1 | A2E1 | C4F3 | A2E1* | C53A* |

On air, the first nibble of a cell has its top bit cleared after A4, CC, C5 or
A2 (see "Bit framing").

## LT8920 packet tests (task 4)

* **Raw stream as back-to-back LT8920 packets: rejected.** The test treats each
  of the 12 S blocks as a sync word in both framings, with trailers of 0–18
  bits and MSB- or LSB-first bytes. It then tries the length byte and 12
  catalogue CRC-16s, with and without the length byte. Result: 0 hits in 14040
  tests (about 0.4 expected by chance). The S blocks are 8–56 bytes apart, and
  some gaps leave no room for a length byte and a CRC (`raw_lt8920_test.py`).
  `reveng` is not installed, so `crc_search.py` does the search in Python.
* **LT8920 format one level up (after despreading): fits.** The layout is a
  header, a 4-bit trailer (raw `1010`), the length byte `07`, a 7-byte payload
  and a CRC-16 with poly `0x1021`, sent LSB-first, low byte first. Init is
  `0x0000` over length + payload. Init `0x084D` gives the same result if the CRC
  also covers the 8 header bytes.

## Hypotheses: result

| hypothesis | result |
|---|---|
| Burst = sequence of 64-bit codewords from a 16-symbol alphabet, 1 nibble each | **Holds.** Exactly 16 blocks; the encoder reproduces all captures |
| Blocks related by rotation / XOR constant / complement | **Rejected.** Random-like table |
| `1388` vs `9388` carries data | **Rejected.** 1-bit framing offset; the previous codeword's last bit |
| Nibble shifts in the stream | **Explained.** One 4-bit raw trailer at byte 128 |
| Blocks recur for setpoints 16 °F apart | **Holds.** Low nibble of SP-60 |
| Setpoint maps to nibbles of °F | **Holds.** SP and SP-60, low nibble first; not °C, not an index from 56 |
| Variable burst length | **Rejected.** Fixed 2309 bits; the earlier "short" ESP frames came from trigger offsets |
| Raw stream = back-to-back LT8920 packets | **Rejected** |
| Despread stream = one LT8920-style packet with CRC-16 | **Holds.** CRC-16/KERMIT |

## Recommended split for nRF52 TX (task 5)

The burst (290 bytes from `9388B0CA`) does not fit in one nRF52 packet: 4
address bytes + 255 payload bytes is the limit. The only internal
`9388B0CA` occurrences on a whole-byte boundary are at bytes **32** and
**96**. Every later S sits at an `x.5` offset, after the trailer.

**Split at byte 32.** Packet 2 then uses the same address, `9388B0CA`:

* Segment 1 (`t`): bytes 4–31, 28 bytes, the same for every setpoint:
  `D019F2E6B52BE101838EE2C9A4717A73025C45161388B0CAD019F2E6`
  (`analysis/bursts/<SP>F.seg1.hex`).
* Segment 2 (`t2`): bytes 36–289, **254 bytes** (`<SP>F.seg2.hex`). If you drop
  the trailing filler, 253 bytes is enough.

Why 32 and not 96:

* The break (gap plus an extra 8-bit preamble) falls in the first of the
  repeated `80 03` lead-in groups.
* Segment 2 holds the rest of the header, the trailer and the whole packet as
  one unbroken stream, and it starts like a real burst (preamble, then S).
* In 205157 the decoder read a weak press (RSSI −75) from byte 32 on. Either the
  dongle missed the first sync, or the remote sent a lead-in 32 bytes shorter.
  The data cannot separate the two.

The split at 96 also fits (92 + 190 bytes), but it cuts the `5A 5A | 80 03`
header in two.

Notes:

* The two segments have different lengths, so the firmware uses the
  CPU-started path (about 1 µs gap, see `nrf52/README.md`), not the 0 µs
  equal-length path.
* The bit before `9388B0CA` in segment 2 is the nRF preamble's last bit. That
  matches the original, where the first `1` is also a preamble bit.
* The original has a 3-byte preamble; nRF52 sends 1 byte.
* Worth testing first: segment 2 alone as a single packet. If the Cube accepts
  it, you do not need the split.

## Files

* `scripts/rfcommon.py`: log parsing and bit helpers.
* `scripts/codebook.py`: the 16 blocks, nibble map and bit-level tokenizer.
* `scripts/decode.py`: stitch, tokenise, the last-bit rule, the encoder,
  checks, and writes `bursts/*.hex` and `decode-output.txt`.
* `scripts/sweep_table.py`: merges the sweep dongle and ESP data and writes
  `setpoint-nibbles.csv`.
* `scripts/crc_search.py`: the CRC-16 brute force.
* `scripts/raw_lt8920_test.py`: the raw-level LT8920 packet test.
* `scripts/codebook_stats.py`: Hamming, rotation, XOR, rank and linear
  complexity tests.
* `scripts/identify_presses.py`: decodes every press in every capture
  (`identify-output.txt`).
* `scripts/setpoint_table.py`: writes `setpoint-table.md`.
