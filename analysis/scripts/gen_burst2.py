#!/usr/bin/env python3
"""Two equal-length nRF52 segments that reproduce the burst bit-for-bit.

The split sits where the stream itself contains the nRF preamble byte
(RAW 10101010 followed by a 1 bit, at stream bit 1053 for every setpoint),
so packet 2's preamble + address are stream bits and nothing is inserted.
Segment 1 is preceded by 211 bits of 0101 alternation (a long preamble).
Prints: addr1 seg1 addr2 seg2 (hex).  usage: gen_burst2.py <degF>"""
import re, sys
from pathlib import Path
from rfcommon import b2h, h2b
from decode import encode, FILE_BITS
line = [l for l in Path(__file__).with_name("..").joinpath("decode-output.txt").read_text().splitlines() if "true last bits" in l][0]
lastbit = dict(re.findall(r"(\w+)=([01])", line.split(":", 1)[1]))
sp = int(sys.argv[1])
s = encode(sp, lastbit)[:FILE_BITS]
SPLIT = 1053
assert s[SPLIT:SPLIT+9] in ("101010101", "010101010"), s[SPLIT:SPLIT+9]
LEAD = 211                       # bits of alternation before the sync, bit -k = 0 if k odd
lead = "".join("0" if (LEAD - i) % 2 == 1 else "1" for i in range(LEAD))
full1 = lead + s[:SPLIT]         # = addr1(32) + payload1
addr1, pay1 = full1[:32], full1[32:]
addr2 = s[SPLIT+8:SPLIT+40]
pay2 = s[SPLIT+40:]
pay2 += "0" * (-len(pay2) % 8)
L = max(len(pay1), len(pay2)) // 8 * 8
pay1 += "0" * (L - len(pay1)); pay2 += "0" * (L - len(pay2))
assert len(pay1) == len(pay2) == L and L % 8 == 0 and L // 8 <= 255
# check: on-air concatenation reproduces the stream
air = ("01010101" if addr1[0] == "0" else "10101010") + addr1 + pay1 + s[SPLIT:SPLIT+8] + addr2 + pay2
assert air.find(s) >= 0, "stream not reproduced"
print(b2h(addr1), b2h(pay1), b2h(addr2), b2h(pay2))
