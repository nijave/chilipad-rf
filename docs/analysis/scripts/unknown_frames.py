"""Analysis of strong nRF frames that do not decode with the 16-codeword book.

Run from docs/analysis/scripts:  python3 unknown_frames.py [section ...]
Sections: list align runs end period whiten codebook all (default all).
Output: docs/analysis/unknown-frames-output.txt; summary in docs/analysis/unknown-frames.md.
"""
import sys
from collections import Counter
from rfcommon import CAP, parse_nrf, h2b, b2h, ham
from codebook import BITS

CAP4 = CAP / "20261104-172417" / "nrf.log"
TRIG = "AAAAAA"


def frame(path, t, trig=TRIG):
    for p in parse_nrf(path):
        if abs(p["t"] - t) < 0.01:
            return dict(p, bits=h2b(trig + p["raw"]))
    raise KeyError(t)


FR = {
    "A1": (CAP4, 16.457),
    "A2": (CAP4, 18.556),
    "B1": (CAP4, 22.714),
    "B2": (CAP4, 26.076),
    "C13": (CAP4, 13.745),
    "C57": (CAP4, 57.719),
    "SP": (CAP4, 7.982),      # known codeword burst (reference)
    # 20261103-202434 strong frames that are not clean setpoint bursts
    "SPARSE": (CAP / "20261103-202434" / "nrf.log", 1.753),
    "SPARSE2": (CAP / "20261103-202434" / "nrf.log", 9.823),
    "X": (CAP / "20261103-202434" / "nrf.log", 21.051, "555555"),
}


def load():
    return {k: frame(*v) for k, v in FR.items()}


def inv(b):
    return b.translate(str.maketrans("01", "10"))


def runs(b):
    out, cur, n = [], b[0], 0
    for c in b:
        if c == cur:
            n += 1
        else:
            out.append((cur, n))
            cur, n = c, 1
    out.append((cur, n))
    return out


def best_align(a, b, minlen=48):
    """Best (agreement fraction, shift, overlap) of b against a over all shifts."""
    best = (0, 0, 0)
    for s in range(-(len(b) - minlen), len(a) - minlen + 1):
        if s >= 0:
            x, y = a[s:], b
        else:
            x, y = a, b[-s:]
        n = min(len(x), len(y))
        if n < minlen:
            continue
        agree = n - ham(x[:n], y[:n])
        f = agree / n
        if f > best[0]:
            best = (f, s, n)
    return best


def longest_common_run(a, b, minrun=24):
    """Longest exact common substring (bits) between a and b (any offset)."""
    best = (0, 0, 0)
    for s in range(-len(b) + 1, len(a)):
        i0, j0 = max(s, 0), max(-s, 0)
        run = 0
        while i0 < len(a) and j0 < len(b):
            if a[i0] == b[j0]:
                run += 1
                if run > best[0]:
                    best = (run, i0 - run + 1, j0 - run + 1)
            else:
                run = 0
            i0 += 1
            j0 += 1
    return best


def sec_list(F):
    for k, f in F.items():
        print(f"{k:4s} t={f['t']:.3f} rssi={f['rssi']} bits={len(f['bits'])}")
        print("    ", b2h(f["bits"][:256]))


def sec_align(F):
    print("== pairwise alignment (longest exact common bit run, any offset) ==")
    keys = ["A1", "A2", "B1", "B2", "C13", "C57"]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            for tag, bb in (("", F[b]["bits"]), ("inv", inv(F[b]["bits"])),
                            ("rev", F[b]["bits"][::-1]), ("revinv", inv(F[b]["bits"][::-1]))):
                r = longest_common_run(F[a]["bits"], bb)
                if tag == "" or r[0] >= 20:
                    print(f"  {a}-{b} {tag:6s} longest run {r[0]:4d} bits  at {a}[{r[1]}] / {b}[{r[2]}]")
    # Chance level: longest run between two random 2064-bit strings ~ log2(n*m) ~ 22
    print("  (chance: longest exact common run of two random 2064-bit strings is ~19-23 bits; the max over these 60 comparisons is expected near 26)")
    print("\n== A vs B: 96-bit prefix (after the AAAAAA trigger) at all 8 shifts ==")
    for x in ("A1",):
        pa = F[x]["bits"][24:24 + 80]
        for y in ("B1",):
            for tag, src in (("plain", F[y]["bits"]), ("inv", inv(F[y]["bits"])),
                             ("rev", F[y]["bits"][::-1]), ("revinv", inv(F[y]["bits"][::-1]))):
                best = min((ham(pa, src[s:s + 80]), s) for s in range(0, len(src) - 80))
                print(f"  {x}[24:104] vs {y} {tag:6s}: min Hamming {best[0]}/80 at bit {best[1]}")
    print("  (chance: min Hamming of 80 random bits over ~2000 offsets is 23-26)")
    print("\n== within-family common prefix ==")
    for a, b, sh in (("A1", "A2", 4), ("B1", "B2", 0)):
        x, y = F[a]["bits"], F[b]["bits"][sh:]
        n = 0
        while x[n] == y[n]:
            n += 1
        print(f"  {a} vs {b}<<{sh}: identical for {n} bits incl. 24-bit trigger "
              f"({n - 24} bits after trigger); first diff at bit {n}")
        # agreement after the prefix in 64-bit windows
        row = []
        for w in range(n, min(len(x), len(y)) - 64, 64):
            row.append(64 - ham(x[w:w + 64], y[w:w + 64]))
        print("   agreement/64 per 64-bit window after prefix:", row)


