"""Family A ("poll/reply pairs"): bit alignment, timing, CRC/length search.

Run from analysis/scripts:  python3 poll_reply.py > ../poll-reply-output.txt
See ../poll-reply.md for the write-up.

Every instance is turned into an on-air bit string (trigger bytes + RAW) and the
24-bit sync 632F4B is searched at every bit offset (<=1 bit error).  The 4-byte
trigger 54C65E97 is the same sync seen 1 bit early: 0101010 | 632F4B | 1.
"""
import re
import sys
from itertools import product

from rfcommon import CAP, b2h, h2b, ham, parse_esp, parse_nrf

SYNC = h2b("632F4B")
FIELDS = [("T", 8), ("C1", 8), ("W", 16), ("4D", 8), ("tail", 16)]
HDR_BITS = sum(n for _, n in FIELDS)          # 56 bits after the sync
LEAD = []


# --------------------------------------------------------------------------- data
def load():
    """(label, receiver, log time s, timer us or None, rssi or None, bits)."""
    out = []

    def add_nrf(cap, trig, keep=None):
        for p in parse_nrf(CAP / cap / "nrf.log"):
            if keep and not any(abs(p["t"] - k) < 0.01 for k in keep):
                continue
            out.append((f"{cap[9:]} N{p['t']:6.2f}", "nrf", p["t"], p["t_us"], p["rssi"],
                        h2b(trig + p["raw"])))

    def add_esp(cap, trig):
        for p in parse_esp(CAP / cap / "esp.log"):
            out.append((f"{cap[9:]} E{p['t']:6.2f}", "esp", p["t"], None, None,
                        h2b(trig + p["raw"])))

    add_nrf("20261104-172417", "AAAAAA", keep=(16.457, 18.556))
    add_nrf("20261104-184855", "632F4B")
    add_esp("20261104-184855", "632F4BBA")
    add_nrf("20261104-185111", "54C65E97")
    add_esp("20261104-185111", "632F4B")
    return out


def sync_hits(bits):
    """Sync positions; a hit counts only if C1 follows 8 bits later (or the window
    ends first), which drops chance matches in noise."""
    out = []
    for i in range(len(bits) - 24):
        if ham(bits[i:i + 24], SYNC) <= 1:
            c1 = bits[i + 32:i + 40]
            if len(c1) < 8 or c1 == h2b("C1"):
                out.append(i)
    return out


def preamble_len(bits, i):
    """Alternating bits that end right before position i (the sync)."""
    if i == 0:
        return 0
    n = 1
    while i - n - 1 >= 0 and bits[i - n - 1] != bits[i - n]:
        n += 1
    return n


def split(bits, i):
    j = i + 24
    f = {}
    for name, n in FIELDS:
        f[name] = bits[j:j + n]
        j += n
    f["post"] = bits[j:]
    return f


def runs(b):
    out, k = [], 1
    for x, y in zip(b, b[1:]):
        if x == y:
            k += 1
        else:
            out.append(k)
            k = 1
    out.append(k)
    return out


# --------------------------------------------------------------------- sections
def sec_align(inst):
    print("== 1. bit alignment (sync 632F4B at any offset, <=1 error)")
    print("label                rx   rssi  pos  pre  T  C1 W    4D tail  tail-runs     next 16 bits")
    pk = []
    for lab, rx, t, tus, rssi, bits in inst:
        hits = sync_hits(bits)
        for k, i in enumerate(hits):
            f = split(bits, i)
            if len(f["tail"]) < 16:
                print(f"{lab:20s} {rx} {'':5s} {i:4d}  (window ends inside the header: "
                      f"{b2h(bits[i + 24:])})")
                pk.append((lab, rx, t, tus, rssi, i, k, None))
                continue
            pre = preamble_len(bits, i)
            pre_s = f"{pre:3d}" + ("+" if i - pre <= 0 else " ")
            role = "p1" if f["T"] in (h2b("BA"), h2b("BC")) else "p2"
            print(f"{lab:20s} {rx} {str(rssi) if (rssi and k == 0) else '':5s} {i:4d} {pre_s}"
                  f" {b2h(f['T'])} {b2h(f['C1'])} {b2h(f['W'])} {b2h(f['4D'])} "
                  f"{b2h(f['tail'])}  {','.join(map(str, runs(f['tail']))):13s} "
                  f"{f['post'][:16]}  {role}")
            pk.append((lab, rx, t, tus, rssi, i, k, f))
    return pk


