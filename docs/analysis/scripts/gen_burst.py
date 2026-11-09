#!/usr/bin/env python3
"""Print the two nRF52 TX segments (seg1 seg2, hex) for a setpoint in degF.
usage: gen_burst.py <setpoint>   (run from docs/analysis/scripts)"""
import re, sys
from pathlib import Path
from rfcommon import b2h
from decode import encode, FILE_BITS, SPLIT, SYNC4
line = [l for l in Path(__file__).with_name("..").joinpath("decode-output.txt").read_text().splitlines() if "true last bits" in l][0]
lastbit = dict(re.findall(r"(\w+)=([01])", line.split(":", 1)[1]))
sp = int(sys.argv[1])
hx = b2h(encode(sp, lastbit)[:FILE_BITS])
assert hx[2*SPLIT:2*SPLIT+8] == SYNC4
print(hx[8:2*SPLIT], hx[2*SPLIT+8:])