def sec_runs(F):
    print("== run-length histograms (bits at 1 Mbit) ==")
    regions = {
        "SP 0:2064": F["SP"]["bits"],
        "A1 24:520": F["A1"]["bits"][24:520],
        "A1 520:2064": F["A1"]["bits"][520:],
        "A2 28:520": F["A2"]["bits"][28:520],
        "B1 24:2064": F["B1"]["bits"][24:],
        "B2 24:2064": F["B2"]["bits"][24:],
        "C13 all": F["C13"]["bits"][24:],
        "C57 all": F["C57"]["bits"][24:],
    }
    for name, b in regions.items():
        r = [n for _, n in runs(b)][1:-1]
        h = Counter(r)
        tot = len(r)
        mean = sum(r) / tot
        ones = b.count("1") / len(b)
        hist = " ".join(f"{k}:{h[k]}" for k in range(1, 21) if h[k])
        long_ = sum(v for k, v in h.items() if k > 20)
        mod4 = sum(v for k, v in h.items() if k % 4 == 0) / tot
        print(f"  {name:12s} runs={tot:4d} mean={mean:4.2f} P(1)={ones:.2f} "
              f"frac(len%4==0)={mod4:.2f}  >20:{long_}")
        print(f"      {hist}")
    print("  geometric (random 1 Mbit bits): P(len=k)=2^-k, mean 2.0, frac(len%4==0)~0.07")


def autocorr(b, lags):
    out = {}
    for L in lags:
        n = len(b) - L
        out[L] = sum(b[i] == b[i + L] for i in range(n)) / n
    return out


def sec_period(F):
    print("== autocorrelation (fraction of equal bits at lag L); peaks > ~0.6 are structure ==")
    for k, lo, hi in (("SP", 0, 2064), ("A1", 24, 520), ("A2", 28, 520), ("B1", 24, 2064),
                      ("B2", 24, 2064), ("C13", 24, 2064), ("C57", 24, 2064)):
        b = F[k]["bits"][lo:hi]
        ac = autocorr(b, range(1, 257))
        top = sorted(ac.items(), key=lambda x: -x[1])[:8]
        print(f"  {k:4s} [{lo}:{hi}] lag16={ac[16]:.2f} lag32={ac[32]:.2f} lag64={ac[64]:.2f} "
              f"top: " + " ".join(f"{L}:{v:.2f}" for L, v in top))
    print("\n== repeated 64-bit / 32-bit windows (<=3 / <=1 errors) at any phase within a frame ==")
    for k, lo, hi in (("SP", 0, 2064), ("A1", 24, 520), ("A2", 28, 520), ("B1", 24, 2064),
                      ("B2", 24, 2064), ("C13", 24, 2064), ("C57", 24, 2064)):
        b = F[k]["bits"][lo:hi]
        for L, maxe in ((64, 3), (32, 1)):
            w = [int(b[i:i + L], 2) for i in range(len(b) - L + 1)]
            hits, ex = 0, []
            for i in range(len(w)):
                wi = w[i]
                for j in range(i + L, len(w)):
                    if (wi ^ w[j]).bit_count() <= maxe:
                        hits += 1
                        if len(ex) < 3:
                            ex.append((i + lo, j + lo))
            print(f"  {k:4s} {L}-bit repeats: {hits:5d}  e.g. {ex}")
    print("  (chance for 2000 bits: 64-bit ~0, 32-bit <=1 err ~0.03)")