def sec_spacing(pk):
    print("\n== 2a. sync-to-sync spacing inside one window (bits = us)")
    by = {}
    for lab, rx, t, tus, rssi, i, k, f in pk:
        by.setdefault(lab, []).append(i)
    sp = {}
    for lab, pos in by.items():
        if len(pos) > 1:
            sp.setdefault(pos[1] - pos[0], []).append(lab)
    for d, labs in sorted(sp.items()):
        print(f"  {d} us: {len(labs)} windows")


def sec_end(inst):
    """Where packet 1 ends and packet 2's preamble starts, from bit bias."""
    print("\n== 2b. packet 1 end / packet 2 preamble start (windows holding both)")
    print("  window bits after the p1 tail, 8-bit groups (count of ones per group)")
    for lab, rx, t, tus, rssi, bits in inst:
        hits = sync_hits(bits)
        if len(hits) < 2:
            continue
        a = hits[0] + 24 + HDR_BITS
        b = hits[1]
        seg = bits[a:b]
        ones = [seg[j:j + 8].count("1") for j in range(0, len(seg) - 7, 8)]
        pre = preamble_len(bits, b)
        m = list(re.finditer(r"1{3,}(0{6,9})1", bits[a:b]))
        lead = ""
        if m:
            z0, z1 = a + m[-1].start(1), a + m[-1].end(1)
            o0 = a + m[-1].start()
            lead = (f"lead-in: ones from {o0} (run {z0 - o0}), zeros {z0}-{z1} "
                    f"({z1 - z0}), then {b - z1} bits to the sync")
            LEAD.append((rx, o0, z0, z1, b))
        print(f"  {lab:20s} {rx}  p1 hdr+tail end {a:3d}, p2 sync {b:3d}, clean preamble "
              f"{pre:2d} bits; {lead}")
        print(f"      ones/8: {ones}\n      {seg}")
    for rx in ("nrf", "esp"):
        L = [x for x in LEAD if x[0] == rx]
        if L:
            rel = [(o0 - b, z0 - b, z1 - b) for _, o0, z0, z1, b in L]
            print(f"  {rx}: relative to p2 sync: ones start {sorted(r[0] for r in rel)}, "
                  f"zeros {sorted(r[1] for r in rel)}..{sorted(r[2] for r in rel)}")


def sec_timing():
    print("\n== 2c. pair repetition (dongle TIMER1 us at ADDRESS)")
    for cap in ("20261104-184855", "20261104-185111"):
        pk = parse_nrf(CAP / cap / "nrf.log")
        print(f"  {cap}")
        prev = None
        for p in pk:
            if p["dt"] < 1000:
                print(f"    t={p['t']:7.3f}  +{p['dt']} us (second sync in the same event) "
                      f"rssi {p['rssi']}")
                continue
            s = ""
            if prev is not None:
                d = p["t_us"] - prev
                n = round(d / 100000)
                s = f"dt={d:8d} us = {n:3d} x 100 ms {d - n * 100000:+5d} us " \
                    f"({(d - n * 100000) / d * 1e6:+6.0f} ppm)"
            print(f"    t={p['t']:7.3f}  {p['raw'][:8]}  rssi {p['rssi']}  {s}")
            prev = p["t_us"]
    print("  20261104-184432 energy run (E start_us dur max)")
    E = []
    for line in (CAP / "20261104-184432" / "nrf.log").read_text().splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[1] == "E":
            E.append(tuple(int(x) for x in parts[2:]))
    for k, (s, d, m) in enumerate(E):
        gap = f"+{s - E[k - 1][0]} us after previous start" if k else ""
        print(f"    {s:8d} {d:5d} us {m:4d} dBm  {gap}")


# ------------------------------------------------------------------------- CRC
def crc_reg(bits, poly, w, init=0):
    top, mask = 1 << (w - 1), (1 << w) - 1
    r = init
    for c in bits:
        fb = ((r & top) != 0) ^ (c == "1")
        r = ((r << 1) & mask) ^ (poly if fb else 0)
    return r


def rev(x, w):
    return int(f"{x:0{w}b}"[::-1], 2)


def bytes_rev(bits):
    return "".join(bits[j:j + 8][::-1] for j in range(0, len(bits), 8))


STD16 = {0x1021: "CCITT", 0x8005: "IBM/ARC", 0x3D65: "DNP", 0x8BB7: "T10-DIF",
         0xA097: "TELEDISK", 0xC867: "CDMA2000", 0x0589: "DECT", 0x5935: "EN-13757",
         0x6F63: "LJ1200", 0x755B: "OPENSAFETY-B", 0x5935 ^ 0: "M17", 0x1DCF: "PROFIBUS",
         0x2F15: "?", 0x080B: "?", 0x0805: "?"}
