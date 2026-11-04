"""Reconstruct, decode and re-encode the ChiliPad press burst.

  * stitches the 255-byte head (trigger 9388B0CA, 20261104-163310) and tail
    (trigger A7E03171, 20261104-164149) windows into full bursts for 74/75 F
  * tokenises every burst into 64-bit blocks at bit level (task 2)
  * derives each block's true last bit (the "first-bit flip" rule)
  * builds an encoder from the decoded packet and checks it bit-for-bit against
    all captures; predicts the unseen tail for 76/77 F
  * writes analysis/bursts/<sp>F.hex

Bit positions are counted from the first bit of the first 9388B0CA; byte
offsets are pos/8 (".5" = nibble-shifted).
"""
import collections
from rfcommon import *
from codebook import *
from crc_search import crc16

HEAD_SP = [75, 76, 77, 76, 75, 74]
TAIL_SP = [75, 74, 75, 74, 75, 74]
TAIL_AT = 1284                      # first A7E03171 sits at bit 1284 (byte 160.5)
HDR = [0x80, 0x03, 0x80, 0x03, 0x5A, 0x5A, 0x80, 0x03]
TRAILER = "1010"
DATA_END = 1 + 16 * 64 + 4 + 20 * 64   # 2309: bits through the last codeword bit
FILE_BITS = 2320                       # write 290 bytes: + "0010011" + "0000"
SPLIT = 32                             # byte offset of the split (an internal 9388B0CA)


def common_prefix(strs):
    n = min(map(len, strs))
    for i in range(n):
        if len({s[i] for s in strs}) > 1:
            return i
    return n


def stitched():
    head = [onair(p["raw"]) for p in parse_nrf(CAP / "20261104-163310/nrf.log")]
    tail = [onair(p["raw"], "A7E03171") for p in parse_nrf(CAP / "20261104-164149/nrf.log")]
    A = h2b("A7E03171")
    out = {}
    for sp in (74, 75):
        hs = [h for h, s in zip(head, HEAD_SP) if s == sp]
        ts = [t for t, s in zip(tail, TAIL_SP) if s == sp]
        for h in hs:
            assert h.find(A) == TAIL_AT
            for t in ts:
                ov = h[TAIL_AT:]
                assert t[:len(ov)] == ov, "head/tail overlap mismatch"
        n = common_prefix(ts)            # tail bits identical in every capture
        out[sp] = dict(bits=hs[0][:TAIL_AT] + ts[0][:n], common=TAIL_AT + n,
                       noise=[b2h(t[n:n + 32]) for t in ts])
    heads = {sp: h for h, sp in zip(head, HEAD_SP)}
    return out, heads


def true_last_bits(streams):
    """For each block name, the set of first-bits seen on the block that follows it."""
    seen = collections.defaultdict(collections.Counter)
    for b in streams:
        toks = tokenize(b, 0, 3)
        for a, c in zip(toks, toks[1:]):
            if a[0] == "blk" and c[0] == "blk" and c[1] == a[1] + 64:
                seen[a[2]][c[3]] += 1
            if a[0] == "blk" and c[0] == "gap" and len(c[2]) == 4:
                seen[a[2]][c[2][0]] += 1   # gap nibble (trailer) after the block
    return seen


def logical(sp_byte, s60_byte):
    pl = [0x07, 0xA5, 0x44, sp_byte & 0xFF, 0x40, 0x40, 0x00, s60_byte & 0xFF]
    c = crc16(pl, 0x1021, 0, True, True)       # CRC-16/KERMIT
    return HDR, pl + [c & 0xFF, c >> 8]


def nibbles(byts):
    for b in byts:
        yield b & 0xF
        yield b >> 4


def encode(setpoint, lastbit):
    """On-air bits (9388B0CA-aligned view) for a press at `setpoint` degF."""
    hdr, pkt = logical(setpoint, setpoint - 60)

    def cw(n):
        name = BY_NIBBLE[n]
        return BITS[name][1:] + lastbit[name]     # true codeword is 1 bit later
    bits = "1"                                   # last preamble bit
    bits += "".join(cw(n) for n in nibbles(hdr))
    bits += TRAILER
    bits += "".join(cw(n) for n in nibbles(pkt))
    assert len(bits) == DATA_END
    return bits + END_TAIL