def berlekamp_massey(bits):
    """Linear complexity of a 0/1 list over GF(2)."""
    n = len(bits)
    c, bb = [1] + [0] * n, [1] + [0] * n
    L, m = 0, -1
    for i in range(n):
        d = bits[i]
        for j in range(1, L + 1):
            d ^= c[j] & bits[i - j]
        if d:
            t = c[:]
            for j in range(n - i + m):
                if j + i - m <= n:
                    c[j + i - m] ^= bb[j]
            if 2 * L <= i:
                L, m, bb = i + 1 - L, i, t
    return L


def nordic_whiten(ch, n):
    """BLE data whitening (x^7+x^4+1) as in the nRF/Core-spec reference code."""
    lf = (ch & 0x3F) | 0x40
    out = []
    for _ in range(n):
        out.append(lf & 1)
        if lf & 1:
            lf ^= 0x88
        lf >>= 1
    return out


def sec_whiten(F):
    """Whitening tests.
    1. A whitened run of constant data (0x00/0xFF padding, a zero CRC field, an
       LT8920/BLE/nRF52 7-bit or CC2500 PN9 scrambler) is an LFSR output, so its linear
       complexity is <= 9.  Random 48-bit windows have linear complexity ~24.
    2. BLE: the 2450 MHz BLE data channel is index 23. Dewhiten the 16-bit header after
       the 32-bit access address and check LLID (00 is reserved)."""
    print("== minimum linear complexity of any 48-bit window (LFSR-whitened constant data -> <=9) ==")
    for k, lo, hi in (("SP", 0, 2064), ("A1", 24, 2064), ("A2", 28, 2064), ("B1", 24, 2064),
                      ("B2", 24, 2064), ("C13", 24, 2064), ("C57", 24, 2064)):
        b = [int(c) for c in F[k]["bits"][lo:hi]]
        lcs = [(berlekamp_massey(b[i:i + 48]), i + lo) for i in range(0, len(b) - 48, 4)]
        mn = min(lcs)
        low = sum(1 for v, _ in lcs if v <= 12)
        print(f"  {k:4s} min LC {mn[0]:2d} at bit {mn[1]:4d}; windows with LC<=12: {low}")
    print("  (a window that is all-0/all-1 or 1010 also has LC <= 2; check the bits at the reported spot)")
    print("\n== BLE data channel 23 (2450 MHz) dewhitening of the header after a 32-bit access address ==")
    w = nordic_whiten(23, 16)
    for k, aa in (("A1", 24), ("A1", 255), ("A2", 28), ("A2", 258), ("B1", 24), ("B2", 24)):
        b = F[k]["bits"]
        h = "".join(str(int(b[aa + 32 + i]) ^ w[i]) for i in range(16))
        llid = h[1] + h[0]
        ln = int(h[8:16][::-1], 2)
        print(f"  {k} AA@{aa}: header {h}  LLID={llid}{' (reserved)' if llid == '00' else ''} len={ln}")
    print("  Empty-PDU test (header 10xxx00000000000) for all 64 BLE channel seeds, both A packet 1s: ", end="")
    hits = 0
    for ch in range(64):
        w = nordic_whiten(ch, 16)
        for k, aa in (("A1", 24), ("A2", 28)):
            b = F[k]["bits"]
            h = "".join(str(int(b[aa + 32 + i]) ^ w[i]) for i in range(16))
            if h[0:2] == "10" and h[5:] == "0" * 11:
                hits += 1
    print(f"{hits} hits")


def manchester_check(b):
    """Fraction of 2-bit pairs that are 01/10 at the best phase."""
    out = []
    for ph in (0, 1):
        pr = [b[i:i + 2] for i in range(ph, len(b) - 1, 2)]
        out.append(sum(p in ("01", "10") for p in pr) / len(pr))
    return out