STD8 = {0x07: "CRC-8", 0x31: "MAXIM", 0x1D: "SAE-J1850", 0x9B: "WCDMA", 0x39: "DARC",
        0xD5: "DVB-S2", 0x2F: "AUTOSAR", 0x49: "?", 0x8D: "?", 0xA7: "BT", 0x4D: "?"}


def sec_crc(pk):
    print("\n== 3. CRC / length search")
    # distinct headers (T C1 W 4D) and the observed 16 tail bits
    hdrs = {}
    for lab, rx, t, tus, rssi, i, k, f in pk:
        if f:
            hdrs.setdefault(f["T"] + f["C1"] + f["W"] + f["4D"], set()).add(f["tail"])
    H = sorted(hdrs)
    for h in H:
        print(f"  header {b2h(h)}  tails seen: {sorted(b2h(x) for x in hdrs[h])}")

    # (a) W as a function of T: affine test
    T = {b2h(h[:8]): int(b2h(h[16:32]), 16) for h in H}
    print("  T -> W:", {k: f"{v:04X}" for k, v in T.items()})
    x = T["BA"] ^ T["BC"] ^ T["BE"] ^ T["B8"]
    print(f"  W(BA)^W(BC)^W(BE)^W(B8) = {x:04X}  (0 means W is an affine function of T;"
          f" chance 2^-16)")
    print(f"  W(BA)^W(B8) [T bit 0x02] = {T['BA'] ^ T['B8']:04X},  "
          f"W(BA)^W(BE) [T bit 0x04] = {T['BA'] ^ T['BE']:04X}  (second = first << 1)")

    # (b) brute-force: is W a CRC-16 of (sync?) T C1 under any poly / reflection?
    #     XOR trick: differences remove init/xorout and constant leading bytes.
    msgs = {k: h2b(k) + h2b("C1") for k in T}
    hits = []
    for poly in range(1, 1 << 16):
        for refin, refout, swap in product((0, 1), repeat=3):
            ok = True
            base = None
            for k in ("BC", "BE", "B8"):
                a = msgs["BA"] if not refin else bytes_rev(msgs["BA"])
                b = msgs[k] if not refin else bytes_rev(msgs[k])
                d = "".join("1" if p != q else "0" for p, q in zip(a, b))
                c = crc_reg(d, poly, 16)
                if refout:
                    c = rev(c, 16)
                tgt = T["BA"] ^ T[k]
                if swap:
                    tgt = ((tgt & 0xFF) << 8) | (tgt >> 8)
                if c != tgt:
                    ok = False
                    break
            if ok:
                hits.append((poly, refin, refout, swap))
    print(f"  CRC-16 over [T C1] (any leading constant bytes, any init/xorout): "
          f"{len(hits)} polys fit the 3 differences:")
    for poly, ri, ro, sw in hits[:20]:
        print(f"     poly {poly:04X} {STD16.get(poly, '')} refin={ri} refout={ro} byteswap={sw}")
    # also over T only (W directly after T would skip C1) and over [T] with C1 after W
    for label, mk in (("[T]", lambda k: h2b(k)),):
        n = 0
        for poly in range(1, 1 << 16):
            d1 = "".join("1" if p != q else "0" for p, q in zip(mk("BA"), mk("B8")))
            if crc_reg(d1, poly, 16) == T["BA"] ^ T["B8"]:
                n += 1
                print(f"  CRC-16 over {label}: poly {poly:04X} {STD16.get(poly, '')}")
        print(f"  CRC-16 over {label}: {n} polys")

    # (c) any 8- or 16-bit CRC over a span ending at a byte boundary that reproduces
    #     the next 8/16 bits in every distinct header (init 0/FF..; both reflections)
    print("  span search: CRC over bits [s,e) of sync+header equal to the following "
          "8/16 bits, every header")
    full = [SYNC + h + sorted(hdrs[h])[0] for h in H]
    nfound = 0
    for w, polys in ((8, STD8), (16, STD16)):
        mask = (1 << w) - 1
        for poly in polys:
            for init in (0, mask):
                for xo in (0, mask):
                    for refin in (0, 1):
                        for s in range(0, 24 + 1, 8):
                            for e in range(s + 8, len(full[0]) - w + 1, 8):
                                good = True
                                for fb in full:
                                    m = fb[s:e]
                                    if refin:
                                        m = bytes_rev(m)
                                    c = crc_reg(m, poly, w, init) ^ xo
                                    if refin:
                                        c = rev(c, w)
                                    if f"{c:0{w}b}" != fb[e:e + w]:
                                        good = False
                                        break
                                if good:
                                    nfound += 1
                                    print(f"     HIT w={w} poly {poly:X} init {init:X} xo {xo:X}"
                                          f" refin={refin} span [{s},{e})")
    print(f"  span search hits: {nfound}")

    # (c2) constant fields as CRCs of the varying ones: a CRC over a span that holds
    #      T and W can only stay constant if the XOR differences leave remainder 0.
    hk = {b2h(h[:8]): h for h in H if b2h(h[:8]) in ("BA", "BC", "BE", "B8")}
    for w, span, what in ((8, (0, 32), "4D = CRC-8 over [T C1 W]"),
                          (16, (0, 40), "tail = CRC-16 over [T C1 W 4D]"),
                          (16, (8, 40), "tail = CRC-16 over [C1 W 4D]")):
        n = 0
        for poly in range(1, 1 << w):
            for refin in (0, 1):
                ok = True
                for k in ("BC", "BE", "B8"):
                    a, b = hk["BA"][span[0]:span[1]], hk[k][span[0]:span[1]]
                    if refin:
                        a, b = bytes_rev(a), bytes_rev(b)
                    d = "".join("1" if p != q else "0" for p, q in zip(a, b))
                    if crc_reg(d, poly, w):
                        ok = False
                        break
                if ok:
                    n += 1
        print(f"  {what}: {n} of {(1 << w) - 1} polys (x2 reflections) give a constant value")
    # (c3) tail as a standard CRC over constant-only spans (sync bytes, 4D)
    tails = sorted({b2h(t) for h in H for t in hdrs[h]})
    n = 0
    for poly in STD16:
        for init, xo, refin in product((0, 0xFFFF), (0, 0xFFFF), (0, 1)):
            for s, e in ((0, 24), (8, 24), (16, 24), (56, 64)):
                m = full[0][s:e]
                if refin:
                    m = bytes_rev(m)
                c = crc_reg(m, poly, 16, init) ^ xo
                if refin:
                    c = rev(c, 16)
                if f"{c:04X}" in tails:
                    n += 1
                    print(f"     tail HIT poly {poly:04X} span [{s},{e}) -> {c:04X}")
    print(f"  tail as a standard CRC-16 over sync bytes or 4D: {n} hits")

    # (d) length byte: a byte equal to the number of bytes that follow (0..10)
    print("  length byte: candidate bytes and the payload bytes that would follow:")
    for name, off in (("T", 24), ("C1", 32), ("W hi", 40), ("W lo", 48), ("4D", 56)):
        v = [int(b2h(fb[off:off + 8]), 16) for fb in full]
        remaining = (len(full[0]) - off - 8) // 8
        print(f"     {name:4s} values {[f'{x:02X}' for x in sorted(set(v))]}  "
              f"(bytes left to the end of the tail: {remaining}; low 4/5 bits "
              f"{sorted(set(x & 0x1F for x in v))})")


