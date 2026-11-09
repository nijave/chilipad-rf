#!/usr/bin/env python3
"""docs/analysis/scripts/gen_burst2.py with a selectable command byte.

usage: gen_burst2_cmd.py <degF> <cmdhex>     (44 = setpoint/ON, 50 = OFF)

Packet 07 A5 CMD SP 40 40 00 X, X = (CMD + SP - 0x80) & 0xFF (= SP-60 for
0x44), CRC-16/KERMIT.  Everything else, including the split and its preamble
assert, is gen_burst2.py unchanged."""
import runpy
import sys
from pathlib import Path

S = Path(__file__).resolve().parents[2] / "docs" / "analysis" / "scripts"
sys.path.insert(0, str(S))
import decode                      # noqa: E402
from crc_search import crc16       # noqa: E402

cmd = int(sys.argv.pop(2), 16)


def logical(sp_byte, _s60_byte):
    pl = [0x07, 0xA5, cmd, sp_byte & 0xFF, 0x40, 0x40, 0x00, (cmd + sp_byte - 0x80) & 0xFF]
    c = crc16(pl, 0x1021, 0, True, True)
    return decode.HDR, pl + [c & 0xFF, c >> 8]


decode.logical = logical
runpy.run_path(str(S / "gen_burst2.py"), run_name="__main__")
