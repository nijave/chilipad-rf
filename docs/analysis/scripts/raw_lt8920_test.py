"""Task 4: is the raw on-air stream a run of back-to-back LT8920 packets?

Model tested: every 64-bit S block (9388B0CA D019F2E6, either bit framing) is an
LT8920 sync word, followed by a trailer of 0..18 bits, then a length byte, a
payload of that length and a CRC-16.  Bytes are read MSB-first (RAW view) and
LSB-first (REV view).  CRCs: the common CRC-16 catalogue.  Prints hit counts.
"""
from rfcommon import *
from crc_search import crc16, rev8

CAT = {  # name: (poly, init, refin, refout, xorout)
    "CCITT-FALSE": (0x1021, 0xFFFF, False, False, 0), "XMODEM": (0x1021, 0, False, False, 0),
    "KERMIT": (0x1021, 0, True, True, 0), "X25": (0x1021, 0xFFFF, True, True, 0xFFFF),
    "MCRF4XX": (0x1021, 0xFFFF, True, True, 0), "AUG-CCITT": (0x1021, 0x1D0F, False, False, 0),
    "GENIBUS": (0x1021, 0xFFFF, False, False, 0xFFFF), "ARC": (0x8005, 0, True, True, 0),
    "MODBUS": (0x8005, 0xFFFF, True, True, 0), "BUYPASS": (0x8005, 0, False, False, 0),
    "DNP": (0x3D65, 0, True, True, 0xFFFF), "DECT-R": (0x0589, 0, False, False, 1),
}


def to_bytes(bits, lsb_first):
    out = []
    for i in range(0, len(bits) - 7, 8):
        v = int(bits[i:i + 8], 2)
        out.append(rev8(v) if lsb_first else v)
    return out


def main():
    burst = h2b(open(REPO / "docs/analysis/bursts/75F.hex").read().strip())[:2309]
    S = h2b("9388B0CAD019F2E6")
    starts = [i for i in range(len(burst) - 63) if sum(a != b for a, b in zip(burst[i + 1:i + 64], S[1:])) <= 2]
    print("S block positions (bytes):", [p / 8 for p in starts])
    gaps = [(b - a) / 8 for a, b in zip(starts, starts[1:])]
    print("distance between consecutive S blocks (bytes):", gaps)
    tests = hits = len_ok = 0
    for s in starts:
        for shift in (0, 1):                         # 9388B0CA view or 1-bit-later framing
            for tr in range(0, 19):
                body = burst[s + shift + 64 + tr:]
                for lsb in (False, True):
                    by = to_bytes(body, lsb)
                    if not by:
                        continue
                    L = by[0]
                    if 1 + L + 2 > len(by):
                        continue
                    len_ok += 1
                    for name, (poly, init, ri, ro, xo) in CAT.items():
                        for cover in (by[:1 + L], by[1:1 + L]):
                            c = crc16(cover, poly, init, ri, ro, xo)
                            tests += 1
                            got = by[1 + L:3 + L]
                            if c in (got[0] << 8 | got[1], got[1] << 8 | got[0]):
                                hits += 1
                                print("  hit", s / 8, shift, tr, lsb, name, L)
    print(f"length-byte candidates that fit inside the burst: {len_ok}; CRC tests: {tests}; hits: {hits}")
    print(f"(expected false hits ~ {tests * 2 / 65536:.2f})")


if __name__ == "__main__":
    main()