def main():
    full, heads = stitched()
    lines = []
    P = lines.append
    P("== stitched bursts ==")
    for sp, d in full.items():
        P(f"{sp}F: bits identical across captures = {d['common']} "
          f"({d['common']/8} bytes); noise after: {d['noise']}")
        P("   tokens: " + " ".join(f"{t[1]/8:g}:{label(t)}" for t in tokenize(d['bits'], 0, 3)))
    seen = true_last_bits([d["bits"] for d in full.values()] + list(heads.values()))
    lastbit = {}
    P("== first bit of the following block, per block (=> true last bit) ==")
    for k in BITS:
        c = seen.get(k, {})
        P(f"  {k:>2} {BLOCKS[k]}  next-first-bit {dict(c)}")
        if c:
            assert len(c) == 1, f"inconsistent follower bit for {k}"
            lastbit[k] = next(iter(c))
    # blocks never seen followed by anything in the long captures: take from sweep data
    from sweep_table import collect
    sw = collect()
    for sp, lst in sw.items():
        for src, known, end in lst:
            for p, (name, fb) in known.items():
                prev = known.get(p - 64)
                if prev:
                    lastbit.setdefault(prev[0], fb)
                    assert lastbit[prev[0]] == fb, (prev, fb, sp, src)
    P("  true last bits: " + " ".join(f"{k}={v}" for k, v in lastbit.items()))
    P("  blocks whose last bit is 0 (follower shows 1388/27E0/0A27/.. form): " +
      ", ".join(k for k, v in lastbit.items() if v == "0"))
    # block statistics (task 2)
    P("== block occurrences in the 74F/75F bursts ==")
    for sp, d in full.items():
        cnt = collections.Counter(t[2] for t in tokenize(d["bits"], 0, 3) if t[0] == "blk")
        P(f"  {sp}F: " + ", ".join(f"{k}x{v}" for k, v in cnt.most_common()))
    # encoder verification
    P("== encoder check ==")
    for sp, d in full.items():
        e = encode(sp, lastbit)
        P(f"  {sp}F: encoder == capture over {d['common']} bits: {e[:d['common']] == d['bits'][:d['common']]}")
    for sp, h in heads.items():
        e = encode(sp, lastbit)
        P(f"  {sp}F head window ({len(h)} bits): match {e[:len(h)] == h}")
    bad = 0
    tot = 0
    for p_sp, lst in sw.items():
        for src, known, end in lst:
            if p_sp == 56 and "203934" in src:
                continue          # at-min presses carry 55/46, checked in crc_search notes
            e = encode(p_sp, lastbit)
            for p, (name, fb) in known.items():
                tot += 1
                if e[p + 1:p + 64] != BITS[name][1:] or e[p] != fb:
                    bad += 1
            if end is not None:
                tot += 1
                bad += end != DATA_END - 1
    P(f"  sweep/dongle/ESP blocks checked against encoder: {tot}, mismatches {bad}")
    # write bursts
    (REPO / "analysis/bursts").mkdir(exist_ok=True)
    P("== written ==")
    for sp in (74, 75, 76, 77):
        e = encode(sp, lastbit)[:FILE_BITS]
        if sp in full:
            src = "captured (head+tail), equals encoder"
            assert e == full[sp]["bits"][:FILE_BITS]
        else:
            h = heads[sp]
            assert e[:len(h)] == h
            src = f"captured bytes 0..{len(h)//8} + predicted tail (CRC-16/KERMIT)"
        hx = b2h(e)
        (REPO / f"analysis/bursts/{sp}F.hex").write_text(hx + "\n")
        # two nRF52 segments split before the internal 9388B0CA at byte 32:
        # seg1 = bytes 4..31 (after the leading address), seg2 = bytes 36..289
        assert hx[2 * SPLIT:2 * SPLIT + 8] == SYNC4
        (REPO / f"analysis/bursts/{sp}F.seg1.hex").write_text(hx[8:2 * SPLIT] + "\n")
        (REPO / f"analysis/bursts/{sp}F.seg2.hex").write_text(hx[2 * SPLIT + 8:] + "\n")
        hdr, pkt = logical(sp, sp - 60)
        P(f"  {sp}F.hex  {len(e)//8} bytes  packet {' '.join(f'{b:02X}' for b in pkt)}  [{src}]")
    print("\n".join(lines))
    (REPO / "analysis/decode-output.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
