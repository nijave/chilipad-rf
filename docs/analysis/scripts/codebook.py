"""Candidate 64-bit block alphabet observed in the bursts and a bit-level tokenizer."""
from rfcommon import h2b, b2h, ham

# Canonical form uses the variant whose first bit is 1 (the first bit is seen flipped
# in some positions, see burst-structure.md).
BLOCKS = {
    "S":  "9388B0CAD019F2E6",
    "B5": "B52BE101838EE2C9",
    "A4": "A4717A73025C4516",
    "F0": "F020764CA1565672",
    "8A": "8A27C039D2C6C844",
    "CC": "CC18FE2A95645252",
    "A7": "A7E03171410A772C",
    "C5": "C53AD4112D89E197",
    "8C": "8CB0A48A7E244638",
    "81": "81C0514D66CA15F8",
    "8B": "8B18AA986C3302DE",
    "E3": "E33124028D105F97",
    "93": "93035E1B4395C402",
    "C4": "C4F30304A5ECD55C",
    "A2": "A2E1F402A42EF39A",
    "A0": "A00C1ED3888E902E",
}
BITS = {k: h2b(v) for k, v in BLOCKS.items()}


def match_at(bits, pos, maxerr=2):
    """Best block at bit pos comparing bits 1..63 (first bit reported separately)."""
    seg = bits[pos:pos + 64]
    if len(seg) < 64:
        return None
    best = None
    for k, cw in BITS.items():
        e = ham(seg[1:], cw[1:])
        if best is None or e < best[1]:
            best = (k, e, seg[0])
    return best if best[1] <= maxerr else None


def tokenize(bits, start=0, maxerr=2):
    """Return list of tokens: ('blk', pos, name, firstbit, errs) or ('gap', pos, bitstr)."""
    toks, pos = [], start
    while pos + 64 <= len(bits):
        m = match_at(bits, pos, maxerr)
        if m:
            toks.append(("blk", pos, m[0], m[2], m[1]))
            pos += 64
            continue
        nxt = next((q for q in range(pos + 1, len(bits) - 63) if match_at(bits, q, maxerr)), None)
        if nxt is None:
            toks.append(("gap", pos, bits[pos:]))
            return toks
        toks.append(("gap", pos, bits[pos:nxt]))
        pos = nxt
    if pos < len(bits):
        toks.append(("gap", pos, bits[pos:]))
    return toks


def label(tok):
    if tok[0] == "gap":
        return f"<{tok[2]}>" if len(tok[2]) <= 16 else f"<gap {len(tok[2])}b>"
    _, pos, name, fb, e = tok
    s = name if fb == "1" else name.lower() + "'"
    return s + ("*" * e)


# Nibble value of each codeword (derived in decode.py / burst-structure.md from the
# setpoint sweep: field at 176.5 B equals the setpoint in degF, field at 240.5 B
# equals setpoint-60, both low nibble first).
NIBBLE = {"S": 0x0, "C4": 0x1, "A2": 0x2, "A4": 0x3, "A7": 0x4, "8A": 0x5, "81": 0x6,
          "CC": 0x7, "B5": 0x8, "A0": 0x9, "F0": 0xA, "C5": 0xB, "E3": 0xC, "93": 0xD,
          "8B": 0xE, "8C": 0xF}
BY_NIBBLE = {v: k for k, v in NIBBLE.items()}
# Codewords whose (true, 1-bit-later-framed) last bit is 0: the block that follows
# shows a 0 as its first bit in the 9388B0CA-aligned view.
END_TAIL = "0010011" + "0000000"   # bits seen after the last codeword in every capture


def match_partial(bits, pos, minbits=24):
    """Identify a truncated block at the end of a window by its prefix (bits 1..n)."""
    seg = bits[pos:pos + 64]
    if len(seg) < minbits:
        return None
    hits = [(k, ham(seg[1:], cw[1:len(seg)])) for k, cw in BITS.items()]
    hits.sort(key=lambda x: x[1])
    if hits[0][1] <= 1 and hits[1][1] > 4:
        return hits[0][0], seg[0], len(seg)
    return None


def blocks_by_pos(bits, origin_bits, maxerr=3):
    """Map absolute block position (bits from first 9388B0CA) -> (name, firstbit).

    `origin_bits` is the absolute bit offset of bits[0].  Also detects the end marker.
    Returns (dict, end_pos or None)."""
    out, end = {}, None
    toks = tokenize(bits, 0, maxerr)
    for t in toks:
        if t[0] == "blk":
            out[origin_bits + t[1]] = (t[2], t[3])
    # trailing partial / end marker after the last full block
    last = max((t[1] for t in toks if t[0] == "blk"), default=None)
    if last is not None:
        p = last + 64
        rest = bits[p:]
        if len(rest) >= 15 and rest[1:15] == END_TAIL:
            end = origin_bits + p
        else:
            m = match_partial(bits, p)
            if m:
                out[origin_bits + p] = (m[0], m[1])
    return out, end
