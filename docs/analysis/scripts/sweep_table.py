"""Build per-setpoint codeword/nibble maps from every capture that has a known setpoint.

Sources
  * 20261103-203934 nrf.log: 64-byte dongle windows, positions from the dt timer.
  * docs/analysis/setpoint-f4.csv: ESP frame 4 (32 bytes), placed by matching overlap.
  * 20261104-163310 + 20261104-164149: 255-byte head and tail windows (74..77 F).
Positions are in bits from the first bit of the first 9388B0CA (byte view: pos/8).
Prints a table and writes docs/analysis/setpoint-nibbles.csv.
"""
import csv
from rfcommon import *
from codebook import *

FIELD = {176.5: "set.lo", 184.5: "set.hi", 240.5: "s60.lo", 248.5: "s60.hi",
         256.5: "crcA.lo", 264.5: "crcA.hi", 272.5: "crcB.lo", 280.5: "crcB.hi"}


def merge(dst, src, tag):
    for p, v in src.items():
        if p in dst and dst[p][0] != v[0]:
            print(f"  conflict {tag} @{p/8}: {dst[p]} vs {v}")
        dst.setdefault(p, v)


def from_dongle_burst(grp, trig=SYNC4, first_origin=0):
    known, end, off = {}, None, first_origin
    for i, p in enumerate(grp):
        if i:
            off += p["dt"] * 1  # 1 us == 1 bit at 1 Mbit/s
        o = round(off / 4) * 4   # snap to nibble grid
        b = onair(p["raw"], trig)
        k, e = blocks_by_pos(b, o)
        merge(known, k, "dongle")
        end = end or e
    return known, end


def place_esp(known, f4hex):
    """Try each S position X for the ESP frame-4 trigger; keep the consistent one."""
    b = onair(f4hex)
    cands = []
    for xB in [x / 2 for x in range(2 * 176, 2 * 290)]:
        X = int(xB * 8)
        if X % 4:
            continue
        k, e = blocks_by_pos(b, X)
        if not k and e is None:
            continue
        if X in known and known[X][0] != "S":
            continue
        agree = sum(1 for p, v in k.items() if p in known and known[p][0] == v[0])
        clash = sum(1 for p, v in k.items() if p in known and known[p][0] != v[0])
        if clash:
            continue
        if e is not None and any(p >= e for p in known):
            continue
        cands.append((agree, X, k, e))
    if not cands:
        return None
    best = max(c[0] for c in cands)
    top = [c for c in cands if c[0] == best]
    return top[0] if len(top) == 1 and best > 0 else ("ambiguous", [c[1] / 8 for c in top])


def collect():
    data = {}  # setpoint -> list of (source, known, end)
    rows = list(csv.DictReader(open(REPO / "docs/analysis/setpoint-f4.csv")))
    for grp in bursts(parse_nrf(CAP / "20261103-203934/nrf.log")):
        r = min(rows, key=lambda r: abs(float(r["t_capture_s"]) - grp[0]["t"]))
        known, end = from_dongle_burst(grp)
        note = ""
        if r["f4_raw_after_sync"]:
            pl = place_esp(known, r["f4_raw_after_sync"])
            if pl and pl[0] != "ambiguous":
                merge(known, pl[2], "esp")
                end = end or pl[3]
                note = f"esp@{pl[1]/8}"
            else:
                note = f"esp-unplaced {pl}"
        data.setdefault(int(r["setpoint_F"]), []).append((f"203934#{r['burst']} {note}", known, end))
    head = parse_nrf(CAP / "20261104-163310/nrf.log")
    tail = parse_nrf(CAP / "20261104-164149/nrf.log")
    for p, sp in zip(head, [75, 76, 77, 76, 75, 74]):
        k, e = blocks_by_pos(onair(p["raw"]), 0)
        data.setdefault(sp, []).append(("163310 head", k, e))
    for p, sp in zip(tail, [75, 74, 75, 74, 75, 74]):
        k, e = blocks_by_pos(onair(p["raw"], "A7E03171"), 1284)
        data.setdefault(sp, []).append(("164149 tail", k, e))
    return data


def combined(data):
    out = {}
    for sp, lst in data.items():
        known, end = {}, None
        for src, k, e in lst:
            merge(known, k, f"{sp} {src}")
            end = end or e
        out[sp] = (known, end)
    return out


if __name__ == "__main__":
    data = collect()
    comb = combined(data)
    for sp in sorted(data):
        for src, k, e in data[sp]:
            if "unplaced" in src:
                print(sp, src)
    hdr = ["setpoint_F"] + [f"{p}:{n}" for p, n in FIELD.items()] + ["end_B"]
    w = csv.writer(open(REPO / "docs/analysis/setpoint-nibbles.csv", "w", newline=""))
    w.writerow(hdr)
    print(f"{'sp':>3} " + " ".join(f"{n:>8}" for n in FIELD.values()) + "   end   set  s-60  crc")
    for sp in sorted(comb):
        known, end = comb[sp]
        cells, nib = [], {}
        for p in FIELD:
            v = known.get(int(p * 8))
            if v:
                nib[p] = NIBBLE[v[0]]
                cells.append(f"{v[0]}={NIBBLE[v[0]]:X}")
            else:
                cells.append("?")
        def byte(lo, hi):
            return f"{nib[hi]:X}{nib[lo]:X}" if lo in nib and hi in nib else "??"
        w.writerow([sp] + cells + [end / 8 if end else ""])
        print(f"{sp:>3} " + " ".join(f"{c:>8}" for c in cells) +
              f"  {end/8 if end else '?':>5}  {byte(176.5,184.5)}  {byte(240.5,248.5)}  "
              f"{byte(256.5,264.5)} {byte(272.5,280.5)}")
