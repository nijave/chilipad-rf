"""Brute-force CRC-16 over the decoded packets (all 65536 polynomials, both
reflections, both CRC byte orders).  Uses the XOR-difference trick: for equal-length
messages crc(a) ^ crc(b) = crc0(a ^ b) where crc0 has init=0, xorout=0, so the
polynomial is found independently of init/xorout and of how many constant leading
bytes are covered.  Then solves init for a few coverage choices.

Packets: [len=07] A5 44 <setpoint F> 40 40 00 <setpoint-60> | crcA crcB
(nibbles low-first, values from codebook.NIBBLE; see burst-structure.md).
"""
import sys

HDR = [0x80, 0x03, 0x80, 0x03, 0x5A, 0x5A, 0x80, 0x03]   # 16 codewords before trailer
# setpoint -> (crcA, crcB) for every press where all 4 CRC nibbles were observed
KNOWN = {57: (0x50, 0x22), 58: (0x07, 0x0D), 60: (0x6E, 0x28), 61: (0xA3, 0x32),
         64: (0x89, 0x0C), 74: (0x7B, 0xEF), 75: (0xB6, 0xF5), 87: (0x63, 0x60)}
# setpoint -> crcA only
PARTIAL = {56: 0x9D, 59: 0xCA, 62: 0xF4, 63: 0x39, 65: 0x44, 66: 0x13, 67: 0xDE,
           68: 0xF5, 69: 0x38, 70: 0x6F, 71: 0xA2, 72: 0xE1, 73: 0x2C, 76: 0x1C,
           77: 0xD1, 78: 0x86, 79: 0x4B, 81: 0x85, 82: 0xD2, 83: 0x1F, 84: 0x34,
           85: 0xF9, 86: 0xAE, 88: 0x20, 89: 0xED, 90: 0xBA, 91: 0x77, 92: 0xDF,
           93: 0x12}


def payload(sp):
    return [0x07, 0xA5, 0x44, sp & 0xFF, 0x40, 0x40, 0x00, (sp - 60) & 0xFF]


def rev8(x):
    return int(f"{x:08b}"[::-1], 2)


def rev16(x):
    return int(f"{x:016b}"[::-1], 2)


def crc16(data, poly, init=0, refin=False, refout=False, xorout=0):
    r = init
    for b in data:
        if refin:
            b = rev8(b)
        r ^= b << 8
        for _ in range(8):
            r = ((r << 1) ^ poly) & 0xFFFF if r & 0x8000 else (r << 1) & 0xFFFF
    if refout:
        r = rev16(r)
    return r ^ xorout


def value(a, b, order):
    return (a << 8 | b) if order == "AB" else (b << 8 | a)


def search():
    ref = 74
    diffs = [(sp, [x ^ y for x, y in zip(payload(sp), payload(ref))]) for sp in KNOWN if sp != ref]
    hits = []
    for refin in (False, True):
        for refout in (False, True):
            for order in ("AB", "BA"):
                tgt = [value(*KNOWN[sp], order) ^ value(*KNOWN[ref], order) for sp, _ in diffs]
                for poly in range(1, 0x10000):
                    if all(crc16(d, poly, 0, refin, refout) == t for (sp, d), t in zip(diffs, tgt)):
                        hits.append((poly, refin, refout, order))
    return hits


def solve_init(poly, refin, refout, order, prefix):
    """Find init (xorout=0) so that crc(prefix+payload) matches all KNOWN."""
    sols = []
    for init in range(0x10000):
        if all(crc16(prefix + payload(sp), poly, init, refin, refout) == value(*KNOWN[sp], order)
               for sp in (74, 75)):
            sols.append(init)
    return sols


if __name__ == "__main__":
    hits = search()
    print("polynomial hits (poly, refin, refout, byte order):", [(f"0x{p:04X}", a, b, o) for p, a, b, o in hits])
    for poly, refin, refout, order in hits:
        for name, pre in (("len+payload", []), ("hdr+len+payload", HDR)):
            for init in solve_init(poly, refin, refout, order, pre):
                ok = all(crc16(pre + payload(sp), poly, init, refin, refout) == value(*KNOWN[sp], order) for sp in KNOWN)
                part = sum(((crc16(pre + payload(sp), poly, init, refin, refout) >> (8 if order == "AB" else 0)) & 0xFF) == a
                           for sp, a in PARTIAL.items())
                print(f"  poly=0x{poly:04X} refin={refin} refout={refout} order={order} cover={name} "
                      f"init=0x{init:04X}: full {ok} ({len(KNOWN)}), crcA partial {part}/{len(PARTIAL)}")
        if len(sys.argv) > 1:
            break