def sec_codebook(F):
    print("== Manchester: fraction of 01/10 pairs at phase 0/1 (Manchester -> ~1.0) ==")
    for k, lo, hi in (("SP", 0, 2064), ("A1", 24, 520), ("A2", 28, 520), ("B1", 24, 2064),
                      ("B2", 24, 2064), ("C13", 24, 2064), ("C57", 24, 2064)):
        b = F[k]["bits"][lo:hi]
        m1 = manchester_check(b)
        # Manchester at 250 kbit data / 500 kchip would show as 0011/1100 pairs at 1 Mbit
        b2 = b[::2]
        m2 = [manchester_check(b[ph::2]) for ph in (0, 1)]
        print(f"  {k:4s} 1-bit chips: {m1[0]:.2f}/{m1[1]:.2f}   2-bit chips (decimated): "
              + " ".join(f"{a:.2f}/{c:.2f}" for a, c in m2))
    print("\n== differential decode (b[i] xor b[i-1]) then codebook / repeats ==")
    for k in ("A1", "B1"):
        b = F[k]["bits"][24:]
        d = "".join("1" if b[i] != b[i - 1] else "0" for i in range(1, len(b)))
        print(f"  {k} diff ones fraction {d.count('1') / len(d):.2f}; head {b2h(d[:120])}")
    print("\n== nearest known codeword at every phase (bits 1..63), all frames ==")
    for k in ("A1", "A2", "B1", "B2", "C13", "C57"):
        b = F[k]["bits"]
        for tag, bb in (("plain", b), ("inv", inv(b)), ("rev", b[::-1])):
            best = (64, 0, "")
            for p in range(0, len(bb) - 64):
                for cn, cb in BITS.items():
                    e = ham(bb[p + 1:p + 64], cb[1:])
                    if e < best[0]:
                        best = (e, p, cn)
            print(f"  {k:4s} {tag:5s} best codeword match {best[0]}/63 ({best[2]} at {best[1]})")
    print("  (chance minimum over ~2000 offsets x 16 words, from random bits: 12-17/63, median 16)")
    print("\n== decimate by 2/3/4 (lower symbol rate oversampled) then codebook ==")
    for k in ("A1", "B1", "B2"):
        b = F[k]["bits"][24:]
        for dfac in (2, 3, 4):
            best = (64, None)
            for ph in range(dfac):
                bb = b[ph::dfac]
                for p in range(0, len(bb) - 64):
                    for cn, cb in BITS.items():
                        e = ham(bb[p + 1:p + 64], cb[1:])
                        if e < best[0]:
                            best = (e, (ph, p, cn))
            print(f"  {k} /{dfac}: best codeword match {best[0]}/63 {best[1]}")


def sec_bias(F):
    """A strong signal off the receiver centre frequency biases the 1 Mbit slicer: with
    a bias to 1 a 0 can read as 1 but a 1 never reads as 0, and the reverse.  Test every
    frame against the 16 codewords under that one-sided error model, at 64-bit spacing."""
    M = (1 << 63) - 1
    cws = {k: int(v[1:], 2) for k, v in BITS.items()}
    print("== one-sided (frequency-offset) codeword test: 64-bit-spaced blocks with <=1 violation,")
    print("   >=2 margin to the next codeword, >=8 ones in the window ==")
    for name, f in F.items():
        b = f["bits"]
        best = (0,)
        for d in ("to1", "to0"):
            for ph in range(64):
                n, seq = 0, []
                for p in range(ph, len(b) - 64, 64):
                    r = int(b[p + 1:p + 64], 2)
                    if r.bit_count() < 8:
                        continue
                    sc = sorted(((((t & ~r) if d == "to1" else (r & ~t)) & M).bit_count(), k)
                                for k, t in cws.items())
                    if sc[0][0] <= 1 and sc[1][0] - sc[0][0] >= 2:
                        n += 1
                        seq.append(sc[0][1])
                best = max(best, (n, d, ph, seq))
        print(f"  {name:7s} {best[0]:2d} of {(len(b) - 64) // 64} blocks (bias {best[1]}, phase {best[2]}) "
              f"{' '.join(best[3][:8])}")
    print("  SP loses half its blocks because the codeword grid moves by 4 bits after byte 128.")
    print("\n== X burst (202434 t=21.05) words vs the true codewords rotated by 2 bits ==")
    from codebook import NIBBLE
    true = {"S": "27116195A033E5CD", "B5": "6A57C203071DC593", "A4": "48E2F4E604B88A2C"}
    new = {"S": "BCCF8ED681DFB775", "B5": "AB5F181C3CF736DD", "A4": "679BD7B836E669B1"}
    for k in true:
        t = h2b(true[k])
        t = t[2:] + t[:2]
        n = h2b(new[k])
        up = sum(1 for x, y in zip(t, n) if x == "0" and y == "1")
        dn = sum(1 for x, y in zip(t, n) if x == "1" and y == "0")
        lead = sum(1 for i in range(63) if t[i] == "0" and n[i] == "1" and t[i + 1] == "1")
        print(f"  {k}: 0->1 errors {up}, 1->0 errors {dn}; 0->1 errors just before a 1: {lead}")