def sec_compare(pk):
    print("\n== 4. variable bits vs setpoint / temperature encodings")
    tails = sorted({b2h(f["tail"]) for *_, f in pk if f})
    print("  observed tails:", tails)
    print("  tail run lengths:", {t: runs(h2b(t)) for t in tails})
    for t in tails:
        v = int(t, 16)
        b0, b1 = v >> 8, v & 0xFF
        sw = lambda x: ((x & 15) << 4) | (x >> 4)
        print(f"   {t}: bytes {b0} {b1}; nibble-swapped {sw(b0)} {sw(b1)}; bit-reversed "
              f"{rev(b0, 8)} {rev(b1, 8)}; 16-bit LSB-first {rev(v, 16)}")
    print("  setpoint burst bytes for 70 F: A5 44 46 40 40 00 0A; 69..71 F -> 45/46/47, "
          "09/0A/0B; 21 C = 15; 70 F = 0x46")
    want = {"70F": 0x46, "70F-60": 0x0A, "21C": 0x15, "A5": 0xA5, "44": 0x44, "40": 0x40}
    for lab, rx, t, tus, rssi, i, k, f in pk:
        if not f:
            continue
        hb = SYNC + f["T"] + f["C1"] + f["W"] + f["4D"] + f["tail"]
        found = []
        for name, val in want.items():
            pat = f"{val:08b}"
            for j in range(24, len(hb) - 7):
                if hb[j:j + 8] in (pat, pat[::-1]):
                    found.append(f"{name}@{j}{'r' if hb[j:j + 8] != pat else ''}")
        print(f"   {lab:20s} k={k}: {' '.join(found) or '-'}")
        break


def main():
    inst = load()
    pk = sec_align(inst)
    sec_spacing(pk)
    sec_end(inst)
    sec_timing()
    sec_crc(pk)
    sec_compare(pk)


if __name__ == "__main__":
    sys.exit(main())