def changepoint(b):
    """Single change point in P(1) by maximum likelihood (Bernoulli before/after)."""
    import math
    n = len(b)
    pre = [0]
    for c in b:
        pre.append(pre[-1] + (c == "1"))

    def ll(k, m):
        if m == 0 or k in (0, m):
            return 0.0
        p = k / m
        return k * math.log(p) + (m - k) * math.log(1 - p)
    best = None
    for i in range(32, n - 32):
        v = ll(pre[i], i) + ll(pre[n] - pre[i], n - i)
        if best is None or v > best[0]:
            best = (v, i, pre[i] / i, (pre[n] - pre[i]) / (n - i))
    return best


def sec_end(F):
    print("== ones per 32-bit window (idle noise on weak lines is balanced, P(1)~0.5;")
    print("   after a strong packet the dongle output turns 0-biased, P(1)~0.3) ==")
    for k in ("SP", "A1", "A2", "B1", "B2", "C13", "C57"):
        b = F[k]["bits"]
        print(f"  {k:4s}", " ".join(f"{b[i:i + 32].count('1'):2d}" for i in range(0, len(b) - 31, 32)))
    print("\n== maximum-likelihood change point in P(1) ==")
    for k, lo, hi in (("A1", 24, 240), ("A1", 255, 2064), ("A2", 28, 240), ("A2", 258, 2064),
                      ("B1", 24, 2064), ("B2", 24, 2064), ("C13", 24, 2064), ("C57", 24, 2064)):
        v, i, p0, p1 = changepoint(F[k]["bits"][lo:hi])
        print(f"  {k:4s} [{lo}:{hi}] change at bit {lo + i:4d}  P(1) {p0:.2f} -> {p1:.2f}")


def crc_bits(bits, poly, w, init):
    r, mask = init, (1 << w) - 1
    for c in bits:
        fb = ((r >> (w - 1)) & 1) ^ (c == "1")
        r = (r << 1) & mask
        if fb:
            r ^= poly
    return r


def sec_crc(F):
    """nRF24/ESB, LT8920 and similar radios end a packet with a CRC-16 over the bit
    stream from the address/sync on.  Try every start bit near the end of the preamble
    and every end bit, CRC-16 polys 0x1021 and 0x8005, init 0/FFFF, xorout 0/FFFF, CRC
    sent MSB-first or bit-reversed (reflected, LSB-first radios)."""
    print("== CRC-16 at every packet end (bit-stream CRC from the sync start) ==")
    # A: also every start up to 64 sync + 18 trailer bits later (LT8920 CRC skips the sync)
    jobs = (("A1 p1", "A1", range(20, 111), 240), ("A2 p1", "A2", range(24, 115), 240),
            ("A1 p2", "A1", range(251, 342), 520), ("A2 p2", "A2", range(254, 345), 520),
            ("B1", "B1", range(20, 29), 2064), ("B2", "B2", range(20, 29), 2064),
            ("C13", "C13", range(20, 29), 2064), ("C57", "C57", range(20, 29), 2064))
    for name, k, starts, hi in jobs:
        b = F[k]["bits"]
        hits, tests = [], 0
        for s0 in starts:
            for poly in (0x1021, 0x8005):
                for init in (0, 0xFFFF):
                    r = init
                    mask = 0xFFFF
                    # incremental CRC over b[s0:e]
                    for e in range(s0, hi - 16):
                        if e - s0 >= 16:
                            for xo in (0, 0xFFFF):
                                v = format(r ^ xo, "016b")
                                tests += 2
                                tail = b[e:e + 16]
                                if tail == v:
                                    hits.append((s0, e, hex(poly), hex(init), hex(xo), "msb"))
                                if tail == v[::-1]:
                                    hits.append((s0, e, hex(poly), hex(init), hex(xo), "rev"))
                        fb = ((r >> 15) & 1) ^ (b[e] == "1")
                        r = (r << 1) & mask
                        if fb:
                            r ^= poly
        # with init 0, leading zero bits do not change the CRC: count distinct (end, model)
        uniq = sorted({h[1:] for h in hits})
        print(f"  {name:6s} tests {tests:6d}  expected chance hits {tests / 65536:.2f}  "
              f"distinct hits {len(uniq)} ends {[u[0] for u in uniq]}")


def main():
    F = load()
    secs = sys.argv[1:] or ["list", "align", "runs", "end", "period", "whiten", "codebook", "bias", "crc"]
    for s in secs:
        globals()["sec_" + s](F)
        print()


if __name__ == "__main__":
    main()
